"""M20 漫画册：把 output/ 里的一页页攒成一本（月历 / 心情曲线 / 翻页 / PDF）。

不额外生图：全部数据来自已有页面的 storyboard + page.png。

角色维度（第八轮）：新页面在 output/<char>/<date>/；历史平铺页 output/<date>/
按 context.json 的 character_id 归属。scan_pages 的 char_id 过滤以归属为准，
避免一个角色的旧页混进另一个角色的漫画册。
"""

import json
import re
from pathlib import Path

from visdiary.config import Config
from visdiary.schemas import Storyboard

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 21 种情绪的效价（-2 最负 … +2 最正），加权到强度用。D2。
_VALENCE = {
    "大哭": -2, "崩溃": -2, "哭": -2, "暴怒": -2,
    "难过": -1, "委屈": -1, "生气": -1, "疲惫": -1, "无语": -1, "紧张": -1,
    "平静": 0, "思考": 0, "惊讶": 0, "震惊": -1,
    "害羞": 0, "期待": 1, "感动": 1, "得意": 1, "坚定": 1,
    "开心": 2, "大笑": 2,
}


class PageEntry(dict):
    """一天一页：date/character/dir/title/mood/weather/main_emotion/valence/highlight。"""


def page_owner(d: Path) -> str:
    """页面归属：context.json 的 character_id；读不到为空串（不算任何角色）。"""
    ctx = d / "context.json"
    if ctx.is_file():
        try:
            return json.loads(ctx.read_text(encoding="utf-8")).get("character_id", "") or ""
        except Exception:
            pass
    return ""


def scan_pages(cfg: Config, char_id: str | None = None) -> list[PageEntry]:
    """收页面。char_id 给定：只收归属该角色的（角色子目录 + 归属它的平铺旧页）；
    None：收全部。同一日期角色子目录与平铺并存时，取角色子目录（新版）。
    """
    from visdiary.character.store import list_character_ids

    out_root = cfg.path("output")
    cands: list[tuple[str, str, Path]] = []  # (日期, 归属, 目录)
    if out_root.is_dir():
        for c in list_character_ids(cfg):
            cdir = out_root / c
            if not cdir.is_dir():
                continue
            for d in sorted(cdir.iterdir()):
                if _DATE_RE.fullmatch(d.name) and (d / "storyboard.json").is_file():
                    cands.append((d.name, c, d))
        for d in sorted(out_root.iterdir()):
            if (not _DATE_RE.fullmatch(d.name) or d.name.startswith("_")
                    or not (d / "storyboard.json").is_file()):
                continue
            cands.append((d.name, page_owner(d), d))

    pages: list[PageEntry] = []
    seen: set[tuple[str, str]] = set()
    # 同一角色的同日期：子目录版排前（parent 不是 output 根 → False < True），
    # 先到先得；不同角色的同日期是两本漫画册，不去重。
    # 无归属平铺页（owner=""）只出现在「全部」视图：同日期被任何角色子目录版覆盖
    for date, owner, d in sorted(cands, key=lambda t: (t[0], t[2].parent == out_root)):
        if char_id and owner != char_id:
            continue
        if owner == "" and date in {c[0] for c in cands if c[1] != ""}:
            continue   # 无归属页被同日的有归属版本覆盖（全角色子目录+有归属平铺）
        if (owner, date) in seen:
            continue
        seen.add((owner, date))
        try:
            pages.append(_entry_from(d, owner))
        except Exception:
            continue   # storyboard 损坏的目录不算一页（round6：占位 {} 也要能列出）
    return pages


