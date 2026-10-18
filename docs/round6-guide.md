# VisDiary 第六轮开发指南：收尾修复 + 提速 + 漫画册（M20）+ 多人物（M19）

> 背景：黑客松刚开始，时间充足。目前一整页需要 5–10 分钟。演示以预生成内容为主，路演时视情况现场生成一页。
> 优先级：**A 收尾修复 → B 强度核实 → C 提速与渐进显示 → D 漫画册 → E 演示素材（后台批量生成）→ F 多人物 → G 演示打磨**。
> 硬性规则沿用：不许修改已有角色的 character.json；交付报告必须附证据（diff / 测试输出 / 原始 JSON）。

---

## A. 第五轮遗留修复（先做，约 1–2 小时）

| # | 位置 | 问题 | 修复 | 验收 |
|---|---|---|---|---|
| A1 | `webapp.list_pages` | `sorted()` 直接比较 dict，**输出目录有 2 页及以上时抛 TypeError**，漫画页打不开 | `sorted(..., key=lambda d: d["date"])`；只收目录名符合 `^\d{4}-\d{2}-\d{2}$` 的；CLI 生成的页（没有 job.json）也要列出来，改为以 `storyboard.json` 存在为准 | 新增测试：造 3 个输出目录（含一个 `2026-10-09-v4`），接口返回 2 个合法日期并按顺序排列 |
| A2 | `tests/` | **测试会写真实数据**：覆盖 `diaries/2026-10-08.md`、`output/2026-10-08/`，删除 `characters/demo/feedback.jsonl`，mock 生图还计入 prod 池 | conftest 增加 autouse fixture：复制一份最小项目（config + characters/demo + samples）到 `tmp_path`，`cfg.root` 指向临时目录；`VISDIARY_ENV=test`，`budget` 增加 `test` 池或直接 monkeypatch `record` | 跑完 `pytest` 后，真实 `diaries/`、`output/`、`usage/`、`characters/demo/feedback.jsonl` 的 mtime 和 sha256 都不变（报告里附前后对比） |
| A3 | `director.redraw_panel` | 中途失败时只恢复了 meta，**没有用 `variant-a.png` 恢复正式格图片** | finally 中先 `shutil.copy2(va, png)` 再恢复 meta；`raise` 前保留原始异常（`raise ... from`） | 测试：让第二个候选的 `check_panel` 抛异常，断言正式格图片存在且 sha256 与原图一致 |
| A4 | `webapp.api_edit` | edit 仍在请求线程里同步执行，没走 runner（与文档说法不一致） | 提交到 runner；前端改为等任务结束后再发起 redraw | 生成进行中调用 edit，会排队等待而不是同时写同一目录 |
| A5 | `webapp.api_redraw` | `variants` 没有上限 | 限制在 2–4，超出返回 400 | 测试 |
| A6 | `test_round5` | SSE 竞态测试复制了一份消费逻辑来测，测的不是真实代码；`../../etc` 那条在路由层就已经 404，测不出校验 | 把 `job_events` 里的 `gen()` 抽成模块级函数 `sse_stream(job)`，测试直接调用它；日期校验改测 `/api/pages/2026-1-1`（能进 handler，必须返回 400） | 测试 |
| A7 | `director.edit_panel` | 只改 note 时，如果前端带了没变化的 emotion，也会写一条 feedback | 写入条件改为"emotion 或 intensity **确实变了**" | 测试 |
| A8 | README | ① 强度 κ 从 0.2 到 0.384 不能写成"改善"：强度描述没改，变的只是样本；② "人工 40/48 和 AI 39/48 一样，所以机器指标可信"推理不成立；③ 表情基准小节标题还写着"第四轮口径"、默认 24 格；④ emotion_bench 的 docstring 过时 | ① 改为"样本不同，κ 的不确定性很大，需要 bootstrap CI"；② 补上 `exact_kappa_21` 和逐格一致率；③④ 同步更新 | — |
| A9 | `bench/calibrate.py` | guide §3.3 要求的 bootstrap 95% CI 没做 | 对三个一致性指标各做 1000 次 bootstrap | 结果文件里有 `ci95` |

---

## B. 强度核实：问题出在生图，还是出在判官？（约 1 小时，成本很低）

