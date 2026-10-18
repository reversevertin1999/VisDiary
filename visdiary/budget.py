"""月度用量记账与预算控制（A10：prod/dev 两个池子）。

- prod：日常 render / redraw / character，超 budget.monthly_images 熔断
- dev：bench / calibrate / 开发调试，不设上限，只记账
- 切换：环境变量 VISDIARY_ENV=dev，或命令行 --dev；bench 脚本强制 dev
- 用量文件 usage/<YYYY-MM>.json：{"prod": {...}, "dev": {...}}（旧扁平格式自动迁移为 prod）
"""

import json
import os
import threading
from datetime import date
from pathlib import Path

from visdiary.config import Config

_LOCK = threading.Lock()  # 生图是并发的，记账不能读写打架

POOLS = ("prod", "dev")


def _path(cfg: Config, month: str | None = None) -> Path:
    month = month or date.today().strftime("%Y-%m")
    return Path(cfg.path("usage")) / f"{month}.json"


def _load(cfg: Config) -> dict:
    p = _path(cfg)
    if p.is_file():
        data = json.loads(p.read_text(encoding="utf-8"))
        if "prod" not in data and "dev" not in data:  # 旧扁平格式 → 全算 prod
            data = {"prod": data}
        data.setdefault("prod", _blank())
        data.setdefault("dev", _blank())
        for pool in {p_ for p_ in data if isinstance(data.get(p_), dict)} | set(POOLS):
            data.setdefault(pool, _blank())
            for k in ("images", "llm_calls", "vision_calls"):
                data[pool].setdefault(k, 0)
        return data
    return {pool: _blank() for pool in POOLS}


def _blank() -> dict:
    return {"images": 0, "llm_calls": 0, "vision_calls": 0}


def _pool(cfg: Config, pool: str | None) -> str:
    key = pool or cfg.env
    if key not in POOLS:
        raise ValueError(f"未知预算池 {key!r}，可选：{POOLS}")
    return key


def usage(cfg: Config, pool: str | None = None) -> dict:
    return dict(_load(cfg)[_pool(cfg, pool)])


def record(cfg: Config, images: int = 0, llm_calls: int = 0, vision_calls: int = 0,
           pool: str | None = None) -> dict:
    key = _pool(cfg, pool)
    with _LOCK:
        data = _load(cfg)
        data[key]["images"] += images
        data[key]["llm_calls"] += llm_calls
        data[key]["vision_calls"] += vision_calls
        p = _path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        # P1-10：原子写，写一半崩溃不会损坏用量文件
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        return dict(data[key])


def remaining_images(cfg: Config) -> int:
    return cfg.budget.monthly_images - usage(cfg, "prod")["images"]


class BudgetError(Exception):
    pass


def clamp_panel_count(cfg: Config, n_panels: int) -> tuple[int, str | None]:
    """按预算限制本次格数（只管 prod 池；dev 池不熔断）。"""
    if cfg.env == "dev":
        return n_panels, None
    per = 1 + cfg.budget.expected_retry_ratio
    affordable = int(remaining_images(cfg) / per)
    if affordable < 2:
        raise BudgetError(
            f"本月生图预算只剩 {remaining_images(cfg)} 张，连 2 格都不够，已停止。"
            f"下月再说，或调高 config.yaml 的 budget.monthly_images，"
            f"或用 VISDIARY_ENV=dev 走开发池。")
    if n_panels > affordable:
        return affordable, (f"预计生图 {int(n_panels * per)} 张会超出本月预算，"
                            f"格数已从 {n_panels} 下调到 {affordable}")
    return n_panels, None


def report(cfg: Config) -> dict:
    data = _load(cfg)
    out = {"month": date.today().strftime("%Y-%m"),
           "prod": data["prod"], "dev": data["dev"],
           "prod_remaining": remaining_images(cfg)}
    for extra in ("test",):  # 测试池（conftest 用），有数据就带上
        if data.get(extra, _blank()) != _blank():
            out[extra] = data[extra]
    return out
