"""M19 双人基准：小玛丽 × 大肥鱼同框，测身份通过率/串色率 + 串色负样本。

用法：VISDIARY_ENV=dev .venv/bin/python bench/duo_bench.py [--negatives-only]
前置：两个角色都已 approve。
输出：bench/results/duo-<日期>-<HHMMSS>/（图 + duo.json）
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["VISDIARY_ENV"] = "dev"

from visdiary import budget, providers
from visdiary.character.store import load_character
from visdiary.config import load_config
from visdiary.qc.checker import (QCInputError, check_panel, expression_fail_reason,
                                 identity_passes)
from visdiary.render.prompt_builder import SHOT_EN, build_prompt
from visdiary.render.refs import select_refs
from visdiary.schemas import Acting, Panel
from visdiary.styles import load_emotions, load_presets

PROTAG = "xiaomali"
SIDEKICK = "dafeiyu"

# 8 个双人场景（日常向，情绪混合）
SCENES = [
    ("一起吃饭开心聊天", "sharing snacks at a table, both happy", "开心", "开心"),
    ("大肥鱼安慰难过的小玛丽", "sidekick comforting the protagonist who looks down", "难过", "平静"),
    ("一起赶 ddl 疲惫", "both slumped at desks with laptops, late night", "疲惫", "疲惫"),
    ("实验跑通惊讶欢呼", "both startled then cheering at a screen", "惊讶", "开心"),
    ("小玛丽生气大肥鱼无奈", "protagonist pouting angry, sidekick shrugging", "生气", "无语"),
    ("一起逛街期待", "walking together with shopping bags, excited", "期待", "开心"),
    ("看恐怖片紧张", "huddled together watching something scary", "紧张", "惊讶"),
    ("通关游戏大笑击掌", "high-fiving over a game victory", "大笑", "大笑"),
]
SHOTS = ["中景", "全景"]

# 串色负样本：故意交换发色
CROSS_PROMPT_SUFFIX = ("CROSS-CONTAMINATION TEST: swap their hair colors with each "
                       "other deliberately — {a} gets {b}'s hair color and vice versa")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--negatives-only", action="store_true",
                    help="只跑 6 张串色负样本")
    args = ap.parse_args()

    cfg = load_config()
    cfg.provider.llm = cfg.provider.vision = cfg.provider.image = "codex"
    hero = load_character(cfg, PROTAG)
    side = load_character(cfg, SIDEKICK)
    for c in (hero, side):
        approved = any(s.approved for s in c.sheets.values())
        if not approved:
            sys.exit(f"{c.id} 还没 approve")
    image = providers.get_image(cfg)
    vision = providers.get_vision(cfg)
    emotions, _ = load_emotions(cfg)

    run = time.strftime("%Y%m%d-%H%M%S")
    outdir = Path("bench/results") / f"duo-{run}"
    outdir.mkdir(parents=True, exist_ok=True)
    rows = []

    def gen_and_qc(name, panel, prompt, refs, cross_expect=False):
        t0 = time.monotonic()
        for attempt in range(2):
            try:
                data = image.generate(prompt, refs, "3:2", seed=None)
                break
            except Exception as e:
                if attempt:
                    raise
                time.sleep(10)
        budget.record(cfg, images=1)
        p = outdir / f"{name}.png"
        p.write_bytes(data)
        try:
            r = check_panel(cfg, vision, hero, panel, p, refs[0][1]
                            if refs else None, True)
            # 逐角色判定（用双键）
            ok_h, prob_h, _ = identity_passes(r, hero, True, cfg=cfg, panel=panel)
            # 配角视角：临时以 side 为主角再判一次
            ok_s, prob_s, _ = identity_passes(r, side, True, cfg=cfg, panel=panel)
            cross = r.cross_contamination
        except QCInputError as e:
            return {"name": name, "qc_error": str(e), "file": p.name}
        row = {"name": name, "file": p.name,
               "sha256": hashlib.sha256(data).hexdigest()[:16],
               "hero_pass": ok_h, "side_pass": ok_s,
               "cross_detected": cross,
               "hero_problems": prob_h[:3], "side_problems": prob_s[:3],
               "duration_s": round(time.monotonic() - t0, 1)}
        rows.append(row)
        print(f"{name}: 主角{'✓' if ok_h else '✗'} 配角{'✓' if ok_s else '✗'} "
              f"串色{'检出' if cross else '未检出'}", flush=True)
        return row

    def duo_panel(shot):
        return Panel(id=1, shot=shot, scene="duo", characters=[PROTAG, SIDEKICK],
                     acting=Acting(emotion="平静", intensity=1),
                     action="", visual_prompt_en="", source="all")

    if not args.negatives_only:
        for desc, visual, emo_h, emo_s in SCENES:
            for shot in SHOTS:
                name = f"{desc[:6]}_{shot}"
                panel = duo_panel(shot)
                panel.acting.emotion = emo_h
                used = select_refs(cfg, hero, panel, "anime_cel")
                prompt = build_prompt(cfg, hero, panel, "anime_cel", {},
                                      used_views={v for v, _ in used})
                prompt += (f"\nSECOND CHARACTER (right): {side.name} — "
                           + "; ".join(f.value for f in side.identity[:4])
                           + f". simple {emo_s} expression.")
                gen_and_qc(name, panel, prompt, used)

    # 串色负样本：换发色
    hero_hair = next((f.value for f in hero.identity if "发色" in f.name), "unknown")
    side_hair = next((f.value for f in side.identity if "发色" in f.name), "unknown")
    for i, (desc, visual, emo_h, _) in enumerate(SCENES[:6]):
        name = f"neg{i}_{desc[:6]}"
        panel = duo_panel("中景")
        panel.acting.emotion = emo_h
        used = select_refs(cfg, hero, panel, "anime_cel")
        prompt = build_prompt(cfg, hero, panel, "anime_cel", {},
                              used_views={v for v, _ in used})
        prompt += (f"\nSECOND CHARACTER (right): {side.name}. "
                   + CROSS_PROMPT_SUFFIX.format(a=hero.name, b=side.name))
        row = gen_and_qc(name, panel, prompt, used, cross_expect=True)
        if isinstance(row, dict) and "cross_detected" in row:
            row["expected_cross"] = True

    # 汇总
    valid = [r for r in rows if not r.get("qc_error")]
    pos = [r for r in valid if not r.get("expected_cross")]
    neg = [r for r in valid if r.get("expected_cross")]

    def wilson(k, n, z=1.96):
        if not n:
            return (0.0, 0.0)
        p_ = k / n
        d = 1 + z * z / n
        c = (p_ + z * z / (2 * n)) / d
        h = z * ((p_ * (1 - p_) / n + z * z / (4 * n * n)) ** 0.5) / d
        return (round(c - h, 3), round(c + h, 3))

    hk = sum(1 for r in pos if r["hero_pass"])
    sk = sum(1 for r in pos if r["side_pass"])
    ck = sum(1 for r in neg if r["cross_detected"])
    summary = {
        "pairs": len(pos), "qc_error_count": len(rows) - len(valid),
        "hero_identity": {"k": hk, "n": len(pos), "ci95": wilson(hk, len(pos))},
        "side_identity": {"k": sk, "n": len(pos), "ci95": wilson(sk, len(pos))},
        "cross_negatives_caught": {"k": ck, "n": len(neg), "ci95": wilson(ck, len(neg))},
    }
    out = {"run": run, "hero": PROTAG, "side": SIDEKICK,
           "summary": summary, "cells": rows}
    (outdir / "duo.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"输出：{outdir}/duo.json")


if __name__ == "__main__":
    main()
