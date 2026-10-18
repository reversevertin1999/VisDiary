# VisDiary 第七轮开发指南：冻结前的稳定性冲刺

> 适用时间：10/17 18:30 → 24:00（22:00 后只修 bug，24:00 代码冻结并打 tag `techathon-v0`）
> 原则：**不加新功能**。只做"演示会出问题的"和"数字会被评委问住的"这两类修改。
> 每项都标注了预计耗时；超时就砍掉，按优先级往下做。

---

## 0. 第六轮复核结论（总表）

| 项 | 状态 | 说明 |
|---|---|---|
| A1 list_pages 排序/过滤 | ✅ | |
| A2 测试隔离 | ✅ | conftest 双层隔离 + `test_no_real_data_touched`。小风险：若有模块写 `from visdiary.config import find_root`，会绕过 monkeypatch |
| A3 redraw 失败恢复 | ✅ | finally 中用 variant-a 恢复 |
| A4 edit 走 runner | ⚠️ | 后端改了，**前端没跟上**，见 P0-1 |
| A5 variants 2–4 | ⚠️ | 网页端已限制；**CLI `redraw` 默认值仍是 1**，见 P1-6 |
| A6 SSE 抽函数 | ✅ | |
| A7 feedback 条件 | ✅ | 只有情绪或强度真的变了才记录 |
| B 强度核实 | ⚠️ | 结论正确，但文档里的数字写错了，见 P2-2 |
| C1 timing.json | ⚠️ | `qc_s` 恒为 0；`vision_calls` 是估算值，不是实测 |
| C2 流水线 | ⚠️ | 已实现，但**返回的是重画前的旧 meta**，见 P0-3 |
| C3 单格超时 | ❌ | `deadline` 算出来了，但**从没被使用**，见 P0-2 |
| C4 `--live` ≤3 分钟 | ❌ | 实测约 7 分钟，原因见 P0-4 |
| C5 渐进显示 | ✅ | |
| D 漫画册 | ✅ | 月度小结没有接入 UI；PDF 导出只弹路径（P2） |
| E 演示素材 | 待确认 | 需要看 demo-pack 产物 |
| F 多人物 | ❌ | 配角身份 0/16，根因在 prompt，见 P1-4 |
| G `--demo` | ⚠️ | 演示模式下点"生成"会出灰色占位图，见 P1-5 |

---

## P0：今晚必须修（预计 2 小时）

### P0-1 网页"改分镜并重画"流程是断的（15 分钟）

`api_edit` 已改为返回 `{job_id}`，但 `index.html` 的 `applyEdit()` 仍然这样写：

```js
const r = await jpost(`/api/pages/${date}/panels/${pid}/edit`, body);
if (!r.changed.length) alert(...)   // r.changed 是 undefined → TypeError，流程直接中断
doRedraw();                          // 即使不报错，也会和 edit job 抢跑
```

修复方式：等 edit job 结束后再重画，并让 job 的结束事件带上 `changed`。

```js
async function applyEdit(){
  ...
  if (Object.keys(body).length) {
    const r = await jpost(`/api/pages/${date}/panels/${pid}/edit`, body);
    watchJob(r.job_id, $("ed-job"), ok => { if (ok) doRedraw(); else alert("改分镜失败"); });
  } else doRedraw();
}
```

后端：runner 在 `end: done` 事件里附上 `result: {"changed": [...]}`，前端如果拿到空列表就提示"没有实际改动"。

**验收**：用 Playwright 点一次热区 → 改强度 → "改分镜并重画"，弹窗里能出现 2 个候选。

### P0-2 单格超时（C3）没有生效（20 分钟）

`generate_and_qc_all.pipeline_one` 中计算了 `deadline = t0 + panel_timeout_s`，但它没有传给 `_qc_one`。`_qc_one` 内的 `while True` 重画循环完全不看时间，慢的格子会一直重画到 `max_retries` 次。

```python
def _qc_one(cfg, vision, image, char, panel, style_id, outdir, metas,
            front_ref, extras, deadline: float | None = None) -> dict:
    ...
            if retries >= cfg.qc.max_retries:
                meta["flagged"] = True
                break
            if deadline is not None and time.monotonic() > deadline:   # 新增
                meta["flagged"] = True
                meta["timeout"] = True
                break
            retries += 1
```

