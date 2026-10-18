"""Markdown 日记解析：frontmatter + 按 ## HH:MM 时间标题切条。

时间标题兼容中文冒号和尾随小标题：`## 09:30`、`## 9：30`、`## 9:30 早餐` 都行，
时间统一归一成 "HH:MM"。
"""

import datetime as dt
import re
from pathlib import Path

import frontmatter
from pydantic import BaseModel

# 注意只能用 [ \t] 匹配行内空白：\s 会吞掉换行，把下一段正文误当小标题
_TIME_HEADER = re.compile(r"^##[ \t]*(\d{1,2})[:：](\d{2})(?:[ \t]+(.+?))?[ \t]*$", re.M)
_DATE_IN_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})")
_DATE_FORMAT = re.compile(r"\d{4}-\d{2}-\d{2}")
_DIRECTIVE = re.compile(r"<!--\s*vd:\s*(.+?)\s*-->", re.S)  # 导演提示，预览不显示


class DiaryEntry(BaseModel):
    time: str                 # "09:30"，无时间标题时为 "all"
    label: str | None = None  # 时间标题后面的小标题，如 "早餐"
    text: str
    chars: int
    directive: dict | None = None  # <!-- vd: ... --> 解析出的导演提示


class Diary(BaseModel):
    date: str                 # 统一 YYYY-MM-DD，保证能安全用作目录名
    weather: str | None = None
    title: str | None = None
    entries: list[DiaryEntry]
    char_count: int           # 正文非空白字符数
    path: str


def _count(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def _parse_directive(text: str) -> tuple[str, dict | None]:
    """解析 <!-- vd: 开心 2, 不要大笑, 想要那种松了口气的笑 --> 这类导演提示。"""
    from visdiary.schemas import EMOTIONS, normalize_emotion

    m = _DIRECTIVE.search(text)
    if not m:
        return text, None
    body = (text[: m.start()] + text[m.end():]).strip()
    raw = m.group(1)
    d: dict = {"raw": raw, "emotion": None, "intensity": None, "ban": [], "note": ""}
    notes = []
    for part in re.split(r"[，,;；]", raw):
        part = part.strip()
        if not part:
            continue
        mm = re.match(r"^(.+?)\s*([123])\s*$", part)
        if mm and normalize_emotion(mm.group(1)) in EMOTIONS:
            d["emotion"] = normalize_emotion(mm.group(1))
            d["intensity"] = int(mm.group(2))
        elif part in EMOTIONS:
            d["emotion"] = part
        elif part.startswith(("不要", "别")):
            ban = normalize_emotion(re.sub(r"^(不要|别)", "", part).strip())
            if ban in EMOTIONS:
                d["ban"].append(ban)
            else:
                notes.append(part)
        else:
            notes.append(part)
    d["note"] = "，".join(notes)
    return body, d


def _normalize_date(raw, path: Path) -> str:
    if raw is None:
        m = _DATE_IN_NAME.search(path.name)
        if not m:
            raise ValueError(f"{path} 缺少 frontmatter date，文件名里也没有日期")
        return m.group(1)
    if isinstance(raw, dt.datetime):
        raw = raw.date()
    date = raw.isoformat() if isinstance(raw, dt.date) else str(raw).strip()
    if not _DATE_FORMAT.fullmatch(date):
        raise ValueError(f"{path} 的日期格式应为 YYYY-MM-DD：{date!r}")
    return date


def parse_diary(path: str | Path) -> Diary:
    path = Path(path)
    post = frontmatter.load(path)
    date = _normalize_date(post.get("date"), path)

    body = post.content.strip()
    matches = list(_TIME_HEADER.finditer(body))
    entries: list[DiaryEntry] = []
    if matches:
        preamble = body[: matches[0].start()].strip()
        if preamble:
            t, d = _parse_directive(preamble)
            entries.append(DiaryEntry(time="all", text=t, chars=_count(t), directive=d))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            text = body[m.end():end].strip()
            t, d = _parse_directive(text)
            time = f"{int(m.group(1)):02d}:{m.group(2)}"
            entries.append(DiaryEntry(time=time, label=m.group(3) or None,
                                      text=t, chars=_count(t), directive=d))
    else:
        t, d = _parse_directive(body)
        entries.append(DiaryEntry(time="all", text=t, chars=_count(t), directive=d))

    return Diary(
        date=date,
        weather=post.get("weather"),
        title=post.get("title"),
        entries=entries,
        char_count=_count(body),
        path=str(path),
    )
