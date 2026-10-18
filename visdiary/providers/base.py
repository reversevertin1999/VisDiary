"""模型调用接口：所有 LLM / 多模态 / 生图服务都按这套 Protocol 封装。"""

from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class ProviderOutputError(ValueError):
    """provider 的输出不符合 schema。保存原文和 pydantic 错误，
    供 builder / 质检带原因重试（第三轮 P1-13：不再包成 RuntimeError 丢掉重试）。"""

    def __init__(self, schema_name: str, raw: str, errors: list):
        self.raw = raw
        self.errors = errors
        brief = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in errors[:3])
        super().__init__(f"{schema_name} 校验失败：{brief}")


class LLMProvider(Protocol):
    """大模型provider。尽量用服务自带的 JSON 模式 / 结构化输出；如果接入的服务不支持，
    把 schema.model_json_schema() 附进 prompt，拿到文本后自己解析并用 pydantic 校验。

    context 可传 {"notes": []} 收集解析时的归一化提示（如未知情绪归为平静）。"""

    name: str

    def chat_json(self, system: str, user: str, schema: type[T],
                  context: dict | None = None) -> T: ...


class VisionProvider(Protocol):
    name: str

    def analyze_json(self, prompt: str, images: list[Path], schema: type[T],
                     context: dict | None = None) -> T: ...


class ImageProvider(Protocol):
    """refs 是 (角色, 路径) 列表：角色说明由调用方给（如 "身份图 front_full"），
    provider 只做编号，不自己追加语义指令。"""

    name: str
    model: str

    def generate(self, prompt: str, refs: list[tuple[str, Path]], aspect: str,
                 seed: int | None = None, crop: bool = True,
                 kind: str = "panel") -> bytes: ...