`pipeline_one` 调用时传入 `deadline=deadline`。另外要确认 codex provider 的单次生图请求本身设置了超时（我这边没读到 provider 代码，**未核实**），否则一次卡住的请求仍会拖住整页。

**测试**：`test_panel_timeout`：mock vision 永远判失败，mock image 每次 sleep 0.5s，`panel_timeout_s=1`，断言这一格 `retries ≤ 2` 且 `timeout=True`。

### P0-3 流水线返回的是旧 meta（15 分钟）

`_qc_one` 重画时执行 `meta = generator.generate_panel(...)`，生成的是**新 dict**，只写到了局部的 `{panel.id: meta}` 和磁盘上；`pipeline_one` 里 `results[panel.id] = meta` 存的仍然是第一次生图时的 meta（`qc=None`、`accepted_key=None`、cache_key 是旧图的）。
影响：run_pipeline 返回的 metas、高光候选、`cached` 统计都在用错误的数据；如果 webapp 读的是返回值而不是磁盘，角标也会出错。

```python
# _qc_one 所有 return 的 dict 里加 "_meta": meta
# pipeline_one 中：
entry = _qc_one(..., deadline=deadline)
meta = entry.pop("_meta", meta)
```

**测试**：`test_pipeline_returns_final_meta`：mock 第一次质检失败、第二次通过，断言返回的 `metas[pid]["cache_key"]` 等于磁盘 meta.json 中的值，并且 `accepted_key` 不为空。

### P0-4 `--live` 降到 3 分钟（40 分钟，含实测）

7 分钟大致由这几部分叠加（需要用 timing.json 验证）：

1. `concurrency: 3` 而 live 最多 4 格 → 生图分**两波**；
2. **高光候选每页都跑**（`highlight_variants: 2`），而且在流水线**之后串行**执行：额外 1 次生图 + 2 次质检；
3. 超时没生效（P0-2），慢格会重画到底；
4. LLM 拆分镜时间（timing 里应单独列出）。

live 档统一改为：

```python
if live:
    cfg.storyboard.max_panels = min(cfg.storyboard.max_panels, 4)
    cfg.generation.concurrency = max(cfg.generation.concurrency, 4)   # 一波生完
    cfg.generation.panel_timeout_s = min(cfg.generation.panel_timeout_s, 120)
    cfg.qc.max_retries = min(cfg.qc.max_retries, 1)
    cfg.qc.highlight_variants = 0                                     # 关掉高光候选
    cfg.qc.scene_only_fast = True
```

同时修正 timing.json：`qc_s` 在 `check_panel` 前后真实计时并累加（加锁）；`vision_calls` 改为读 budget 记账的差值（调用前后各取一次），不再用 `1+retries` 估算。

**验收**：同一篇演示日记跑 3 次 `--live`（每次改一个字以避开快速路径），记录中位数。
- ≤3.5 分钟：现场演示可以 live；
- 超过：现场只演示网页浏览 + 单格导演，live 生成改用录屏（脚本里早就预留了这条退路）。

**把 3 次的 timing.json 留下来，零点给我**，PPT 里"生成耗时"那页要用。

### P0-5 快速路径会返回过期的页面（20 分钟）

现在的逻辑是"日记 mtime 不变 + page.png 存在 → 直接返回旧页"。存在的问题：
- 改了 emotions.yaml、角色、画风、`--live` 格数后，都不会重新生成；
- `.diary_mtime` 在生成成功之前就写入了：如果中途失败，而目录里留着上一次的 page.png，下次会把旧页当成最新结果返回。

改为内容指纹，并且只在导出成功后写入：

```python
def _fingerprint(cfg, diary_path, char_id, style_id) -> str:
    h = hashlib.sha256(Path(diary_path).read_bytes())
    for p in (characters_dir(cfg) / char_id / "character.json",
              cfg.path("styles/emotions.yaml")):
        if p.is_file():
            h.update(p.read_bytes())
    h.update(json.dumps({"style": style_id, "max_panels": cfg.storyboard.max_panels,
                         "image": cfg.provider.image}, sort_keys=True).encode())
    return h.hexdigest()
# 命中条件：(outdir/".fingerprint").read_text() == fp and page.png 存在
# 写入时机：export_png 成功之后
```

