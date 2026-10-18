"""人工标注校准 v2（§3.3）：先标注后统一揭晓，产出三种一致率。

用法：VISDIARY_ENV=dev .venv/bin/python bench/calibrate.py bench/results/<结果>.json
每格输入：情绪 强度 像不像(1-5) 主角对不对(y/n)，如：开心 2 4 y
  - 情绪支持别名（微笑→开心）；不合法重新提示，不会崩
  - 全部标完才统一揭晓（不在过程中显示系统判定，避免学会判官偏好）
  - 断点续标：已标图片跳过；样本用固定种子 42，写进标注文件
输出（不覆盖旧文件，带 HHMMSS 后缀）：
  - 21 类精确 κ、成对组级一致率（和质检口径一致）、强度二次加权 κ（固定类别 [1,2,3]）
  - 人工 vs 目标的 top-1（真实命中率）
  - 身份一致率
判读：组级一致率 ≥ 0.6 质检可信；0.4–0.6 参考；< 0.4 不可信。
"""

import json
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os
os.environ["VISDIARY_ENV"] = "dev"  # P1-10：强制 dev 池
from visdiary.schemas import EMOTION_GROUPS, EMOTIONS, normalize_emotion

SEED = 42
SAMPLE_N = 60
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _same_group(a: str, b: str) -> bool:
    """成对组级判断：a、b 同组或相等。重叠组（感动属于两组）天然支持。"""
    return a == b or any(a in g and b in g for g in EMOTION_GROUPS)




def _boot_ci(values: list[float], fn, n_boot: int = 1000, seed: int = 7) -> list:
    """A9：对一致性指标做 1000 次 bootstrap，返回 [lo, hi]。"""
    import random as _r
    rng = _r.Random(seed)
    n = len(values)
    if not n:
        return [0.0, 0.0]
    stats = []
    for _ in range(n_boot):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        stats.append(fn(sample))
    stats.sort()
    return [round(stats[int(0.025 * n_boot)], 3), round(stats[int(0.975 * n_boot)], 3)]

def kappa(pairs: list[tuple[str, str]]) -> float:
    if not pairs:
        return 0.0
    labels = sorted({a for a, _ in pairs} | {b for _, b in pairs})
    n = len(pairs)
    po = sum(a == b for a, b in pairs) / n
    pe = sum(
        (sum(a == l for a, _ in pairs) / n) * (sum(b == l for _, b in pairs) / n)
        for l in labels)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def quadratic_kappa(pairs: list[tuple[int, int]]) -> float:
    """强度二次加权 κ，类别固定 [1, 2, 3]（批次间可比）。"""
    cats = [1, 2, 3]
    k = len(cats)
    obs = [[0] * k for _ in range(k)]
    for a, b in pairs:
        obs[cats.index(a)][cats.index(b)] += 1
    N = len(pairs) or 1
    row = [sum(r) for r in obs]
    col = [sum(obs[i][j] for i in range(k)) for j in range(k)]
    num = sum(obs[i][j] * ((i - j) ** 2) for i in range(k) for j in range(k)) / N
    den = sum(row[i] * col[j] * ((i - j) ** 2)
              for i in range(k) for j in range(k)) / (N * N)
    return 1 - num / den if den else 1.0


def ask_label() -> dict | None:
    while True:
        raw = input("你的标注（情绪 强度 像不像1-5 主角y/n，q 结束）：").strip()
        if raw == "q":
            return None
        parts = raw.split()
        if len(parts) != 4:
            print("格式：情绪 强度 像不像 y/n，比如：开心 2 4 y"); continue
        emo = normalize_emotion(parts[0])
        if emo not in EMOTIONS:
            print(f"情绪 {parts[0]!r} 不在词表里，再输一次"); continue
        if parts[1] not in ("1", "2", "3"):
            print("强度必须是 1/2/3"); continue
        if parts[2] not in ("1", "2", "3", "4", "5"):
            print("像不像必须是 1–5"); continue
        if parts[3].lower() not in ("y", "n"):
            print("主角对不对：y 或 n"); continue
        return {"user_emotion": emo, "user_intensity": int(parts[1]),
                "likeness": int(parts[2]), "identity_ok": parts[3].lower() == "y"}


