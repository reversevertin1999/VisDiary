# VisDiary 第一轮开发指导

## 0. 目标与完成标准

第一轮只做一条主链路：**一篇 Markdown 日记 → 分镜 → 版面规划 → 逐格生图（带角色参考图）→ 质检 → 排版导出一页漫画**。

完成标准：

- [ ] `visdiary render diaries/samples/medium.md --character demo --provider mock` 离线跑通，10 秒内出图，不产生任何 API 费用
- [ ] 换成真实 provider 后，同一篇日记生成约 8 格，`page.png` 中文清晰、没有错字
- [ ] 3 个测试角色的设定卡肉眼看是同一个人；正式生成的质检通过率有记录
- [ ] 每格都有 `meta.json`（prompt、参考图哈希、模型、质检结果）
- [ ] `pytest` 全部通过；README 的目录树与实际代码一致

## 1. 技术栈

| 用途 | 选择 | 说明 |
|---|---|---|
| 语言 | Python 3.11+ | |
| 数据结构 | pydantic v2 | 所有 LLM 输出都必须经过 schema 校验 |
| 命令行 | typer | |
| 日记解析 | python-frontmatter | |
| 配置 | pyyaml + python-dotenv | Key 只放 `.env` |
| HTTP | httpx | 调用各家 API |
| 排版 | Jinja2 + HTML/CSS | |
| 导出 | Playwright（Chromium） | 整页截图成 PNG |
| 图像 | Pillow | mock 占位图、缩略图 |
| 测试 | pytest | |

依赖版本以安装时为准，跑通后用 `pip freeze` 锁定。

## 2. 开发顺序：先 mock，后花钱

| 顺序 | 里程碑 | 预计工时 | 是否花 API 费用 |
|---|---|---|---|
| 1 | M0 脚手架 + mock provider | 2h | 否 |
| 2 | M2 日记解析 + 拆分镜 | 3h | 少量（只调 LLM） |
| 3 | M5 版面规划 + 排版导出 | 4h | 否（用 mock 图） |
| 4 | M1 角色建档 + 定妆 | 4h | 是 |
| 5 | M3 逐格生图 | 3h | 是 |
| 6 | M4 质检 | 3h | 是 |
| 7 | M6 模型基准测试 | 2h | 是 |

为什么是这个顺序：排版和规则校验是最容易反复修改的部分，用 mock 图开发就不会白花生图费用。真实模型最后接入，那时候整条链路已经稳定，测出来的数据才有参考价值。

> 和方案页相比有一处调整：**版面规划放在生图之前**。每格的比例（1:1 或 16:9）由版面决定，生图时直接按这个比例出图，避免事后裁切把角色切掉。

## 3. M0 脚手架

### 3.1 目录与配置

按 README 的目录结构建好文件夹，加上：

`.gitignore`

```gitignore
.env
.venv/
output/
cache/
usage/
characters/*
!characters/demo/
__pycache__/
```

`.env.example`

```bash
LLM_API_KEY=
VISION_API_KEY=
IMAGE_API_KEY=
```

`config.yaml`

```yaml
provider:
  llm: mock
  vision: mock
  image: mock
models:
  llm: ""
  vision: ""
  image: ""
storyboard:
  min_panels: 2
  max_panels: 12
generation:
  concurrency: 3
  api_retries: 3
  seed: null
qc:
  enabled: true
  max_retries: 2
budget:
  monthly_images: 300
  expected_retry_ratio: 0.3
layout:
  page_width: 1080
  margin: 32
  gutter: 16
  reading_direction: ltr
  font: assets/fonts/LXGWWenKai-Regular.ttf
```

字体推荐用霞鹜文楷（LXGW WenKai，SIL OFL 协议），手写感适合日记。字体文件和它的 LICENSE 一起放进 `assets/fonts/`。

### 3.2 数据结构 `visdiary/schemas.py`

这是整个项目的契约，先写好再写其他模块。