再给 `render` 加一个 `--force`（跳过快速路径）。

**测试**：`test_fast_path_invalidated`：跑一次 → 修改临时副本里的 emotions.yaml → 再跑一次，断言重新生成了；`test_fast_path_not_written_on_failure`。

### P0-6 前端在出错时会卡死（15 分钟）

- `startRender` / `genOne` / `generateAll`：`jpost` 抛错（预算不足、409）时，`RENDERING` 一直为 true，按钮永久禁用 → 用 `try/catch` 复位并 alert 错误信息；
- `watchJob` 的 `es.onerror` 也要调用 `onDone(false)`，否则断线后同样卡住；
- `loadUsagePill()` 每次 job 结束都会调用，演示模式下会**重复追加**"演示模式"标签 → 先检查是否已存在再追加；
- `new Date().toISOString().slice(0,10)` 取的是 **UTC 日期**：北京时间 00:00–08:00 会得到前一天。"去年今天"、漫画册默认月份、保存日记的默认日期都受影响，**今晚零点后录屏正好会遇到**。改为：

```js
const localDate = () => { const d = new Date(); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0,10); };
```

---

## P1：今晚应该修（预计 1.5 小时）

### P1-1 高光候选默认关闭，并同步 meta（15 分钟）

- `config.yaml`：`highlight_variants: 0`。候选功能保留在"单格导演"里，由用户手动触发；
- `_highlight_variants` 中 `best != "a"` 时，不要自己 `replace` 文件，直接调用 `director.pick_variant(cfg, outdir, pid, best["variant"])`。它会同步 `cache_key / accepted_key / qc / locked` 并清理候选文件。否则下次 `--reuse` 时 accepted_key 仍指向旧图，选中的图会被悄悄换回去。

### P1-2 `/api/generate-all` 二次确认（15 分钟）

现在点一下就会把所有缺失的日期都用 **prod 池**生成。改为：

```python
@app.post("/api/generate-all")
def api_generate_all(character: str, confirm: int = 0, _=Depends(require_header)):
    pending = _pending_dates()
    if demo_mode: raise HTTPException(403, "演示模式不可批量生成")
    if confirm != len(pending):
        return {"pending": len(pending), "dates": pending,
                "est_images": len(pending) * AVG_PANELS, "need_confirm": True}
    # 确认后：强制 dev 池（与 CLI batch 一致）
```

前端先请求一次 → `confirm("将生成 N 篇，约 M 张图，确定？")` → 再请求一次并带上 `confirm=N`。

### P1-3 月度小结改为 POST + 缓存（10 分钟）

`GET /api/book/summary` 会触发 LLM：一方面消耗 prod 额度，另一方面绕过了 `X-VisDiary` 头的 CSRF 防护（GET 不检查这个头）。
改为 `POST /api/book/{month}/summary`，结果缓存到 `output/_book/{month}.summary.json`；`GET` 只读缓存，没有缓存就返回 404。

### P1-4 多人物：先修 prompt，修不好就降级为"实验性"（30 分钟，限时）

配角身份 0/16 的根因在 `prompt_builder.build_prompt`：

1. `obrief = "; ".join(f.value for f in other.identity[:3])` **只取前 3 条特征**。蓝色蝴蝶结和头侧鳍状结构很可能排在第 4 条以后（请对照配角的 character.json 确认），所以模型根本不知道要画它们；
2. `parts_anchor` 拼好之后**从没写进 prompt**（死代码），"主角在左"的空间锚定实际上没有生效；
3. 配角**没有参考图**，只有文字描述；
4. `"Her expression is simple and calm"` 写死了性别；
5. 质检重画提示 `_identity_fix_note` 只处理 `1..len(char.identity)` 的编号，配角（101 起）的失败**不会生成任何修正提示**，所以重画也救不回来；
6. `director.redraw_panel` 调用 `identity_passes` 时没有传 `cfg/panel`，候选不检查配角。

