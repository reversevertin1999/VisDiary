"""角色分析提示词与设定卡视图定义。

第三轮：12 张表情胸像扩成 16 张（补 determined/nervous/touched/speechless，
修正第二轮的错位参考图）；3 张全身视图照旧。
胸像按 2×3 网格分 3 张表情表生成（竖版，每格约 512px，比一张 3×4 清楚得多），
最后一张 2×2。投影法找格子间隙切图，不再硬性均分。
"""

from pydantic import BaseModel, Field

from visdiary.schemas import Feature

# 全身视图：view_id -> (画面描述, 比例)
FULL_VIEWS: dict[str, tuple[str, str]] = {
    "front_full": ("full body front view, neutral expression", "3:4"),
    "side_full": ("full body side view", "3:4"),
    "back_full": ("full body back view", "3:4"),
}

# 16 张表情胸像，按表情表分行（每张表内部行优先）
BUST_SHEETS: list[tuple[list[tuple[str, str]], int, int, str]] = [
    # (views, 列数, 行数, 比例)
    ([("bust_neutral", "neutral calm expression"),
      ("bust_smile", "gentle smiling expression"),
      ("bust_laugh", "laughing out loud expression"),
      ("bust_shy", "shy blushing expression"),
      ("bust_think", "thinking expression"),
      ("bust_sad", "sad downcast expression")], 2, 3, "2:3"),
    ([("bust_cry", "crying with tears expression"),
      ("bust_angry", "angry expression"),
      ("bust_surprised", "surprised expression"),
      ("bust_shock", "shocked devastated expression"),
      ("bust_tired", "exhausted tired expression"),
      ("bust_smug", "smug proud expression")], 2, 3, "2:3"),
    ([("bust_determined", "determined resolute expression"),
      ("bust_nervous", "nervous anxious expression"),
      ("bust_touched", "touched moved expression with misty eyes"),
      ("bust_speechless", "speechless deadpan expression")], 2, 2, "1:1"),
]

BUST_VIEWS: list[tuple[str, str]] = [v for views, _c, _r, _a in BUST_SHEETS for v in views]
BUST_DESC = dict(BUST_VIEWS)

# 兼容：旧代码引用的 VIEWS（8 视图）已拆分，统一入口用 FULL_VIEWS + BUST_VIEWS
VIEWS: dict[str, tuple[str, str]] = {
    **FULL_VIEWS,
    **{vid: (f"bust portrait, {desc}", "1:1") for vid, desc in BUST_VIEWS},
}


class CharacterAnalysis(BaseModel):
    identity: list[Feature] = Field(min_length=6, max_length=12)
    default_outfit: str
    style: str
    confidence: float = Field(ge=0, le=1)
    temperament: str = ""  # 一句话性格印象（人设草稿，用户可改）


ANALYSIS_PROMPT = """你是角色设计师。仔细观察这张二次元角色图，按 JSON schema 输出：
- identity：6–12 条外观特征，每条包含 name、value、critical。
  critical=true 只给"换掉就不像这个角色"的特征：发色、发型、瞳色、标志性配饰、痣或伤疤等。服装不放进 identity。
- default_outfit：图中服装的完整描述。
- style：从这些画风 id 中选最接近的一个：{style_ids}，并给出 0–1 的 confidence。
- temperament：从神态气质推断的一句话性格印象（如"眼神偏冷，像是冷静型"），只是草稿。
- 只描述能看到的内容，看不清的不要猜。"""
