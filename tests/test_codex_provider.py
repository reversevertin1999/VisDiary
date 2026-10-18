"""codex provider 的纯逻辑测试：strict schema 规范化。不发起真实调用。"""

from visdiary.providers.codex import strictify
from visdiary.schemas import Storyboard


def test_strictify_object_rules():
    s = strictify(Storyboard.model_json_schema())
    assert s["additionalProperties"] is False
    assert set(s["required"]) == set(s["properties"])
    panel = s["$defs"]["Panel"]
    assert panel["additionalProperties"] is False
    assert set(panel["required"]) == set(panel["properties"])


def test_strictify_keeps_enums():
    s = strictify(Storyboard.model_json_schema())
    shot = s["$defs"]["Panel"]["properties"]["shot"]
    assert shot["enum"][0] == "远景"


def test_strictify_nested_lists():
    s = strictify(Storyboard.model_json_schema())
    dlg = s["$defs"]["Dialogue"]
    assert dlg["additionalProperties"] is False
