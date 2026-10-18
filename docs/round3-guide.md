# VisDiary 第三轮开发指导

> 第二轮结论：表情明显丰富了，但**人物神情和预想不一致**。
> 这个问题一部分是代码缺陷（可以修、可以测），一部分是"角色该怎么演"的偏好问题（需要给用户控制入口）。
> 第三轮主题：**修偏差 + 人设 + 单格导演**，让角色"演得像她自己"。

---

## 0. "神情和预想不一样"算不算项目缺陷？

从日记到画面有三段，每段都可能出偏差：

| 层 | 偏差 | 归属 | 怎么判断 |
|---|---|---|---|
| ① 理解 | 日记 → `emotion / intensity / inner_voice` 选错了 | **项目**（分镜 prompt、规则） | 打开 `storyboard.json`，看情绪标签是不是你想的 |
| ② 执行 | 标签对了，画出来不是这个情绪 | **项目**（参考图、模板、prompt） | 标签对、图不对 |
| ③ 审美 | 画面符合标签，但"她不会这样笑" | 主观偏好，但**项目要提供调整入口** | 图符合标签，只是不像你心里的她 |

结论：不能全部算成用户主观微调。第二轮代码里有几处会**系统性**造成 ① 和 ②（见 §1 的 P0）。③ 才是主观问题，但现在用户除了重写日记以外没有任何调整入口，这是产品缺口，第三轮要补上。

建议先拿 3–5 格"不像"的例子按上表归类，后面修完再对比。

---

## 1. 第二轮代码检查

已核对：`README.md`、`cli.py`、`pipeline.py`、`schemas.py`、`styles.py`、`styles/emotions.yaml`、`storyboard/{prompts,builder,rules}.py`、`render/{prompt_builder,refs,generator,cache}.py`、`qc/checker.py`、`character/builder.py`、`providers/{__init__,codex}.py`。
未读到：`layout/*`、`character/prompts.py`、`render/` 下的 v1 基线 prompt、`tests/*`；`bench/emotion_bench.py` 和 `bench/baseline_expression.py` 按 README 路径读取都返回"文件不存在"，请确认是否已 push，或者是不是被 `.gitignore` 忽略了。

### P0：直接导致神情偏差

**1. "脸和参考图完全一样"的指令还藏在三个地方** —— `providers/codex.py` 的 `CodexImage.generate`、`character/builder.py` 的 `sheet_prompt` 和 `generate_expression_sheet`

**这是最重要的一条。** 第二轮在 `prompt_builder._ref_hint` 里把"只锁身份、不抄表情"写对了，但 codex provider 在发给生图工具前，又**无条件追加**了一句：

```text
已附输入图：这些参考图展示同一个角色，保持脸、发型、瞳色和配饰与参考图完全一致。
```

这句话追加在 prompt 末尾，又是中文，权重往往比前面的英文提示更高，等于把第二轮的修复抵消了。它还把身份图和表情图统称为"同一个角色"，模型分不清哪张管身份、哪张管表情。`roles` 列表也算出来了，但没有用上（死代码）。

设定卡这边，`sheet_prompt` 和 `generate_expression_sheet` 都写着 `keep the face, hair, eye color and accessories identical to the reference image`，12 张表情胸像都会被原图的表情拉回去，表情参考图本身就"不会演"。

修法：
- **provider 不要自己加语义指令**。`ImageProvider.generate` 改成接收 `refs: list[tuple[str, Path]]`（角色 + 路径），provider 只负责写"输入图 1 = 身份图（front_full），输入图 2 = 表情图（bust_sad）"这类编号说明；"锁什么、不锁什么"统一由 `prompt_builder` 决定。
- 表情表 prompt 改成"只锁身份，不抄表情"，改完重新生成一次表情表：

```text
The reference image shows the character's identity only: keep hair style and color,
eye color, face shape and accessories consistent. Do NOT copy its expression or pose;
each cell must show the expression listed for that cell, clearly and distinctly.
```

- 加一个单测：用 mock provider 记录最终发出去的完整 prompt，断言其中不含 `完全一致` 和 `face ... identical`。

**2. ref_view 映射错位** —— `styles/emotions.yaml`

