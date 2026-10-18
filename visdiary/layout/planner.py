"""版面规划：竖向长页，按阅读顺序把格子排成行，计算每格的像素坐标与比例。

坐标是最终页面的绝对像素（宽 page_width，顶部留出页头高度），
HTML 模板用同一套坐标绝对定位，两边不会打架。
"""

from pydantic import BaseModel

from visdiary.config import LayoutConfig
from visdiary.schemas import Panel, Storyboard

HEADER_HEIGHT = 180  # 页头（日期 / 天气 / 心情 / 标题）的高度，px

SINGLE_ASPECT = "16:9"   # 单格行
DOUBLE_ASPECT = "1:1"    # 双格行每格


class PanelBox(BaseModel):
    id: int          # 对应 Panel.id
    row: int
    x: int
    y: int
    w: int
    h: int
    aspect: str


class PageLayout(BaseModel):
    width: int
    height: int
    header_height: int
    panels: list[PanelBox]


def plan_rows(panels: list[Panel]) -> list[list[Panel]]:
    """高光格独占一行；普通格两格一行；落单的一格也独占一行。"""
    rows, buf = [], []
    for p in panels:
        if p.weight == "highlight":
            if buf:
                rows.append(buf)
                buf = []
            rows.append([p])
        else:
            buf.append(p)
            if len(buf) == 2:
                rows.append(buf)
                buf = []
    if buf:
        rows.append(buf)
    return rows


def plan_layout(sb: Storyboard, cfg: LayoutConfig) -> PageLayout:
    content_w = cfg.page_width - 2 * cfg.margin
    boxes: list[PanelBox] = []
    y = HEADER_HEIGHT
    for row_idx, row in enumerate(plan_rows(sb.panels)):
        if len(row) == 1:
            w = content_w
            h = round(w * 9 / 16)
            boxes.append(PanelBox(id=row[0].id, row=row_idx, x=cfg.margin, y=y,
                                  w=w, h=h, aspect=SINGLE_ASPECT))
            y += h + cfg.gutter
        else:
            w = (content_w - cfg.gutter) // 2
            xs = [cfg.margin, cfg.margin + w + cfg.gutter]
            if cfg.reading_direction == "rtl":
                xs.reverse()
            for p, x in zip(row, xs):
                boxes.append(PanelBox(id=p.id, row=row_idx, x=x, y=y,
                                      w=w, h=w, aspect=DOUBLE_ASPECT))
            y += w + cfg.gutter
    height = y - cfg.gutter + cfg.margin
    return PageLayout(width=cfg.page_width, height=height,
                      header_height=HEADER_HEIGHT, panels=boxes)
