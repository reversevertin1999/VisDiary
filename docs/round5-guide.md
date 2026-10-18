# VisDiary 第五轮开发指南：第四轮复核结论 + 后续优化

> 依据：仓库 `vsplabjy/HKS` 当前 main 分支中的 `providers/codex.py`、`schemas.py`、`qc/checker.py`、`budget.py`、`render/generator.py`、`director.py`、`webapp.py`、`web/index.html`、`bench/emotion_bench.py`、`bench/calibrate.py` 和 README。
> 未能读到：`tests/` 下的冒烟测试（猜了 `test_imports.py` / `test_smoke.py` 两个路径，都不存在），请在交付报告里给出实际路径和 `pytest` 输出。

---

## 0. 一句话结论

**后端修复（A1–A16）大部分做对了，主链路也确实能跑通；但有两类问题必须先处理：**

1. **网页里的"单格导演"实际上不能用，还会污染偏好数据**（前端 3 个 P0 问题，见 §2.1）。
2. **"强度命中≈1.0、演过头率 = 0"是指标口径造成的必然结果，不是模型真的做得好**（见 §3）。人工校准得到的强度 κ = 0.2 才是真实情况：AI 质检判断强度基本靠不住。

---

## 1. A1–A16 逐项核对

| 项 | 状态 | 证据 / 缺口 |
|---|---|---|
| A1 路径修正 | ✅ / ⚠️ | bench 写结果前逐条断言图片存在；**但图片目录按天命名、文件名按 `情绪_强度_镜头_版本` 命名，同一天重跑会覆盖旧图，而 JSON 只是加时间后缀** → 旧 JSON 指向的已经是新图（见 §2.3 P1-9） |
| A2 调用前查图 | ✅ | `_check_images()` 在 `_chat_json` 和 `CodexImage.generate` 中都有调用 |
| A3 images_received / judgeable / qc_error | ✅ / ⚠️ | 字段、提示词、重试 1 次、`QCInputError → qc_error` 都已落实；**但网络类 `RuntimeError` 没有按格捕获，任何一格的 codex 失败都会让整个 `qc_all` 崩溃** |
| A4 特征编号匹配 + 覆盖检查 | ✅ | 漏项、重复编号按 fail 处理，越界编号只记警告，实现得很好 |
| A5 主角不可见 → n/a | ❌ | `expression_fail_reason` 会返回 `"n/a"`，但 `_qc_one` 里写的是 `expression_pass = fail_reason is None` → **n/a 被当成"未命中"计入分母**；另外 `expression is None` 时直接返回 None，**被当成"命中"** |
| A6 bench 保存完整 QCResult | ✅ | `result` 已改为完整的 `model_dump` |
| A7 top-1 主指标 / 强度 1 / Wilson | ⚠️ | 结构上已实现；但 top-1 没做别名归一，"top-2 命中"实际按近义组计算，强度和演过头的阈值在 3 级量表上几乎不可能判失败（§3）；线上 `qc_report.json` 仍然只有组级命中率 |
| A8 calibrate | ⚠️ | 先标注后揭晓、固定种子、三种 κ（Cohen's kappa，一致性系数）都已实现；但组级 κ 的口径和质检实际口径不一致，没有计算"人工 vs 目标"这个真实命中率，n=24 是因为源数据只有 24 格，身份判断没有负样本（§3.3） |
| A9 删除失效脚本 + 冒烟测试 | ❓ | 没找到测试文件 |
| A10 prod/dev 预算池 | ✅ / ⚠️ | 两个池子 + 加锁 + 旧格式迁移都没问题；**bench 用的是 `os.environ.setdefault`，没有真正强制走 dev 池**（shell 里 `VISDIARY_ENV=prod` 时会扣 prod）；JSON 不是原子写，写到一半崩溃会导致文件损坏 |
| A11 取消 crop_hint | ✅（有残留） | checker 重画时已不再传入；`generate_panel` / `CodexImage.generate` 的参数和 docstring 还在，质检 prompt 仍要求输出 `head_bbox`，白白消耗 token |
| A12 GEN_DIR 并发兜底 | ✅ | 已删除"最新文件"兜底，只认回复里写明、位于 GEN_DIR 下、mtime 晚于本次调用开始时间的路径 |
| A13 5xx 正则 | ✅ | `\b(429\|5\d\d)\b`；小问题：会误匹配 "500 tokens" 这类文本 |
| A14 kind 参数 | ✅ | 三种 kind 都有；设定卡构建时是否传了 `kind="sheet"` 本轮没有核对 |
| A15 重试记账 | ⚠️ | vision 只在解析成功后记账，`ProviderOutputError` 那次调用没有记；生图失败的尝试也不记账；codex 内部 3 次 × generator `api_retries` 次，重试次数是相乘的，单格最坏情况可能卡住几十分钟 |
| A16 cleanup | ✅ | 只在成功路径清理；`Image.open` 失败时源文件会残留（问题不大） |
| README | ⚠️ | "12 张表情卡 3×4" 与 "3 全身 + 16 胸像" 前后矛盾；"已知限制"里仍写着网页在后续版本；300 张预算没有区分 prod/dev；没提 B4 是否完成 |