def _entry_from(d: Path, owner: str) -> PageEntry:
    sb = Storyboard.model_validate_json(
        (d / "storyboard.json").read_text(encoding="utf-8"))

    # 主情绪：出现最多，并列取强度高（D2）
    by_emo: dict[str, tuple[int, int]] = {}
    for p in sb.panels:
        cur = by_emo.get(p.acting.emotion, (0, 0))
        by_emo[p.acting.emotion] = (cur[0] + 1, max(cur[1], p.acting.intensity))
    main = max(by_emo, key=lambda e: by_emo[e]) if by_emo else "平静"

    # 心情值：效价按强度加权平均（D2）
    vals = [_VALENCE.get(p.acting.emotion, 0) * p.acting.intensity for p in sb.panels]
    valence = round(sum(vals) / len(vals) / 3, 2) if vals else 0.0

    hl = next((p.id for p in sb.panels if p.weight == "highlight"), 1)
    weather = ""
    ctx_file = d / "context.json"
    if ctx_file.is_file():
        try:
            weather = json.loads(ctx_file.read_text(encoding="utf-8")).get("weather", "") or ""
        except Exception:
            pass
    return PageEntry(
        date=d.name, character=owner, dir=str(d), title=sb.title, mood=sb.mood,
        weather=weather, main_emotion=main, valence=valence,
        highlight=hl, panels=len(sb.panels),
        thumb=f"/api/pages/{d.name}/page.png",
    )


