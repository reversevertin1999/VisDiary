# VisDiary 第二轮开发指导

> 第一轮结论：主链路能跑通，角色一致性还可以，**主要短板是人物神态表情不够丰富**。
> 第二轮先修几个会直接影响出图的 bug，再把"表情"从 1 个词升级成一套系统，最后再做扩展功能。

---

## 0. 第一轮遗留问题（先修）

### P0：会直接影响画面

| # | 位置 | 问题 | 修法 |
|---|---|---|---|
| 1 | `render/prompt_builder.py` `ref_hint` | 原文是 `keep the face, hair, eye color and accessories identical`。"face identical" 会被模型理解成**连表情一起照抄参考图**，参考图大多是正脸平静或微笑，所以每格表情都往参考图上靠。**这是表情单调的直接原因之一。** | 改为只锁身份、放开表情（见 §1.4） |
| 2 | `qc/checker.py` + `render/cache.py` | 质检重画时 `fix_note` 如果和上一次一样（同一特征连续失败很常见），prompt 不变 → cache key 不变 → **直接命中缓存，拿回同一张图**，第 2 次重画等于白跑。`seed` 也是固定的。 | 重画时把 `attempt` 写进 cache key，并且不读缓存：`cache_key(..., seed, attempt)`；`generate_panel(..., attempt=retries)`；`attempt>0` 时跳过 `cache.get` |
| 3 | `storyboard/rules.py` 相邻镜头去重 | `next(s for s in SHOTS if s not in used)` 按列表顺序取，几乎总会选到**远景**。情绪格被改成远景，脸只剩几个像素，表情自然没了。 | 按"景别距离"选最近的替代镜头，情绪强的格子不准改成远景/背影（见 §1.5） |

### P1：逻辑问题

| # | 位置 | 问题 | 修法 |
|---|---|---|---|
| 4 | `pipeline.py` | `cfg.storyboard.max_panels = allowed` 直接改了全局配置；bench 连续跑多篇时，后面的日记都会被前一篇的预算上限卡住 | 用 `cfg.model_copy(deep=True)` 或给 `rules.validate` 传一个临时的 `StoryboardConfig` |
| 5 | `pipeline.py` | 预算下调后若 `allowed < min_panels`，`rules.validate` 抛 `StoryboardError`，没人接，CLI 直接崩 | `allowed = max(allowed, cfg.storyboard.min_panels)`，并在 warning 里说明"预算不足，按下限生成" |
| 6 | `pipeline.py` / `qc/checker.py` | `budget.record(llm_calls=1)` 没算 builder 的重试；质检的多模态调用完全没记账 | builder 返回实际调用次数；`check_panel` 每次调用 `budget.record(vision_calls=1)` |
| 7 | `schemas.py` | 表情只有 10 个 `Literal`，大模型写"无奈""委屈""得意"就校验失败重试，重试时模型会往最保守的"平静/开心"上收 | 第二轮整体换成 §1.1 的情绪结构；过渡期至少把未知值归一到最近类别而不是报错 |
| 8 | `storyboard/rules.py` | `visual_prompt_en` 里写的表情可能和 `expression` 字段矛盾（字段写"哭"，英文写 smiling），没有检查 | 加关键词冲突检查（英文情绪词表），冲突时以字段为准并 warning |

### P2：体验优化

- `rules.py`：没有高光格时选"对白最多"的格子 → 改为选**情绪强度最高**的格子。
- `prompt_builder.py`：配角 extras 只在 `scene` / `narration` 里匹配人名，`dialogue[].speaker` 没算进去。
- `generator.generate_all`：任何一格失败整页失败。可以先用占位图（灰底 + "第 N 格生成失败"）继续排版，并 flagged，方便补画。
- `qc_all` 是串行的，8 格 × 多模态调用比较慢，可以和生图一样用线程池。

### 尚未核对的文件

GitHub 接口限流，这几个文件这次没读到，第二轮开始前建议自查（或之后再发我看）：
`render/refs.py`、`character/builder.py`、`providers/codex.py`、`budget.py`、`layout/*`、`tests/*`。
自查重点：refs 在表情没有对应胸像时退回哪张图；codex provider 的 `seed` 是否真的生效（多数后端会忽略 seed，那么 §0-2 的 attempt 更必要）。

---

## 1. M7 表情系统（第二轮核心）

### 1.0 为什么表情不丰富——根因拆解