"目标 1 级有 81% 被画过头"是 AI 判官得出的结论，而判官和人工在强度上的一致性只有 κ 0.38。先用现有标注确认：

```python
import json, glob, re
f = sorted(glob.glob("bench/results/calibration-2026-10-16-*.json"))[-1]
L = json.load(open(f))["labels"]
human = [[0]*3 for _ in range(3)]; model = [[0]*3 for _ in range(3)]
for l in L:
    t = int(re.search(r"_(\d)_", l["image"].split("/")[-1]).group(1))
    human[t-1][l["user_intensity"]-1] += 1
    if l.get("model_intensity"): model[t-1][l["model_intensity"]-1] += 1
print("人工（行=目标，列=判断）", human); print("AI", model)
```

结果写进 `docs/intensity-check.md`，然后分两种情况处理：

- **人工也认为 1 级被画重了（人工矩阵第 1 行大多落在第 2 列）** → 生图问题。只改 `emotions.yaml` 里 **8 个常用情绪的 level 1**：
  - 用可观察特征来写：嘴闭合或微张、眉毛基本水平、眼睛正常睁开、**无漫符、无泪、无夸张肢体**；
  - prompt_builder 在强度 1 时追加一句 negative：`no exaggerated expression, no manga symbols, no tears`；
  - 只重跑 16 格（8 情绪 × 1 级 × 近景+中景），由**人工**判断，目标是人工判为 1 级的比例 ≥ 60%。
- **人工认为 1 级基本没问题** → 判官问题。强度在质检里永久降级为"仅记录"，README 写明"强度以人工为准"，不再花时间优化判官。

为什么值得做：日常日记以轻微情绪为主（B4 那页平均强度 1.83）。如果 1 级总被画重，整本漫画册会显得很夸张。

---

## C. 提速与渐进显示（直接决定路演能不能现场生成）

### C1 先测量，再优化
`run_pipeline` 记录每个阶段的耗时，写入 `output/<date>/timing.json`，并在 SSE 中推送：

```json
{"storyboard_s": 0, "layout_s": 0, "panels": {"1": {"gen_s": 0, "qc_s": 0, "retries": 0}},
 "export_s": 0, "total_s": 0, "images": 0, "vision_calls": 0}
```

用 short / medium 两篇各跑 2 次，报告里给出耗时分布（时间主要花在哪一段，长尾来自哪一格）。

### C2 生图和质检流水线化
现在是全部格生完再统一质检。改为**一格生完立即提交这格的质检**（共用线程池，`as_completed`），重画也在这格自己的流水线里完成。整页耗时 ≈ 最慢那一格的"生图 + 质检 + 重画"，而不是"所有生图 + 所有质检"之和。

### C3 合并重试层，加单格总超时
- codex 内部重试 3 次、generator `api_retries` 和 bench 外层重试，三层相乘。保留 codex 内部那一层，generator 不再重试，只负责在失败时生成占位图；
- 单格总预算 `generation.panel_timeout_s`（默认 240），超时就用当前最好的一张或占位图，并标记 flagged。

### C4 现场演示档 `--live`
- 最多 4 格（`max_panels=4`），`qc.max_retries=1`；
- 跳过没有主角的格子的完整质检（只查文字）；
- 目标：short 日记 4 格，**≤ 3 分钟**出整页。

### C5 前端渐进显示
- SSE 新增事件 `{"panel_ready": id}`，生成页一出图就把缩略图填进格子；全部完成后再切换为整页；
- 进度条按阶段显示：分镜 → 生图 n/N → 质检 n/N → 排版；
- 现场等待时有画面可看，体验会完全不同。

**验收**：`--live` 下 short 日记连续 3 次 ≤ 3 分钟；medium 默认档比之前快 30% 以上（用 C1 的数据证明）。

---

## D. M20 漫画册（核心亮点）

主张："日积月累，就成了一本以自己为主角的漫画。"漫画册把这句话直接展示出来。**不需要额外生图**。

