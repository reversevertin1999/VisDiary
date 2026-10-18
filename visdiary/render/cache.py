"""生图缓存：键 = sha256(prompt + 每张参考图内容 + provider + model + aspect + seed)。"""

import hashlib
from pathlib import Path

from visdiary.config import Config


def cache_key(prompt: str, refs: list[Path], provider: str, model: str,
              aspect: str, seed: int | None, attempt: int = 0) -> str:
    """attempt 是质检重画次数：同一失败特征反复重画时 prompt 可能不变，
    靠 attempt 区分缓存键，否则重画会命中旧图（多数后端忽略 seed）。"""
    h = hashlib.sha256(prompt.encode("utf-8"))
    for r in refs:
        h.update(Path(r).read_bytes())
    h.update(f"{provider}|{model}|{aspect}|{seed}|{attempt}".encode("utf-8"))
    return h.hexdigest()


def _path(cfg: Config, key: str) -> Path:
    return cfg.path("cache/images") / f"{key}.png"


def get(cfg: Config, key: str) -> bytes | None:
    p = _path(cfg, key)
    return p.read_bytes() if p.is_file() else None


def put(cfg: Config, key: str, data: bytes) -> None:
    p = _path(cfg, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