1. **分镜层**：只给 1 个词、10 选 1，没有强度，没有五官细节、肢体、漫符；大模型倾向写"平静/开心"。
2. **prompt 层**：表情只翻译成 `smiling` / `sad` 一个词，排在很长的画风和身份描述后面，权重很低。
3. **参考图层**：参考图的表情会"泄漏"到生成图，加上 `face identical` 的指令，等于让模型照抄脸。
4. **镜头层**：远景/全景/俯视/背影的格子占比高，加上去重 bug，脸太小。
5. **质检层**：只查身份特征，不查表情；为了通过身份检查重画时，模型更会往参考图靠。

所以只改 prompt 不够，要五层一起改。

### 1.1 数据结构：表情从"一个词"升级成"情绪对象"

```python
# schemas.py
Emotion = Literal[
    "平静", "开心", "大笑", "感动", "得意", "期待",
    "害羞", "思考", "无语", "紧张", "委屈", "难过",
    "哭", "大哭", "生气", "暴怒", "惊讶", "震惊",
    "疲惫", "崩溃", "坚定",
]
Manpu = Literal[  # 漫符（manga symbols）
    "汗滴", "青筋", "腮红线", "闪光", "阴影线", "泪花",
    "集中线", "花背景", "闪电背景", "阴云", "灵魂出窍", "怒气",
]

class Acting(BaseModel):
    emotion: Emotion
    intensity: int = Field(2, ge=1, le=3)        # 1 轻微 / 2 明显 / 3 夸张
    inner_voice: str = ""                          # 主角此刻的内心台词（只给 LLM 自己想清楚用，不上图）
    face_en: str = ""                              # 可选：LLM 自己补充的五官细节
    body_en: str = ""                              # 肢体动作，如 "slumped over the desk, arms dangling"
    manpu: list[Manpu] = []                        # 最多 2 个
    chibi: bool = False                            # Q 版（SD，super deformed），只用于搞笑格

class Panel(BaseModel):
    ...
    acting: Acting
    # 兼容旧 storyboard.json：读到 expression 字段时自动转成 acting
```

- `inner_voice` 很关键：让 LLM **先想主角心里在说什么，再定表情**，比直接选词准确得多。
- 归一化：`_EMOTION_ALIASES` 扩到 40 个左右（"无奈→无语""郁闷→委屈""狂喜→大笑""emo→难过"……），真不认识的归到 `平静` 并 warning，不要抛错。

### 1.2 表情词表：独立成 `styles/emotions.yaml`

每个情绪 × 每个强度写清楚五官、肢体、推荐镜头、推荐漫符、对应参考视图。表情要能调，写在 yaml 里比写死在 Python 里方便。

```yaml
难过:
  ref_view: bust_sad
  shots: [近景, 特写, 中景]
  manpu: [阴影线, 阴云]
  levels:
    1: { face: "slightly downturned mouth, eyes looking down, eyebrows a little raised in the middle",
         body: "shoulders slightly drooped" }
    2: { face: "eyebrows knitted upward, glossy eyes, trembling closed mouth",
         body: "hugging knees, head lowered" }
    3: { face: "eyes brimming with tears about to fall, quivering lips, deep frown",
         body: "curled up, face half buried in arms" }
暴怒:
  ref_view: bust_angry
  shots: [特写, 近景, 仰视]
  manpu: [青筋, 怒气, 集中线]
  levels:
    3: { face: "furious, eyebrows sharply slanted, sharp teeth bared, shadowed eyes with glowing pupils",
         body: "fists clenched and raised, leaning forward" }
崩溃:
  ref_view: bust_shock
  shots: [特写, 中景]
  manpu: [灵魂出窍, 阴影线]
  chibi_ok: true
  levels:
    3: { face: "blank white eyes, mouth open in a wail, face pale with vertical gloom lines",
         body: "collapsed on the floor, limbs splayed" }
```

写词表的原则：
- **具体到五官**：眉毛方向、眼睛形状（含高光/空洞）、嘴形、脸色。`sad` 这种词等于没说。
- **强度要拉开差距**：3 级一定要"漫画式夸张"，否则模型会画成 2 级。
- **肢体和表情同样重要**：远一点的镜头只能靠肢体表达情绪。

### 1.3 prompt：表情前置、独立成块

```text
{style_positive}.
ACTING (most important): {name} is {emotion_en} ({intensity_word}).
  face: {face}{; face_en}
  body: {body}{; body_en}
  manga effects: {manpu_en}
{shot_en}.
{visual}
CHARACTER IDENTITY: {identity}. wearing {outfit}.
single comic panel illustration, expressive manga acting, no text, no speech bubbles, no captions, no watermark, no panel border.
{ref_hint}
```

