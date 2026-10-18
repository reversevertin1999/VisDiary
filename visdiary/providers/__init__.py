"""按 config.yaml 选择 provider。默认 mock；真实服务在 codex（本机 CLI 通道）。"""

from visdiary.config import Config
from visdiary.providers.base import ImageProvider, LLMProvider, VisionProvider
from visdiary.providers.mock import MockImage, MockLLM, MockVision


def _unsupported(kind: str, name: str) -> ValueError:
    return ValueError(
        f"{kind} provider {name!r} 尚未实现。可选：mock（离线）/ codex（本机 CLI）。"
        f"新增服务时在 visdiary/providers/ 加一个文件并在这里注册。"
    )


def get_llm(cfg: Config) -> LLMProvider:
    if cfg.provider.llm == "mock":
        return MockLLM()
    if cfg.provider.llm == "codex":
        from visdiary.providers.codex import CodexLLM
        return CodexLLM(cfg.models.llm)
    raise _unsupported("llm", cfg.provider.llm)


def get_vision(cfg: Config) -> VisionProvider:
    if cfg.provider.vision == "mock":
        return MockVision()
    if cfg.provider.vision == "codex":
        from visdiary.providers.codex import CodexVision
        return CodexVision(cfg.models.vision)
    raise _unsupported("vision", cfg.provider.vision)


def get_image(cfg: Config) -> ImageProvider:
    if cfg.provider.image == "mock":
        return MockImage()
    if cfg.provider.image == "codex":
        from visdiary.providers.codex import CodexImage
        return CodexImage(cfg.models.image, cleanup=cfg.provider.codex.cleanup)
    raise _unsupported("image", cfg.provider.image)
