"""逐格生图：缓存 → 线程池并发 → 指数退避重试 → meta.json。

质检重画走 qc/checker.py，和这里的 API 重试是两套独立的计数。
"""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from visdiary import budget
from visdiary.config import Config
from visdiary.layout.planner import PageLayout
from visdiary.providers.base import ImageProvider
from visdiary.render import cache, prompt_builder, refs as refs_mod
from visdiary.schemas import Character, Panel, Storyboard


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def generate_panel(cfg: Config, image: ImageProvider, char: Character, panel: Panel,
                   aspect: str, style_id: str, extras: dict[str, str],
                   outdir: Path, fix_note: str | None = None, attempt: int = 0) -> dict:
    """生成一格，写 panels/NN.png 和 panels/NN.meta.json，返回 meta。
    attempt>0 表示质检重画：跳过读缓存，且 attempt 参与缓存键。"""
    ref_tuples = refs_mod.select_refs(cfg, char, panel, style_id)
    ref_paths = [p for _v, p in ref_tuples]
    used_views = {v for v, _p in ref_tuples}
    prompt = prompt_builder.build_prompt(cfg, char, panel, style_id, extras, fix_note,
                                         used_views=used_views)
    key = cache.cache_key(prompt, ref_paths, image.name, image.model, aspect,
                          cfg.generation.seed, attempt)

    t0 = time.monotonic()
    data = None if attempt > 0 else cache.get(cfg, key)
    cache_hit = data is not None
    if not cache_hit:
        # C3：重试统一由 provider 内部负责（codex 内部已带退避重试），
        # 这里不叠加重试层，失败直接上抛，由 generate_all 生成占位图
        data = image.generate(prompt, ref_tuples, aspect, cfg.generation.seed)
        cache.put(cfg, key, data)
        budget.record(cfg, images=1)
    duration = time.monotonic() - t0

    (outdir / "panels").mkdir(parents=True, exist_ok=True)
    (outdir / "panels" / f"{panel.id:02d}.png").write_bytes(data)
    meta = {
        "panel": panel.id,
        "prompt": prompt,
        "refs": [{"view": v, "path": str(p), "sha256": _sha256(p)}
                 for v, p in ref_tuples],
        "provider": image.name,
        "model": image.model,
        "aspect": aspect,
        "seed": cfg.generation.seed,  # 注意：codex 通道不支持固定 seed
        "duration_s": round(duration, 3),
        "cache_hit": cache_hit,
        "cache_key": key,
        "locked": False,          # pick 后锁定：重跑直接复用，不再生图/质检
        "accepted_key": None,     # 质检通过时记下，重跑优先复用这张图
        "qc": None,
        "flagged": False,
    }
    (outdir / "panels" / f"{panel.id:02d}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def generate_all(cfg: Config, image: ImageProvider, char: Character, sb: Storyboard,
                 layout: PageLayout, style_id: str, outdir: Path,
                 reuse_metas: dict[int, dict] | None = None,
                 progress=None) -> dict[int, dict]:
    """reuse_metas 给了就复用：locked 格直接用旧图；accepted_key 命中的格
    从缓存拿回"已接受图"，解决重跑时 attempt=0 命中不合格旧图的问题。"""
    aspect = {b.id: b.aspect for b in layout.panels}
    extras = sb.extras_map()
    metas: dict[int, dict] = {}
    todo = []
    for p in sb.panels:
        old = (reuse_metas or {}).get(p.id)
        png = outdir / "panels" / f"{p.id:02d}.png"
        if old and old.get("locked") and png.is_file():
            old["_reused"] = "locked"
            metas[p.id] = old
            continue
        if old and old.get("accepted_key"):
            data = cache.get(cfg, old["accepted_key"])
            if data is not None:
                png.write_bytes(data)
                old["cache_hit"] = True
                old["_reused"] = "accepted"
                metas[p.id] = old
                continue
        todo.append(p)
    with ThreadPoolExecutor(max_workers=cfg.generation.concurrency) as pool:
        futures = {
            pool.submit(generate_panel, cfg, image, char, p, aspect[p.id],
                        style_id, extras, outdir): p.id
            for p in todo
        }
        for fut, pid in futures.items():
            try:
                metas[pid] = fut.result()
                if progress:
                    progress(f"生图 第 {pid} 格完成")
            except Exception as e:
                # 一格失败不再拖垮整页：用占位图继续排版，标记 flagged 可补画
                metas[pid] = _failed_placeholder(cfg, image, sb, pid, aspect[pid],
                                                 outdir, str(e))
    return metas


def _failed_placeholder(cfg: Config, image: ImageProvider, sb: Storyboard, pid: int,
                        aspect: str, outdir: Path, error: str) -> dict:
    from PIL import Image as PILImage, ImageDraw
    from visdiary.providers.mock import MockImage, _ASPECT_SIZES

    w, h = _ASPECT_SIZES.get(aspect, (1024, 1024))
    img = PILImage.new("RGB", (w, h), (150, 150, 150))
    draw = ImageDraw.Draw(img)
    text = f"第 {pid} 格生成失败"
    font = MockImage._font(min(w, h) // 10)
    tw = draw.textlength(text, font=font)
    draw.text(((w - tw) / 2, h / 2 - 30), text, fill=(255, 255, 255), font=font)
    (outdir / "panels").mkdir(parents=True, exist_ok=True)
    img.save(outdir / "panels" / f"{pid:02d}.png")
    meta = {"panel": pid, "prompt": "", "refs": [], "provider": image.name,
            "model": image.model, "aspect": aspect, "seed": cfg.generation.seed,
            "duration_s": 0, "cache_hit": False, "qc": None, "flagged": True,
            "error": error}
    save_meta(outdir, meta)
    return meta


def save_meta(outdir: Path, meta: dict) -> None:
    (outdir / "panels" / f"{meta['panel']:02d}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
