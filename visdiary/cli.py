"""visdiary 命令行入口。"""

import json
from pathlib import Path
from typing import Optional

import typer

from visdiary import budget, providers
from visdiary.character import builder as char_builder
from visdiary.character.store import list_character_ids, load_character
from visdiary.config import Config, load_config
from visdiary.diary.parser import parse_diary
from visdiary.pipeline import run_pipeline
from visdiary.storyboard.builder import build_storyboard
from visdiary.storyboard.rules import StoryboardError
from visdiary.styles import load_presets

app = typer.Typer(help="VisDiary · 把 Markdown 日记画成分格漫画", add_completion=False)


@app.callback()
def main() -> None:
    """VisDiary · 把 Markdown 日记画成分格漫画。"""


def _config(provider: Optional[str]) -> Config:
    cfg = load_config()
    if provider:
        cfg.provider.llm = provider
        cfg.provider.vision = provider
        cfg.provider.image = provider
    return cfg


@app.command()
def storyboard(
    diary: Path = typer.Argument(..., help="Markdown 日记路径"),
    character: str = typer.Option(..., "--character", "-c", help="角色 id"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p",
                                           help="覆盖 config.yaml 的 provider（如 mock）"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="输出目录，默认 output/<日期>/"),
) -> None:
    """只拆分镜，不产生生图费用。"""
    cfg = _config(provider)
    try:
        doc = parse_diary(diary)
        char = load_character(cfg, character)
        known_ids = list_character_ids(cfg)
        if char.id not in known_ids:
            known_ids.append(char.id)

        sb, warnings = build_storyboard(doc, char, known_ids, cfg, providers.get_llm(cfg))
    except (FileNotFoundError, ValueError) as e:
        typer.secho(f"错误：{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except RuntimeError as e:
        typer.secho(f"调用失败：{e}\n（codex 通道：确认 codex login 有效、本机代理 7890 开着）",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
        typer.secho(f"错误：{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except StoryboardError as e:
        typer.secho(f"拆分镜失败：{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    outdir = out or cfg.path("output") / doc.date
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "storyboard.json").write_text(sb.model_dump_json(indent=2), encoding="utf-8")
    (outdir / "storyboard.warnings.json").write_text(
        json.dumps(warnings, ensure_ascii=False, indent=2), encoding="utf-8")

    hl = next(p.id for p in sb.panels if p.weight == "highlight")
    typer.echo(f"分镜完成：{doc.date} 《{sb.title}》 共 {len(sb.panels)} 格，"
               f"高光格 #{hl}，心情：{sb.mood}")
    for w in warnings:
        typer.echo(f"  warning: {w}")
    typer.echo(f"输出：{outdir}")


@app.command()
def render(
    diary: Path = typer.Argument(..., help="Markdown 日记路径"),
    character: str = typer.Option(..., "--character", "-c", help="角色 id"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p",
                                           help="覆盖 config.yaml 的 provider（如 mock）"),
    style: Optional[str] = typer.Option(None, "--style", help="画风 id，默认角色识别画风"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="输出目录，默认 output/<日期>/"),
    reuse: bool = typer.Option(False, "--reuse", help="复用已有 storyboard.json，不重新拆分镜"),
    force: bool = typer.Option(False, "--force", help="跳过快速路径，强制重新生成"),
    dev: bool = typer.Option(False, "--dev", help="走 dev 预算池（不熔断，只记账）"),
    live: bool = typer.Option(False, "--live", help="现场演示档：最多 4 格、重画 1 次、目标 3 分钟"),
) -> None:
    """完整生成一页漫画：分镜 → 版面 → 逐格生图 → 质检 → 排版导出。"""
    cfg = _config(provider)
    if dev:
        cfg.env = "dev"
    if live:  # P0-4 现场档：目标 ≤3 分钟
        cfg.storyboard.max_panels = min(cfg.storyboard.max_panels, 4)
        cfg.generation.concurrency = max(cfg.generation.concurrency, 4)  # 一波生完
        cfg.generation.panel_timeout_s = min(cfg.generation.panel_timeout_s, 120)
        cfg.qc.max_retries = min(cfg.qc.max_retries, 1)
        cfg.qc.highlight_variants = 0   # 关掉高光候选
        cfg.qc.scene_only_fast = True
        typer.echo("⚡ live 档：≤4 格、并发 4、重画 1 次、无高光候选")
    try:
        r = run_pipeline(cfg, diary, character, style, out, reuse=reuse, force=force)
    except (FileNotFoundError, ValueError) as e:
        typer.secho(f"错误：{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except RuntimeError as e:
        typer.secho(f"调用失败：{e}\n（codex 通道：确认 codex login 有效、本机代理 7890 开着）",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
        typer.secho(f"错误：{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except StoryboardError as e:
        typer.secho(f"拆分镜失败：{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except budget.BudgetError as e:
        typer.secho(f"预算不足：{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    typer.echo(f"完成：{r['date']} 《{r['title']}》 {r['panels']} 格"
               f"（{r['cached']} 格命中缓存）")
    for w in r["warnings"]:
        typer.echo(f"  warning: {w}")
    if r["qc"]:
        typer.echo(f"质检：首次通过率 {r['qc']['first_pass_rate']:.0%}，"
                   f"最终 {r['qc']['final_pass_rate']:.0%}")
        if r["qc"]["flagged"]:
            typer.secho(f"  被标记的格子（重画 {cfg.qc.max_retries} 次仍不合格）："
                        f"{r['qc']['flagged']}", fg=typer.colors.YELLOW)
    typer.echo(f"输出：{r['png']}")


@app.command()
def bench(
    providers_csv: str = typer.Option("mock", "--providers",
                                      help="逗号分隔的 provider 列表"),
    characters: str = typer.Option("demo", "--characters", help="逗号分隔的角色 id"),
    diaries: str = typer.Option("diaries/samples/short.md,diaries/samples/medium.md",
                                help="逗号分隔的日记路径"),
) -> None:
    """模型基准测试：每个 provider × 角色 × 日记跑完整流程，输出 bench/results.csv。"""
    import csv

    rows = []
    for provider in providers_csv.split(","):
        for char_id in characters.split(","):
            for diary_path in diaries.split(","):
                cfg = _config(provider)
                outdir = cfg.path("bench") / provider / char_id / Path(diary_path).stem
                try:
                    r = run_pipeline(cfg, Path(diary_path), char_id, outdir=outdir)
                except Exception as e:
                    typer.secho(f"{provider}/{char_id}/{diary_path} 失败：{e}",
                                fg=typer.colors.RED, err=True)
                    continue
                metas = list(r["metas"].values())
                avg_s = sum(m["duration_s"] for m in metas) / len(metas)
                images = sum(1 for m in metas if not m["cache_hit"])
                qc_calls = sum(1 + e["retries"] for e in (r["qc"] or {}).get("panels", []))
                cost = (images * cfg.cost.image + cfg.cost.llm
                        + qc_calls * cfg.cost.vision) / len(metas)
                qc = r["qc"] or {}
                rows.append({
                    "provider": provider, "model": metas[0]["model"],
                    "character": char_id, "diary": Path(diary_path).stem,
                    "panels": r["panels"], "qc_calls": qc_calls,
                    "qc_first_pass": qc.get("first_pass_rate", ""),
                    "qc_final_pass": qc.get("final_pass_rate", ""),
                    "avg_panel_seconds": round(avg_s, 3),
                    "cost_per_panel": round(cost, 4),
                    "cn_score_manual": "",  # 中文理解评分，人工 1–5 分
                })
                typer.echo(f"{provider}/{char_id}/{Path(diary_path).stem}: "
                           f"{r['panels']} 格, 平均 {avg_s:.2f}s/格")

    if not rows:
        typer.secho("没有成功的组合，未生成 results.csv", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    out = cfg.path("bench/results.csv")  # 与渲染产物的根目录一致
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    typer.echo(f"结果写入 {out}")


@app.command()
def edit(
    outdir: Path = typer.Argument(..., help="输出目录，如 output/2026-10-08"),
    panel: int = typer.Option(..., "--panel", help="格号"),
    emotion: Optional[str] = typer.Option(None, "--emotion", help="改情绪"),
    intensity: Optional[int] = typer.Option(None, "--intensity", help="改强度 1-3"),
    note: Optional[str] = typer.Option(None, "--note", help="导演备注（写进 face_en）"),
) -> None:
    """改某格的情绪/强度/备注。改完用 redraw 重画这一格。"""
    from visdiary.director import edit_panel

    cfg = _config(None)
    try:
        changed = edit_panel(cfg, outdir, panel, emotion, intensity, note)
    except (FileNotFoundError, ValueError) as e:
        typer.secho(f"错误：{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except RuntimeError as e:
        typer.secho(f"调用失败：{e}\n（codex 通道：确认 codex login 有效、本机代理 7890 开着）",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
        typer.secho(f"错误：{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.echo(f"第 {panel} 格已改：{'，'.join(changed)}。"
               f"下一步：visdiary redraw {outdir} --panel {panel}")


@app.command()
def redraw(
    outdir: Path = typer.Argument(...),
    panel: int = typer.Option(..., "--panel"),
    variants: int = typer.Option(2, "--variants", help="出几张候选（2-4）"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p"),
    dev: bool = typer.Option(False, "--dev", help="走 dev 预算池"),
) -> None:
    """只重画一格（其他格不动），出候选后用 pick 选定。"""
    from visdiary.director import redraw_panel

    if not 2 <= variants <= 4:
        typer.secho("variants 只允许 2–4", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    cfg = _config(provider)
    if dev:
        cfg.env = "dev"
    try:
        results = redraw_panel(cfg, outdir, panel, variants)
    except (FileNotFoundError, ValueError) as e:
        typer.secho(f"错误：{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except RuntimeError as e:
        typer.secho(f"调用失败：{e}\n（codex 通道：确认 codex login 有效、本机代理 7890 开着）",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
        typer.secho(f"错误：{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    for v in results:
        typer.echo(f"  候选 {v['variant']}: {v['file']} | 身份{'✓' if v['identity_pass'] else '✗'}"
                   f" 表情{('✓' if v['expression_fail'] is None else '✗:' + v['expression_fail'])}")
    typer.echo(f"选定：visdiary pick {outdir} --panel {panel} --variant a")


@app.command()
def pick(
    outdir: Path = typer.Argument(...),
    panel: int = typer.Option(..., "--panel"),
    variant: str = typer.Option(..., "--variant", help="候选字母，如 a / b"),
) -> None:
    """选定候选并锁定该格，重排整页。"""
    from visdiary.director import pick_variant

    cfg = _config(None)
    try:
        png = pick_variant(cfg, outdir, panel, variant)
    except (FileNotFoundError, ValueError) as e:
        typer.secho(f"错误：{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except RuntimeError as e:
        typer.secho(f"调用失败：{e}\n（codex 通道：确认 codex login 有效、本机代理 7890 开着）",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.echo(f"第 {panel} 格已锁定候选 {variant}，页面已更新：{png}")


@app.command()
def batch(
    diary_dir: Path = typer.Argument(..., help="日记目录（diaries/demo）"),
    character: str = typer.Option("xiaomali", "--character", "-c"),
    provider: Optional[str] = typer.Option("codex", "--provider", "-p"),
    force: bool = typer.Option(False, "--force", help="已有 page.png 的也重跑"),
) -> None:
    """E3 批量生成演示素材：串行、断点续跑（有 page.png 跳过）、强制 dev 池。"""
    import os
    os.environ["VISDIARY_ENV"] = "dev"
    cfg = _config(provider)
    cfg.env = "dev"
    files = sorted(diary_dir.glob("*.md"))
    if not files:
        typer.secho(f"目录里没有日记：{diary_dir}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.echo(f"共 {len(files)} 篇（角色 {character}，dev 池）")
    done = skip = failed = 0
    for i, f in enumerate(files, 1):
        from visdiary.diary.parser import parse_diary
        try:
            date = parse_diary(f).date
        except ValueError as e:
            typer.secho(f"[{i}] {f.name} 跳过（{e}）", fg=typer.colors.YELLOW)
            skip += 1
            continue
        outdir = cfg.path("output") / date
        if not force and (outdir / "page.png").is_file():
            typer.echo(f"[{i}/{len(files)}] {date} 已有，跳过")
            skip += 1
            continue
        typer.echo(f"[{i}/{len(files)}] {date} 生成中……")
        try:
            r = run_pipeline(cfg, f, character)
            typer.echo(f"    完成：{r['panels']} 格，质检 "
                       f"{(r['qc'] or {}).get('final_pass_rate', '-')}")
            done += 1
        except Exception as e:
            typer.secho(f"    失败：{e}", fg=typer.colors.RED, err=True)
            failed += 1
    typer.echo(f"批处理结束：成功 {done}，跳过 {skip}，失败 {failed}")


@app.command(name="budget")
def budget_cmd(
    env: Optional[str] = typer.Option(None, "--env", help="查看哪个池（默认两个都看）"),
) -> None:
    """两个预算池当月的用量（prod 熔断 / dev 只记账）。"""
    from visdiary import budget as B

    cfg = _config(None)
    rep = B.report(cfg)
    typer.echo(f"{rep['month']} 用量：")
    for pool in ("prod", "dev"):
        u = rep[pool]
        typer.echo(f"  {pool}: 生图 {u['images']} / LLM {u['llm_calls']} / 质检 {u['vision_calls']}")
    typer.echo(f"  prod 剩余额度：{rep['prod_remaining']} 张"
               f"（budget.monthly_images={cfg.budget.monthly_images}）")


@app.command()
def web(
    port: int = typer.Option(8420, "--port"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p"),
    demo: bool = typer.Option(False, "--demo", help="演示模式：只读 + 生成走 mock"),
) -> None:
    """M18 网页版：本地单页应用（只监听 127.0.0.1）。"""
    import uvicorn
    from visdiary.webapp import create_app

    cfg = _config(provider)
    if demo:  # G1：断网可演示——真实生成一律降级 mock
        cfg.provider.llm = cfg.provider.vision = cfg.provider.image = "mock"
        typer.echo("🎭 演示模式：生成走 mock（预生成内容照常浏览）")
    uvicorn.run(create_app(cfg), host="127.0.0.1", port=port)


character_app = typer.Typer(help="角色建档与定妆")
app.add_typer(character_app, name="character")


@character_app.command("add")
def character_add(
    image: Path = typer.Argument(..., help="角色图路径"),
    name: str = typer.Option(..., "--name", help="角色名"),
    character_id: str = typer.Option(..., "--id", help="角色 id（英文）"),
    style: Optional[str] = typer.Option(None, "--style", help="手动指定画风 id"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p"),
) -> None:
    """分析角色图，建立档案并生成设定卡。"""
    cfg = _config(provider)
    try:
        analysis = char_builder.analyze_character(cfg, providers.get_vision(cfg), image)
    except (FileNotFoundError, ValueError) as e:
        typer.secho(f"错误：{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except RuntimeError as e:
        typer.secho(f"调用失败：{e}\n（codex 通道：确认 codex login 有效、本机代理 7890 开着）",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
        typer.secho(f"错误：{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    presets = load_presets(cfg)
    typer.echo(f"识别画风：{analysis.style}（置信度 {analysis.confidence:.2f}）")
    if not style and analysis.confidence < char_builder.LOW_CONFIDENCE:
        typer.echo("置信度偏低，候选画风：")
        for sid, p in presets.items():
            typer.echo(f"  {sid}: {p.name}")
        style = typer.prompt("请选择画风 id", default=analysis.style)
    if style and style not in presets:
        typer.secho(f"错误：未知画风 {style!r}，候选：{list(presets)}",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    char = char_builder.create_character(cfg, providers.get_image(cfg), image,
                                         character_id, name, analysis, style,
                                         vision=providers.get_vision(cfg))
    used_style = next(iter(char.sheets))
    typer.echo(f"已生成 {used_style} 画风的设定卡（3 张全身 + 16 张表情胸像），"
               f"检查 characters/{char.id}/review.html 后运行："
               f"visdiary character approve {char.id}")


def _resolve_style(char, style: Optional[str]) -> str:
    return char_builder.resolve_style(char, style)


@character_app.command("regen")
def character_regen(
    character_id: str = typer.Argument(...),
    view: str = typer.Option(..., "--view", help="要重画的视图 id，如 bust_cry"),
    style: Optional[str] = typer.Option(None, "--style", help="画风 id，默认识别画风"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p"),
) -> None:
    """只重画设定卡里的一张。"""
    from visdiary.character.prompts import VIEWS
    from visdiary.character.builder import EXPRESSION_SHEET

    cfg = _config(provider)
    try:
        char = load_character(cfg, character_id)
        if view not in VIEWS and view != EXPRESSION_SHEET:
            raise ValueError(f"未知视图 {view!r}，候选：{list(VIEWS)} 或 {EXPRESSION_SHEET}")
        style_id = _resolve_style(char, style)
        char_builder.upsert_sheet(cfg, providers.get_image(cfg), char, style_id, view,
                                  vision=providers.get_vision(cfg))
    except (FileNotFoundError, ValueError) as e:
        typer.secho(f"错误：{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except RuntimeError as e:
        typer.secho(f"调用失败：{e}\n（codex 通道：确认 codex login 有效、本机代理 7890 开着）",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
        typer.secho(f"错误：{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.echo(f"已重画 {character_id} 的 {style_id}/{view}")


@character_app.command("suggest")
def character_suggest(
    character_id: str = typer.Argument(...),
    apply: bool = typer.Option(False, "--apply", help="确认后写进 persona.max_intensity"),
) -> None:
    """根据反馈记录，给出强度上限建议（不悄悄改人设）。"""
    from visdiary.character import feedback as fb

    cfg = _config(None)
    sugg = fb.calibration_suggestions(cfg, character_id)
    if not sugg:
        typer.echo("还没有稳定的偏好模式（至少同一情绪被下调 2 次以上）")
        return
    for emo, lvl in sugg.items():
        typer.echo(f"  建议：{emo} 的 max_intensity = {lvl}")
    if apply:
        if typer.confirm(f"把这些写进 {character_id} 的 persona？"):
            fb.apply_suggestions(cfg, character_id)
            typer.echo("已写入。character.json 里可以随时改。")


@character_app.command("approve")
def character_approve(
    character_id: str = typer.Argument(...),
    style: Optional[str] = typer.Option(None, "--style", help="画风 id，默认识别画风"),
) -> None:
    """确认设定卡，确认后才能用于 render。"""
    cfg = _config(None)
    try:
        char = load_character(cfg, character_id)
        char = char_builder.approve_character(cfg, character_id,
                                              _resolve_style(char, style))
    except (FileNotFoundError, ValueError) as e:
        typer.secho(f"错误：{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except RuntimeError as e:
        typer.secho(f"调用失败：{e}\n（codex 通道：确认 codex login 有效、本机代理 7890 开着）",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
        typer.secho(f"错误：{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.echo(f"{char.name}（{char.id}）已确认，可以用于 render")


if __name__ == "__main__":
    app()

