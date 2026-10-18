"""M8 表情基准测试（第四轮口径）：不走分镜，直接按 情绪×强度×镜头 拼 prompt 生图。

用法（在 VisDiary 目录下）：
  VISDIARY_ENV=dev .venv/bin/python bench/emotion_bench.py              # 8 情绪 × 强度1/2/3 × 近景 = 24 格
  VISDIARY_ENV=dev .venv/bin/python bench/emotion_bench.py --versions v2 v3
  VISDIARY_ENV=dev .venv/bin/python bench/emotion_bench.py --full       # 21 情绪 × 3 级 × 2 镜头 = 126 格

指标（A7）：top-1 精确命中（主指标）、组内命中、top-2 命中、强度 |差|<=1、
演过头率（含强度 1 样本）、身份通过率（排除 qc_error）、qc_error_count。
比例一律报告 k/n 和 Wilson 95% 置信区间。
输出：bench/results/<日期>.json（含每格完整 QCResult）+ 同名 .html 网格。
"""

import argparse
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os
os.environ["VISDIARY_ENV"] = "dev"  # P1-10：bench 强制 dev 池，覆盖外部设置

from visdiary import budget, providers
from visdiary.character.store import load_character
from visdiary.config import load_config
from visdiary.qc.checker import (QCInputError, _front_ref, check_panel,
                                 expression_fail_reason, identity_passes)
from visdiary.render import prompt_builder, prompt_builder_v1, prompt_builder_v2
from visdiary.render.refs import select_refs
from visdiary.schemas import EMOTION_GROUPS, EMOTIONS, Acting, Panel, normalize_emotion

BUILDERS = {"v1": prompt_builder_v1, "v2": prompt_builder_v2, "v3": prompt_builder}

COMMON = ["开心", "大笑", "生气", "难过", "哭", "惊讶", "疲惫", "崩溃"]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (round(c - h, 3), round(c + h, 3))


def cells(full: bool, both_shots: bool = False, only_intensity: int | None = None):
    emos = EMOTIONS if full else COMMON
    intensities = [only_intensity] if only_intensity else [1, 2, 3]
    shots = ["近景", "中景"] if (full or both_shots) else ["近景"]
    return [(e, i, s) for e in emos for i in intensities for s in shots]


def run_one(cfg, char, image, vision, emotion, intensity, shot, version, outdir):
    builder = BUILDERS[version]
    panel = Panel(id=1, shot=shot, scene="ordinary indoor scene", characters=[char.id],
                  acting=Acting(emotion=emotion, intensity=intensity),
                  action="standing", visual_prompt_en="an ordinary daily indoor scene",
                  source="all")
    refs = select_refs(cfg, char, panel, "anime_cel")
    front = _front_ref(cfg, char, "anime_cel")
    assert front is not None, "front_full 参考图缺失，质检口径不成立"  # A3
    kw = {"used_views": {v for v, _p in refs}} if version in ("v2", "v3") else {}
    prompt = builder.build_prompt(cfg, char, panel, "anime_cel", {}, **kw)
    t0 = time.monotonic()
    last_err = None
    for attempt in range(3):
        try:
            data = image.generate(prompt, refs, "1:1", seed=cfg.generation.seed)
            break
        except Exception as e:
            last_err = e
            time.sleep(5 * (attempt + 1))
    else:
        raise RuntimeError(f"{emotion}/{intensity}/{shot}/{version} 生图失败：{last_err}")
    budget.record(cfg, images=1)
    name = f"{emotion}_{intensity}_{shot}_{version}"
    img_path = outdir / f"{name}.png"
    img_path.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()[:16]

    try:
        result = check_panel(cfg, vision, char, panel, img_path, front, True)
    except QCInputError as e:
        return {"emotion": emotion, "intensity": intensity, "shot": shot,
                "version": version, "image": f"{outdir.name}/{name}.png",
                "sha256": sha, "duration_s": round(time.monotonic() - t0, 1),
                "qc_error": str(e), "result": None, "prompt": prompt}

    fail = expression_fail_reason(result, panel, True)
    detected = normalize_emotion(result.expression.detected_emotion) \
        if result.expression else ""
    return {
        "emotion": emotion, "intensity": intensity, "shot": shot, "version": version,
        "image": f"{outdir.name}/{name}.png",  # outdir.name 已含 img- 前缀，别再拼
        "sha256": sha,
        "duration_s": round(time.monotonic() - t0, 1),
        "identity_pass": identity_passes(result, char, True)[0],
        "expression_fail": fail,
        "detected": detected,
        "result": json.loads(result.model_dump_json()),  # A6：完整明细
        "prompt": prompt,
    }


