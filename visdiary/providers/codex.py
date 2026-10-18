"""codex 通道：把本机已登录的 codex CLI 当 provider 用。

- LLM / Vision：`codex exec --output-schema`，ChatGPT OAuth 登录，无需 API Key
- 生图：`codex exec` 触发内置 image_gen 工具，产物在 ~/.codex/generated_images/
- codex 通道不支持固定 seed（后端会忽略）；质检重画靠 attempt 参与缓存键区分

第三轮修正：
- provider 不再追加任何语义指令；参考图只做编号说明（输入图 1 = 身份图…）
- 路径提取只认 GEN_DIR 下、晚于本次调用开始时间的新文件
- 逐格生图宽幅裁剪锚定上方（保住"留空放气泡"的区域）
- 设定卡/表情表请求 crop=False 不裁剪（网格切图自己处理比例）
"""

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from pydantic import BaseModel, ValidationError

from visdiary.providers.base import ProviderOutputError

PROXY = os.environ.get("VISDIARY_PROXY", "http://127.0.0.1:7890")
DEFAULT_MODEL = ""  # 空 = 不指定，用 codex 自己的配置（~/.codex/config.toml）
GEN_DIR = Path.home() / ".codex" / "generated_images"

# 后端抖动的特征：这些错误值得退避重试；未登录/参数错等不该重试（A13：正则匹配 429/5xx）
_TRANSIENT = re.compile(r"\b(429|5\d\d)\b|Reconnecting|worker quit|Transport|"
                        r"timed out|ECONN|EOF|ps/mcp", re.I)


