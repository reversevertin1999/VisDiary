"""生成身份负样本（§3.3.2）：故意改掉角色的 critical 特征，用来检验身份质检能否抓错。

用法：VISDIARY_ENV=dev .venv/bin/python bench/negatives.py [--count 10]
输出：bench/results/negatives-<HHMMSS>/（图片 + negatives.json，含每张改了什么）
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["VISDIARY_ENV"] = "dev"  # 强制 dev 池

from visdiary import budget, providers
from visdiary.character.store import load_character
from visdiary.config import load_config
from visdiary.render.prompt_builder import SHOT_EN
from visdiary.schemas import Acting, Panel
from visdiary.styles import load_emotions, load_presets

# 负样本配方：改掉的 critical 特征（写进 prompt 的反面描述）
RECIPES = [
    ("换发色", lambda i, v: f"hair color changed to {['jet black', 'bright pink', 'platinum blonde', 'green'][i % 4]}"),
    ("去掉猫耳", lambda i, v: "no cat ears on the head, plain hair"),
    ("换瞳色", lambda i, v: f"eye color changed to {['red', 'green', 'golden'][i % 3]}"),
    ("换发型", lambda i, v: "hair styled into a short bob instead of long hair"),
    ("换角色", lambda i, v: "a completely different anime schoolgirl, silver short hair, green eyes"),
    ("去掉发饰", lambda i, v: "no hair accessory or head decoration"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--character", default="xiaomali")
    args = ap.parse_args()

    cfg = load_config()
    cfg.provider.llm = cfg.provider.vision = cfg.provider.image = "codex"
    char = load_character(cfg, args.character)
    image = providers.get_image(cfg)
    preset = load_presets(cfg)["anime_cel"]
    emotions, _ = load_emotions(cfg)
    identity = "; ".join(f.value for f in char.identity)

    run = time.strftime("%Y%m%d-%H%M%S")
    outdir = Path("bench/results") / f"negatives-{run}"
    outdir.mkdir(parents=True, exist_ok=True)

    rows = []
    for i in range(args.count):
        recipe_name, tweak_fn = RECIPES[i % len(RECIPES)]
        tweak = tweak_fn(i, identity)
        emo = ["开心", "难过", "生气", "惊讶"][i % 4]
        level = emotions[emo].levels[2]
        prompt = (f"{preset.positive}. ACTING: {char.name} is {emotions[emo].en} "
                  f"(clearly visible).\n  face: {level.face}\n  body: {level.body}\n"
                  f"{SHOT_EN['近景']}.\n"
                  f"an ordinary indoor scene. CHARACTER: {identity} — EXCEPT {recipe_name}. "
                  f"IDENTITY BREAK: {tweak}. "
                  f"no text, no speech bubbles, no watermark.")
        try:
            data = image.generate(prompt, [("身份图（front_full）",
                                            cfg.path(f"characters/{char.id}/sheets/anime_cel/front_full.png"))],
                                  "1:1", seed=None)
        except Exception as e:
            print(f"[{i+1}/{args.count}] {recipe_name} 生成失败：{e}")
            continue
        budget.record(cfg, images=1)
        f = outdir / f"neg_{i:02d}_{recipe_name}.png"
        f.write_bytes(data)
        rows.append({"file": f.name, "recipe": recipe_name, "tweak": tweak,
                     "sha256": hashlib.sha256(data).hexdigest()[:16],
                     "target": emo})
        print(f"[{i+1}/{args.count}] {recipe_name} -> {f.name}", flush=True)

    (outdir / "negatives.json").write_text(
        json.dumps({"character": char.id, "run": run, "count": len(rows),
                    "files": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"完成 {len(rows)} 张负样本 -> {outdir}")


if __name__ == "__main__":
    main()