修复：写入配角的**全部 critical 特征**；把锚定句加入 prompt；`_identity_fix_note` 支持 101+ 编号；如果 provider 支持多张参考图，把配角的 `front_full` 作为第 3 张参考图。改完只跑 **4 格 duo 小样**：
- 配角身份 ≥3/4：可以放 1 张精选双人格进 PPT，并注明"小样 n=4"；
- 否则：README 中把多人物从 ✅ 改为"🧪 实验性（配角身份一致性未达标，0/16）"，现场不演示。

### P1-5 演示模式下的"生成"（10 分钟）

`web --demo` 会把生成降级为 mock，现场点"开始生成"会出现灰色占位图。建议：demo 模式下把生成按钮文字改成"生成（演示模式：示意图）"，或者直接隐藏"一键生成"。主路径用预生成内容，live 环节放录屏。

### P1-6 CLI `redraw` 默认值（5 分钟）

`--variants` 默认是 1 → `range(1, 1)` 为空，只会对原图重新质检一遍，不会生成任何新候选。默认值改为 2，并像网页端一样校验 2–4。

---

## P2：文档与数字（22:00 前完成，约 30 分钟）

### P2-1 README
- 第 5、6 节顺序颠倒了；
- "已知限制"里仍写着"只支持单一主角，多角色、漫画册在后续版本"——漫画册已经做完，多人物应写"实验性"；
- 网页版小节"每一样都会写进 feedback.jsonl"与 A7 不符 → 改为"情绪或强度确实修改时才写入"；
- 表情基准默认格数写的是 24，请与 bench 实际默认值核对；
- 新增"演示模式"和"`--live`"两小节，写清楚各自的耗时和限制。

### P2-2 intensity-check.md
- "离 60% 还差 2 个百分点" → **实际差 10 个百分点**（8/16 = 50%）；
- 补充 Wilson 95% 置信区间：8/16 → **约 [28%, 72%]**，n=16 的样本不足以区分 50% 和 60%；
- 补充权衡：修复后类别 top-1 从 39/48（81%）降到 10/16（63%）。轻微表情本身就更难辨认类别，这一点要写出来，不要藏；
- 人工标注从未给出 1 级，可能存在趋中倾向，写进"局限"。

**emotions.yaml 的 1 级措辞今晚不要改**：一改就会使缓存失效，演示页面和评测数字也会对不上。放进"后续工作"即可。

### P2-3 清理（只删死代码，不改逻辑）
- `checker.identity_passes` 中 `return` 之后那段不可达代码（引用了不存在的 `critical_ids/by_id`）；
- `cli.py` 中多处 `raise typer.Exit(1)` 之后的不可达代码；
- `book.py`：`weather` 从 context.json 读取；PDF 封面的角色名改为从 character.json 读取；删掉未使用的 import；PDF 和小结中的标题做 HTML 转义。

---

## 冻结流程（22:00–24:00）

1. `pytest -q` 全绿（包括本轮新增的 6 个测试）；
2. `visdiary web --demo` 用 Playwright 走一遍冒烟测试：日记 → 漫画 → 点格子改分镜 → 选候选 → 漫画册翻页 → 去年今天 → 导出 PDF；
3. demo-pack 双备份（U 盘 + 网盘），包含预生成的 14 篇日记、页面和 PDF；
4. 一段原始录屏：live 生成全程（真实计时，不剪辑）+ 单格导演；
5. `git tag techathon-v0 && git push --tags`；
6. 零点后**不再提交代码**，文档、PPT、视频交给我。

---

## 零点交接清单（给凛）

- [ ] 项目 zip（代码 + `output/` + `characters/xiaomali` 与 demo + 演示日记；**不要**带 `.venv`、`cache`、`.env`）
- [ ] 整页 PNG ×3–5（最好看的几页）+ 单格导演前后对比截图
- [ ] 3 次 `--live` 的 timing.json
- [ ] 最新评测数字：表情命中率、强度 MAE、首次/最终通过率、串色负样本 6/6、duo 小样结果
- [ ] 队名、成员、分工、提交负责人
- [ ] 小玛丽角色图的来源和授权说明（原创声明要用）
- [ ] 原始录屏文件
- [ ] 赛事群关于"展示时间 15:00 还是 17:00"的确认结果
