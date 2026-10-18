from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

Shot = Literal["远景", "全景", "中景", "近景", "特写", "俯视", "仰视", "背影"]
SHOTS: list[str] = list(Shot.__args__)

Emotion = Literal[
    "平静", "开心", "大笑", "感动", "得意", "期待",
    "害羞", "思考", "无语", "紧张", "委屈", "难过",
    "哭", "大哭", "生气", "暴怒", "惊讶", "震惊",
    "疲惫", "崩溃", "坚定",
]
EMOTIONS: list[str] = list(Emotion.__args__)

Manpu = Literal[  # 漫符（manga symbols）
    "汗滴", "青筋", "腮红线", "闪光", "阴影线", "泪花",
    "集中线", "花背景", "闪电背景", "阴云", "灵魂出窍", "怒气",
]
MANPUS: list[str] = list(Manpu.__args__)

# 大模型常写出的近义说法，解析时归一化到标准值，避免 Literal 校验直接失败
_SHOT_ALIASES = {
    "大远景": "远景", "全身": "全景", "半身": "中景",
    "中近景": "近景", "大特写": "特写", "背面": "背影",
}
_EMOTION_ALIASES = {
    "冷静": "平静", "淡定": "平静", "放松": "平静",
    "微笑": "开心", "高兴": "开心", "快乐": "开心", "愉快": "开心",
    "狂喜": "大笑", "爆笑": "大笑",
    "欣慰": "感动", "感激": "感动", "动容": "感动",
    "自豪": "得意", "得瑟": "得意", "骄傲": "得意",
    "憧憬": "期待", "盼望": "期待", "兴奋": "期待",
    "脸红": "害羞", "羞涩": "害羞", "腼腆": "害羞",
    "沉思": "思考", "琢磨": "思考", "犹豫": "思考",
    "无奈": "无语", "汗颜": "无语", "哭笑不得": "无语",
    "慌张": "紧张", "焦虑": "紧张", "不安": "紧张", "忐忑": "紧张",
    "郁闷": "委屈", "憋屈": "委屈", "冤枉": "委屈",
    "沮丧": "难过", "失落": "难过", "伤心": "难过", "emo": "难过", "消沉": "难过",
    "哭泣": "哭", "流泪": "哭", "啜泣": "哭",
    "嚎啕大哭": "大哭", "痛哭": "大哭",
    "愤怒": "生气", "恼火": "生气", "不爽": "生气",
    "气炸": "暴怒", "震怒": "暴怒", "火大": "暴怒",
    "吃惊": "惊讶", "惊诧": "惊讶",
    "惊呆": "震惊", "震撼": "震惊", "傻眼": "震惊",
    "劳累": "疲惫", "累": "疲惫", "疲倦": "疲惫", "精疲力尽": "疲惫",
    "破防": "崩溃", "绝望": "崩溃", "心态崩了": "崩溃",
    "坚决": "坚定", "决然": "坚定", "笃定": "坚定", "认真": "坚定",
}


class Feature(BaseModel):
    name: str                 # 例：发色
    value: str                # 例：银白色长发
    critical: bool = True     # True = 质检必查


class EmotionOverride(BaseModel):
    """角色专属演法：她生气是鼓腮，不是吼。"""
    face: dict[int, str] = {}       # 按强度覆盖五官描述
    body: dict[int, str] = {}
    ref_view: str | None = None     # 这个角色专属的表情参考视图


class Persona(BaseModel):
    """人设：让角色"演得像她自己"。全部为空时行为与没有人设完全一致。"""
    temperament: str = ""                    # 一句话性格，如 "傲娇，嘴硬心软"
    expressiveness: float = 1.0              # 表演幅度 0.5–1.5
    max_intensity: dict[str, int] = {}       # 某些情绪的强度上限
    banned: list[str] = []                   # 禁用：情绪名 / 漫符 / "chibi"
    signature: list[str] = []                # 习惯动作（英文）
    overrides: dict[str, EmotionOverride] = {}


class StyleSheet(BaseModel):
    style_id: str
    views: dict[str, str]     # view_id -> 相对路径，如 "front_full": "sheets/anime_cel/front_full.png"
    approved: bool = False
    # 表情表盲校验结果：view_id -> {"expected": ..., "detected": ...}，不一致要人工确认
    cell_check: dict[str, dict] = {}


class Character(BaseModel):
    id: str
    name: str
    aliases: list[str] = []   # M19：日记里的称呼（如"室友"）映射到这个角色
    source_image: str
    identity: list[Feature]
    outfits: dict[str, str] = {"default": ""}
    style_detected: str
    style_confidence: float
    sheets: dict[str, StyleSheet] = {}
    persona: Persona = Persona()


class Dialogue(BaseModel):
    speaker: str
    # 长度故意不在 schema 层限制，超长由 storyboard/rules.py 截断并记 warning
    text: str