- 顺序：画风 → **表演** → 镜头 → 画面 → 身份。表演放在身份前面，权重更高。
- `intensity_word`：1=`subtle`，2=`clearly visible`，3=`exaggerated manga-style, over-the-top`。
- 漫符英文映射示例：汗滴 `a big cartoon sweat drop on the side of the head`，青筋 `a cross-shaped anger vein mark on the forehead`，阴影线 `vertical gloom lines over the upper face`，集中线 `radial speed lines in the background`，灵魂出窍 `a small ghost-like soul floating out of the mouth`。
- 画面描述 `visual_prompt_en` 不要再重复写表情，避免冲突（在分镜 prompt 里说明，rules 里查）。

### 1.4 参考图：身份图和表情图分开

**设定卡扩充**：表情胸像从 5 张扩到 12 张左右：
`bust_neutral, bust_smile, bust_laugh, bust_shy, bust_think, bust_sad, bust_cry, bust_angry, bust_surprised, bust_shock, bust_tired, bust_smug`

省钱做法：**一次生成一张 3×4 表情表（expression sheet）**，代码按网格切成 12 张，再让用户在 review.html 里逐张确认，不满意的单张 regen。1 次生图 ≈ 12 张参考图。

**选图规则**（`refs.select_refs`）：

| 情况 | 参考图（≤3 张） |
|---|---|
| 主角在画面里 | ① 身份图：`front_full`（全景/远景）或 `bust_neutral`（近景/特写）② 表情图：`emotions.yaml` 里的 `ref_view` ③ 侧/背视图（仅侧面/背影镜头） |
| intensity = 3 | 只给 ① + ②，**不要再加中性脸**，避免中和夸张表情 |
| 背影 | `back_full` + `front_full`，不给表情图 |

**`ref_hint` 改写**（修 §0-1）：

```text
Reference image 1 shows the character's identity: keep hair style and color, eye color,
face shape and accessories consistent. Reference image 2 only shows the type of expression.
Do NOT copy the pose or the expression from reference image 1; the expression must follow ACTING.
```

### 1.5 镜头规则：给表情留出脸

在 `rules.py` 里加：

1. **景别距离**：`远景(0) 全景(1) 中景(2) 近景(3) 特写(4)`，`俯视/仰视/背影` 单独处理。去重时选距离原镜头最近、且不和前后格重复的那个。
2. **强情绪锁近景**：`intensity == 3` 或高光格 → 只能是 `近景/特写/仰视`；如果 LLM 给了远景，改成近景并 warning。
3. **背影不配强表情**：`背影` 只允许 `intensity == 1` 或氛围格（主角不在画面中也可）。
4. **情绪节奏**：
   - 6 格以上至少 3 种不同情绪；
   - "平静"不能连续超过 2 格，总占比 ≤ 30%；
   - 高光格 `intensity` 必须是 3；
   - 不满足时不自动改（情绪改错比不改更糟），作为 `StoryboardError` 带原因让 LLM 重试一次，再不行只 warning。

### 1.6 分镜 prompt 补充

在 `USER_TEMPLATE` 规则里加：

```text
12. 每格先写 inner_voice（主角此刻心里那句话），再根据它选 emotion 和 intensity。
13. 这是漫画，不是写实插画：情绪要外放。日常小事用 1–2 级，当天情绪最强的时刻用 3 级。
14. 一页漫画要有情绪起伏，不要整页都是平静或开心。
15. body_en 写主角的肢体动作；远景格尤其要靠肢体表达情绪。
16. 搞笑、自嘲、崩溃的时刻可以用 chibi=true（Q 版）和漫符。
17. visual_prompt_en 只写场景、动作、构图，不要写表情（表情由 acting 字段负责）。
```

并把 `emotions.yaml` 里的情绪名和一句中文说明贴进 prompt，让模型知道可选范围。

### 1.7 质检：加一项"表情是否到位"

```python
class ExpressionCheck(BaseModel):
    detected_emotion: str          # 多模态模型看到的情绪
    detected_intensity: int        # 1–3
    face_size: Literal["large", "medium", "small", "not_visible"]
    note: str = ""

class QCResult(BaseModel):
    ...
    expression: ExpressionCheck | None = None
```

判定规则：
- 情绪类别不一致（允许近义组：哭/大哭/难过，生气/暴怒，惊讶/震惊……）→ fail；
- `detected_intensity < intensity - 1` → fail（例如要 3 级只画出 1 级）；
- `face_size` 为 small / not_visible 且镜头不是远景/背影 → fail。

重画策略：
- 只有表情失败时，`fix_note` 写 `make the expression much more exaggerated: {face}`，并且**参考图减到 1 张身份图 + 1 张表情图**；
- 身份失败和表情失败同时出现时，优先修身份（身份错了整页都不能用），表情只 warning；
- 表情失败不算 critical 时可以配置：`qc.expression_strict: false` 时只记录不重画，省额度。

