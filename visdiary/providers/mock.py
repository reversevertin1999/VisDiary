"""Mock providers：离线开发用，不产生任何 API 费用。

- MockLLM：解析分镜提示词里的固定格式，按日记条目生成确定性分镜。
- MockVision：角色分析返回示例档案；质检按图片内容哈希作种子，约 80% 判过，
  专门用来测试重画流程。
- MockImage：用 Pillow 画占位图，背景色按表情区分，图上标注镜头与表情。
"""

import hashlib
import io
import random
import re
from pathlib import Path

from pydantic import BaseModel

from visdiary.config import DEFAULT_ROOT
from visdiary.schemas import EMOTIONS, Extra, QCCheck, QCResult, Storyboard


class MockLLM:
    name = "mock"

    def chat_json(self, system: str, user: str, schema: type[BaseModel],
                  context: dict | None = None) -> BaseModel:
        if schema is Storyboard:
            sb = self._storyboard(user)
            # 与真实通道一致：过一遍 schema 校验，让 context["notes"] 收集归一化提示
            return Storyboard.model_validate(sb.model_dump(), context=context)
        raise NotImplementedError(f"MockLLM 不支持 schema: {schema.__name__}")

    def _storyboard(self, user: str) -> Storyboard:
        from visdiary.schemas import Dialogue, Panel

        name, pid = re.search(r"主角是\s*(.+?)（角色 id：(.+?)）", user).groups()
        min_p, max_p = (int(x) for x in re.search(r"格数范围\s*(\d+)\s*[–-]\s*(\d+)", user).groups())
        hint = int(re.search(r"建议约\s*(\d+)\s*格", user).group(1))
        date = re.search(r"日记日期：(.+)", user).group(1).strip()
        weather = re.search(r"天气：(.+)", user).group(1).strip()
        title = re.search(r"标题：(.*)", user).group(1).strip() or f"{date} 的一天"

        body = user.split("日记内容：", 1)[1]
        entries = re.findall(r"## (\d{1,2}:\d{2}|all)(?:[ \t][^\n]*)?\n(.*?)(?=\n## |\Z)",
                             body, re.S)
        entries = [(t, txt.strip()) for t, txt in entries] or [("all", body.strip())]

        # 日记里出现的熟人给固定外貌描述，保证配角每格长得一样
        extras = [Extra(name=w, desc_en=d) for w, d in _EXTRAS.items() if w in body]

        # M16：识别 few-shot 纠正记录（情绪「X」：系统给过 A 级，她纠正为 Y B 级）
        clamps: dict[str, int] = {}
        for m in re.finditer(r"情绪「(.+?)」：系统给过 \d 级，她纠正为 .+? (\d) 级", user):
            clamps[m.group(1)] = int(m.group(2))

        n = max(min_p, min(hint, max_p))
        shots = ["中景", "近景", "全景", "特写", "远景", "俯视", "背影", "仰视"]
        emos = ["开心", "难过", "期待", "生气", "感动", "惊讶", "坚定", "大笑", "思考", "疲惫"]
        hi_idx = max(range(len(entries)), key=lambda i: len(entries[i][1])) % n

        from visdiary.schemas import Acting
        panels = []
        prev_shot = None
        for i in range(n):
            time, text = entries[i % len(entries)]
            snippet = re.sub(r"\s+", "", text)[:28] or "这一天"
            is_hi = i == hi_idx
            shot = shots[i % len(shots)]
            if shot == prev_shot:  # 高光格改过镜头后，下一格可能撞上
                shot = shots[(i + 1) % len(shots)]
            intensity = 2
            emotion = emos[i % len(emos)]
            if is_hi:
                shot, emotion, intensity = "特写", "大笑", 3
            if shot == "背影":
                intensity = 1  # 背影不配强表情
            if emotion in clamps:  # M16：用户纠正过的情绪按用户强度收敛
                intensity = min(intensity, clamps[emotion])
            prev_shot = shot
            panels.append(Panel(
                id=i + 1,
                weight="highlight" if is_hi else "normal",
                shot=shot,
                scene=text.strip()[:80],
                characters=[pid],
                acting=Acting(emotion=emotion, intensity=intensity,
                              inner_voice=snippet[:20],
                              body_en="natural expressive posture",
                              manpu=["闪光"] if is_hi else []),
                action=snippet,
                visual_prompt_en=f"mock scene {i + 1}: character {pid} in a slice-of-life "
                                 f"moment, story beats from diary entry {time}",
                narration=snippet,
                dialogue=[Dialogue(speaker=name, text=snippet[:28])] if i % 3 == 0 else [],
                source=time,
            ))
        return Storyboard(date=date, title=title, extras=extras,
                          mood=weather if weather != "未知" else "平静", panels=panels)


