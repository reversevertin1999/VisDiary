"""逐格生图的 prompt 拼装：画风 + 角色档案 + 镜头/表情 + 画面描述 + 禁令。"""

from visdiary.config import Config
from visdiary.schemas import Character, Panel
from visdiary.styles import load_presets

SHOT_EN = {
    "远景": "wide shot, character small in the environment",
    "全景": "full shot, whole body visible",
    "中景": "medium shot, waist up",
    "近景": "medium close-up, chest up",
    "特写": "close-up on the face",
    "俯视": "high-angle shot",
    "仰视": "low-angle shot",
    "背影": "back view, seen from behind",
}

# 表情 -> (expression_en, 对应的胸像视图，没有则为 None)
EXPRESSION_EN = {
    "开心": ("smiling", "bust_smile"),
    "大笑": ("laughing", "bust_smile"),
    "难过": ("sad", "bust_cry"),
    "哭": ("crying with tears", "bust_cry"),
    "生气": ("angry", "bust_angry"),
    "惊讶": ("surprised", "bust_surprised"),
    "疲惫": ("exhausted", "bust_tired"),
    "平静": ("calm", None),
    "害羞": ("blushing shyly", None),
    "思考": ("thinking", None),
}

# 第二轮的 21 种情绪映射回第一轮最近的词，供 A/B 基线复用
_V1_EMOTION = {
    "平静": "平静", "开心": "开心", "大笑": "大笑", "感动": "开心",
    "得意": "开心", "期待": "开心", "害羞": "害羞", "思考": "思考",
    "无语": "平静", "紧张": "惊讶", "委屈": "难过", "难过": "难过",
    "哭": "哭", "大哭": "哭", "生气": "生气", "暴怒": "生气",
    "惊讶": "惊讶", "震惊": "惊讶", "疲惫": "疲惫", "崩溃": "哭",
    "坚定": "平静",
}

_TEMPLATE = """{style_positive}.
{name}: {identity}. wearing {outfit}.
{shot_en}. {expression_en}.
{visual}
single comic panel illustration, no text, no speech bubbles, no captions, no watermark, no panel border.
{dialogue_hint}{ref_hint}"""


def build_prompt(cfg: Config, char: Character, panel: Panel, style_id: str,
                 extras: dict[str, str], fix_note: str | None = None) -> str:
    preset = load_presets(cfg)[style_id]
    identity = "; ".join(f.value for f in char.identity)
    outfit = char.outfits.get(panel.outfit) or char.outfits.get("default", "")
    expression_en = EXPRESSION_EN[_V1_EMOTION[panel.acting.emotion]][0]

    visual = panel.visual_prompt_en
    for name, desc in extras.items():
        if name in panel.scene or name in (panel.narration or ""):
            visual += f" {name}: {desc}."  # 配角引用固定描述，各格长相一致

    prompt = _TEMPLATE.format(
        style_positive=preset.positive, name=char.name, identity=identity,
        outfit=outfit, shot_en=SHOT_EN[panel.shot], expression_en=expression_en,
        visual=visual,
        dialogue_hint=("leave some empty space in the upper part of the frame.\n"
                       if panel.dialogue else ""),
        ref_hint=("the reference images show the same character; keep the face, hair, "
                  "eye color and accessories identical." if char.id in panel.characters
                  else "no reference images; pure scene."),
    )
    if fix_note:
        prompt += f"\nIMPORTANT: {fix_note}"
    return prompt