```python
from typing import Literal
from pydantic import BaseModel, Field

Shot = Literal["远景", "全景", "中景", "近景", "特写", "俯视", "仰视", "背影"]
Expression = Literal["平静", "开心", "大笑", "难过", "哭", "生气", "惊讶", "疲惫", "害羞", "思考"]


class Feature(BaseModel):
    name: str                 # 例：发色
    value: str                # 例：银白色长发
    critical: bool = True     # True = 质检必查


class StyleSheet(BaseModel):
    style_id: str
    views: dict[str, str]     # view_id -> 相对路径，如 "front_full": "sheets/anime/front_full.png"
    approved: bool = False


class Character(BaseModel):
    id: str
    name: str
    source_image: str
    identity: list[Feature]
    outfits: dict[str, str] = {"default": ""}
    style_detected: str
    style_confidence: float
    sheets: dict[str, StyleSheet] = {}


class Dialogue(BaseModel):
    speaker: str
    text: str = Field(max_length=40)


class Panel(BaseModel):
    id: int
    weight: Literal["normal", "highlight"] = "normal"
    shot: Shot
    scene: str
    characters: list[str]                  # 只能是已建档的角色 id
    expression: Expression
    outfit: str = "default"
    action: str
    metaphor: str | None = None            # 心情的视觉比喻，仅用于记录
    visual_prompt_en: str                  # 给生图模型的英文画面描述
    narration: str | None = Field(None, max_length=40)
    dialogue: list[Dialogue] = Field(default_factory=list, max_length=2)
    source: str                            # 对应日记的哪一条，如 "09:30"


class Storyboard(BaseModel):
    date: str
    title: str
    mood: str
    panels: list[Panel]


class QCCheck(BaseModel):
    name: str
    result: Literal["pass", "fail", "not_visible"]
    note: str = ""


class QCResult(BaseModel):
    character_visible: bool
    has_text: bool
    checks: list[QCCheck]
```

### 3.3 模型调用接口 `visdiary/providers/base.py`

```python
from pathlib import Path
from typing import Protocol, TypeVar
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMProvider(Protocol):
    name: str
    def chat_json(self, system: str, user: str, schema: type[T]) -> T: ...


class VisionProvider(Protocol):
    name: str
    def analyze_json(self, prompt: str, images: list[Path], schema: type[T]) -> T: ...


class ImageProvider(Protocol):
    name: str
    model: str
    def generate(self, prompt: str, refs: list[Path], aspect: str,
                 seed: int | None = None) -> bytes: ...
```

- 每家服务一个文件，例如 `providers/<服务名>.py`，在 `providers/__init__.py` 里按 `config.yaml` 选择。
- 尽量使用服务自带的 JSON 模式或结构化输出；拿到结果后**一律用 pydantic 校验**。
- 校验失败时，把错误信息附在原请求后面**重试 1 次**，仍失败就报错退出，不要无限重试。

### 3.4 mock provider

- `MockLLM`：根据日记条数返回固定格式的分镜，格数按 3.5 节的长度规则计算。
- `MockVision`：返回一份示例角色档案；质检时用固定种子按约 80% 的概率判定通过，**专门用来测试重画流程**。
- `MockImage`：用 Pillow 画占位图，背景色按表情区分，图上写格号、镜头和动作，尺寸按 `aspect` 生成。

**验收**：`visdiary render ... --provider mock` 能输出一页带占位图的漫画。

## 4. M2 日记解析与拆分镜

### 4.1 解析 `diary/parser.py`

- 用 python-frontmatter 读取 `date`、`weather` 等字段；缺少 `date` 时用文件名里的日期。
- 正文按 `## HH:MM` 标题切成多条随笔；没有时间标题时，全文作为一条。
- 统计正文字数，作为格数建议的依据。

### 4.2 格数建议

| 正文字数 | 建议格数 |
|---|---|
| < 150 | 2–4 |
| 150–600 | 5–8 |
| > 600 | 8–12 |

这只是传给 LLM 的**参考值**，最终由 LLM 根据情节判断，再由代码按 `min_panels` / `max_panels` 和预算截断。

### 4.3 拆分镜系统提示词 `storyboard/prompts.py`