def main():
    src = Path(sys.argv[1])
    data = json.loads(src.read_text(encoding="utf-8"))
    cells = [c for c in data["cells"]
             if c.get("result") and c["result"].get("expression")
             and c.get("expression_fail") != "n/a"]
    sample = random.Random(SEED).sample(cells, min(SAMPLE_N, len(cells)))
    out_file = src.parent / f"labels-{src.stem}-{SEED}.json"

    labels = []
    if out_file.exists():
        labels = json.loads(out_file.read_text(encoding="utf-8"))
        print(f"断点续标：已有 {len(labels)} 条（种子 {SEED}，样本不变）")
    done = {l["image"] for l in labels}

    print(f"共抽 {len(sample)} 格（种子 {SEED}）。词表：{'/'.join(EMOTIONS)}")
    print("注意：过程中不显示系统判定，全部标完统一揭晓。")
    pending = [c for c in sample if c["image"] not in done]
    for i, c in enumerate(pending, 1):
        print(f"\n[{len(done)+i}/{len(sample)}] 图片：{src.parent / c['image']}")
        lab = ask_label()
        if lab is None:
            break
        labels.append({"image": c["image"], "target": c["emotion"],
                       **lab})
        out_file.write_text(json.dumps(labels, ensure_ascii=False, indent=2),
                            encoding="utf-8")

    if not labels:
        print("没有标注")
        return
    # 统一揭晓：把系统判定合并进 labels 副本（只写进结果文件，不改标注文件）
    by_img = {c["image"]: c for c in cells}
    merged = []
    for l in labels:
        c = by_img.get(l["image"], {})
        det = (c.get("result") or {}).get("expression") or {}
        merged.append({**l,
                       "model_emotion": normalize_emotion(det.get("detected_emotion", "")),
                       "model_intensity": det.get("detected_intensity"),
                       "model_identity_pass": c.get("identity_pass")})

    emo_pairs = [(l["user_emotion"], l["model_emotion"]) for l in merged]
    exact = kappa(emo_pairs)
    exact_ci = _boot_ci(emo_pairs, lambda s: kappa(s))
    pair_flags = [_same_group(l["user_emotion"], l["model_emotion"]) for l in merged]
    pair_ok = sum(pair_flags)
    pair_ci = _boot_ci([1.0 if f else 0.0 for f in pair_flags],
                       lambda s: sum(s) / len(s))
    int_pairs = [(l["user_intensity"], l["model_intensity"])
                 for l in merged if l["model_intensity"]]
    inten = quadratic_kappa(int_pairs)
    inten_ci = _boot_ci(int_pairs, lambda s: quadratic_kappa(s))
    user_top1 = sum(l["user_emotion"] == l["target"] for l in merged)
    idk = sum(1 for l in merged if l["identity_ok"] == l.get("model_identity_pass"))
    like = sum(l["likeness"] for l in merged) / len(merged)
    header = {"sample": len(merged), "seed": SEED,
              "labeled_at": time.strftime("%Y-%m-%d %H:%M"), "source": src.name,
              "reveal": "end"}
    out = {"header": header,
           "exact_kappa_21": round(exact, 3), "exact_kappa_ci95": exact_ci,
           "group_pair_agreement": f"{pair_ok}/{len(merged)}"
                                 f" ({pair_ok/len(merged):.3f})",
           "group_pair_ci95": pair_ci,
           "intensity_quadratic_kappa_fixed123": round(inten, 3),
           "intensity_kappa_ci95": inten_ci,
           "user_vs_target_top1": f"{user_top1}/{len(merged)}"
                                 f" ({user_top1/len(merged):.3f})",
           "identity_agreement": f"{idk}/{len(merged)}",
           "likeness_mean": round(like, 2), "labels": merged}
    dest = src.parent / f"calibration-{src.stem}-{time.strftime('%H%M%S')}.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n标注 {len(merged)} 条（种子 {SEED}，先标后揭）")
    print(f"  人工 vs 目标 top-1 = {user_top1}/{len(merged)}（真实命中率）")
    print(f"  21 类精确 κ = {exact:.3f}")
    print(f"  成对组级一致率 = {pair_ok}/{len(merged)}（≥0.6 可信；0.4–0.6 参考）")
    print(f"  强度二次加权 κ（固定 [1,2,3]）= {inten:.3f}  CI {inten_ci}")
    print(f"  身份一致率 = {idk}/{len(merged)}，像不像均分 = {like:.2f}")
    print(f"结果写入 {dest}（未覆盖旧文件）")


if __name__ == "__main__":
    main()
