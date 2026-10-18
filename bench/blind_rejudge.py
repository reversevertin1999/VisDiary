"""同一把尺子重判 bench/results/*.json 里的图（只花多模态调用，不花生图额度）。

用法：
  VISDIARY_ENV=dev .venv/bin/python bench/blind_rejudge.py bench/results/2026-10-14.json
输出：bench/results/rejudge-<原文件名>，指标口径与 emotion_bench 完全一致。
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os
os.environ["VISDIARY_ENV"] = "dev"  # P1-10：bench 强制 dev 池，覆盖外部设置

from visdiary import providers
from visdiary.character.store import load_character
from visdiary.config import load_config
from visdiary.qc.checker import (QCInputError, _front_ref, check_panel,
                                 expression_fail_reason, identity_passes)
from visdiary.schemas import Acting, Panel, normalize_emotion


def main():
    src = Path(sys.argv[1])
    data = json.loads(src.read_text(encoding="utf-8"))
    cfg = load_config()
    cfg.provider.llm = cfg.provider.vision = cfg.provider.image = "codex"
    char = load_character(cfg, data.get("character", "xiaomali"))
    vision = providers.get_vision(cfg)
    front = _front_ref(cfg, char, "anime_cel")
    assert front is not None, "front_full 参考图缺失"  # A3

    rows = []
    for i, cell in enumerate(data["cells"], 1):
        img = src.parent / cell["image"]
        panel = Panel(id=1, shot=cell["shot"], scene="", characters=[char.id],
                      acting=Acting(emotion=cell["emotion"], intensity=cell["intensity"]),
                      action="", visual_prompt_en="", source="all")
        try:
            r = check_panel(cfg, vision, char, panel, img, front, True)
        except QCInputError as e:
            rows.append({**{k: cell[k] for k in ("emotion", "intensity", "shot", "version")},
                         "qc_error": str(e), "result": None})
            print(f"[{i}/{len(data['cells'])}] {cell['emotion']}/{cell['version']} qc_error",
                  flush=True)
            continue
        rows.append({**{k: cell[k] for k in ("emotion", "intensity", "shot", "version")},
                     "identity_pass": identity_passes(r, char, True)[0],
                     "expression_fail": expression_fail_reason(r, panel, True),
                     "detected": (normalize_emotion(r.expression.detected_emotion)
                                  if r.expression else ""),
                     "result": json.loads(r.model_dump_json())})
        print(f"[{i}/{len(data['cells'])}] {cell['emotion']}/{cell['intensity']}/"
              f"{cell['version']} -> {rows[-1]['detected']}", flush=True)

    from emotion_bench import summarize  # 同一把尺子
    summary = {v: summarize([r for r in rows if r["version"] == v])
               for v in sorted({r["version"] for r in rows})}
    out = src.parent / f"rejudge-{src.name}"
    if out.exists():  # 同一文件多次重判：防覆盖
        out = src.parent / f"rejudge-{time.strftime('%H%M')}-{src.name}"
    out.write_text(json.dumps({"source": src.name, "summary": summary, "cells": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