class Acting(BaseModel):
    """情绪对象：先想内心台词，再定表情。强度 1 轻微 / 2 明显 / 3 夸张。"""
    emotion: Emotion
    intensity: int = Field(2, ge=1, le=3)
    inner_voice: str = ""       # 主角此刻的内心台词（只帮 LLM 想清楚，不上图）
    face_en: str = ""           # 可选：LLM 自己补充的五官细节
    body_en: str = ""           # 肢体动作，如 "slumped over the desk, arms dangling"
    manpu: list[Manpu] = []     # 漫符，最多 2 个
    chibi: bool = False         # Q 版（SD），只用于搞笑格

    @field_validator("emotion", mode="before")
    @classmethod
    def _norm_emotion(cls, v, info: ValidationInfo):
        v = str(v).strip()
        if v in EMOTIONS:
            return v
        if v in _EMOTION_ALIASES:
            return _EMOTION_ALIASES[v]
        if info.context and isinstance(info.context.get("notes"), list):
            info.context["notes"].append(f"未知情绪 {v!r} 已归为平静")
        return "平静"

    @field_validator("intensity", mode="before")
    @classmethod
    def _norm_intensity(cls, v):
        try:
            return max(1, min(3, int(v)))
        except (TypeError, ValueError):
            return 2

    @field_validator("manpu", mode="before")
    @classmethod
    def _norm_manpu(cls, v):
        if not isinstance(v, list):
            return []
        return [m for m in v if m in MANPUS][:2]


class Panel(BaseModel):
    id: int
    weight: Literal["normal", "highlight"] = "normal"
    shot: Shot
    scene: str
    characters: list[str]                  # 只能是已建档的角色 id
    acting: Acting
    outfit: str = "default"
    action: str
    metaphor: str | None = None            # 心情的视觉比喻，仅用于记录
    visual_prompt_en: str                  # 给生图模型的英文画面描述
    narration: str | None = None           # 超长由 rules 截断
    dialogue: list[Dialogue] = []          # 超过 2 条由 rules 截断
    source: str                            # 对应日记的哪一条，如 "09:30"

    @model_validator(mode="before")
    @classmethod
    def _compat_expression(cls, data):
        """兼容旧 storyboard.json：只有 expression 字段时自动转成 acting。"""
        if isinstance(data, dict) and "acting" not in data and "expression" in data:
            data = dict(data)
            data["acting"] = {"emotion": data.pop("expression"), "intensity": 2}
        return data

    @field_validator("shot", mode="before")
    @classmethod
    def _norm_shot(cls, v):
        return _SHOT_ALIASES.get(str(v).strip(), str(v).strip())


class Extra(BaseModel):
    """未建档配角：人名 + 固定的一句英文外貌描述。
    用列表而不是 dict：strict JSON Schema 里 dict 会被清空（第三轮 P1-14）。"""
    name: str
    desc_en: str


# 近义情绪组：同一组内不算类别不一致；一个情绪可以属于多个组
EMOTION_GROUPS = [
    {"哭", "大哭", "难过", "委屈", "感动"},
    {"生气", "暴怒"},
    {"惊讶", "震惊"},
    {"开心", "大笑", "得意", "期待", "感动"},
    {"平静", "思考", "无语"},
    {"疲惫", "崩溃"},
    {"害羞", "紧张"},
    {"坚定", "得意"},
]


def normalize_emotion(v: str) -> str:
    v = str(v).strip()
    return v if v in EMOTIONS else _EMOTION_ALIASES.get(v, v)


class Storyboard(BaseModel):
    date: str
    title: str
    mood: str
    panels: list[Panel]
    extras: list[Extra] = []

    @model_validator(mode="before")
    @classmethod
    def _compat_extras_dict(cls, data):
        """兼容旧 storyboard.json：extras 是 {人名: 描述} 字典时转成列表。"""
        if isinstance(data, dict) and isinstance(data.get("extras"), dict):
            data = dict(data)
            data["extras"] = [{"name": k, "desc_en": v} for k, v in data["extras"].items()]
        return data

    def extras_map(self) -> dict[str, str]:
        return {e.name: e.desc_en for e in self.extras}


class QCCheck(BaseModel):
    name: str = ""
    result: Literal["pass", "fail", "not_visible"]
    feature_id: int = 0   # 对应质检 prompt 里的特征编号；0 = 没给编号
    character_id: str = ""  # M19：多角色时标注属于哪个角色；空 = 主角/单人
    note: str = ""


class ExpressionCheck(BaseModel):
    """盲测：不告诉模型目标情绪，让它从词表里选 top-2，由代码比对。"""
    detected_emotion: Emotion = "平静"   # 最像的情绪（strict schema 直接约束词表）
    detected_top2: list[Emotion] = []    # top-2 候选
    detected_intensity: int = Field(2, ge=1, le=3)
    face_size: Literal["large", "medium", "small", "not_visible"] = "medium"
    note: str = ""


class QCResult(BaseModel):
    images_received: int = 0        # 模型实际收到了几张图（核对有没有被静默丢图）
    judgeable: bool = True          # 图缺失 / 损坏 / 看不清 → false，结论不计入统计
    unjudgeable_reason: str = ""
    character_visible: bool
    has_text: bool
    checks: list[QCCheck]
    expression: ExpressionCheck | None = None
    cross_contamination: bool | None = None  # M19：A 的特征是否出现在 B 身上