class MockVision:
    """质检用图片内容哈希作种子：同一张图判定结果固定，换图重新掷骰。"""

    name = "mock"

    def analyze_json(self, prompt: str, images: list[Path], schema: type[BaseModel],
                     context: dict | None = None) -> BaseModel:
        for i, img in enumerate(images, 1):  # A2：缺图直接抛，和真实通道一致
            if not Path(img).is_file():
                raise FileNotFoundError(f"MockVision：第 {i} 张图片不存在：{img}")
        if schema is QCResult:
            return self._qc(prompt, images)
        if schema.__name__ == "CellEmotion":  # 表情表盲校验：mock 认词表第一个
            from visdiary.character.builder import CellEmotion
            m = re.search(r"选最像的两个：(.+?)。", prompt)
            first = m.group(1).split("、")[0] if m else "开心"
            return CellEmotion(detected_emotion=first, detected_top2=[first])
        if schema.__name__ == "CharacterAnalysis":
            from visdiary.character.prompts import CharacterAnalysis
            from visdiary.schemas import Feature
            return CharacterAnalysis(
                identity=[
                    Feature(name="发色", value="银白色"),
                    Feature(name="发型", value="及腰长双马尾，系红色缎带"),
                    Feature(name="瞳色", value="琥珀色"),
                    Feature(name="配饰", value="左耳星星耳坠"),
                    Feature(name="痣", value="右眼角泪痣"),
                    Feature(name="体型", value="娇小"),
                ],
                default_outfit="白色水手服配藏青色百褶裙，红色领结",
                style="anime_cel",
                confidence=0.9,
            )
        raise NotImplementedError(f"MockVision 不支持 schema: {schema.__name__}")

    def _qc(self, prompt: str, images: list[Path]) -> QCResult:
        from visdiary.schemas import ExpressionCheck
        seed = hashlib.sha256()
        for p in images:
            seed.update(p.read_bytes())
        rng = random.Random(seed.digest())
        checklist = re.findall(r"^\d+\. (.+?)：(.+)$", prompt, re.M)
        ok = rng.random() < 0.8
        checks = [
            QCCheck(name=n, feature_id=i, result="pass" if ok else "fail",
                    note="" if ok else "mock: 特征不符")
            for i, (n, _v) in enumerate(checklist, 1)
        ] or [QCCheck(name="mock", result="pass" if ok else "fail")]
        expression = None
        if images:  # 盲测：没有目标情绪可抄，mock 按图种随机给一个
            emo = rng.choice(EMOTIONS)
            expression = ExpressionCheck(detected_emotion=emo,
                                         detected_top2=[emo, "平静"],
                                         detected_intensity=rng.randint(1, 3),
                                         face_size="large")
        return QCResult(images_received=len(images), judgeable=True,
                        character_visible=True, has_text=False, checks=checks,
                        expression=expression)


_ASPECT_SIZES = {
    "1:1": (1024, 1024),
    "16:9": (1600, 900),
    "9:16": (900, 1600),
    "3:4": (960, 1280),
    "4:3": (1280, 960),
    "2:3": (1024, 1536),
}

_EXPR_COLORS = [
    (("laughing", "smiling"), (255, 223, 128)),
    (("crying", "sad"), (142, 172, 205)),
    (("angry",), (224, 122, 95)),
    (("surprised",), (244, 180, 120)),
    (("exhausted", "tired"), (150, 150, 160)),
    (("blushing",), (246, 190, 200)),
    (("thinking",), (170, 190, 180)),
    (("calm",), (200, 214, 190)),
]

_SHOT_KEYWORDS = ["wide shot", "full shot", "medium shot", "medium close-up",
                  "close-up", "high-angle", "low-angle", "back view"]

# 常见配角的固定外貌描述（mock 用）
_EXTRAS = {
    "室友": "the roommate, short black hair, round glasses",
    "同学": "a classmate, shoulder-length brown hair, school uniform",
    "朋友": "a friend, ponytail, casual hoodie",
    "导师": "the advisor, middle-aged, gray shirt, kind eyes",
    "妈妈": "her mother, gentle smile, apron",
    "爸爸": "her father, tall, plaid shirt",
}


class MockImage:
    name = "mock"
    model = "mock-image-v0"

    def generate(self, prompt: str, refs: list[tuple[str, Path]], aspect: str,
                 seed: int | None = None, crop: bool = True,
                 kind: str = "panel") -> bytes:
        from PIL import Image, ImageDraw, ImageFont

        for role, p in refs:  # A2：缺图直接抛，画都没画就报错
            if not Path(p).is_file():
                raise FileNotFoundError(f"MockImage：参考图不存在：{p}")

        w, h = _ASPECT_SIZES.get(aspect, (1024, 1024))
        low = prompt.lower()
        color = next((c for keys, c in _EXPR_COLORS if any(k in low for k in keys)),
                     (210, 210, 210))
        digest = hashlib.sha256((prompt + str(seed)).encode()).hexdigest()
        jitter = int(digest[:2], 16) % 25
        color = tuple(min(255, max(0, c - jitter)) for c in color)

        img = Image.new("RGB", (w, h), color)
        draw = ImageDraw.Draw(img)
        font = self._font(min(w, h) // 12)
        small = self._font(min(w, h) // 24)
        shot = next((k for k in _SHOT_KEYWORDS if k in low), "mock")
        expr = next((k for keys, _c in _EXPR_COLORS for k in keys if k in low), "")
        lines = [(f"MOCK · {shot} {expr}", font), (f"#{digest[:8]}", small)]
        total_h = sum(draw.textbbox((0, 0), t, font=f)[3] for t, f in lines) + 20
        y = (h - total_h) // 2
        for text, f in lines:
            tw = draw.textlength(text, font=f)
            draw.text(((w - tw) / 2, y), text, fill=(40, 40, 40), font=f)
            y += draw.textbbox((0, 0), text, font=f)[3] + 20

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    @staticmethod
    def _font(size: int):
        from PIL import ImageFont
        font_path = DEFAULT_ROOT / "assets/fonts/LXGWWenKai-Regular.ttf"
        try:
            return ImageFont.truetype(str(font_path), size)
        except OSError:
            return ImageFont.load_default()
