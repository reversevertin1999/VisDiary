"""拆分镜的系统提示词与用户提示词拼装。

注意：用户提示词里有几处固定格式（主角行、格数范围、建议格数、已知角色 id、
日记日期、## HH:MM 条目），MockLLM 依赖这些格式解析，改动时要同步 mock。
"""

from visdiary.config import Config
from visdiary.diary.parser import Diary
from visdiary.schemas import Character
from visdiary.styles import load_emotions

SYSTEM_PROMPT = "你是漫画分镜师。只按 JSON schema 输出，不要输出其他内容。"

USER_TEMPLATE = """把用户一天的日记改编成竖向条漫分镜。主角是 {protagonist_name}（角色 id：{protagonist_id}）。

规则：
1. 一个有意义的情节或情绪转折画一格；流水账要合并，不要逐句画。
2. 格数范围 {min_panels}–{max_panels}，本篇建议约 {hint} 格。
3. 恰好一格 weight=highlight，选当天最重要或情绪最强的时刻，它的 intensity 必须是 3。
4. 相邻两格的 shot 不能相同。
5. 压力、开心、疲惫等抽象感受可以用视觉比喻表达，写进 metaphor，并体现在 visual_prompt_en 里。
6. 旁白和对白用第一人称、口语化，每条不超过 28 个字；每格最多 2 条对白。
7. 只使用日记里出现的事件，不要编造；可以补充合理的场景细节（光线、天气、环境）。
8. characters 只能使用已建档角色 id：{known_ids}。cast 里的角色（含别名）可以进 characters，一格最多 2 个已建档角色；cast 之外的人写进 scene 和 visual_prompt_en，不放进 characters；反复出现的配角在 extras 里记一条"人名 -> 固定的一句英文外貌描述"，各格引用同一句。{cast_block}
9. visual_prompt_en 用英文描述画面（动作、场景、构图），不要描述画风，不要要求画面出现任何文字。
10. source 填写这一格对应的随笔时间标题；没有时间标题时填 "all"。
11. 只按 JSON schema 输出，不要输出其他内容。
12. 每格先写 inner_voice（主角此刻心里那句话），再根据它选 emotion 和 intensity。
13. 这是漫画，不是写实插画：情绪要外放。日常小事用 1–2 级，当天情绪最强的时刻用 3 级。
14. 一页漫画要有情绪起伏，不要整页都是平静或开心。
15. body_en 写主角的肢体动作；远景格尤其要靠肢体表达情绪。
16. 搞笑、自嘲、崩溃的时刻可以用 chibi=true（Q 版）和漫符。
17. visual_prompt_en 只写场景、动作、构图，不要写表情（表情由 acting 字段负责）。
18. emotion 只能从词表里选：{emotion_list}

角色档案：
{character_sheet}
{persona_block}
日记日期：{date}
天气：{weather}
标题：{title}
日记内容：
{diary_body}{feedback_block}"""


def _cast_block(cfg: Config, protagonist_id: str) -> str:
    """F2：cast 清单（id、名字、别名、一句话外貌），空 cast 返回空串（行为不变）。"""
    from visdiary.character.store import load_character
    lines = []
    for m in cfg.cast:
        if m.id == protagonist_id:
            continue
        try:
            c = load_character(cfg, m.id)
            brief = "；".join(f.value for f in c.identity[:3])
            names = "/".join([c.name] + m.aliases)
            lines.append(f"- {m.id}（{names}）：{brief}")
        except FileNotFoundError:
            continue
    if not lines:
        return ""
    return "\n出场角色（含主角最多一格 2 人；双人格用中景或全景，不安排特写）：\n" + \
        "\n".join(lines)


def build_user_prompt(diary: Diary, character: Character, known_ids: list[str],
                      cfg: Config, hint: int,
                      feedback: list[str] | None = None) -> str:
    sheet = "；".join(f"{f.name}：{f.value}" for f in character.identity)
    sheet += f"。默认服装：{character.outfits.get('default', '')}"
    persona = character.persona
    persona_block = ""
    if persona.temperament or persona.signature:
        persona_block = f"\n角色性格：{persona.temperament or '未设定'}。"
        if persona.signature:
            persona_block += f"她的习惯动作：{'；'.join(persona.signature)}。"
        persona_block += "请按这个性格选情绪和强度，不要把她演成别的角色。\n"
    feedback_block = ""
    if feedback:
        feedback_block = ("\n\n这个角色过去被纠正过的例子（她的演法偏好）：\n"
                          + "\n".join(f"- {f}" for f in feedback))
    # 导演提示：<!-- vd: --> 写在对应条目后面
    body = "\n".join(
        f"## {e.time}{(' ' + e.label) if e.label else ''}\n{e.text}"
        + (f"\n（导演提示：{e.directive['raw']}）" if e.directive else "")
        for e in diary.entries)
    emotions, _ = load_emotions(cfg)
    emotion_list = "、".join(f"{name}（{p.desc}）" for name, p in emotions.items())
    return USER_TEMPLATE.format(
        protagonist_name=character.name,
        protagonist_id=character.id,
        min_panels=cfg.storyboard.min_panels,
        max_panels=cfg.storyboard.max_panels,
        hint=hint,
        known_ids="、".join(known_ids),
        character_sheet=sheet,
        persona_block=persona_block,
        feedback_block=feedback_block,
        date=diary.date,
        weather=diary.weather or "未知",
        title=diary.title or "",
        emotion_list=emotion_list,
        cast_block=_cast_block(cfg, character.id),
        diary_body=body,
    )
