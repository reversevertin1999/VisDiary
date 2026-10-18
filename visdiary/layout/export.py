"""用 Playwright 把 page.html 整页截图成 page.png。

优先用 `playwright install chromium` 装的浏览器；没装就回退到系统 Chromium
（可用环境变量 VISDIARY_CHROMIUM 指定路径）。
"""

import os
import shutil
from pathlib import Path


def _launch(playwright):
    try:
        return playwright.chromium.launch()
    except Exception:
        candidates = [os.environ.get("VISDIARY_CHROMIUM"),
                      shutil.which("chromium"), shutil.which("chromium-browser"),
                      shutil.which("google-chrome")]
        for exe in filter(None, candidates):
            try:
                return playwright.chromium.launch(executable_path=exe)
            except Exception:
                continue
        raise RuntimeError(
            "没有可用的 Chromium。运行 playwright install chromium，"
            "或设 VISDIARY_CHROMIUM 指向系统浏览器。")


def export_png(html_path: Path, png_path: Path, page_width: int) -> Path:
    from playwright.sync_api import sync_playwright

    html_path = Path(html_path).resolve()  # as_uri 只接受绝对路径
    png_path = Path(png_path)
    with sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": page_width, "height": 1000},
                                device_scale_factor=2)
        page.goto(html_path.as_uri())
        page.wait_for_load_state("networkidle")  # 等字体和图片加载完
        page.screenshot(path=str(png_path), full_page=True)
        browser.close()
    return png_path