| 情绪 | 当前参考图 | 实际效果 |
|---|---|---|
| 坚定 | `bust_angry` | 容易画成生气 |
| 紧张 | `bust_surprised` | 容易画成惊讶 |
| 感动 | `bust_cry` | 容易画成哭 |
| 期待 | `bust_smile` | 差别被抹平 |
| 无语 | `bust_neutral` | 被中性脸中和 |

表情参考图的影响力往往比文字大，**用错的参考图比不用参考图更糟**。

修法（二选一，也可以都做）：
- 允许 `ref_view: null`：没有对应胸像的情绪只给身份图，表情完全靠 ACTING 文本；
- 补 `bust_determined / bust_nervous / bust_touched / bust_speechless` 4 张胸像，共 16 张。

注意参考图分辨率：codex 生图的原生尺寸一般只有 1024×1024、1024×1536、1536×1024 三种。一张 3×4 表情表每格只有约 340 像素见方，扩到 4×4 后每格只剩约 256 像素，作为参考图太糊。建议改成**每张 2×3（竖版 1024×1536，每格约 512×512），16 张胸像分 3 张表**生成。生图次数从 1 次变成 3 次，但参考图质量好得多，而且一张表画坏了只需重画 6 格。

**3. 情绪模板的肢体动作覆盖了剧情** —— `prompt_builder.acting_block`

`level.body` 每次都会写进 prompt，LLM 写的 `body_en` 只是追加在后面。例如"和室友吃火锅 + 开心 3 级"会拼出 `jumping with joy, both arms thrown up`；"近景 + 大哭 3 级"会拼出 `rolling on the floor`。剧情动作被模板抢走，近景镜头还会被全身动作撑乱构图。

修法：
- `body_en` 非空时**只用 `body_en`**，模板 body 只作为兜底；
- 模板 body 按景别区分：`近景/特写` 只允许上半身和手部动作（手捂嘴、托腮、握拳到胸前），全身动作只用于 `中景/全景/远景`；
- yaml 里几条会让角色变样的描述要改：坚定 3 级 `cape-like coat flaring`（凭空加了披风，和服装冲突）、暴怒 3 级 `sharp teeth, glowing pupils`（容易画成反派）、疲惫 3 级 `melted puddle`（变成非人形）、害羞 3 级"双手捂脸 + 蹲下藏脸"（脸看不见，质检必然判 face not_visible）。

**4. 纯场景格也会被写进表演块** —— `prompt_builder.build_prompt`

`acting_block(...)` 无条件加入 prompt。主角不在画面的格子会写着 `ACTING (most important): 小玛丽 is ...`，后面又写 `no reference images; pure scene.`，模型很可能在纯场景格里画出一个"长得不像她的人"。纯场景格的质检只查文字，查不出来。

另外，`rules._check_shots` 的"强情绪锁近景"也没判断 `p.characters`，纯场景格会被锁成近景。

修法：`if char.id in panel.characters:` 才加表演块；镜头锁定同样只对有主角的格子生效。

### P1：逻辑问题

**5. 预算下调后可能直接崩溃** —— `pipeline.py`

下调格数后调用的是 `rules.validate(...)`，默认 `soft=False`。截断后"平静占比""情绪种类"很容易不达标，会抛出 `EmotionRuleError`，没有人接。修法：这里传 `soft=True`。

**6. 表情质检的结论不可信** —— `qc/checker.py`

- **锚定偏差**：`QC_PROMPT` 先告诉多模态模型"目标情绪：开心"，再让它判断。模型会倾向于说"开心"，`expression_hit_rate` 会偏高。要改成**盲测**：不给目标，让它从 21 个情绪里选 top-2，再由代码比对。
- **只查不够，不查过头**：`detected_intensity < target - 1` 只拦住画得不够的情况。目标 1 级却画成 3 级会被放过，而"演过头"正是神情不像本人的常见原因。改成 `abs(detected - target) <= 1`，目标为 1 级时要求 `detected <= 2`。
- `face_size` 规则只豁免远景和背影，全景、俯视会被误判。只对 `近景/特写` 要求脸要大。
- `_expression_fix_note` 不管失败原因，一律写 `much more exaggerated`。如果失败原因是情绪类别错了或演过头，这句提示会让结果更糟。要按失败类型分别写：类别错 → 强调目标情绪；不够 → 加强；过头 → `more restrained and subtle`；脸小 → `face larger in frame`。
- 近义组：`感动` 在开心组里，可感动经常带眼泪，被判成"哭"就会失败。可以让一个情绪属于多个组；`害羞 / 紧张 / 坚定` 目前不属于任何组，判定过严。

