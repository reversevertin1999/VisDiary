"""逐格生图的 prompt 拼装（第三轮）：画风 → 表演 → 镜头 → 画面 → 身份。

- 表演（ACTING）放在身份前面；纯场景格（主角不在画面）不写表演块
- 描述合并优先级：panel.face_en/body_en > persona.overrides > emotions.yaml
- body 按景别：近景/特写只允许上半身和手部动作（body_close）
- "expressive manga acting" 只在 intensity ≥ 2 时加（安静格不强推夸张）
"""

from visdiary.config import Config
from visdiary.schemas import Character, Panel, Persona
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
CLOSE_SHOTS = {"近景", "特写"}


def acting_block(char: Character, panel: Panel, emotions, manpu_en) -> str:
    a = panel.acting
    preset = emotions[a.emotion]
    level = preset.levels[a.intensity]
    ov = char.persona.overrides.get(a.emotion)

    # 合并优先级：面板自带 > 人设覆盖 > 词表模板
    face = a.face_en or (ov.face.get(a.intensity) if ov else None) or level.face
    body_default = (level.body_close or level.body) if panel.shot in CLOSE_SHOTS \
        else level.body
    body = a.body_en or (ov.body.get(a.intensity) if ov else None) or body_default

    lines = [f"ACTING (most important): {char.name} is {preset.en} "
             f"({INTENSITY_WORD[a.intensity]}).",
             f"  face: {face}",
             f"  body: {body}"]
    if a.intensity == 1:  # B 修复：强度 1 明确压住夸张倾向
        lines.append("  restraint: this is a SUBTLE expression — no exaggerated "
                     "expression, no manga symbols, no tears")
    if a.manpu:
        lines.append("  manga effects: " + "; ".join(manpu_en[m] for m in a.manpu))
    if a.chibi:
        lines.append("  draw the character in chibi / super deformed style "
                     "for comedic effect.")
    if char.persona.signature:
        lines.append("  signature mannerisms: " + "; ".join(char.persona.signature))
    return "\n".join(lines)


def _ref_hint(used_views: set[str], expression_view: str | None) -> str:
    if not used_views:
        return "no reference images; pure scene."
    identity = ("keep hair style and color, eye color, face shape and accessories "
                "consistent.")
    if expression_view and expression_view in used_views:
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
    has_protagonist = char.id in panel.characters

    # M19 双人格：空间锚定 + 配角完整 critical 特征（P1-4 修复）
    cast_extra = ""
    others = [c for c in panel.characters if c != char.id]
    if has_protagonist and others:
        from visdiary.character.store import load_character
        pos = ["left", "right"]  # 主角左、配角右
        for j, oid in enumerate(others[:1]):  # 最多 1 个配角
            try:
                other = load_character(cfg, oid)
            except FileNotFoundError:
                continue
            # P1-4-1：全部 critical 特征（不只前 3 条）
            crit = [f for f in other.identity if f.critical] or other.identity
            obrief = "; ".join(f.value for f in crit)
            # P1-4-4：不写死性别
            cast_extra = (f"\nSECOND CHARACTER (on the {pos[1]}): {other.name} — "
                          f"{obrief}. wearing {other.outfits.get('default', '')}. "
                          f"This character has a simple calm expression. "
                          f"SPATIAL LAYOUT: {char.name} is on the {pos[0]}, "
                          f"{other.name} is on the {pos[1]}. "
                          f"Do NOT mix their features: each character keeps their own "
                          f"hair color and eye color.")

    visual = panel.visual_prompt_en
    speakers = {d.speaker for d in panel.dialogue}
    for name, desc in extras.items():
        if name in panel.scene or name in (panel.narration or "") or name in speakers:
            visual += f" {name}: {desc}."  # 配角引用固定描述，各格长相一致

    parts = [f"{preset.positive}."]
    if has_protagonist:
        ab = acting_block(char, panel, emotions, manpu_en)
        if cast_extra:  # 双人时把空间锚定并进 ACTING 块
            ab += f"\n  layout: {cast_extra}"
        parts.append(ab)
    parts.append(SHOT_EN[panel.shot] + ".")
    parts.append(visual)
    if has_protagonist:
        parts.append(f"CHARACTER IDENTITY: {identity}. wearing {outfit}.")
    no_text = "no text, no speech bubbles, no captions, no watermark, no panel border."
    if panel.acting.intensity >= 2:
        no_text = f"expressive manga acting, {no_text}"
    parts.append(f"single comic panel illustration, {no_text}")
    if panel.dialogue:
        parts.append("leave some empty space in the upper part of the frame.")
    if has_protagonist:
        parts.append(_ref_hint(used_views or set(),
                               _expression_view(char, panel, emotions)))
    else:
        parts.append("no reference images; pure scene.")
    prompt = "\n".join(parts)
    if fix_note:
        prompt += f"\nIMPORTANT: {fix_note}"
    return prompt


def _expression_view(char: Character, panel: Panel, emotions) -> str | None:
    ov = char.persona.overrides.get(panel.acting.emotion)
    if ov and ov.ref_view:
        return ov.ref_view
    return emotions[panel.acting.emotion].ref_view