### D1 数据层：`visdiary/book.py`
- `scan_pages(cfg) -> list[PageEntry]`：遍历 `output/<date>/`（只收合法日期、且有 page.png），从 storyboard 读取 `title / mood / weather`、各格的 `acting`、highlight 格 id；
- 每个日期只有一个"当前版本"；重做的版本放到 `output/_archive/<date>-<n>/`，不出现在漫画册里；
- 缩略图：`output/<date>/thumb.jpg`（整页缩略，宽 480）和 `cover.jpg`（highlight 格居中裁成方图，没有 highlight 就取第 1 格），用 PIL 生成，缓存到 page.png 更新为止。

### D2 情绪数据（零成本）
从各格 acting 统计：
- 每天的主情绪：出现最多的情绪，并列时取强度高的；
- 每天的"心情值"：给 21 种情绪人工定一个 −2…+2 的效价（valence，正负向），写在 `emotions.yaml` 的 `valence` 字段，按强度加权平均；
- 本月的情绪占比和高光格列表。

### D3 网页：新增「漫画册」标签
1. **月历视图**：月份网格，有漫画的日期显示 `cover.jpg`，边框颜色按心情值从冷色到暖色；没有漫画的日期显示灰色"+"，点击跳到写日记；
2. **心情曲线**：月历上方是一条当月心情值折线（纯 SVG，不引入图表库），点击某个点跳到当天；
3. **翻页阅读**：点进某天后左右翻页（键盘 ←/→），顶部显示日期、天气和标题；
4. **"N 天前的今天"**：首页卡片显示 7 天前、30 天前、一年前同日（存在才显示），文案例如"一个月前的今天，你在……"（取那天的标题）；
5. **月度小结**（可选，只调用 1 次 LLM，花 prod 池的 llm_calls）：输入当月各天的标题、mood 和主情绪，输出 3 句话小结，缓存到 `output/_book/<YYYY-MM>.json`。

### D4 导出
`visdiary book export 2026-10 --pdf`：封面（月份 + 当月高光格拼图 + 情绪占比）→ 按日期排列的整页 → 封底小结。用 Playwright 把 HTML 打印成 PDF，A4 竖版，每页一张漫画。网页上放一个"导出本月 PDF"按钮（走 runner）。

### D5 API
- `GET /api/book?month=YYYY-MM` → `[{date, title, mood, weather, main_emotion, valence, thumb, cover}]`
- `GET /api/book/on-this-day?date=YYYY-MM-DD`
- `GET /api/book/{month}/summary`，`POST /api/book/{month}/export`
- 所有参数都走现有白名单校验。

### D6 验收
- 有 14 天以上数据时，月历、曲线、翻页、导出 PDF 全部正常；
- 没有数据时显示引导页；只有 1 天数据时不报错；
- 测试：`scan_pages` 跳过非法目录和 `_archive`，valence 计算，on-this-day 边界情况（闰年 2/29 → 一年前取 2/28）。

---

## E. 演示素材（和 C、D 并行，放后台跑）

漫画册要"看起来满"，至少需要 **2 周、14 篇左右**的漫画。

1. **日记**：14 篇（可基于真实经历脱敏或改写），情绪分布要有起伏：赶 ddl、训练崩、实验跑通、和朋友吃饭、熬夜、交稿、出去玩。每篇 3–6 格，混合轻微和强烈情绪；
2. **"去年今天"**：另写 2–3 篇去年同期的日记（例如 `2025-10-16`）。**界面和 README 里要标注为演示数据**，不要假装是真实记录；
3. **批量生成**：`visdiary batch diaries/demo/*.md --character xiaomali`，串行执行、可断点续跑（已有 page.png 的跳过），走 **dev 池**并记账。预计 14 × 6 张 ≈ 90 张，加上重画约 120 张，时间约 2 小时，可以挂后台；
4. **人工过一遍**：每页看一遍，不满意的格子用网页导演修，这本身也顺便测试了导演功能；
5. 把整个 `output/` 打包成 `demo-pack-<日期>.zip`，演示机器断网时可以直接解压使用。

---

## F. M19 多人物（漫画册完成后再做）

### F1 数据
- 角色库沿用现有的建档流程。**新角色由你提供图片并确认版权**；agent 可以跑建档，但 `approve` 必须由你在 review.html 里完成；
- `character.json` 增加 `aliases: ["室友", "小林"]`（新角色建档时填写；已有角色的这项由你手动添加）；
- `config.yaml` 新增 `cast: [xiaomali, roommate]`，表示本次生成可用的角色。