---

## 2. 新发现的问题（按优先级）

### 2.1 P0：网页单格导演（`web/index.html` + `director.py`）

**P0-1 候选图全部显示成同一张。**
`applyEdit()` 渲染候选时，每个候选用的都是 `/api/pages/${date}/panels/${pid}.png`，也就是正式格；而 `redraw_panel` 结束时会把正式格恢复成候选 a。所以用户看到的 a/b 是同一张原图，点"选 b"等于盲选。后端也没有提供读取 `NN.variant-x.png` 的接口。
修复：新增 `GET /api/pages/{date}/panels/{pid}/variants/{letter}.png`（letter 只允许 `[a-z]`），前端按 `v.file` 渲染。

**P0-2 固定等 4 秒就去读候选。**
`setTimeout(..., 4000)` 的注释写着"redraw 很快"，但一次 codex 生图加质检通常要几十秒到几分钟。4 秒后读到的不是空列表就是上一次的候选。
修复：在 `watchJob(..., onDone)` 的回调里再去拉取 meta；同时把 job 日志显示在弹窗里。

**P0-3 编辑器不回填当前值，每次重画都会把分镜改成"平静 2"并写入偏好反馈。**
`openEditor()` 不读当前格的 `acting`，下拉框默认是第一项"平静"，滑块默认是 2；`applyEdit()` 每次都会把 emotion 和 intensity 发出去。`edit_panel` 只要收到这两个值就记一条 feedback，不判断值有没有变化。
后果：用户只想"换一张"，结果分镜被改掉，**M16 偏好学习还会收到一条假的"用户把开心 3 改成平静 2"**。
修复：
- `openEditor` 从 `/api/pages/{date}` 的 storyboard 回填 emotion / intensity / 当前 face_en；
- 前端只发送有改动的字段；"只重画不改分镜"单独做一个按钮；
- `edit_panel` 只在 `new != old` 时写 feedback；
- **清洗已有的 feedback 文件**：删除 `user.emotion=="平静" and user.intensity==2 and note==""` 且发生在第四轮之后的记录，或者干脆整批作废。

