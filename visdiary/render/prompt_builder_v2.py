"""逐格生图的 prompt 拼装（第二轮）：画风 → 表演 → 镜头 → 画面 → 身份。

表演（ACTING）放在身份前面，权重更高；参考图只锁身份，表情以 ACTING 为准。
"""

from visdiary.config import Config
from visdiary.schemas import Character, Panel
from visdiary.styles import load_emotions, load_presets

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

INTENSITY_WORD = {1: "subtle", 2: "clearly visible",
                  3: "exaggerated manga-style, over-the-top"}


def acting_block(char: Character, panel: Panel, emotions, manpu_en) -> str:
    a = panel.acting
    preset = emotions[a.emotion]
    level = preset.levels[a.intensity]
    face = level.face + (f"; {a.face_en}" if a.face_en else "")
    body = level.body + (f"; {a.body_en}" if a.body_en else "")
    lines = [f"ACTING (most important): {char.name} is {preset.en} "
             f"({INTENSITY_WORD[a.intensity]}).",
             f"  face: {face}",
             f"  body: {body}"]
    if a.manpu:
        lines.append("  manga effects: " + "; ".join(manpu_en[m] for m in a.manpu))
    if a.chibi:
        lines.append("  draw the character in chibi / super deformed style "
                     "for comedic effect.")
    return "\n".join(lines)


def _ref_hint(used_views: set[str], expression_view: str) -> str:
    if not used_views:
        return "no reference images; pure scene."
    identity = ("keep hair style and color, eye color, face shape and accessories "
                "consistent.")
    if expression_view in used_views:
        return (f"Reference image 1 shows the character's identity: {identity} "
                f"Reference image 2 only shows the type of expression. Do NOT copy "
                f"the pose or the expression from reference image 1; the expression "
                f"must follow ACTING.")
    return (f"The reference images show the character's identity: {identity} "
            f"The expression must follow ACTING, not the reference.")


def build_prompt(cfg: Config, char: Character, panel: Panel, style_id: str,
                 extras: dict[str, str], fix_note: str | None = None,
                 used_views: set[str] | None = None) -> str:
    emotions, manpu_en = load_emotions(cfg)
    preset = load_presets(cfg)[style_id]
    identity = "; ".join(f.value for f in char.identity)
    outfit = char.outfits.get(panel.outfit) or char.outfits.get("default", "")

    visual = panel.visual_prompt_en
    speakers = {d.speaker for d in panel.dialogue}
    for name, desc in extras.items():
        if name in panel.scene or name in (panel.narration or "") or name in speakers:
            visual += f" {name}: {desc}."  # 配角引用固定描述，各格长相一致

    parts = [
        f"{preset.positive}.",
        acting_block(char, panel, emotions, manpu_en),
        f"{SHOT_EN[panel.shot]}.",
        visual,
        f"CHARACTER IDENTITY: {identity}. wearing {outfit}.",
        "single comic panel illustration, expressive manga acting, no text, "
        "no speech bubbles, no captions, no watermark, no panel border.",
    ]
    if panel.dialogue:
        parts.append("leave some empty space in the upper part of the frame.")
    if char.id in panel.characters:
        parts.append(_ref_hint(used_views or set(), emotions[panel.acting.emotion].ref_view))
    else:
        parts.append("no reference images; pure scene.")
    prompt = "\n".join(parts)
    if fix_note:
        prompt += f"\nIMPORTANT: {fix_note}"
    return prompt