**7. 规则整体推着角色往夸张演** —— `rules.py` / `prompts.py` / `prompt_builder.py`

高光格必须 3 级，平静不能超过 30%，自动选的高光格会被强制设成 3 级（包括"平静"格：3 级平静配上 `exaggerated manga-style, over-the-top`，自相矛盾），每格都带 `expressive manga acting`，分镜 prompt 还写着"情绪要外放"。安静的一天或冷静型角色会被硬推成戏剧化。

修法：这些阈值放进配置，并由 §2 的人设决定（`expressiveness`、`calm_max_ratio`、`highlight_min_intensity`）。`expressive manga acting` 只在 intensity ≥ 2 时加。

**8. 表情冲突清理会误删** —— `rules._strip_emotion_conflicts`

正则没有词首边界，也不处理否定：`unhappy` 会被删成 `un`，`not smiling` 会被删成 `not`，场景里的 `calm lake` 会被删掉 `calm`，`nomad` 里的 `mad` 也会命中。根源是 `prompts.py` 第 9 条仍写着"visual_prompt_en 描述（动作、表情、场景、构图）"，和第 17 条"不要写表情"矛盾，LLM 自然会写表情。

修法：先删掉第 9 条里的"表情"二字；清理时正则加 `\b...\b`，前面 3 个词内有 `not / no / without / un-` 的不删；实在冲突时只 warning，不改文本。

**9. 表情表切图太理想化** —— `character/builder.slice_expression_grid`

切图假设模型画出的是严格等分、没有边距、顺序正确的网格。实际经常有留白、间隙不均、行列错位，甚至顺序错（第 8 格应为 angry，画出来却是笑脸）。一旦标签错位，后面每一格都在用错的参考图。

修法：
- 用投影法找格子：按行列统计接近白色的像素，找出间隙，而不是硬性均分；
- 切完让多模态模型盲判每格的表情，和 `BUST_VIEWS` 的标签比对，不一致的在 review.html 里标红，只 regen 那一格；
- review.html 每格下面显示"预期 / 识别结果"。

**10. 质检重画时生图失败会拖垮整页** —— `qc/checker._qc_one`

`generate_all` 有占位图兜底，但质检路径里的 `generator.generate_panel` 抛出异常后，会一路冒到 `qc_all` 的 `f.result()`，整页失败。修法：捕获异常后保留上一张图并 flagged。

**11. 重跑不可复现，微调成本高** —— `generator` / `pipeline`

- 质检重画后的最终图存在 `attempt>0` 的缓存键下。下次重跑时 `attempt=0` 命中的是**原来那张不合格的图**，又要质检、重画，既浪费额度，结果也可能变。
- 每次 `render` 都会重新拆分镜。只想改一格，整页都会变。

修法：见 §3 M15（复用分镜 + 锁定已接受的格子）。

**12. `_schema_notes` 是模块级全局列表** —— `schemas.py`

- 线程不安全，做网页版并发请求时，不同请求的提示会混在一起；
- 走 `ValidationError` 分支时没有 drain，上一次失败的提示会混进下一次的 warning。

修法：用 pydantic 的 validation context：`Storyboard.model_validate(data, context={"notes": notes})`，在 validator 里通过 `info.context["notes"].append(...)` 记录。

**13. codex 通道把 `ValidationError` 换成了 `RuntimeError`** —— `providers/codex._chat_json`

`schema.model_validate_json` 失败后被包成 `RuntimeError` 抛出，但 `storyboard/builder.py` 只捕获 `ValidationError`。结果是：用 mock 时"带错误原因重试一次"的逻辑能用，**用 codex 时完全不生效**，第一轮修好的 P0 在真实通道上又失效了。CLI 也不捕获 `RuntimeError`，用户会直接看到 traceback。

修法：在 `providers/base.py` 定义 `ProviderOutputError(ValueError)`，保存原始输出和 pydantic 错误列表；builder 和质检都捕获它并重试。CLI 统一捕获 `RuntimeError`，给出友好提示（例如"codex 未登录 / 代理未开"）。

**14. strict schema 把 `dict` 字段清空了** —— `providers/codex.strictify`