def ensure_thumbs(cfg: Config, pages: list[PageEntry]) -> None:
    """生成/缓存缩略图：thumb.jpg（整页 480 宽）与 cover.jpg（高光格方图）。"""
    from PIL import Image

    for pg in pages:
        d = Path(pg["dir"])
        page_png = d / "page.png"
        if not page_png.is_file():
            continue
        thumb = d / "thumb.jpg"
        if not thumb.is_file() or thumb.stat().st_mtime < page_png.stat().st_mtime:
            img = Image.open(page_png)
            w = 480
            h = round(img.size[1] * w / img.size[0])
            img.resize((w, h), Image.LANCZOS).save(thumb, quality=82)
        cover = d / "cover.jpg"
        panel = d / "panels" / f"{pg['highlight']:02d}.png"
        src = panel if panel.is_file() else page_png
        if not cover.is_file() or cover.stat().st_mtime < src.stat().st_mtime:
            img = Image.open(src).convert("RGB")
            w, h = img.size
            side = min(w, h)
            img.crop(((w - side) // 2, (h - side) // 2,
                      (w + side) // 2, (h + side) // 2)).resize(
                (320, 320), Image.LANCZOS).save(cover, quality=85)


def month_pages(cfg: Config, month: str, char_id: str | None = None) -> list[PageEntry]:
    return [p for p in scan_pages(cfg, char_id) if p["date"].startswith(month)]


def on_this_day(cfg: Config, date: str, char_id: str | None = None) -> list[PageEntry]:
    """N 天前的今天：7 天、30 天、一年前（闰年 2/29 → 取 2/28）。"""
    import datetime as dt

    try:
        d = dt.date.fromisoformat(date)
    except ValueError:
        return []
    cands = []
    try:
        cands.append(("7天前", d - dt.timedelta(days=7)))
    except ValueError:
        pass
    cands.append(("30天前", d - dt.timedelta(days=30)))
    try:
        cands.append(("一年前", d.replace(year=d.year - 1)))
    except ValueError:  # 2/29
        cands.append(("一年前", dt.date(d.year - 1, 2, 28)))
    pages = scan_pages(cfg, char_id)
    out = []
    for label, day in cands:
        hit = next((p for p in pages if p["date"] == day.isoformat()), None)
        if hit:
            out.append(PageEntry({**hit, "label": label}))
    return out


def summary_cache(cfg: Config, month: str, char_id: str | None) -> Path:
    """月度小结缓存路径：角色版与全部版分开存。"""
    name = f"{month}-{char_id}.json" if char_id else f"{month}.json"
    return cfg.path("output") / "_book" / name


def month_summary(cfg: Config, month: str, char_id: str | None = None,
                  llm=None) -> dict:
    """月度小结（可选，只调 1 次 LLM），缓存 output/_book/<YYYY-MM>[-<char>].json。"""
    cache = summary_cache(cfg, month, char_id)
    if cache.is_file():
        return json.loads(cache.read_text(encoding="utf-8"))
    pages = month_pages(cfg, month, char_id)
    if not pages:
        return {"month": month, "summary": "", "pages": 0}
    if llm is None:
        return {"month": month, "pages": len(pages),
                "summary": "", "note": "llm 未提供，跳过小结"}
    lines = [f"{p['date']}《{p['title']}》主情绪 {p['main_emotion']}" for p in pages]
    prompt = (f"这是 {month} 每天的日记漫画标题和主情绪，写 3 句话的月度小结，"
              f"口语化：\n" + "\n".join(lines))
    from visdiary.schemas import BaseModel

    class Summary(BaseModel):
        summary: str

    r = llm.chat_json("只按 JSON schema 输出。", prompt, Summary)
    out = {"month": month, "pages": len(pages), "summary": r.summary}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def export_pdf(cfg: Config, month: str, out_path: Path,
               char_id: str | None = None) -> Path:
    """封面（月份+高光拼图+情绪占比）→ 每天一页 → 小结封底，A4 竖版 PDF。"""
    pages = month_pages(cfg, month, char_id)
    if not pages:
        raise ValueError(f"{month} 没有漫画页")
    ensure_thumbs(cfg, pages)
    import base64

    def b64(p: Path) -> str:
        return "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode()

    covers = []
    for p in pages:
        c = Path(p["dir"]) / "cover.jpg"
        if c.is_file():
            covers.append(f'<figure><img src="{b64(c)}"><figcaption>{p["date"][5:]}'
                          f' {p["main_emotion"]}</figcaption></figure>')
    from collections import Counter

    emo_count = Counter(p["main_emotion"] for p in pages)
    emo_rows = "".join(
        f"<tr><td>{e}</td><td>{n}</td><td>{'▇' * n}</td></tr>"
        for e, n in emo_count.most_common())
    summary = month_summary(cfg, month, char_id)
    page_imgs = "".join(
        f'<div class="sheet"><img src="{b64(Path(p["dir"]) / "page.png")}" '
        f'alt="{p["date"]}"></div>'
        for p in pages)
    if char_id:
        try:
            from visdiary.character.store import load_character
            owner_name = load_character(cfg, char_id).name
        except Exception:
            owner_name = char_id
    else:
        owner_name = "全部角色"
    from html import escape as _esc
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
      @page {{ size: A4 portrait; margin: 0; }}
      body {{ margin:0; font-family:"LXGW WenKai","Noto Sans CJK SC",serif; }}
      .cover,.back {{ page-break-after: always; padding:60px; text-align:center; }}
      .gridc {{ display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin:30px 0; }}
      .gridc figure {{ margin:0; }} .gridc img {{ width:100%; }}
      figcaption {{ font-size:11px; color:#666; }}
      .sheet {{ page-break-after: always; }}
      .sheet img {{ width:100%; }}
      table {{ margin:20px auto; border-collapse:collapse; font-size:14px; }}
      td,th {{ border:1px solid #ccc; padding:4px 14px; }}
    </style></head><body>
    <div class="cover"><h1>{_esc(month)} 的漫画册</h1>
      <p>{_esc(owner_name)} · 共 {len(pages)} 天</p>
      <div class="gridc">{''.join(covers[:12])}</div>
      <table><tr><th>主情绪</th><th>天数</th><th></th></tr>{emo_rows}</table>
    </div>
    {page_imgs}
    <div class="back"><h2>这个月</h2>
      <p style="max-width:70%;margin:0 auto;text-align:left">{summary.get('summary') or ''}</p>
    </div></body></html>"""
    tmp_html = out_path.with_suffix(".html")
    tmp_html.write_text(html, encoding="utf-8")
    from visdiary.layout.export import export_png  # noqa: F401  （保留 import 便于排查）
    # PDF：用 playwright 的 pdf 接口
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page()
        pg.goto(tmp_html.resolve().as_uri())
        pg.wait_for_load_state("networkidle")
        pg.pdf(path=str(out_path), format="A4",
               margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        b.close()
    tmp_html.unlink(missing_ok=True)
    return out_path