### F2 分镜
- 拆分镜时给 LLM 一份 cast 清单（id、名字、别名、一句话外貌）；日记里提到的别名映射到 id，没建档的人仍按 extras 处理；
- 规则：**一格最多 2 个已建档角色**；超过 2 人时降级为远景，只保证主角清楚，其他人作为 extras；双人格默认中景或全景，不安排特写。

### F3 生图
- 参考图预算每格最多 4 张：主角 2 张（身份 + 表情），配角 1 张身份图；prompt 中逐张标注"输入图 3 = 角色「小林」身份图"；
- **空间锚定**：每个角色写明位置（`left: 小玛丽, right: 小林`），并把各自的外貌放在各自的位置描述里，减少属性串色（例如发色互换）；
- 配角的表情用简化版（只写 emotion 的英文 + 1 句 face），不叠加完整的五官块。

### F4 质检
- 一次调用，同时给所有出场角色的参考图；checks 按 `(character_id, feature_id)` 双键匹配，schema 中 `QCCheck` 增加 `character_id`；
- `identity_passes` 逐个角色判定，任何一个出场角色的 critical 特征不通过都算失败；重画提示只写失败的那个角色；
- 新增**串色检查**：问判官"A 的发色是否出现在 B 身上"，结果单独计数；
- 表情只盲测主角，配角不测（控制成本）；
- 双人格重画 2 次仍失败 → 降级重画：主角正面，配角改为背影或只露局部。

### F5 双人基准 `bench/duo_bench.py`
- 8 个双人场景 × 2 个景别 = 16 格；
- 指标：两个角色各自的身份通过率、串色率、人工"像不像"（复用 calibrate，每格标两个角色）；
- 负样本：故意交换两人发色生成 6 张，检验串色检查能否抓到；
- 上线门槛：两个角色的人工身份通过率都 ≥ 80%，否则多人物只在演示里展示精选结果，README 写明限制。

---

## G. 演示打磨（最后做）

1. **`visdiary web --demo`**：只读演示模式，读取 demo-pack；生成按钮走 mock，或者需要二次确认后才走真实生成；顶部显示"演示模式"；
2. **3 分钟脚本**：
   - 痛点 20 秒；
   - 打开漫画册月历，展示心情曲线，"一个月前的今天" 40 秒；
   - 翻到一页，讲表情和人设，点开一格改情绪、选候选 60 秒；
   - 技术亮点 40 秒：参考图建档、盲测质检、人工校准 κ，以及"我们发现了自己指标的假象并修正"；
   - 现场生成（可选，先启动，讲完回来看）或播放录屏 20 秒；
3. **备用视频**：完整流程 2 分钟，含现场生成的倍速片段；
4. **断网检查清单**：字体本地化、demo-pack 本地化、无外部 CDN；
5. README 首页换成漫画册截图；`docs/assets/` 补月历图和翻页 GIF。

---

## H. 建议节奏

| 阶段 | 内容 | 产出 |
|---|---|---|
| 1 | A 全部 + B 脚本 | 测试干净；`intensity-check.md` |
| 2 | C1 测量 → C2/C3 → 启动 E 批量生成（后台） | `timing.json` 前后对比 |
| 3 | D 漫画册 + C5 渐进显示 + B 分支修复 | 漫画册可用 |
| 4 | E 人工修图、打包 demo-pack；C4 现场档 | demo-pack；现场档 ≤ 3 分钟 |
| 5 | F 多人物 + duo bench | 双人结果和门槛结论 |
| 6 | G 演示打磨 + 彩排 2 次 | 脚本、录屏、清单 |

**F 不达标时宁可只展示精选结果，也不要让它拖住 G。**

## 交付报告要求
1. 每项附 diff 片段或文件行号；`pytest` 全部输出，以及 A2 的"真实数据未被改动"证明；
2. `timing.json` 的前后对比，`intensity-check.md`；
3. 漫画册截图（月历、曲线、翻页、导出的 PDF 首页）；
4. demo-pack 的清单：日期、格数、生图张数（dev 池）、人工修改格数；
5. 多人物：duo bench 原始 JSON 和人工标注结果；
6. 依然不许修改已有角色的 character.json；新角色只建档，不擅自 approve。
