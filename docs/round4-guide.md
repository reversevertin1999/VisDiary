# VisDiary 第四轮开发指导

> 第三轮结论：provider、质检、人设、单格导演都已落地。但复核评测数据时发现，**"v2 → v3 身份通过率 0.0 → 0.938"这个结论不成立**。根源是一串小 bug 叠在一起，最后变成了一个看似合理的错误解释。
>
> 第四轮分三部分：
> - **A. 修复**：把这条错误链上的每一环补上，并清理第三轮遗留的问题；
> - **B. 重新评测**：用同一把尺子重判新旧两组，完成第三轮没跑成的整页验收；
> - **C. 网页版**：把 M15 单格导演做成图形界面，作为黑客松 demo。
>
> 顺序必须是 A → B → C。尺子没校准之前，任何"变好了"的结论都不可信。

---

## 0. 本轮的两条原则

### 0.1 输入要校验，结论要能追溯

第三轮的错误链：

```text
emotion_bench 写结果时 image 路径多拼了一个 "img-"（img-img-2026-10-14/...）
  → blind_rejudge 按记录的路径找图，文件不存在
  → codex 收到不存在的 -i 图片，没报错，静默跳过
  → 模型只收到参考图，如实回答"第二张图加载失败"
  → strict schema 强制它填 character_visible，只能填 false
  → identity_passes 判定 32/32 失败
  → 汇总报告里写成"被点状鼻子卡掉"（面部细节本来就是 critical=false）
```

每一环单独看都是小问题，所以本轮的修复按**防线**来组织：文件是否存在 → 模型实际收到几张图 → 模型能不能判断 → 结果有没有完整保存 → 报告里的数字有没有出处。

### 0.2 交付报告必须附证据

本轮交付汇总里的**每个数字、每个"原因是 X"的判断**，都要写明：
- 来源文件路径（如 `bench/results/rejudge-v4-2026-10-20.json`）
- 样本量 n 和 95% 置信区间
- 能复现的命令

没有证据支撑的因果解释，要写成"推测"，不能写成结论。

---

## A. 修复

### P0：评测链路

**A1. bench 图片路径重复拼接 `img-`** —— `bench/emotion_bench.py`

- 找到拼 `image` 字段的地方（`rg -n '"image"|img-' bench/emotion_bench.py`），改成相对结果 JSON 所在目录的正确路径；
- 写结果前逐条断言：`assert (out_json.parent / cell["image"]).is_file(), cell["image"]`；
- 写一个一次性迁移脚本 `bench/migrate_paths.py`：备份 `*.json.bak`，把 `img-img-` 改成 `img-`，最后输出"仍找不到 N 条"，N 必须为 0；
- 同时检查 `2026-10-14.html` / `2026-10-15.html` 网格对比图里的图片路径，修完后用浏览器打开确认图片能正常显示。

验收：迁移后两个结果文件的所有 `image` 路径都存在；新跑一次 bench，断言不触发。

**A2. provider 调用前检查图片是否存在** —— `providers/codex.py`

- `_chat_json`：对 `images` 逐个检查 `Path(img).is_file()`，不存在就抛 `FileNotFoundError`，错误信息里写明缺的是第几张、路径是什么；
- `CodexImage.generate`：参考图也做同样检查；
- mock provider 同样检查，否则单测测不到这条防线。

验收：单测传入一张不存在的图片，`chat_json` / `analyze_json` / `generate` 都立刻报错，并且**没有发生任何 codex 调用**（用 mock 记录调用次数来断言）。

**A3. 质检要核对模型实际收到的图片数量，并给"无法判断"留出口** —— `schemas.py` / `qc/checker.py`

strict schema 会强制模型把每个字段都填上。即使它看不到图，`character_visible` 也只能填 `false`，于是"看不到图"和"主角不在画面里"被混成了一回事。

```python
class QCResult(BaseModel):
    images_received: int              # 你实际收到了几张图
    judgeable: bool                   # 能否完成判断（图缺失、损坏、看不清 → false）
    unjudgeable_reason: str = ""
    character_visible: bool
    has_text: bool
    checks: list[QCCheck]
    expression: ExpressionCheck | None = None
```

- `check_panel`：`images_received != len(images)` 或 `judgeable == false` 时，抛 `QCInputError`，重试 1 次后仍失败就标记为 `qc_error`；
- `qc_error` 的格子**不计入**身份和表情的任何统计，在报告里单独计数；
- prompt 要和实际传入的图片数量一致：`front_ref is None` 时，不能再写"第一张是参考图，第二张是待检查图"。bench 和重判脚本里要 `assert front_ref is not None`。