def strictify(schema: dict) -> dict:
    """pydantic JSON Schema -> OpenAI strict schema：补 additionalProperties:false
    和全字段 required，Literal 的 enum 原样保留。"""
    if isinstance(schema, list):
        return [strictify(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    schema = dict(schema)
    if schema.get("type") == "object":
        props = schema.get("properties", {})
        schema["properties"] = {k: strictify(v) for k, v in props.items()}
        schema["required"] = sorted(props)
        schema["additionalProperties"] = False
    if "items" in schema:
        schema["items"] = strictify(schema["items"])
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            schema[key] = [strictify(x) for x in schema[key]]
    if "$defs" in schema:
        schema["$defs"] = {k: strictify(v) for k, v in schema["$defs"].items()}
    return schema


def _run_codex(args: list[str], cwd: Path, prompt: str, timeout: int = 600) -> str:
    """跑一次 codex exec。prompt 走 stdin（参数 "-"），网络抖动退避重试，
    非网络错误（未登录、命令不存在等）直接抛。"""
    env = dict(os.environ)
    if PROXY:
        env["HTTPS_PROXY"] = env["HTTP_PROXY"] = PROXY
    if not shutil.which("codex"):
        raise RuntimeError("找不到 codex 命令，先安装并 codex login")

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            proc = subprocess.run(
                ["codex", "exec", "--skip-git-repo-check", "-s", "read-only", *args, "-"],
                cwd=cwd, env=env, input=prompt, capture_output=True, text=True,
                timeout=timeout)
            if proc.returncode == 0:
                return proc.stdout
            tail = (proc.stderr or proc.stdout)[-800:]
            last_err = RuntimeError(f"codex exec 失败（{proc.returncode}）：{tail}")
            if not _TRANSIENT.search(tail):
                break  # 非网络错误重试无意义
        except subprocess.TimeoutExpired:
            last_err = RuntimeError(f"codex exec 超时（{timeout}s）")
        time.sleep(5 * (attempt + 1))
    raise last_err


def _check_images(images: list[Path], where: str) -> None:
    """A2：调 provider 前核对图片存在。缺图直接抛，不让 codex 静默跳过。"""
    for i, img in enumerate(images, 1):
        if not Path(img).is_file():
            raise FileNotFoundError(f"{where}：第 {i} 张图片不存在：{img}")


def _chat_json(system: str, user: str, schema: type[BaseModel],
               model: str, images: list[Path] | None = None,
               context: dict | None = None) -> BaseModel:
    """跑一次 codex exec，拿结构化输出并 pydantic 校验。
    校验失败抛 ProviderOutputError（保留原文与错误列表，供上层带原因重试）。"""
    images = images or []
    _check_images(images, "chat_json")
    with tempfile.TemporaryDirectory(prefix="visdiary-codex-") as td:
        td = Path(td)
        schema_file = td / "schema.json"
        schema_file.write_text(json.dumps(strictify(schema.model_json_schema()),
                                          ensure_ascii=False), encoding="utf-8")
        out_file = td / "out.txt"
        args = ["--output-schema", str(schema_file), "-o", str(out_file)]
        if model:
            args = ["-m", model] + args
        for img in images or []:
            args += ["-i", str(Path(img).resolve())]
        prompt = f"{system}\n\n{user}"
        _run_codex(args, cwd=td, prompt=prompt)
        raw = out_file.read_text(encoding="utf-8").strip()
    try:
        return schema.model_validate_json(raw, context=context)
    except ValidationError as e:
        raise ProviderOutputError(schema.__name__, raw, e.errors()) from e


class CodexLLM:
    name = "codex"

    def __init__(self, model: str = ""):
        self.model = model  # 空 = codex 默认模型

    def chat_json(self, system: str, user: str, schema: type[BaseModel],
                  context: dict | None = None):
        return _chat_json(system, user, schema, self.model, context=context)


class CodexVision:
    name = "codex"

    def __init__(self, model: str = ""):
        self.model = model

    def analyze_json(self, prompt: str, images: list[Path],
                     schema: type[BaseModel], context: dict | None = None):
        return _chat_json("只按 JSON schema 输出。", prompt, schema, self.model,
                          images=images, context=context)


_ASPECTS = {"1:1": 1.0, "16:9": 16 / 9, "9:16": 9 / 16,
            "3:4": 3 / 4, "4:3": 4 / 3, "2:3": 2 / 3}

_PATH_RE = re.compile(r"([A-Za-z]:[\\/]\S+\.(?:png|jpg|jpeg|webp)|/\S+\.(?:png|jpg|jpeg|webp))")


def _crop_to_aspect(img, target: float):
    """裁到目标比例。宽幅（如 16:9）默认锚定上方，保住上部的留白/气泡区。"""
    from PIL import Image
    w, h = img.size
    cur = w / h
    if abs(cur - target) < 0.01:
        return img
    if cur > target:  # 太宽，水平居中裁
        nw = round(h * target)
        x = (w - nw) // 2
        return img.crop((x, 0, x + nw, h))
    # 太高，垂直裁：锚定上方（保住留给气泡的空区）
    nh = round(w / target)
    return img.crop((0, 0, w, nh))


class CodexImage:
    name = "codex"
    model = "builtin-image_gen"

    def __init__(self, model: str = "", cleanup: bool = True):
        # 生图走 codex 内置 image_gen 工具，model 仅用于记录
        self.model = model or self.model
        self.cleanup = cleanup  # A16：读完删 GEN_DIR 源文件

    def generate(self, prompt: str, refs: list[tuple[str, Path]], aspect: str,
                 seed: int | None = None, crop: bool = True,
                 kind: str = "panel") -> bytes:
        from PIL import Image

        _check_images([p for _r, p in refs], "CodexImage.generate")  # A2
        started = time.time()
        task_note = {"panel": "这是一张漫画分格插画",
                     "sheet": "这是一张角色设定参考图",
                     "expression_sheet": "这是一张角色表情参考表"}[kind]  # A14
        parts = [
            f"用内置 image_gen 工具生成一张图片，{task_note}，"
            "不需要输出任何解释。要求：",
            prompt,
        ]
        if crop:
            parts.append(f"图片比例 {aspect}。")
        with tempfile.TemporaryDirectory(prefix="visdiary-img-") as td:
            td = Path(td)
            args = []
            for i, (role, ref) in enumerate(refs, 1):
                args += ["-i", str(Path(ref).resolve())]
                parts.append(f"输入图 {i} = {role}。")
            parts.append("生成完成后，回复中必须单独一行给出图片文件的绝对路径。"
                         "不要做其他事。")
            out_file = td / "out.txt"
            _run_codex(args + ["-o", str(out_file)], cwd=td, prompt="\n".join(parts))
            text = out_file.read_text(encoding="utf-8") if out_file.exists() else ""

        src = self._extract_path(text, started)
        img = Image.open(src).convert("RGB")
        if crop and aspect in _ASPECTS:
            img = _crop_to_aspect(img, _ASPECTS[aspect])
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        if self.cleanup:  # A16：GEN_DIR 里留着角色相关图片，读完即清
            try:
                src.unlink()
            except OSError:
                pass
        return buf.getvalue()

    @staticmethod
    def _extract_path(text: str, started: float) -> Path:
        """只认回复里写明、且在 GEN_DIR 下、晚于本次调用开始的路径。
        A12：不做"最新文件"兜底——并发时会把别的线程刚生成的图认领过来。"""
        candidates = []
        for m in _PATH_RE.findall(text):
            p = Path(m)
            try:
                if p.is_file() and GEN_DIR in p.resolve().parents \
                        and p.stat().st_mtime >= started - 1:
                    candidates.append(p)
            except OSError:
                continue
        if not candidates:
            raise RuntimeError(f"codex 没有返回新生成的图片路径，回复：{text[:300]}")
        return max(candidates, key=lambda p: p.stat().st_mtime)