`strictify` 给每个 object 强制设置 `additionalProperties: false`。`Storyboard.extras: dict[str, str]` 在 JSON Schema 里本来是 `{"type": "object", "additionalProperties": {"type": "string"}}`，被改写后变成"不允许任何键的空对象"。所以**用 codex 时 extras 永远是 `{}`**，配角固定外貌描述这个功能实际上一直没生效。以后 `CharacterAnalysis`、人设草稿里的 dict 字段也会遇到同样的问题。

修法：给大模型输出用的 schema 不要放 `dict`，改成列表：`extras: list[Extra]`，其中 `Extra(name: str, desc_en: str)`，读入后再转成 dict。加一个单测：遍历所有传给 `chat_json` / `analyze_json` 的 schema，断言 strictify 之后不存在"`properties` 为空且 `additionalProperties: false`"的 object。

**15. 中心裁剪会切掉表情表的头和脚** —— `providers/codex.CodexImage.generate`

生图结果统一按目标比例做中心裁剪。表情表请求的是 3:4，如果原图是 1024×1536（2:3），上下会各裁掉约 5.5%，**第一行的头顶和最后一行的下巴可能被裁掉**；之后 `slice_expression_grid` 还会再裁一次。逐格生图也一样：16:9 的格子从 3:2 原图裁上下，`leave some empty space in the upper part` 留出的空间可能被裁掉，对话气泡就会压到脸上。

修法：
- 设定卡和表情表**不裁剪**，直接在原图上找网格（P1-9）；
- 逐格生图：版面规划时优先使用接近原生比例的格子（1:1、2:3、3:2）；必须裁剪时，用质检返回的 `head_bbox` 决定裁剪位置，避免切到脸。

**16. 生成图路径的提取可能拿错文件** —— `providers/codex.CodexImage.generate`

只要回复里出现的路径是一个存在的文件，就会被当作候选，最后取最后一个。如果 codex 在回复里复述了输入参考图的路径，**参考图就会被当成生成结果**。在 Windows 上，`/\S+\.png` 也匹配不到 `C:\...` 这种路径。

修法：只接受 `GEN_DIR` 下、修改时间晚于本次调用开始时间的文件；找不到路径时，按修改时间在 `GEN_DIR` 里找最新的文件兜底；路径正则同时兼容 Windows 盘符。

### P2

- `providers/codex._run_codex` 内部重试 3 次，`generator` 外层还有 `api_retries` 次，一格最多会调用 3 × api_retries 次，每次都可能真的消耗一次生图额度，但只有成功那次会记账。"未登录""找不到命令"这类非网络错误不应该重试。
- `providers/codex`：`seed` 实际被忽略。README 和 meta.json 里应写明"codex 通道不支持固定 seed"，避免误以为结果可以复现。
- `cli.py`：`character add` 完成后提示"已生成 8 张设定卡"，实际是 3 张全身 + 12 张胸像；`bench` 命令把结果写到相对当前目录的 `bench/results.csv`，生成的图片却写在 `cfg.path("bench")` 下，两者的根目录可能不一致；成本只算了 LLM 和生图，没算质检调用。
- `builder.py`：`EmotionRuleError` 后用 `soft=True` 重跑时，第一次 validate 的修复 warning 已经丢了（rules 是原地修改）；第二次如果遇到硬错误，会把第一次可用的结果也丢掉；`ValidationError` 分支没有记 `llm_calls`。
- `generator.generate_panel`：参数 `attempt` 被内层的 `for attempt in range(api_retries)` 遮蔽。现在还没出 bug，但很容易踩坑，改名为 `api_try`。
- `character/builder.py`：单张 regen 后 `approved` 不会重置，新图没经过复核就直接生效；表情表生图没走重试，也没记账。
- `emotions.yaml` 里的 `shots`、`manpu` 推荐字段代码没用上。可以作为锁定镜头时的候选，以及 LLM 没给漫符时的默认值。

### README

目录结构更新后清楚多了。还有几处需要改：