def summarize(rows: list[dict]) -> dict:
    """§3.2 新口径：所有比例 k/n + Wilson CI；强度改为精确命中 / MAE / 偏差 /
    分层演过头率 / 3×3 混淆矩阵。n/a 与 qc_error 不进任何分母。"""
    valid = [r for r in rows if not r.get("qc_error")]
    n = len(valid)
    d = lambda r: r["result"]["expression"]  # noqa: E731
    expr_rows = [r for r in valid if d(r) and r.get("expression_fail") != "n/a"]
    m = len(expr_rows)

    def top1(r):
        return normalize_emotion(d(r)["detected_emotion"]) == r["emotion"]

    def top1_exact_v2(r):  # top-2 里任一精确等于目标
        cands = [normalize_emotion(x) for x in
                 [d(r)["detected_emotion"]] + d(r).get("detected_top2", [])]
        return r["emotion"] in cands

    def inten_exact(r):
        return d(r)["detected_intensity"] == r["intensity"]

    def over(r):
        return d(r)["detected_intensity"] > r["intensity"]  # 差 1 就算演过头

    def rate(fn, denom):
        k = sum(1 for r in denom if fn(r))
        nn = len(denom)
        return {"k": k, "n": nn, "rate": round(k / nn, 3) if nn else None,
                "ci95": wilson(k, nn)}

    diffs = [d(r)["detected_intensity"] - r["intensity"] for r in expr_rows]
    conf = [[0] * 3 for _ in range(3)]  # 行=目标，列=检测
    for r in expr_rows:
        conf[r["intensity"] - 1][d(r)["detected_intensity"] - 1] += 1
    idk = sum(1 for r in valid if r["identity_pass"])
    return {
        "panels": len(rows), "qc_error_count": len(rows) - n,
        "expression_na_count": sum(1 for r in valid if r.get("expression_fail") == "n/a"),
        "expression_top1_exact": rate(top1, expr_rows),
        "expression_top2_exact": rate(top1_exact_v2, expr_rows),
        "expression_group_hit": rate(lambda r: not (r.get("expression_fail") == "category"),
                                     expr_rows),
        "intensity_exact": rate(inten_exact, expr_rows),
        "intensity_mae": round(sum(abs(x) for x in diffs) / m, 3) if m else None,
        "intensity_bias": round(sum(diffs) / m, 3) if m else None,
        "overacted_by_target": {
            str(t): rate(over, [r for r in expr_rows if r["intensity"] == t])
            for t in (1, 2, 3)},
        "intensity_confusion": {"rows_target_cols_detected": conf},
        "identity_pass": {"k": idk, "n": n, "rate": round(idk / n, 3) if n else None,
                          "ci95": wilson(idk, n)},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--both-shots", action="store_true", help="子集也跑近景+中景（48 格）")
    ap.add_argument("--only-intensity", type=int, choices=[1, 2, 3],
                    help="只跑指定强度（B 复测用：强度1 × 8情绪 × 2镜头 = 16 格）")
    ap.add_argument("--versions", nargs="+", choices=["v1", "v2", "v3"],
                    default=["v3"], help="prompt 版本：v1 / v2 / v3（默认 v3）")
    ap.add_argument("--character", default="xiaomali")
    ap.add_argument("--provider", default="codex", help="默认 codex 真实通道")
    args = ap.parse_args()

    cfg = load_config()
    cfg.provider.llm = cfg.provider.vision = cfg.provider.image = args.provider
    char = load_character(cfg, args.character)
    image = providers.get_image(cfg)
    vision = providers.get_vision(cfg)

    today = time.strftime("%Y-%m-%d")
    run = time.strftime("%H%M%S")
    outdir = Path("bench/results") / f"img-{today}-{run}"  # P1-9：独立 run 目录，不覆盖旧图
    outdir.mkdir(parents=True, exist_ok=True)

    jobs = [(e, i, s, v) for e, i, s in cells(args.full, args.both_shots, args.only_intensity) for v in args.versions]
    print(f"共 {len(jobs)} 格（{args.character}，{cfg.provider.image}）", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=cfg.generation.concurrency) as pool:
        futs = {pool.submit(run_one, cfg, char, image, vision, e, i, s, v, outdir):
                (e, i, s, v) for e, i, s, v in jobs}
        for k, (fut, key) in enumerate(futs.items(), 1):
            r = fut.result()
            results.append(r)
            print(f"[{k}/{len(jobs)}] {key[0]}/{key[1]}/{key[3]} "
                  f"{'qc_error' if r.get('qc_error') else r['detected']}",
                  flush=True)

    summary = {v: summarize([r for r in results if r["version"] == v])
               for v in args.versions}
    out_json = Path("bench/results") / f"{today}.json"
    if out_json.exists():  # 同一天多次跑：加时间后缀防覆盖
        out_json = Path("bench/results") / f"{today}-{time.strftime('%H%M')}.json"
    for r in results:  # A1：写结果前逐条断言图片路径存在
        assert (out_json.parent / r["image"]).is_file(), r["image"]
    out_json.write_text(
        json.dumps({"date": today, "character": args.character,
                    "provider": cfg.provider.image, "summary": summary,
                    "cells": results}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    write_grid(out_json.with_suffix(".html"), results, args.versions, summary, today)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def write_grid(path, results, versions, summary, today):
    rows = []
    for r in results:
        cell = f"{r['emotion']}/强度{r['intensity']}/{r['shot']}"
        if r.get("qc_error"):
            verdict = f"qc_error: {r['qc_error']}"
        else:
            verdict = (f"检测：{r['detected']}<br>表情{'✓' if r['expression_fail'] is None else '✗'}"
                       f" 身份{'✓' if r['identity_pass'] else '✗'}")
        rows.append(f"""<tr><td>{cell}</td><td>{r['version']}</td>
<td><img src="{r['image']}" loading="lazy"></td><td>{verdict}</td></tr>""")
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>表情基准 {today}</title><style>
body{{font-family:sans-serif;max-width:1300px;margin:20px auto}}
table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccc;padding:6px;font-size:13px;vertical-align:top}}
img{{width:260px;display:block}}
tr:nth-child(even){{background:#f6f6f6}}
</style></head><body><h1>表情基准 {today}</h1>
<pre>{json.dumps(summary, ensure_ascii=False, indent=2)}</pre>
<table><tr><th>格</th><th>prompt</th><th>图</th><th>判定</th></tr>
{''.join(rows)}
</table></body></html>"""
    path.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