### 1.8 漫符叠加层（可选，效果很好）

模型画漫符不稳定（经常漏掉或画成文字）。可以分两类：
- **让模型画**：眼泪、脸红、汗、阴影线——和脸贴得很紧，叠加不自然。
- **代码叠加**：集中线、闪光、花背景、拟声词（"咚！""哗啦"）——在 HTML/SVG 层画，清晰可控，和对话气泡同一套机制。

需要知道脸在哪：质检时顺便让多模态模型返回主角头部的大致框 `head_bbox: [x, y, w, h]`（0–1 归一化），叠加层按 bbox 放汗滴/青筋。拿不到 bbox 时只放背景类效果（集中线、闪光）。

### M7 验收标准

- [ ] 旧的 `storyboard.json`（只有 `expression`）能读，自动转成 `acting`
- [ ] `emotions.yaml` 覆盖 21 种情绪 × 3 级，单元测试检查每个情绪都有 `ref_view` 和三级描述
- [ ] 设定卡支持 12 张表情胸像，可以用 3×4 表情表一次生成并自动切图
- [ ] 镜头规则单测：强情绪格不会被改成远景/背影；去重选最近景别
- [ ] 质检报告里有 `expression`，`qc_report.json` 增加 `expression_hit_rate`
- [ ] 用同一篇 medium 日记 A/B 对比：
  - 每页不同情绪数 ≥ 3（第一轮基线先测出来）
  - 表情命中率（多模态模型判定类别一致）≥ 80%
  - 身份通过率不低于第一轮（表情放开了，身份不能变差）

---

## 2. M8 表情基准测试（bench）

改 prompt 很容易"感觉变好了"，要用数字说话。

- `bench/emotion_bench.py`：21 情绪 × 3 强度 × 2 镜头（近景、中景）= 126 格，不走分镜，直接拼 prompt 生图。
- 指标：表情类别命中率、强度命中率、身份通过率、脸部大小分布。
- 额度不够就先跑子集：8 个常用情绪 × 强度 2/3 × 近景 = 16 格。
- 每次改 `emotions.yaml` 或 prompt 模板后跑一遍，结果写进 `bench/results/YYYY-MM-DD.json`，README 放一张对比图。

验收：能输出一张"情绪 × 强度"的网格对比图（HTML 即可），第一轮 prompt 和第二轮 prompt 并排。

---

## 3. 后续功能（M9–M12，按优先级）

### M9 多角色 / 角色库
- `characters/` 下多个角色，日记里用 `@室友小A` 或 `extras` 升级为正式角色。
- 分镜里 `characters` 可以有 2 个已建档角色；参考图按"主角 2 张 + 配角 1 张"分配，总数仍 ≤ 3。
- 质检对每个已建档角色分别核对。
- 验收：两个建档角色同框时，双方身份通过率 ≥ 70%（多人同框本来就难，先定低一点）。

### M10 网页输入
- FastAPI + 单页前端：写日记 / 上传角色图 / 看设定卡 / 一键生成 / 单格重画。
- 生成过程用 SSE（Server-Sent Events，服务器推送）推进度：分镜 → 第 N 格 → 质检 → 完成。
- 单格编辑：可以直接改某格的 emotion / intensity / 对白，然后只重画这一格。**这对表情调整特别有用。**
- 验收：不碰命令行，从写日记到下载 page.png 能走完；单格重画不影响其他格。

### M11 漫画册
- 按月汇总成 PDF：封面（本月出现最多的情绪 + 高光格拼贴）、目录、每天一页。
- "去年今天"：同一天的旧漫画放在页尾小卡片里。
- 验收：`visdiary book 2026-10` 生成带封面和目录的 PDF。

### M12 隐私与联动（可选）
- 脱敏模式：发给大模型前把人名、地点替换成占位符，生成后再换回（对白里也换）。
- Notion 联动：生成后把 page.png 和标题写回 Notion 日记页（Notion API）。

---

## 4. 建议开发顺序

```text
P0 修复（§0 的 1–3）
  → M7.1–7.3 情绪结构 + 词表 + prompt   ← 先跑一次 A/B，确认方向对
  → M7.4 表情表设定卡 + 选图规则
  → M7.5–7.6 镜头规则 + 分镜 prompt
  → M7.7 表情质检
  → M8 bench（建议和 M7 同步做，每步都测）
  → P1/P2 修复
  → M10 网页（黑客松 demo 最直观）
  → M9 / M11 / M12 视时间而定
```

黑客松时间紧时，最小可演示版本是：**P0 修复 + M7.1–7.3 + M7.5**。不需要新增设定卡，就能明显改善表情；表情表设定卡和质检是锦上添花。
