"""M18 网页版：把 CLI 已有的能力包成本地网页（不新增业务逻辑）。

启动：.venv/bin/visdiary web [--port 8420]   （只监听 127.0.0.1）

第五轮加固：
- Job 加锁修 SSE finish 竞态；事件用 JSON 帧包装
- edit / pick 也走 runner 串行，避免和 render 同时写同一输出目录
- 安全最小集：TrustedHost + Origin/X-VisDiary 校验（防 CSRF/DNS rebinding）+
  date/cid/variant 参数白名单 + is_relative_to 路径校验
"""

import json
import queue
import re
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from visdiary import budget, providers
from visdiary.character import builder as char_builder
from visdiary.character.store import characters_dir, list_character_ids, load_character
from visdiary.config import Config, load_config
from visdiary.director import (edit_panel, load_pack, pick_variant, rebuild_page,
                               redraw_panel)
from visdiary.diary.parser import parse_diary
from visdiary.pipeline import run_pipeline

WEB_DIR = Path(__file__).parent / "web"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


class Job:
    def __init__(self, job_id: str, kind: str):
        self.id = job_id
        self.kind = kind
        self.status = "queued"   # queued / running / done / failed
        self.steps: list[str] = []
        self.error = ""
        self.result: dict = {}
        self.subs: list[queue.Queue] = []
        self.lock = threading.Lock()  # 订阅与 finish 互斥，修 SSE 竞态

    def push(self, line: str):
        with self.lock:
            self.steps.append(line)
            for q in self.subs:
                q.put(line)

    def finish(self, status: str, result: dict | None = None, error: str = ""):
        with self.lock:
            self.status = status
            self.result = result or {}
            self.error = error
            for q in self.subs:
                q.put(None)  # 结束信号
            self.subs.clear()

    def snapshot(self) -> tuple[list[str], str, str]:
        """锁内取当前状态（订阅前调用，防止 finish 插在两步之间）。"""
        with self.lock:
            return list(self.steps), self.status, self.error


class Runner:
    """单 worker 串行执行（codex 通道本身受并发限制）。"""

    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._work, daemon=True).start()

    def submit(self, kind: str, fn) -> Job:
        job = Job(uuid.uuid4().hex[:8], kind)
        self.jobs[job.id] = job
        self.q.put((job, fn))
        return job

    def _work(self):
        while True:
            job, fn = self.q.get()
            job.status = "running"
            try:
                result = fn(job)
                job.finish("done", result)
            except Exception as e:
                job.finish("failed", error=str(e)[:500])


