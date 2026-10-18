"""拆分镜主流程：格数建议 → 拼提示词 → LLM → 代码规则校验。

重试策略：
- 输出不合规（ProviderOutputError / ValidationError）：带原因重试 1 次，
  每次真实调用都记账（包括输出不合规的）
- 硬性规则失败（格数不足）：同样重试 1 次，仍失败报错
- 情绪节奏不达标（EmotionRuleError）：重试 1 次，仍不达标降级为 warning 放行，
  且不丢掉第一次已经修好的部分
"""

from pydantic import ValidationError

from visdiary import budget
from visdiary.config import Config
from visdiary.diary.parser import Diary
from visdiary.providers.base import LLMProvider, ProviderOutputError
from visdiary.schemas import Character, Storyboard
from visdiary.storyboard import prompts, rules
from visdiary.styles import load_emotions


def suggest_panel_count(diary: Diary, cfg: Config) -> int:
    """按正文字数连续分段给 LLM 的参考格数，保证每条随笔至少分到一格，
    最后夹到配置的上下限里。"""
    c = diary.char_count
    if c < 150:
        hint = 2 + c // 60                     # 2–4
    elif c <= 600:
        hint = 5 + (c - 150) * 3 // 450        # 5–8
    else:
        hint = 8 + (c - 600) // 150            # 8 起，每多 150 字加 1 格
    hint = max(hint, len(diary.entries))
    return max(cfg.storyboard.min_panels, min(hint, cfg.storyboard.max_panels))


def build_storyboard(diary: Diary, character: Character, known_ids: list[str],
                     cfg: Config, llm: LLMProvider) -> tuple[Storyboard, list[str]]:
    hint = suggest_panel_count(diary, cfg)
    from visdiary.character.feedback import find_similar
    feedback = find_similar(cfg, character.id,
                            text="".join(e.text for e in diary.entries))
    user = prompts.build_user_prompt(diary, character, known_ids, cfg, hint,
                                     feedback=feedback)
    sources = {e.time for e in diary.entries}
    directives = {e.time: e.directive for e in diary.entries if e.directive}
    emotions = load_emotions(cfg)[0]

    last_error = ""
    soft_sb = None
    soft_notes: list[str] = []
    for _ in range(2):  # 校验失败时把错误附在原请求后重试 1 次
        if last_error:
            user += f"\n\n上次输出未通过校验：{last_error}，请修正后重新输出。"
        notes: list[str] = []
        try:
            sb = llm.chat_json(prompts.SYSTEM_PROMPT, user, Storyboard,
                               context={"notes": notes})
        except (ProviderOutputError, ValidationError) as e:
            budget.record(cfg, llm_calls=1)  # 调用已发生，只是输出不合规
            last_error = str(e)
            continue
        budget.record(cfg, llm_calls=1)
        try:
            sb, warns = rules.validate(sb, cfg.storyboard, known_ids, sources,
                                       persona=character.persona,
                                       directives=directives, emotions=emotions)
            return sb, notes + warns
        except rules.EmotionRuleError as e:  # 情绪节奏：允许降级放行
            last_error = str(e)
            soft_sb = sb          # 保留这次已修好的结果，硬错误也不丢
            soft_notes = notes
        except rules.StoryboardError as e:  # 硬性规则：重试后仍失败就报错
            last_error = str(e)
    if soft_sb is not None:
        sb, warns = rules.validate(soft_sb, cfg.storyboard, known_ids, sources,
                                   persona=character.persona, directives=directives,
                                   emotions=emotions, soft=True)
        return sb, soft_notes + warns
    raise rules.StoryboardError(f"重试后仍未通过校验：{last_error}")