验收：
- [ ] 用 mock 模拟"只收到 1 张图"的回复，结果被标为 `qc_error`，不被算作身份失败；
- [ ] `qc_report.json` 和 bench 结果里都有 `qc_error_count`。

**A4. 特征改用编号匹配，并检查是否全部覆盖** —— `qc/checker.py`

重判输出证实，模型会改写特征名：`头顶元素` 被写成 `头顶猫耳状结构`，`发饰/背景头饰` 被写成 `黄色冠状或太阳状装饰`。`identity_passes` 是按名字精确匹配 critical 特征的，这两个 critical 特征即使画错，也匹配不上，**身份通过率会被高估**。

- prompt 里给特征编号：`1. 发色：…`、`2. 发型：…`；
- `QCCheck` 增加 `feature_id: int`，按编号匹配，不再按名字匹配；
- 覆盖检查：critical 特征在 `checks` 里缺失、或编号重复，都当作 `fail`，并写 warning；
- 编号不在合法范围内的检查项忽略，并写 warning。

⚠️ **特征名称和 critical 设置属于角色契约，agent 不要自己修改 `character.json`**。如果认为某个名称有歧义（比如 `头顶元素` 实际是猫耳状结构，`发饰/背景头饰` 实际是黄色冠状装饰），在交付报告里列出修改建议，由用户确认后再改。

验收：单测覆盖"模型改写名字""漏掉一个 critical 特征""编号重复"三种情况。

**A5. 主角不可见时，表情判定为"不适用"** —— `qc/checker.expression_fail_reason`

`character_visible == False` 或 `judgeable == False` 时返回 `"n/a"`，不计入表情命中率。否则会出现"身份 0.0、表情 1.0"这种互相矛盾的统计。

**A6. bench 结果保存完整质检明细** —— `bench/emotion_bench.py`

每条记录都附上 `result: QCResult.model_dump()`。`2026-10-15.json` 里那 1 格身份失败，现在完全查不到原因，以后不能再出现这种情况。

### P1：评测方法

**A7. 指标改造** —— `bench/emotion_bench.py` / `bench/blind_rejudge.py`

| 指标 | 定义 | 说明 |
|---|---|---|
| 表情 top-1 精确命中率 | `detected_emotion == target` | **主指标** |
| 表情组内命中率 | top-1 落在同一近义组 | 辅助 |
| 表情 top-2 命中率 | top-1 或 top-2 在同一近义组 | 辅助（就是第三轮的口径，太宽松，不能当主指标） |
| 强度命中率 | `abs(detected - target) <= 1` | |
| 演过头率 | `detected - target >= 2`，或目标 1 级时判定为 3 级 | **必须包含强度 1 的样本**，否则这个指标结构上永远是 0 |
| 身份通过率 | `identity_passes`（编号匹配） | 排除 `qc_error` |
| 质检错误数 | `qc_error_count` | 必须报告 |

- bench 默认子集改为：8 个情绪 × **强度 1/2/3** × 近景 = 24 格；
- 所有比例都报告 `k/n` 和 Wilson 95% 置信区间：

```python
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (round(c - h, 3), round(c + h, 3))
```

- README 的对比表加一句说明：置信区间有重叠时，不能下"变好了"的结论。

**A8. 修 `calibrate.py`** —— `bench/calibrate.py`

- **先标注，后揭晓**：用户输入标注之后，才显示系统的判定结果。现在的写法是先打印系统判定，再提示"先别看"，会锚定标注者；
- 输入校验：情绪必须在词表里（也支持别名），强度必须是 1–3，相似度必须是 1–5；不合法时重新输入，不能崩溃；
- 抽样用固定种子（`random.Random(42)`），抽样结果写进标注文件，断点续标时样本不变；
- 输出三个一致性指标：
  - 21 类精确匹配的 Cohen's κ；
  - **近义组级别的 κ**（和质检的实际判定口径一致）；
  - 强度的**二次加权 κ**（强度是有序变量）；
- 增加一项身份人工标注："主角身份对不对（y/n）"，输出人工和 `identity_passes` 的一致率；
- 结果文件顶部记录：样本量、种子、标注时间、使用的质检版本。

**A9. 删除失效脚本，增加 import 冒烟测试**

- `bench/baseline_expression.py` 还在 import 已经被改名的 `expression_passes`，运行即报 ImportError。它的功能已经被 `blind_rejudge.py` 取代，直接删除，README 同步更新；
- `tests/test_bench_imports.py`：把 `bench/*.py` 逐个 import 一遍，防止脚本悄悄失效。