### 2.2 P1：正确性

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| P1-1 | `checker._qc_one` | n/a 被计为未命中，`expression None` 被计为命中（见 A5） | `expression_pass = None if fail_reason == "n/a" else (fail_reason is None)`；可见但 `expression is None` 时返回 `"n/a"` 并在报告里计数 |
| P1-2 | `checker.qc_all` | 单格网络异常会让整页崩溃 | `_qc_one` 外层捕获 `Exception`，按格标记 `qc_error`，整页继续 |
| P1-3 | `checker.check_panel` | `ProviderOutputError` 那次调用没有记账 | 在 except 分支里也调用 `budget.record(vision_calls=1)` |
| P1-4 | `director.edit_panel` | emotion 直接赋值没有校验（pydantic 默认不做赋值校验）；注释说"保存时校验"并不成立，非法值会原样写入，下次加载时被静默归为平静；intensity 不做截断，0 会被当成"没改" | 用 `normalize_emotion` 校验，不在词表里就返回 400；intensity 限制在 1–3；返回值注解改为 `list[str]` |
| P1-5 | `director.redraw_panel` | 生成候选时 `generate_panel` 会写正式格 png 和 meta.json，随后又被 `png.replace` 移走；如果中途 `check_panel` 失败，**正式格图片缺失，meta 也被覆盖** | `generate_panel` 增加 `out_name` 参数直接写入 variant 文件，或者整段放进 `try/finally` 恢复原图和原 meta |
| P1-6 | `director.redraw_panel` | `identity_passes(r, char, True)` 写死了 True；没有主角的格子身份永远判 ✗ | 传入 `char.id in panel.characters` |
| P1-7 | `webapp.job_events` | 竞态：先检查 status 再把订阅者加入 `subs`，如果 `finish()` 恰好发生在两步之间，SSE（Server-Sent Events，服务器推送事件）会永远挂起；错误信息里带换行会破坏 SSE 帧 | 每个 Job 加一把 `threading.Lock`，订阅和 finish 互斥；每条事件用 `json.dumps` 包装 |
| P1-8 | `webapp` 并发 | edit / pick 在请求线程里同步执行，不经过 runner；render 进行中改同一天的格子会让两边同时写同一个目录 | edit / pick 也提交到 runner，或者按日期加锁；job 状态（包括 failed）落盘到 `job.json` |
| P1-9 | `emotion_bench` | 同一天重跑会覆盖图片（见 A1）；cells 里没有图片的 sha256 | 每次运行使用独立目录 `img-<日期>-<HHMMSS>`；每格写入 `sha256`；calibrate 加载时校验 sha256，不一致就拒绝 |
| P1-10 | `budget` | bench 没有强制 dev 池；写文件不是原子操作 | `os.environ["VISDIARY_ENV"] = "dev"`；写临时文件后 `os.replace`；`cfg.env` 不在 POOLS 中时直接报错 |

### 2.3 P1：本地网页的安全性（演示前务必修复）

只监听 127.0.0.1 并不等于安全：

- **CSRF（Cross-Site Request Forgery，跨站请求伪造）**：`/api/render/{date}`、`/redraw`、`/pick`、`/approve` 都是不带 body 的 POST，属于"简单请求"，任何网页都能直接向 `127.0.0.1:8420` 发起，消耗 prod 配额和 ChatGPT 额度。
- **DNS 重绑定（DNS rebinding）**：没有校验 Host，恶意网站有机会读取 `/api/diaries/*/raw`，也就是**用户的日记原文**。
- **`char_img` 路径校验可以绕过**：`str(p).startswith(str(base))` 存在前缀问题（`demo` 能匹配 `demo2/`）；`cid=".."` 时 base 会变成项目根目录，进而可以读取 config、usage、diaries 等任意文件。

最小修复（大约 30 行）：
1. `TrustedHostMiddleware(allowed_hosts=["127.0.0.1", "localhost"])`；
2. 中间件校验所有非 GET 请求：`Origin` 必须是 `http://127.0.0.1:<port>` 或 `http://localhost:<port>`，并且要求带自定义请求头 `X-VisDiary: 1`（前端 `jpost` / `jput` 统一加上）；
3. 统一的参数校验：`date` 必须匹配 `^\d{4}-\d{2}-\d{2}$`，`cid` 必须匹配 `^[a-z0-9_-]{1,32}$`，`variant` 必须匹配 `^[a-z]$`；
4. 文件路径校验改用 `p.is_relative_to(base)`，并且 base 必须位于 `characters_dir` 之下。

### 2.4 P2：清理

- `index.html`：`loadComicDates` 无意义地请求了 `/api/pages/__none__`；漫画日期列表应该来自 output 目录而不是 diaries（新增 `GET /api/pages`）；`loadComic` 重复请求；`loadChar` 里有未使用的 `base`；每次加载都新建 ResizeObserver，造成泄漏；name / persona 直接写入 innerHTML。
- `director._qc_score` 的注释写的是"表情过才加 1"，实现却只判断 expression 是否存在；`auto_pick_highlight` 没有用到 `expression_fail`。
- `ExpressionCheck.detected_emotion` 改为 `Emotion` Literal，`detected_intensity` 加 `ge=1, le=3`，让 strict schema 直接约束模型输出。
- 删除 `head_bbox`、`crop_hint` 的残留。