- `docs/` 下补上 `round3-guide.md`；
- "已知限制"里还写着"**第一轮**只支持单一主角"，应改为"目前只支持单一主角"；
- 特性里写"自动核对角色特征和表情是否到位，不合格自动重画"，但 `qc.expression_strict` 默认是 `false`，表情不合格只记录、不重画，建议改成"身份不合格自动重画；表情不合格默认只记录（`qc.expression_strict: true` 时也重画）"；
- "数据流向与隐私"只列了 `characters/`、`output/`、`cache/`、`.env`，目录结构里标了"不入库"的 `data/`、`bench/results/` 也要写进去。另外 `visdiary bench` 生成的整页漫画写在 `bench/<provider>/<角色>/<日记>/` 下，里面有角色图和日记内容，确认它也在 `.gitignore` 里；
- 目录结构里 `bench/` 列出了 `emotion_bench.py` 和 `baseline_expression.py`，但仓库里读不到这两个文件（见本节开头）；
- "codex 通道……自动重试 3 次"：按现在的实现，实际最多是 3 × `api_retries` 次（见 P2）。

---

## 2. M14 角色人设（Persona）——解决"她不会这样演"

### 2.1 数据结构

```python
class EmotionOverride(BaseModel):
    face: dict[int, str] = {}       # 按强度覆盖五官描述
    body: dict[int, str] = {}
    ref_view: str | None = None

class Persona(BaseModel):
    temperament: str = ""                    # 一句话性格，如 "傲娇，嘴硬心软，很少放声大笑"
    expressiveness: float = 1.0              # 表演幅度 0.5–1.5
    max_intensity: dict[str, int] = {}       # 某些情绪的强度上限，如 {"大笑": 2, "大哭": 2}
    banned: list[str] = []                   # 禁用：情绪名 / 漫符 / chibi，如 ["灵魂出窍", "chibi"]
    signature: list[str] = []                # 习惯动作（英文），如 "crossing arms and looking away when embarrassed"
    overrides: dict[str, EmotionOverride] = {}   # 角色专属演法：她生气是鼓腮，不是吼

class Character(BaseModel):
    ...
    persona: Persona = Persona()
```

### 2.2 生效位置

| 环节 | 怎么用 |
|---|---|
| 分镜 prompt | 贴 `temperament` 和 `signature`，并要求"按这个性格演，不要演成别的角色" |
| rules | 强度先按 `expressiveness` 缩放：`round(i × e)`，再夹到 1–3 和 `max_intensity`；`banned` 中的情绪归到最近的允许情绪；`calm_max_ratio`、`highlight_min_intensity` 按 `expressiveness` 调整 |
| prompt_builder | 描述按优先级合并：`panel 的 face_en / body_en` > `persona.overrides` > `emotions.yaml` |
| refs | `overrides[e].ref_view` 优先 |
| 质检 | 判定"演过头"时也参考 `max_intensity` |

### 2.3 怎么填人设

- 建档时由多模态模型看原图起草一版 `temperament`（例如"眼神偏冷，像是冷静型"），用户修改。起草用的输出 schema 不要带 `dict` 字段（见 P1-14），`overrides` 这类字典只由用户编辑；
- review.html 增加"人设"区块，表情表每格下面可以点"她不会这样"，把这一格写进 `overrides`，或给这个情绪设强度上限；
- **审表情表就是校准人设**：表情表的 12–16 格是角色演技的"标准答案"，用户确认过的表情表比任何文字描述都管用。

验收：
- [ ] 人设为空时，输出和第二轮完全一致（向后兼容，用单测保证）
- [ ] `expressiveness=0.6` 时同一篇日记 3 级格明显变少，`max_intensity` / `banned` 在 storyboard 里一定生效
- [ ] `overrides` 在 meta.json 的 prompt 里能看到，并且优先级正确

---

## 3. M15 单格导演——让用户能微调

### 3.1 复用分镜 + 锁定格子

```bash
visdiary render 2026-10-08.md --character xiaomali --reuse      # 用已有 storyboard.json，不重新拆分镜
visdiary edit output/2026-10-08 --panel 3 --emotion 委屈 --intensity 2 \
    --note "忍着不哭，眼睛看向窗外"                              # 改分镜里的一格
visdiary redraw output/2026-10-08 --panel 3 --variants 2        # 只重画第 3 格，出 2 张候选
visdiary pick output/2026-10-08 --panel 3 --variant b           # 选定后锁定
```

- `panels/NN.meta.json` 增加 `locked: true` 和 `accepted_key`。锁定的格子重跑时直接复用，不再生图、不再质检；
- 质检通过的格子自动记下 `accepted_key`，解决 P1-11 的重跑问题；
- `--note` 写进 `acting.face_en` / `body_en`，作为最高优先级。