```text
你是漫画分镜师。把用户一天的日记改编成竖向条漫分镜。主角是 {protagonist_name}（角色 id：{protagonist_id}）。

规则：
1. 一个有意义的情节或情绪转折画一格；流水账要合并，不要逐句画。
2. 格数范围 {min_panels}–{max_panels}，本篇建议约 {hint} 格。
3. 恰好一格 weight=highlight，选当天最重要或情绪最强的时刻。
4. 相邻两格的 shot 不能相同。
5. 压力、开心、疲惫等抽象感受可以用视觉比喻表达，写进 metaphor，并体现在 visual_prompt_en 里。
6. 旁白和对白用第一人称、口语化，每条不超过 28 个字；每格最多 2 条对白。
7. 只使用日记里出现的事件，不要编造；可以补充合理的场景细节（光线、天气、环境）。
8. characters 只能使用已知角色 id：{known_ids}。日记里提到的其他人写进 scene 和 visual_prompt_en，不放进 characters。
9. visual_prompt_en 用英文描述画面（动作、表情、场景、构图），不要描述画风，不要要求画面出现任何文字。
10. source 填写这一格对应的随笔时间标题；没有时间标题时填 "all"。
11. 只按 JSON schema 输出，不要输出其他内容。
```

### 4.4 代码层规则校验 `storyboard/rules.py`

不要完全相信 LLM 的输出，代码里再检查一遍：

| 检查项 | 不满足时的处理 |
|---|---|
| 格数在范围内 | 超出上限：保留高光格，再按顺序截断；低于下限：报错重试 |
| 恰好 1 个高光格 | 没有：把对白最多的一格设为高光；多个：只保留第一个 |
| 相邻镜头不重复 | 自动把后一格换成相邻镜头中没用过的一种，并记录 warning |
| 角色 id 都已建档 | 删除未知 id，并记录 warning |
| 对白、旁白长度 | 超长截断，并记录 warning |
| `id` 连续 | 重新编号 |

所有 warning 写进 `storyboard.json` 旁边的 `storyboard.warnings.json`。

**验收**：准备 `diaries/samples/short.md`（约 100 字）、`medium.md`（约 400 字）、`long.md`（约 900 字），分别得到约 3、8、12 格；`tests/test_rules.py` 覆盖上表每一条规则。

## 5. M5 版面规划与排版导出

### 5.1 版面规划 `layout/planner.py`

竖向长页，宽 1080px，按阅读顺序排成行：

```python
def plan_rows(panels):
    rows, buf = [], []
    for p in panels:
        if p.weight == "highlight":
            if buf:
                rows.append(buf); buf = []
            rows.append([p])          # 高光格独占一行
        else:
            buf.append(p)
            if len(buf) == 2:
                rows.append(buf); buf = []
    if buf:
        rows.append(buf)              # 剩下的单格也独占一行
    return rows
```

- 单格行：比例 16:9，宽 = 页宽 − 2 × 边距。
- 双格行：每格 1:1，宽 = (内容宽 − 间距) / 2。
- 输出 `layout.json`：每格的 `x, y, w, h, aspect`。生图时按 `aspect` 出图。
- 生图服务支持的比例不同，在 provider 里把 `aspect` 映射到最接近的可用尺寸。

### 5.2 排版 `layout/templates/page.html.j2`

- 页头：日期、天气、心情、当天标题。
- 每格：`<img>` 用 `object-fit: cover` 填满格子，外加黑色描边。
- 旁白：放在格子左上角的矩形框里。
- 对白：圆角气泡，第一条放右上，第二条放左下；说话人不是主角时，在气泡里加名字前缀。
- 字体用 `@font-face` 引用 `assets/fonts/` 里的**相对路径**，不依赖系统字体。

### 5.3 导出 `layout/export.py`

- Playwright 打开本地 `page.html`，视口宽 1080，`device_scale_factor=2`，`full_page=True` 截图成 `page.png`。
- 所有路径用 `pathlib` 处理，文件统一用 UTF-8 读写，**不要写死 `/tmp`**（PixelMorph 在 Windows 上踩过这个坑）。

**验收**：用 mock 图跑短、中、长三篇日记，页面不重叠、不溢出，中文正常显示，高光格独占一行。

## 6. M1 角色建档与定妆

### 6.1 角色分析提示词

```text
你是角色设计师。仔细观察这张二次元角色图，按 JSON schema 输出：
- identity：6–12 条外观特征，每条包含 name、value、critical。
  critical=true 只给"换掉就不像这个角色"的特征：发色、发型、瞳色、标志性配饰、痣或伤疤等。服装不放进 identity。
- default_outfit：图中服装的完整描述。
- style：从这些画风 id 中选最接近的一个：{style_ids}，并给出 0–1 的 confidence。
- 只描述能看到的内容，看不清的不要猜。
```