---

## 3. 指标口径：为什么"演过头 = 0"不可信

### 3.1 结构性原因

强度只有 1/2/3 三级：

- `intensity_hit = |检测 − 目标| ≤ 1`：只有 (1, 3) 和 (3, 1) 两种组合会判失败，**9 种组合里有 7 种必然算命中**；
- `overacted = 差值 ≥ 2`：**只有"目标 1、检测 3"这一种情况会计入**；
- 线上质检的 `weak` / `strong` 用的是同一个阈值 → **强度在质检里实际上没有被检查**。

所以强度命中 ≈1.0、演过头 0/24 都是口径决定的结果。人工给出的强度二次加权 κ = 0.2 说明，AI 判官的强度判断和人的判断几乎不一致，这个 0.2 才是真实情况。

### 3.2 新口径（bench 和 qc_report 统一使用）

| 指标 | 定义 |
|---|---|
| top-1 精确命中（主指标） | `normalize(检测) == 目标` |
| 组级命中（参考） | 使用 `_same_group`，和质检口径保持一致 |
| top-2 精确命中 | top-1 或 top-2 中任一**精确**等于目标（不再按组计算） |
| 强度精确命中 | `检测强度 == 目标强度` |
| 强度 MAE（Mean Absolute Error，平均绝对误差） | `mean(|差值|)` |
| 强度偏差 | `mean(检测 − 目标)`，正数表示整体演过头 |
| 演过头率 | `检测 > 目标`（差 1 就算），分强度 1/2/3 分别报告 |
| 强度混淆矩阵 | 3×3，直接写入报告 |
| 所有比例 | k/n + Wilson 95% CI |

### 3.3 校准重做（calibrate v2）

1. **新样本、n ≥ 60**：`--full` 中抽样，或 8 情绪 × 3 强度 × {近景, 中景} = 48 格，再加 12 格身份负样本；不要复用已经看过系统判定的那 24 格，否则会受锚定影响。
2. **身份负样本**：故意生成 8–12 张"不是她"的图（换发色、去掉头顶元素、换成另一个角色），用来检验身份质检能不能抓到错误。现在的 24/24 一致只能说明"没有误报"，**不能说明它能发现问题**。
3. **增加"人工 vs 目标"**：这才是真实的表情命中率，比"AI 判官 vs 目标"更重要。现有 labels 文件里已经有 `target` 和 `user_emotion`，可以马上算一次：
   ```bash
   python - <<'PY'
   import json; d=json.load(open("bench/results/calibration-2026-10-15.json"))
   L=d["labels"]; n=len(L)
   print("人工top1 vs 目标:", sum(l["user_emotion"]==l["target"] for l in L), "/", n)
   PY
   ```
4. **组级一致率改为成对计算**：近义组有重叠（感动、得意都属于两个组），`_to_group` 取第一个匹配的组，相当于把它们硬拆成互不重叠的分区，和质检口径不一致。改为报告 `_same_group(人, 机)` 一致率 + 21 类精确 κ。
5. **二次加权 κ 固定类别为 [1, 2, 3]**（现在只用实际出现过的值，不同批次之间无法比较），并给出 bootstrap 95% CI；n=24 时，κ = 0.2 的置信区间大约横跨 [−0.1, 0.5]。
6. **揭晓方式**：改为全部标完后统一揭晓，避免标注者在过程中逐渐学会判官的偏好。
7. 结果文件不覆盖：`calibration-<源文件>-<HHMMSS>.json`。

### 3.4 强度判断怎么救（按顺序尝试，每一步都用 §3.3 重新测）

