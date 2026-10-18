"""画风预设 styles/presets.yaml 的读取。"""

from pathlib import Path

import yaml
from pydantic import BaseModel

from visdiary.config import Config


class StylePreset(BaseModel):
    name: str
    positive: str


def load_presets(cfg: Config) -> dict[str, StylePreset]:
    path = cfg.path("styles/presets.yaml")
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return {k: StylePreset(**v) for k, v in data.items()}


class ActingLevel(BaseModel):
    face: str
    body: str
    body_close: str | None = None  # 近景/特写专用：只写上半身和手部


class EmotionPreset(BaseModel):
    en: str
    desc: str
    ref_view: str | None = None   # null = 没有对应胸像，表情全靠 ACTING 文本
    shots: list[str]
    manpu: list[str] = []
    chibi_ok: bool = False
    levels: dict[int, ActingLevel]


def load_emotions(cfg: Config) -> tuple[dict[str, EmotionPreset], dict[str, str]]:
    """读 styles/emotions.yaml，返回 (情绪词表, 漫符英文映射)。"""
    path = cfg.path("styles/emotions.yaml")
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    manpu_en = data.get("manpu_en", {})
    emotions = {k: EmotionPreset(**v) for k, v in data.items() if k != "manpu_en"}
    return emotions, manpu_en
