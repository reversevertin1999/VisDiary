"""一次性迁移：修掉 bench/results/*.json 里多拼的 "img-img-" 前缀（A1）。

用法：.venv/bin/python bench/migrate_paths.py
每个 json 先备份成 .json.bak 再改；最后报告"仍找不到 N 条"，N 必须为 0。
"""

import json
import shutil
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"


def main():
    total_missing = 0
    for js in sorted(RESULTS.glob("*.json")):
        if js.name.startswith(("rejudge-", "labels-", "baseline-")):
            continue
        data = json.loads(js.read_text(encoding="utf-8"))
        cells = data.get("cells", [])
        if not any("img-img-" in c.get("image", "") for c in cells):
            continue
        shutil.copy2(js, js.with_suffix(".json.bak"))
        fixed = 0
        for c in cells:
            if "img-img-" in c.get("image", ""):
                c["image"] = c["image"].replace("img-img-", "img-", 1)
                fixed += 1
        js.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{js.name}: 修了 {fixed} 条（备份 {js.name}.bak）")
    # 同步修 HTML 网格里的引用
    for html in sorted(RESULTS.glob("*.html")):
        s = html.read_text(encoding="utf-8")
        if "img-img-" in s:
            shutil.copy2(html, html.with_suffix(".html.bak"))
            html.write_text(s.replace("img-img-", "img-"), encoding="utf-8")
            print(f"{html.name}: 已修")
    # 校验：迁移后所有路径都必须存在
    for js in sorted(RESULTS.glob("*.json")):
        if js.name.startswith(("rejudge-", "labels-", "baseline-")):
            continue
        for c in json.loads(js.read_text(encoding="utf-8")).get("cells", []):
            if c.get("image") and not (js.parent / c["image"]).is_file():
                total_missing += 1
                print(f"  仍找不到: {js.name} -> {c['image']}")
    print(f"仍找不到 {total_missing} 条")
    sys.exit(1 if total_missing else 0)


if __name__ == "__main__":
    main()
