from visdiary.config import LayoutConfig
from visdiary.layout.planner import plan_layout, plan_rows
from visdiary.schemas import Panel, Storyboard

CFG = LayoutConfig()


def make_panel(i: int, weight: str = "normal") -> Panel:
    return Panel(id=i, weight=weight, shot="中景", scene="s", characters=["demo"],
                 expression="平静", action="a", visual_prompt_en="s", source="all")


def make_sb(panels) -> Storyboard:
    return Storyboard(date="2026-10-08", title="t", mood="m", panels=panels)


class TestRows:
    def test_highlight_gets_own_row(self):
        panels = [make_panel(1), make_panel(2, "highlight"), make_panel(3)]
        rows = plan_rows(panels)
        assert [[p.id for p in r] for r in rows] == [[1], [2], [3]]

    def test_normal_panels_pair_up(self):
        panels = [make_panel(i) for i in range(1, 5)]
        assert [[p.id for p in r] for r in plan_rows(panels)] == [[1, 2], [3, 4]]

    def test_leftover_single_gets_own_row(self):
        panels = [make_panel(i) for i in range(1, 6)]
        assert [[p.id for p in r] for r in plan_rows(panels)] == [[1, 2], [3, 4], [5]]

    def test_highlight_flushes_buffer(self):
        panels = [make_panel(1), make_panel(2), make_panel(3, "highlight"),
                  make_panel(4)]
        rows = plan_rows(panels)
        assert [[p.id for p in r] for r in rows] == [[1, 2], [3], [4]]


class TestGeometry:
    def test_boxes_never_overlap_and_stay_on_page(self):
        panels = [make_panel(i) for i in range(1, 10)]
        panels[4].weight = "highlight"
        layout = plan_layout(make_sb(panels), CFG)
        boxes = layout.panels
        for a in boxes:
            assert a.x >= 0 and a.y >= layout.header_height - 1
            assert a.x + a.w <= layout.width
            assert a.y + a.h <= layout.height
            for b in boxes:
                if a.id < b.id:
                    assert (a.x + a.w <= b.x or b.x + b.w <= a.x
                            or a.y + a.h <= b.y or b.y + b.h <= a.y)

    def test_aspects(self):
        panels = [make_panel(1), make_panel(2), make_panel(3, "highlight"),
                  make_panel(4)]
        layout = plan_layout(make_sb(panels), CFG)
        by_id = {b.id: b for b in layout.panels}
        assert by_id[1].aspect == "1:1"      # 双格行
        assert by_id[3].aspect == "16:9"     # 高光独占一行
        assert by_id[4].aspect == "16:9"     # 落单也独占一行
        assert abs(by_id[3].w * 9 / 16 - by_id[3].h) <= 1
        assert by_id[1].w == by_id[1].h

    def test_rtl_swaps_columns(self):
        panels = [make_panel(1), make_panel(2)]
        rtl = LayoutConfig(reading_direction="rtl")
        ltr_boxes = {b.id: b for b in plan_layout(make_sb(panels), CFG).panels}
        rtl_boxes = {b.id: b for b in plan_layout(make_sb(panels), rtl).panels}
        assert ltr_boxes[1].x == rtl_boxes[2].x
        assert ltr_boxes[2].x == rtl_boxes[1].x