### 3.2 在日记里直接写导演提示（可选）

```markdown
## 21:00
晚上和室友去吃了火锅，心情好多了。
<!-- vd: 开心 2, 不要大笑, 想要那种松了口气的笑 -->
```

`diary/parser.py` 解析 `<!-- vd: ... -->`，作为这一条随笔的约束传给分镜 prompt，并在 rules 里强制执行。HTML 注释在普通 Markdown 预览里不显示，不影响日记本身。

### 3.3 高光格出两张候选

高光格默认生成 2 张（`qc.highlight_variants: 2`），质检都通过时在终端或 review 页里二选一，没人选就取质检分高的那张。只多花 1 张图，最重要的那一格就有了选择。

验收：
- [ ] `--reuse` 重跑时，0 次 LLM 调用，锁定格 0 次生图
- [ ] 改一格只重画这一格，其他格的文件哈希不变
- [ ] `<!-- vd: -->` 中的情绪约束 100% 写进 storyboard

---

## 4. M16 偏好学习——越用越像她

所有用户修改都记进 `characters/<id>/feedback.jsonl`：

```json
{"date": "2026-10-08", "panel": 3, "text": "被导师说了两句……",
 "system": {"emotion": "难过", "intensity": 3},
 "user":   {"emotion": "委屈", "intensity": 2, "note": "忍着不哭"},
 "picked_variant": "b"}
```

用法：
1. **few-shot**：拆分镜时按关键词或情绪检索最相近的 3–5 条修改记录，作为"这个角色过去被纠正过的例子"贴进 prompt；
2. **强度校准**：按情绪统计 `user - system` 的平均差。例如"大笑"平均被下调 1 级，就自动更新 `persona.max_intensity`，用户确认后生效；
3. 指标：**每页人工修改格数**。这是比"表情命中率"更接近"像不像她"的指标。

验收：
- [ ] 连续使用 2 周的模拟数据（可以用脚本生成）中，被纠正过的情绪再次出现时，系统输出和用户修正一致的比例明显上升
- [ ] 偏好更新都要用户确认，不悄悄改人设

---

## 5. M17 评测修正

1. 质检改成盲测（§1 P1-6）；
2. **人工标注校准**：你亲自给 30–50 格标"情绪 + 强度 + 像不像她（1–5 分）"，算多模态模型和你的一致率（Cohen's κ，科恩卡帕系数）。κ < 0.4 时质检的表情结论只能参考，不能用来自动重画；
3. bench 要真正提交到仓库，并加入"演过头率"（detected − target ≥ 2 的比例）；
4. 第二轮 vs 第三轮的 A/B：同 3 篇日记，比较表情命中率（盲测）、演过头率、身份通过率、人工"像不像"均分。

验收：bench 一条命令可以复现；README 放一张 v2/v3 对比表。

---

## 6. 之后（沿用第二轮规划）

- **M18 网页版**：FastAPI + 单页；单格编辑器就是 M15 的图形界面（情绪下拉框、强度滑块、备注框、候选二选一）。
- **M19 多角色**：每个角色都有自己的 persona；同框时参考图按"主角 2 张 + 配角 1 张"分配。
- **M20 漫画册 / 去年今天 / 脱敏 / Notion 联动**。

---

## 7. 开发顺序

```text
P0 修复（provider 去掉"完全一致" + 参考图编号说明 → 设定卡 prompt → 重新生成表情表；
        ref_view；body 优先级；纯场景格）
  → P1-13 / 14 / 15 / 16（codex 通道：重试生效、extras 恢复、不裁坏、不拿错图）
  → P1-5 / P1-6（不崩溃 + 质检盲测；先把尺子校准，后面的改进才量得准）
  → M15.1 --reuse + 锁定格（微调的基础，也省额度）
  → M14 人设
  → M15.2–15.3 导演提示 + 高光候选
  → M17 评测
  → M16 偏好学习
  → 其余 P1/P2 → M18 网页
```

黑客松时间紧时，最小可演示版本：**P0 修复 + P1-13/14 + 质检盲测 + `--reuse` / 单格重画 + 人设里的 `temperament` 和 `expressiveness`**。演示时可以现场改一格的情绪再重画，比只展示一页成品更有说服力。
