"""项目配置：加载 config.yaml 与 .env，解析项目根目录。"""

from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT = PACKAGE_DIR.parent  # visdiary/ 的上一级即项目根


class CodexOptions(BaseModel):
    cleanup: bool = True  # 生图读取成功后删除 ~/.codex/generated_images/ 里的源文件


class ProviderConfig(BaseModel):
    llm: str = "mock"
    vision: str = "mock"
    image: str = "mock"
    codex: CodexOptions = CodexOptions()


class ModelsConfig(BaseModel):
    llm: str = ""
    vision: str = ""
    image: str = ""


class StoryboardConfig(BaseModel):
    min_panels: int = 2
    max_panels: int = 12


class GenerationConfig(BaseModel):
    concurrency: int = 3
    api_retries: int = 3   # 已不叠加：重试只在 provider 内部（C3）
    seed: int | None = None
    panel_timeout_s: int = 240  # C3：单格生图+质检+重画的总预算，超时用占位图


class QCConfig(BaseModel):
    enabled: bool = True
    max_retries: int = 2
    expression_strict: bool = False  # true 时表情不到位也重画（更费额度）
    highlight_variants: int = 0      # P1-1 默认关；候选在单格导演里手动触发
    scene_only_fast: bool = False    # live 档：纯场景格只查文字跳过完整质检


class BudgetConfig(BaseModel):
    monthly_images: int = 300
    expected_retry_ratio: float = 0.3


class CostConfig(BaseModel):
    image: float = 0.0   # 每张生图成本（元），mock 为 0
    llm: float = 0.0     # 每次 LLM 调用成本（元）
    vision: float = 0.0  # 每次多模态质检调用成本（元）


class LayoutConfig(BaseModel):
    page_width: int = 1080
    margin: int = 32
    gutter: int = 16
    reading_direction: str = "ltr"
    font: str = "assets/fonts/LXGWWenKai-Regular.ttf"


class CastMember(BaseModel):
    id: str
    aliases: list[str] = []


class Config(BaseModel):
    cast: list[CastMember] = []   # M19：本次生成可用的角色清单
    provider: ProviderConfig = ProviderConfig()
    models: ModelsConfig = ModelsConfig()
    storyboard: StoryboardConfig = StoryboardConfig()
    generation: GenerationConfig = GenerationConfig()
    qc: QCConfig = QCConfig()
    budget: BudgetConfig = BudgetConfig()
    cost: CostConfig = CostConfig()
    layout: LayoutConfig = LayoutConfig()

    root: Path = DEFAULT_ROOT
    env: str = "prod"  # prod 熔断预算；dev 只记账（VISDIARY_ENV / --dev 切换）

    def path(self, rel: str | Path) -> Path:
        """把项目相对路径解析成绝对路径。"""
        p = Path(rel)
        return p if p.is_absolute() else self.root / p


def find_root(start: Path | None = None) -> Path:
    """从 start 向上找含 config.yaml 的目录；找不到就用包默认根。"""
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        if (d / "config.yaml").is_file():
            return d
    return DEFAULT_ROOT


def load_config(root: Path | None = None) -> Config:
    import os

    root = find_root(root)
    load_dotenv(root / ".env")
    cfg_file = root / "config.yaml"
    data = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) if cfg_file.is_file() else {}
    env = os.environ.get("VISDIARY_ENV", "prod")
    return Config(**(data or {}), root=root, env=env)