`confidence < 0.6` 时，命令行列出候选画风，让用户手动选。

### 6.2 画风预设 `styles/presets.yaml`

```yaml
anime_cel:
  name: 日漫赛璐璐
  positive: "anime style, cel shading, clean lineart, flat colors, vibrant"
chibi:
  name: Q 版
  positive: "chibi style, super deformed, big head small body, cute, simple shading"
painterly:
  name: 厚涂
  positive: "painterly anime illustration, soft brush strokes, rich lighting"
watercolor:
  name: 水彩绘本
  positive: "watercolor picture book illustration, soft edges, paper texture, pastel colors"
american:
  name: 美漫
  positive: "american comic style, bold ink lines, halftone shading"
pixel:
  name: 像素风
  positive: "pixel art, 16-bit, limited palette"
```

### 6.3 设定卡

每种画风生成 8 张，参考图用用户上传的原图：

| view_id | 内容 | 比例 |
|---|---|---|
| `front_full` | 全身正面，中性表情 | 3:4 |
| `side_full` | 全身侧面 | 3:4 |
| `back_full` | 全身背面 | 3:4 |
| `bust_smile` | 胸像，笑 | 1:1 |
| `bust_cry` | 胸像，哭 | 1:1 |
| `bust_angry` | 胸像，生气 | 1:1 |
| `bust_surprised` | 胸像，惊讶 | 1:1 |
| `bust_tired` | 胸像，疲惫 | 1:1 |

设定卡的 prompt 模板：

```text
{style.positive}. character reference sheet, single character, plain white background,
{view description}. {name}: {identity values}. wearing {default_outfit}.
no text, no labels, no watermark. keep the face, hair, eye color and accessories identical to the reference image.
```

- 存放路径：`characters/<id>/sheets/<style_id>/<view_id>.png`
- 生成 `characters/<id>/review.html`，把 8 张图排成一页，方便检查。
- `character regen` 只重画指定的那一张；`character approve` 把对应画风的 `approved` 设为 `true`。
- 没有 `approved` 的画风不能用于 `render`，命令行要给出明确提示。
- M4 做完后，定妆生成的图也要过一遍质检。

**验收**：3 个不同画风的测试角色都完成定妆，`review.html` 里 8 张图肉眼看是同一个人。

## 7. M3 逐格生图

### 7.1 prompt 拼装 `render/prompt_builder.py`

```text
{style.positive}.
{name}: {identity values joined}. wearing {outfit}.
{shot_en}. {expression_en}.
{visual_prompt_en}
single comic panel illustration, no text, no speech bubbles, no captions, no watermark, no panel border.
{有对白时追加：leave some empty space in the upper part of the frame.}
the reference images show the same character; keep the face, hair, eye color and accessories identical.
```

镜头和表情映射：

| 镜头 | shot_en |
|---|---|
| 远景 | wide shot, character small in the environment |
| 全景 | full shot, whole body visible |
| 中景 | medium shot, waist up |
| 近景 | medium close-up, chest up |
| 特写 | close-up on the face |
| 俯视 | high-angle shot |
| 仰视 | low-angle shot |
| 背影 | back view, seen from behind |

| 表情 | expression_en | 参考图 |
|---|---|---|
| 开心、大笑 | smiling / laughing | `bust_smile` |
| 难过、哭 | sad / crying with tears | `bust_cry` |
| 生气 | angry | `bust_angry` |
| 惊讶 | surprised | `bust_surprised` |
| 疲惫 | exhausted | `bust_tired` |
| 平静、害羞、思考 | calm / blushing shyly / thinking | 不额外加 |

### 7.2 参考图选择

1. 总是带 `front_full`。
2. 表情有对应的胸像时，加上那张胸像。
3. 镜头是背影时，加上 `back_full`。
4. **最多 3 张**。参考图太多反而会让模型混乱。
5. 这一格没有主角（纯场景）时，不带参考图。

### 7.3 生成、缓存与重试 `render/generator.py`

- 用线程池并发生成，并发数取 `generation.concurrency`（默认 3）。
- 遇到 429 或 5xx 时，指数退避重试 `api_retries` 次。这和质检重画是两套独立的计数。
- 缓存键：`sha256(prompt + 每张参考图的哈希 + provider + model + aspect + seed)`，结果存到 `cache/images/<key>.png`。命中缓存就不再调用 API。
- 每格写一份 `panels/NN.meta.json`：prompt、参考图路径和哈希、provider、model、aspect、seed、耗时、缓存是否命中、质检结果。

