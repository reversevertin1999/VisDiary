"""把分镜 + 版面 + 格子图组装成一页 HTML。"""

import os
from pathlib import Path

import jinja2

from visdiary.config import Config
from visdiary.layout.planner import PageLayout
from visdiary.schemas import Storyboard

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def build_page(sb: Storyboard, layout: PageLayout, cfg: Config,
               outdir: Path, protagonist: str, weather: str | None,
               panels_dir: str = "panels") -> Path:
    """渲染 page.html 到 outdir。格子图引用相对路径 panels/NN.png。"""
    outdir = Path(outdir)
    panel_by_id = {p.id: p for p in sb.panels}
    items = [{"box": box, "panel": panel_by_id[box.id]} for box in layout.panels]

    font_abs = cfg.path(cfg.layout.font)
    font_href = os.path.relpath(font_abs, outdir)

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TEMPLATE_DIR),
                             autoescape=True)
    html = env.get_template("page.html.j2").render(
        sb=sb, layout=layout, cfg=cfg, items=items,
        panels_dir=panels_dir, protagonist=protagonist,
        weather=weather or "未知", font_href=font_href,
    )
    out = outdir / "page.html"
    out.write_text(html, encoding="utf-8")
    return out