**A10. 预算拆成两个池子**（用户已确认：开发不设上限）

| 池子 | 用途 | 上限 | 行为 |
|---|---|---|---|
| `prod` | 日常 `render` / `redraw` / `character` | `budget.monthly_images`（默认 300） | 超出就熔断 |
| `dev` | `bench/*`、`calibrate`、`visdiary bench`、开发调试 | 不设上限 | **只记账，不熔断** |

- 切换方式：`--dev` 参数或环境变量 `VISDIARY_ENV=dev`；`visdiary bench` 和 `bench/*.py` 强制使用 dev；
- 用量文件按池子分开：`{"2026-10": {"prod": {...}, "dev": {...}}}`，dev 的用量不计入 prod 的月度额度；
- 新增 `visdiary budget` 命令，显示两个池子当月的生图、LLM、质检调用次数；
- dev 虽然不设上限，也要记账，这样才知道什么时候会碰到 ChatGPT 计划自身的限额。

验收：
- [ ] 跑一次 bench，prod 用量不变，dev 用量增加；
- [ ] prod 达到上限后 `render` 被拦下，同时 bench 照常运行；
- [ ] 有单测。

### P1：provider 和质检的遗留问题

**A11. 重画时不再使用 `crop_hint`** —— `qc/checker._qc_one`

现在用的 `head_bbox` 来自**上一张不合格的图**，而且是裁剪后图片里的坐标，拿去裁新生成的原图，坐标根本对不上。重画时只用默认的上方锚定。

**A12. GEN_DIR 兜底逻辑在并发下可能拿错图** —— `providers/codex.CodexImage._extract_path`

回复里没有路径时，会在 `GEN_DIR` 里找最新的文件。`concurrency > 1` 时，可能拿到其他线程刚生成的图，导致两格互换。

修法（二选一）：
- 去掉兜底，回复里没有路径就当作失败重试；
- 或者用进程内的锁记录"已被认领的文件"，兜底时只取未被认领的文件，并在 meta 里记一条 warning。

**A13. `_TRANSIENT` 里的 `"5xx"` 匹配不到** —— `providers/codex.py`

`"5xx"` 是字面字符串，永远匹配不到真实的 500 或 502。改成正则：`re.search(r"\b(429|5\d\d)\b|Reconnecting|worker quit|Transport|timed out|ECONN|EOF", tail)`。

**A14. provider 里残留的语义指令** —— `providers/codex.CodexImage.generate`

开头固定写着"这是一张漫画分格插画"，生成设定卡和表情表时也会带上。改成由调用方传入 `kind: Literal["panel", "sheet", "expression_sheet"]`，provider 只写对应的一句任务说明。

**A15. 质检重试的记账** —— `qc/checker.check_panel`

捕获 `ProviderOutputError` 后重试，实际调用了 2 次，但只记了 1 次 `vision_calls`。每次真实调用都要记账。

**A16. 清理 codex 生成目录** —— `providers/codex.py` / README

`~/.codex/generated_images/` 里的图会一直留着，里面有角色图。读取成功后把源文件移入 `cache/` 或删除（加配置项 `provider.codex.cleanup: true`），并在 README 的隐私一节写明。

### P2：README

- 删除"点状鼻子导致旧图失败"之类没有证据的说法；对比表全部换成 B 部分重判后的数字（附 n 和置信区间）；
- `docs/` 目录树补上 `round3-guide.md`、`round4-guide.md`；
- 隐私一节补充：`data/`、`bench/` 下生成的图片和整页、`~/.codex/generated_images/`；
- 预算一节改成两个池子的说明。

---

## B. 重新评测（A 全部完成后再做）

### B1. 同一把尺子重判新旧两组

```bash
VISDIARY_ENV=dev .venv/bin/python bench/blind_rejudge.py bench/results/2026-10-14.json   # v1/v2 旧图
VISDIARY_ENV=dev .venv/bin/python bench/blind_rejudge.py bench/results/2026-10-15.json   # v3 新图
```

输出一张表：每个指标都写 `k/n（95% CI）`，`qc_error_count` 必须为 0。

### B2. 补跑强度 1 的样本

用新的默认子集（8 情绪 × 强度 1/2/3 × 近景）跑一次 v3，补上演过头率的数据。

### B3. 人工校准（由用户完成）

```bash
VISDIARY_ENV=dev .venv/bin/python bench/calibrate.py bench/results/<B2 的结果>.json
```

用户标 40 格以上，输出三个 κ。判读规则：