def sse_stream(job: "Job"):
    """A6：把 SSE 生成器抽成模块级函数，测试可以直接调用（不用复制逻辑）。"""
    def frame(payload: dict) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    q: queue.Queue = queue.Queue()
    with job.lock:  # 快照 + 订阅原子完成，finish 插不进来
        history = list(job.steps)
        status, error = job.status, job.error
        if status in ("queued", "running"):
            job.subs.append(q)
    for line in history:
        yield frame({"step": line})
    if status in ("done", "failed"):
        yield frame({"end": status, "error": error, "result": job.result})
        return
    while True:
        line = q.get()
        if line is None:
            _, status, error = job.snapshot()
            yield frame({"end": status, "error": error, "result": job.result})
            return
        yield frame({"step": line})


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    app = FastAPI(title="VisDiary")
    runner = Runner()

    # ---------- 安全最小集（§2.3）----------
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # 非 GET 请求要求同源 + 自定义头（普通网页发起的 CSRF 不带 X-VisDiary）
        if request.method != "GET":
            origin = request.headers.get("origin", "")
            port = request.url.port or 80
            ok_origin = origin in (
                f"http://127.0.0.1:{port}", f"http://localhost:{port}",
                *(("http://127.0.0.1", "http://localhost") if port == 80 else ()))
            if not ok_origin or request.headers.get("x-visdiary") != "1":
                return JSONResponse({"detail": "拒绝：非本源请求"}, status_code=403)
        return await call_next(request)

    def _check_date(date: str) -> str:
        if not _DATE_RE.fullmatch(date):
            raise HTTPException(400, "日期格式应为 YYYY-MM-DD")
        return date

    def _check_cid(cid: str) -> str:
        if not _CID_RE.fullmatch(cid):
            raise HTTPException(400, "角色 id 只允许小写字母/数字/-/_")
        return cid

    # ---------- 页面 ----------
    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    # ---------- 日记 ----------
    class DiaryIn(BaseModel):
        content: str

    @app.get("/api/diaries")
    def list_diaries():
        root = cfg.path("diaries")
        by_date: dict[str, dict] = {}
        for p in sorted(root.rglob("*.md")):
            try:
                date = parse_diary(p).date
            except Exception:
                date = p.stem
            if not _DATE_RE.fullmatch(date):
                continue
            # 同一日期多篇（网页保存的 diaries/<date>.md vs 样例）：取最新的
            cur = by_date.get(date)
            if cur is None or p.stat().st_mtime > Path(cur["path"]).stat().st_mtime:
                by_date[date] = {"date": date, "path": str(p), "name": p.stem}
        return sorted(by_date.values(), key=lambda d: d["date"])

    def _diary_file(date: str) -> Path:
        """按日期找日记：优先 diaries/<date>.md，否则用列表里选出的那篇。"""
        direct = cfg.path("diaries") / f"{date}.md"
        if direct.is_file():
            return direct
        for d in list_diaries():
            if d["date"] == date:
                return Path(d["path"])
        return direct

    @app.put("/api/diaries/{date}")
    def save_diary(date: str, body: DiaryIn):
        _check_date(date)
        p = cfg.path("diaries") / f"{date}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body.content, encoding="utf-8")
        return {"ok": True, "path": str(p)}

    @app.get("/api/diaries/{date}/raw")
    def diary_raw(date: str):
        _check_date(date)
        p = _diary_file(date)
        if not p.is_file():
            raise HTTPException(404, "日记不存在")
        return {"content": p.read_text(encoding="utf-8"), "path": str(p)}

    # ---------- 生成（SSE） ----------
    @app.post("/api/render/{date}")
    def start_render(date: str, character: str = "demo", reuse: bool = False):
        _check_date(date)
        character = _check_cid(character)
        diary = _diary_file(date)
        if not diary.is_file():
            raise HTTPException(404, f"日记不存在：{diary}")

        def fn(job: Job):
            import re as _re

            def progress(line: str):
                m = _re.match(r"生图 第 (\d+) 格完成", line)
                if m:  # C5：单格出图事件，前端渐进填图
                    job.push(f"panel_ready:{m.group(1)}")
                job.push(line)
            r = run_pipeline(cfg, diary, character, reuse=reuse, progress=progress)
            out = r["outdir"]
            (out / "job.json").write_text(json.dumps(
                {"job_id": job.id, "status": "done", "steps": job.steps,
                 "date": date, "character": character, "kind": "render",
                 "finished_at": time.strftime("%H:%M:%S")}, ensure_ascii=False),
                encoding="utf-8")
            return {"outdir": str(out), "png": str(r["png"]),
                    "panels": r["panels"], "qc": r["qc"], "warnings": r["warnings"]}

        job = runner.submit("render", fn)
        return {"job_id": job.id}

    @app.get("/api/jobs/{job_id}/events")
    def job_events(job_id: str):
        job = runner.jobs.get(job_id)
        if not job:
            raise HTTPException(404, "没有这个任务")
        return StreamingResponse(sse_stream(job), media_type="text/event-stream")

    # ---------- 漫画页 / 单格 ----------
    def _page_dir(date: str, character: str | None) -> Path:
        """角色分目录后：<char>/<date>；找不到再回退旧平铺 <date>/。

        回退时校验归属：平铺页 context.json 的 character_id 必须与查询角色
        一致（无归属或不同角色都视为不存在），防止角色 A 的旧页冒充角色 B。
        """
        if character:
            new = cfg.path("output") / character / date
            if new.is_dir():
                return new
            old = cfg.path("output") / date
            if old.is_dir() and BK.page_owner(old) == character:
                return old
            raise HTTPException(404, f"{character} 在 {date} 还没有漫画")
        out = cfg.path("output") / date
        if not out.is_dir():
            raise HTTPException(404, f"{date} 还没有漫画")
        return out

    @app.get("/api/pages")
    def list_pages(character: str | None = None):
        """角色给定时只列归属该角色的页面；不给则列全部（去重，子目录版优先）。

        只查 storyboard.json 是否存在，不解析内容（占位/损坏的目录也能列出）。
        """
        if character:
            _check_cid(character)
        from visdiary.character.store import list_character_ids

        out_root = cfg.path("output")
        cands: list[tuple[str, Path]] = []

        def _flat_dates(root: Path, owner_filter: str | None):
            if not root.is_dir():
                return
            for d in sorted(root.iterdir()):
                if (not _DATE_RE.fullmatch(d.name) or d.name.startswith("_")
                        or not (d / "storyboard.json").is_file()):
                    continue
                if owner_filter is not None and BK.page_owner(d) != owner_filter:
                    continue
                cands.append((d.name, d))

        if character:
            _flat_dates(out_root / character, None)       # 角色子目录归属即角色
            _flat_dates(out_root, character)              # 平铺旧页按归属过滤
        else:
            for c in list_character_ids(cfg):
                _flat_dates(out_root / c, None)
            _flat_dates(out_root, None)
        seen, pages = set(), []
        # 子目录版排前：parent 不是 output 根 → False < True。
        # 「全部」视图按（归属,日期）去重：不同角色同日是两页，不去重
        for date, d in sorted(cands, key=lambda t: (t[0], t[1].parent == out_root)):
            key = ((d.parent.name if d.parent.parent == out_root   # 角色子目录：归属=父目录
                    else BK.page_owner(d)),                        # 平铺：读 context.json
                   date)
            if key in seen:
                continue
            seen.add(key)
            pages.append({"date": date, "finished": (d / "page.png").is_file()})
        return pages

    @app.get("/api/pages/{date}")
    def get_page(date: str, character: str | None = None):
        _check_date(date)
        out = _page_dir(date, character)
        if not (out / "storyboard.json").is_file():
            raise HTTPException(404, "还没有生成结果")
        sb, ctx, metas = load_pack(out)
        layout = json.loads((out / "layout.json").read_text(encoding="utf-8"))
        return {
            "date": date, "outdir": str(out),
            "storyboard": json.loads(sb.model_dump_json()),
            "layout": layout,
            "page_png": str(out / "page.png"),
            "metas": {str(k): v for k, v in metas.items()},
            "context": ctx,
        }

    def _page_png_path(date: str, character: str | None = None) -> Path:
        _check_date(date)
        p = _page_dir(date, character) / "page.png"
        if not p.is_file():
            raise HTTPException(404, "还没生成")
        return p

    @app.get("/api/pages/{date}/page.png")
    def page_png(date: str, character: str | None = None):
        return FileResponse(_page_png_path(date, character),
                            headers={"Cache-Control": "no-cache"})

    @app.get("/api/pages/{date}/panels/{pid}.png")
    def panel_png(date: str, pid: int, character: str | None = None):
        _check_date(date)
        p = _page_dir(date, character) / "panels" / f"{pid:02d}.png"
        if not p.is_file():
            raise HTTPException(404, "没有这一格")
        return FileResponse(p, headers={"Cache-Control": "no-cache"})

    @app.get("/api/pages/{date}/panels/{pid}/variants/{letter}.png")
    def variant_png(date: str, pid: int, letter: str, character: str | None = None):
        """P0-1：候选图读取接口（letter 只允许单个 a-z）。"""
        _check_date(date)
        if not re.fullmatch(r"[a-z]", letter):
            raise HTTPException(400, "variant 只允许 a-z")
        p = (_page_dir(date, character) / "panels"
             / f"{pid:02d}.variant-{letter}.png")
        if not p.is_file():
            raise HTTPException(404, "没有这个候选")
        return FileResponse(p, headers={"Cache-Control": "no-cache"})

    class EditIn(BaseModel):
        emotion: str | None = None
        intensity: int | None = None
        note: str | None = None

    @app.post("/api/pages/{date}/panels/{pid}/edit")
    def api_edit(date: str, pid: int, body: EditIn, character: str | None = None):
        _check_date(date)
        out = _page_dir(date, character)

        def fn(job: Job):  # A4：走 runner 串行，避免和 render 同时写同一目录
            changed = edit_panel(cfg, out, pid, body.emotion, body.intensity, body.note)
            return {"changed": changed}

        return {"job_id": runner.submit("edit", fn).id, "ok": True}

    @app.post("/api/pages/{date}/panels/{pid}/redraw")
    def api_redraw(date: str, pid: int, variants: int = 2,
                    character: str | None = None):
        _check_date(date)
        if not 2 <= variants <= 4:  # A5：候选数上限
            raise HTTPException(400, "variants 只允许 2–4")
        out = _page_dir(date, character)

        def fn(job: Job):
            return {"variants": redraw_panel(cfg, out, pid, variants,
                                             progress=job.push)}

        return {"job_id": runner.submit("redraw", fn).id}

    @app.post("/api/pages/{date}/panels/{pid}/pick")
    def api_pick(date: str, pid: int, variant: str, character: str | None = None):
        _check_date(date)
        out = _page_dir(date, character)

        def fn(job: Job):  # P1-8：走 runner，和 render 串行，避免同目录并发写
            png = pick_variant(cfg, out, pid, variant)
            return {"png": str(png)}

        return {"job_id": runner.submit("pick", fn).id}

    # ---------- 角色 ----------
    @app.get("/api/characters")
    def list_chars():
        return [{"id": i} for i in list_character_ids(cfg)]

    @app.get("/api/characters/{cid}")
    def get_char(cid: str):
        _check_cid(cid)
        try:
            char = load_character(cfg, cid)
        except FileNotFoundError:
            raise HTTPException(404, f"角色 {cid} 不存在")
        base = characters_dir(cfg) / cid
        d = json.loads(char.model_dump_json())
        for sheet in d["sheets"].values():
            sheet["exists"] = {v: (base / rel).is_file()
                               for v, rel in sheet["views"].items()}
        return d

    @app.get("/api/characters/{cid}/img")
    def char_img(cid: str, rel: str):
        _check_cid(cid)  # 挡住 ".."；base 就落在 characters/<cid> 之下
        base = (characters_dir(cfg) / cid).resolve()
        p = (base / rel).resolve()
        if not p.is_relative_to(base) or not p.is_file():
            raise HTTPException(404, "图片不存在")
        return FileResponse(p)

    class RegenIn(BaseModel):
        view: str
        style: str | None = None

    @app.post("/api/characters/{cid}/regen")
    def api_regen(cid: str, body: RegenIn):
        _check_cid(cid)

        def fn(job: Job):
            char = load_character(cfg, cid)
            style = char_builder.resolve_style(char, body.style)
            char_builder.upsert_sheet(cfg, providers.get_image(cfg), char, style,
                                      body.view, vision=providers.get_vision(cfg))
            return {"ok": True}

        return {"job_id": runner.submit("regen", fn).id}

    @app.post("/api/characters/{cid}/approve")
    def api_approve(cid: str, style: str | None = None):
        _check_cid(cid)
        char = load_character(cfg, cid)
        style = char_builder.resolve_style(char, style)
        char_builder.approve_character(cfg, cid, style)
        return {"ok": True}

    @app.post("/api/generate-all")
    def api_generate_all(character: str = "demo", confirm: int = 0):
        """P1-2：一键生成，先返回预估，confirm=N 才真跑；demo 模式拒绝。"""
        _check_cid(character)
        if cfg.provider.image == "mock" and cfg.env != "dev":
            raise HTTPException(403, "演示模式不可批量生成")
        diaries = [d for d in list_diaries()]
        owned = {p["date"] for p in BK.scan_pages(cfg, character)}
        pending = [d["date"] for d in diaries
                   if d["date"] not in owned
                   and not (cfg.path("output") / character / d["date"] / "page.png").is_file()]
        if confirm != len(pending):
            return {"pending": len(pending), "dates": pending,
                    "est_images": len(pending) * 4, "need_confirm": True}

        def fn(job: Job):
            cfg.env = "dev"   # P1-2：批量走 dev 池
            results = {"done": [], "skipped": [], "failed": []}
            for i, date in enumerate(pending, 1):
                job.push(f"[{i}/{len(pending)}] {date} 开始")
                try:
                    r = run_pipeline(cfg, _diary_file(date), character,
                                     progress=lambda l: None)
                    if r.get("fast_path"):
                        results["skipped"].append(date)
                        job.push(f"    {date} 已是最新")
                    else:
                        results["done"].append(date)
                        job.push(f"    {date} 完成 {r['panels']} 格")
                except Exception as e:
                    results["failed"].append(f"{date}: {e}"[:120])
                    job.push(f"    {date} 失败 {str(e)[:80]}")
            return results

        return {"job_id": runner.submit("gen-all", fn).id,
                "pending": len(pending)}

    # ---------- 漫画册（M20）----------
    import visdiary.book as BK  # noqa: F811  （便捷别名，模块已在顶部导入过）

    @app.get("/api/book")
    def api_book(month: str, character: str | None = None):
        if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
            raise HTTPException(400, "month 格式 YYYY-MM")
        if character:
            _check_cid(character)
        pages = BK.month_pages(cfg, month, character)
        BK.ensure_thumbs(cfg, pages)
        # 月历：该月每天的格子（有漫画的带封面，有日记没生成的标 empty，其余空白）
        import datetime as _dt
        y, m = int(month[:4]), int(month[5:])
        days_in = (_dt.date(y + (m == 12), m % 12 + 1, 1) - _dt.timedelta(days=1)).day
        by_date = {p["date"]: p for p in pages}
        diary_dates = {d["date"] for d in list_diaries()}
        cq = f"?character={character}" if character else ""
        out = []
        for d in range(1, days_in + 1):
            date = f"{month}-{d:02d}"
            if date in by_date:
                p = by_date[date]
                out.append({**p, "state": "done",
                            "thumb": f"/api/pages/{date}/thumb.jpg{cq}",
                            "cover": f"/api/pages/{date}/cover.jpg{cq}"})
            elif date in diary_dates:
                out.append({"date": date, "state": "diary_only"})
            else:
                out.append({"date": date, "state": "empty"})
        return out

    @app.get("/api/book/on-this-day")
    def api_on_this_day(date: str, character: str | None = None):
        _check_date(date)
        if character:
            _check_cid(character)
        hits = BK.on_this_day(cfg, date, character)
        BK.ensure_thumbs(cfg, hits)
        cq = f"?character={character}" if character else ""
        return [{**h, "cover": f"/api/pages/{h['date']}/cover.jpg{cq}"} for h in hits]

    @app.get("/api/book/{month}/summary")
    def api_book_summary(month: str, character: str | None = None):
        """P1-3：GET 只读缓存，没有就 404（不再触发 LLM）。"""
        if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
            raise HTTPException(400, "month 格式 YYYY-MM")
        cache = BK.summary_cache(cfg, month, character)
        if not cache.is_file():
            raise HTTPException(404, "还没有小结，POST 生成")
        return json.loads(cache.read_text(encoding="utf-8"))

    @app.post("/api/book/{month}/summary")
    def api_book_summary_gen(month: str, character: str | None = None):
        if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
            raise HTTPException(400, "month 格式 YYYY-MM")
        return BK.month_summary(cfg, month, character,
                                llm=providers.get_llm(cfg) if cfg.env != "test" else None)

    @app.post("/api/book/{month}/export")
    def api_book_export(month: str, character: str | None = None):
        if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
            raise HTTPException(400, "month 格式 YYYY-MM")
        if character:
            _check_cid(character)
        suffix = f"-{character}" if character else ""

        def fn(job: Job):
            out = cfg.path("output") / "_book" / f"{month}{suffix}.pdf"
            out.parent.mkdir(parents=True, exist_ok=True)
            return {"pdf": str(BK.export_pdf(cfg, month, out, character))}

        return {"job_id": runner.submit("book-export", fn).id}

    @app.get("/api/pages/{date}/thumb.jpg")
    def page_thumb(date: str, character: str | None = None):
        _check_date(date)
        p = _page_dir(date, character) / "thumb.jpg"
        if not p.is_file():
            raise HTTPException(404, "缩略图不存在")
        return FileResponse(p, headers={"Cache-Control": "no-cache"})

    @app.get("/api/pages/{date}/cover.jpg")
    def page_cover(date: str, character: str | None = None):
        _check_date(date)
        p = _page_dir(date, character) / "cover.jpg"
        if not p.is_file():
            raise HTTPException(404, "封面不存在")
        return FileResponse(p, headers={"Cache-Control": "no-cache"})

    # ---------- 用量 ----------
    @app.get("/api/budget")
    def api_budget():
        r = budget.report(cfg)
        r["demo_mode"] = cfg.provider.image == "mock"
        return r

    return app


app = create_app()


def main(port: int = 8420):
    import uvicorn
    app.state.port = port
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