1. **把 emotions.yaml 的分级描述改成可观察的特征**：嘴张开程度（闭 / 微张 / 大张）、眉毛角度、眼睛开合度、有无漫符、肢体幅度。不要用"轻微 / 明显 / 夸张"这类形容词。判官 prompt 也用同一份描述。
2. **锚点参考**：质检时同时给判官看该情绪强度 1/2/3 的表情设定卡（如果有的话），让它"选最接近的那张"，而不是凭空打分。
3. **成对 / 排序判断**：bench 中同一情绪的 1/2/3 三张图一起交给判官排序，计算 Kendall τ（肯德尔等级相关系数）。排序通常比绝对打分可靠得多。
4. 如果三步之后强度 κ 仍低于 0.4：**强度在质检中只作为警告，不再触发重画**；强度交给生成端的 prompt 和用户 edit 来保证，并在 README 中写明。

---

## 4. B4 整页验收（第四轮漏做，本轮补上）

- 3 篇真实日记 × medium 档，每篇和第二轮同日期的整页并排对比；
- 每一格：人工"像不像"（1–5）+ 表情是否对（y/n）+ 是否需要人工 edit；
- 每页记录：总耗时（拆成分镜 LLM / 生图 / 质检三段）、生图张数（含重画）、重画率、flagged 格数、qc_error 数；
- 产出 `docs/acceptance-round5.md`，附三张对比图。

---

## 5. 后续优化路线

### R5-0 本轮必须完成（约半天）
§2.1 三个 P0 → §2.3 安全最小集 → P1-1/2/5/7 → §3.2 新口径。

### R5-1 黑客松演示打磨（1 天内）
- **离线演示模式**：`visdiary web --demo`，使用 mock provider 加上预生成的 3 篇完整漫画（直接从缓存读取），保证现场没网、codex 限流时也能完整演示；真实生成作为"现场加演"。
- **演示脚本（3 分钟）**：写日记 → 生成（SSE 进度条按"分镜 → 生图 n/N → 质检 n/N → 排版"分阶段展示）→ 点一格改情绪 → 两张候选并排 → 选定 → 整页刷新。提前录好一段备用视频。
- **失败兜底**：占位格提供"一键补画"按钮；生成失败时页面显示可读的原因（限流 / 未登录 / 预算耗尽），不要只显示 traceback 的最后几行。
- **体验**：生成页显示预计耗时和本次会消耗的 prod 张数；漫画页提供 PNG 下载。
- 更新 README 和 `docs/assets/demo.png`。

### R5-2 性能与成本
- **生图和质检流水线化**：现在是全部生完再质检；改成一格生完立即提交质检（共用一个线程池或 `as_completed`），整页耗时预计可减少 30–40%。
- **合并重试层**：codex 内部重试和 generator 的 `api_retries` 只保留一层；单格设置总超时。
- **质检降本**：去掉 `head_bbox`；没有主角的格子只做文字检测；已通过身份检查且 prompt 未变的重画格可以只复检表情。
- 每页成本面板：生图张数 / LLM / vision 次数，以及缓存命中率。

### R5-3 产品功能（黑客松之后）
- **M19 多角色**：`panel.characters` 支持多个已建档角色，质检按角色分别给出编号清单，参考图按出场角色选取（每格参考图数量要设上限）。
- **M20 漫画册**：按月或按周装订、"去年今天"、导出 PDF；日记脱敏（人名 / 地点替换后再发给 LLM）；可选的 Notion 联动（从数据库读取日记、把漫画写回页面）。
- **线上隐式监控**：用户 edit 率、pick 非 a 候选的比例、重画次数作为质检质量的长期指标（前提是先修好 P0-3，否则数据是脏的）；feedback 只记录真正发生的修改。

---

## 6. 第五轮交付报告要求

1. 每个修复项附上文件:行号或 diff 片段；
2. `pytest` 全部输出（包括测试文件路径）；新增测试至少覆盖：n/a 不计入分母、`edit_panel` 拒绝非法值、未改动时不写 feedback、`char_img` 拒绝 `..` 和前缀绕过、非法 Origin 请求返回 403、SSE 在 finish 竞态下能结束；
3. 新 bench 和 calibrate 的原始 JSON（独立 run 目录 + sha256），并说明样本没有和旧样本重叠；
4. "人工 vs 目标"的 top-1 结果；
5. B4 验收文档；
6. 已清洗的 feedback 条数；
7. 依然不许改动 character.json。
