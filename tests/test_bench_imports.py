"""A9：bench 脚本 import 冒烟，防止悄悄失效。"""

import importlib.util
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parent.parent / "bench"


@pytest.mark.parametrize("name", sorted(p.stem for p in BENCH.glob("*.py")))
def test_bench_script_importable(name):
    spec = importlib.util.spec_from_file_location(f"bench_{name}", BENCH / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # 只 import（模块级代码），不执行 main