### 7.4 预算 `budget.py`

- 用量记在 `usage/<YYYY-MM>.json`，包括生图张数和 LLM 调用次数。
- 生成前估算：本次张数 = 格数 × (1 + `expected_retry_ratio`)。
- 预计会超出月度预算时，把 `max_panels` 下调到剩余额度允许的格数，并给出提示；剩余额度连 2 格都不够时，直接停止并提示。

**验收**：一篇 8 格日记用真实服务完整跑通；再跑一次时全部命中缓存，不产生新的生图调用。

## 8. M4 质检 `qc/checker.py`

### 8.1 质检提示词

```text
第一张图是角色 {name} 的标准参考图，第二张是待检查的漫画格。
逐条检查待检查图中的 {name} 是否符合以下特征：
{checklist: 每行一条 "- 名称：值"}
另外检查：
- has_text：画面里是否出现了文字、对话框或水印
- character_visible：{name} 是否出现在画面中
背影、远景等情况下看不到的特征，标记为 not_visible，不算失败。
只按 JSON schema 输出。
```

### 8.2 判定与重画

- 这一格有主角时：`character_visible` 为真、`has_text` 为假，且没有 critical 特征是 `fail`，才算通过。
- 这一格没有主角时：只检查 `has_text`。
- 不通过时，把失败项写进 prompt 末尾（例如 "IMPORTANT: hair must be silver-white long twin tails"），然后重画，最多 `qc.max_retries` 次。
- 用完重试次数仍不通过：保留最后一张，标记 `flagged: true`。导出的页面照常生成，命令行列出被标记的格子。
- 汇总写入 `qc_report.json`：每格的结果、重试次数，以及整体通过率。

**验收**：故意把参考图换成另一个角色，质检能判定失败；正常生成时通过率有记录。

## 9. M6 模型基准测试

- 命令：`visdiary bench --providers a,b,c`
- 数据：3 个测试角色 × 2 篇日记，每个候选服务都跑完整流程。
- 输出 `bench/results.csv`，字段包括：provider、model、角色、日记、格数、质检首次通过率、最终通过率、平均单格耗时、单格成本（按 `config.yaml` 里填写的单价计算）、中文理解评分（人工 1–5 分）。
- 选 1 个主力服务，再留 1 个备用，结论写进 README。

## 10. 测试清单

| 测试文件 | 覆盖内容 |
|---|---|
| `test_parser.py` | frontmatter、时间标题切分、没有标题的情况、文件名日期兜底 |
| `test_rules.py` | 4.4 节的每一条规则 |
| `test_layout.py` | 高光格独占一行、双格行、剩余单格；坐标不重叠 |
| `test_prompt_builder.py` | 镜头、表情映射，参考图最多 3 张，背影时带背面图 |
| `test_cache.py` | 输入相同则缓存键相同，任一输入改变则缓存键改变 |
| `test_budget.py` | 预算下调格数，额度不足时停止 |
| `test_pipeline_mock.py` | mock 模式端到端跑通，输出文件齐全 |

## 11. 常见坑

- **LLM 输出的 JSON 不稳定**：优先用结构化输出，用 pydantic 校验，最多修复重试 1 次。
- **生图模型在画面里写字**：prompt 里明确禁止，再由质检的 `has_text` 兜底。
- **参考图带太多**：超过 3 张后，一致性反而下降。
- **在 Playwright 截图里中文变成方块**：字体必须用相对路径的 `@font-face` 引入。
- **Windows 兼容**：统一用 `pathlib`，用 UTF-8 读写文件，临时文件用 `tempfile`。
- **API Key 泄露**：Key 只放 `.env`，提交前检查 `git status`。
- **开发时花冤枉钱**：默认使用 mock，调用真实服务必须显式传 `--provider`。

## 12. 第一轮结束后提交检查

- 更新 README 的目录树，补上真实的 demo 截图。
- 提交一份完整的输出示例：`storyboard.json`、`layout.json`、`qc_report.json` 和 `page.png`。
- 提交 `bench/results.csv` 和模型选择结论。
- 写明遇到的问题，以及哪些地方还不满意。