| 组级 κ | 结论 |
|---|---|
| ≥ 0.6 | 表情质检可信，可以考虑 `qc.expression_strict: true` |
| 0.4–0.6 | 只作参考，表情不合格只记录，不自动重画 |
| < 0.4 | 表情质检不可信，先改质检 prompt 或换判定模型 |

### B4. medium 整页验收（第三轮没跑成的那一项）

```bash
visdiary render diaries/samples/medium.md --character xiaomali --provider codex
```

- 走 prod 池；
- 用户给每格"像不像她"打 1–5 分，和第二轮同一篇日记的整页对比；
- 记录：每页不同情绪数、平均强度、人设生效情况（`temperament` 有没有体现）、需要人工 `edit` 的格数。

**第四轮整体验收的核心就是 B4**：用户的主观评分有没有比第二轮提高。B1–B3 是为了确认机器指标和人的判断一致。

---

## C. M18 网页版（B 完成后）

把 CLI 已有的能力包成本地网页，作为黑客松 demo。**不新增业务逻辑**，所有操作都调用 `pipeline` / `character` / `M15` 已有的函数。

### C1. 技术选型

- 后端：FastAPI，默认只监听 `127.0.0.1`（本地使用，不做账号体系）；
- 进度推送：SSE（Server-Sent Events，服务器推送事件）；
- 任务队列：单 worker 串行执行（codex 通道本身也受并发限制），任务状态存在 `output/<日期>/job.json`，页面刷新后可以恢复；
- 前端：单页 HTML + 少量原生 JS（或 Alpine.js 这类不需要构建的库），不引入构建链。

### C2. 页面

| 页面 | 功能 |
|---|---|
| 日记 | 选日期、写 Markdown（支持 `<!-- vd: -->` 导演提示）、保存到 `diaries/` |
| 角色 | 设定卡审阅：显示盲校验结果，标签错位的格子标红；单张 regen；approve；编辑 persona |
| 生成 | 一键生成，实时显示进度：拆分镜 → 第 N 格生图 → 质检 → 排版；显示预算池用量 |
| 漫画 | 整页预览；**点击某一格打开编辑器** |
| 单格编辑器 | 情绪下拉框、强度滑块、备注框 → 重画（可选出 2 张候选）→ 选定 → 锁定；显示这一格的 prompt、参考图、质检结果 |
| 用量 | prod / dev 两个池子当月的用量 |

### C3. API（示例）

```text
GET  /api/diaries                     列出日记
PUT  /api/diaries/{date}              保存日记
POST /api/render/{date}               开始生成（返回 job_id）
GET  /api/jobs/{job_id}/events        SSE 进度
GET  /api/pages/{date}                storyboard + layout + 每格 meta
POST /api/pages/{date}/panels/{n}/edit     修改情绪、强度、备注
POST /api/pages/{date}/panels/{n}/redraw   重画（variants 参数）
POST /api/pages/{date}/panels/{n}/pick     选定候选并锁定
GET  /api/characters/{id}             角色档案 + 设定卡
POST /api/characters/{id}/regen       单张重画
POST /api/characters/{id}/approve
GET  /api/budget
```

### C4. 验收

- [ ] 从写日记到下载 `page.png`，全程不用命令行；
- [ ] 单格编辑后只重画这一格，其他格的文件哈希不变（和 M15 的 CLI 验收一致）；
- [ ] 生成中途刷新页面，进度能恢复；
- [ ] 用户的编辑和选择照常写入 `feedback.jsonl`（M16 不受影响）；
- [ ] API 层有测试（用 mock provider，FastAPI TestClient）；
- [ ] README 增加"网页版"一节和截图。

---

## D. 开发顺序与交付要求

```text
A1 路径 → A2 图片存在性检查 → A3 输入核对 / 无法判断 → A4 特征编号 → A5 / A6
  → A7 指标 → A8 calibrate → A9 冒烟测试 → A10 预算池
  → A11–A16 → README
  → B1 重判 → B2 补测强度 1 → （用户）B3 校准 → B4 整页验收
  → C 网页版
```

交付报告要包含：
1. A 部分每一条的改动文件和对应测试名；
2. B1/B2 的结果表（`k/n`、95% CI、`qc_error_count`、来源文件路径、复现命令）；
3. 需要用户决定的事项清单（例如特征名的修改建议），**不要擅自修改 `character.json`**；
4. 未完成或推迟的事项，以及原因。

黑客松时间紧时，最小可演示版本是：**A1–A6 + A10 + B1 + B4 + C 里的"漫画页 + 单格编辑器"**。演示时，现场点开一格，把"大笑 3"改成"开心 1 + 备注：抿嘴笑"再重画，最能体现这个项目和普通文生图工具的区别。
