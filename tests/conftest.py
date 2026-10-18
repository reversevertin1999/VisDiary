"""全局测试隔离（A2）：所有测试跑在临时项目副本里，不碰真实数据。

用法：conftest 里的 autouse fixture `_isolated_project`。
真实 diaries/、output/、usage/、characters/demo/feedback.jsonl 在整个测试
会话前后 mtime 与 sha256 保持不变（test_round6.py::test_no_real_data_touched 验证）。
"""

import shutil
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
# 需要复制到临时副本的最小项目内容
_COPY = [
    "config.yaml",
    "styles",
    "assets/fonts/LXGWWenKai-Regular.ttf",
    "characters/demo",
    "diaries/samples",
]


def _make_isolated_root(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    for rel in _COPY:
        src = PROJECT / rel
        dst = root / rel
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    for d in ("output", "cache", "usage", "diaries"):
        (root / d).mkdir(exist_ok=True)
    # 隔离副本不带走历史 feedback（测试数据干净起点）
    (root / "characters/demo/feedback.jsonl").unlink(missing_ok=True)
    return root


@pytest.fixture(autouse=True)
def _isolated_project(tmp_path, monkeypatch):
    """每个测试都用临时项目根：load_config 的 root 发现与 budget 落盘都被重定向。"""
    import os

    root = _make_isolated_root(tmp_path)
    os.environ["VISDIARY_ENV"] = "test"  # budget 池白名单里没有 test → 不落 prod/dev
    monkeypatch.setattr("visdiary.config.find_root", lambda start=None: root)
    # budget 对未知池会报错：测试里统一落到临时 usage（池名校验放开为接受任意）
    import visdiary.budget as B
    monkeypatch.setattr(B, "POOLS", ("prod", "dev", "test"))
    # record 直接照写临时目录（cfg.root 已指向 tmp）
    yield root
    os.environ.pop("VISDIARY_ENV", None)


@pytest.fixture()
def project_tmp(_isolated_project):
    """兼容旧测试：返回临时项目下的工作目录（output/.pytest-tmp）。"""
    out = _isolated_project / "output" / ".pytest-tmp"
    out.mkdir(parents=True, exist_ok=True)
    return out


def pytest_configure(config):
    """collection 兜底：模块级 load_config 在任何 fixture 之前执行，
    这里先 patch 一个会话级隔离根，_isolated_project 再按测试细切。"""
    import shutil
    import visdiary.config as C
    root = Path(config.option.basetemp or "/tmp/visdiary-collect") / "collect-root"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    for rel in _COPY:
        src = PROJECT / rel
        dst = root / rel
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    for d in ("output", "cache", "usage", "diaries"):
        (root / d).mkdir(exist_ok=True)
    (root / "characters/demo/feedback.jsonl").unlink(missing_ok=True)
    C.find_root = (lambda start=None, _r=root: _r)
