"""角色档案的读写：characters/<id>/character.json。"""

import json
from pathlib import Path

from visdiary.config import Config
from visdiary.schemas import Character


def characters_dir(cfg: Config) -> Path:
    return cfg.path("characters")


def load_character(cfg: Config, character_id: str) -> Character:
    path = characters_dir(cfg) / character_id / "character.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"角色 {character_id!r} 不存在（{path}）。"
            f"先用 visdiary character add 建档。"
        )
    return Character(**json.loads(path.read_text(encoding="utf-8")))


def save_character(cfg: Config, character: Character) -> Path:
    path = characters_dir(cfg) / character.id / "character.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(character.model_dump_json(indent=2), encoding="utf-8")
    return path


def list_character_ids(cfg: Config) -> list[str]:
    root = characters_dir(cfg)
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir()
                  if d.is_dir() and (d / "character.json").is_file())
