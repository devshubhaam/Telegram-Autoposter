"""Pure helpers: time maths, text parsing, formatting. No I/O in here."""
from __future__ import annotations

import hashlib
import html
import random
import re
from datetime import UTC, datetime, time, timedelta
from typing import Any, Iterable

from .config import SETTINGS, TZ

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
MAX_TEXT_LEN = 4096
MAX_CAPTION_LEN = 1024
MAX_BUTTON_LABEL = 64
INVISIBLE = "⁣"

REACTION_EMOJIS = ["😍", "👍", "🤫"]

DEFAULT_OPTIONS: dict[str, Any] = {
    "loop": True,            # False => "one pass": pause once every post has been published
    "dedupe": True,          # skip duplicate uploads (same media / same text)
    "notify_cycle": False,   # DM admins whenever a full cycle completes
    "auto_delete_minutes": 0,  # 0 = never delete published posts
    "footer": None,          # HTML footer appended to every post
    "jitter_minutes": 0,     # random delay (0..N min) added to each slot so timing looks organic
}

AUTO_DELETE_PRESETS = [(0, "Off"), (60, "1 hour"), (360, "6 hours"), (720, "12 hours"), (1440, "24 hours"), (2880, "48 hours"), (10080, "7 days")]

_TAG_RE = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(dt: datetime | None) -> datetime | None:
    """Mongo/JSON may hand back naive datetimes; they are always UTC in this app."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def format_dt(dt: datetime | None) -> str:
    dt = aware(dt)
    if not dt:
        return "—"
    return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M %Z")


def format_ago(dt: datetime | None) -> str:
    dt = aware(dt)
    if not dt:
        return "never"
    secs = int((utcnow() - dt).total_seconds())
    if secs < 0:
        return "in the future"
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{round(secs / 60)} min ago"
    if secs < 172800:
        return f"{round(secs / 3600)} h ago"
    return f"{round(secs / 86400)} d ago"


def format_minutes(minutes: int) -> str:
    if minutes <= 0:
        return "off"
    if minutes % 1440 == 0:
        d = minutes // 1440
        return f"{d} day{'s' if d != 1 else ''}"
    if minutes % 60 == 0:
        h = minutes // 60
        return f"{h} h"
    return f"{minutes} min"


def parse_hhmm(value: str) -> time:
    try:
        hour, minute = str(value).strip().split(":", 1)
        return time(hour=int(hour), minute=int(minute))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Time must be HH:MM, e.g. 09:30 (got {value!r})") from exc


def parse_days(value: str) -> list[int]:
    try:
        result = sorted({int(part.strip()) for part in str(value).split(",") if part.strip()})
    except ValueError as exc:
        raise ValueError("Days must be comma separated weekday numbers 0-6 (0=Mon … 6=Sun)") from exc
    if not result or any(day not in range(7) for day in result):
        raise ValueError("Days must be comma separated weekday numbers 0-6 (0=Mon … 6=Sun)")
    return result


def parse_duration_minutes(value: str) -> int:
    """'90', '90m', '2h', '1d' -> minutes."""
    raw = str(value).strip().lower()
    match = re.fullmatch(r"(\d+)\s*([mhd]?)", raw)
    if not match:
        raise ValueError(f"Duration must look like 90, 90m, 2h or 1d (got {value!r})")
    amount, unit = int(match.group(1)), match.group(2) or "m"
    return amount * {"m": 1, "h": 60, "d": 1440}[unit]


def day_labels(days: Iterable[int]) -> str:
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    days = sorted(set(days))
    if days == list(range(7)):
        return "Every day"
    if days == [0, 1, 2, 3, 4]:
        return "Mon–Fri"
    if days == [5, 6]:
        return "Sat, Sun"
    return ", ".join(names[d] for d in days if 0 <= d < 7) or "—"


def default_schedule() -> dict[str, Any]:
    return {
        "enabled": True,
        "interval_minutes": SETTINGS.default_interval_minutes,
        "batch_size": SETTINGS.default_batch_size,
        "start_time": SETTINGS.default_start_time,
        "end_time": SETTINGS.default_end_time,
        "days": list(SETTINGS.default_days),
    }


def chat_opts(chat: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_OPTIONS)
    if chat:
        merged.update({k: v for k, v in (chat.get("options") or {}).items() if k in DEFAULT_OPTIONS})
    return merged


def compute_next_run(after_utc: datetime, schedule: dict[str, Any], jitter_minutes: int = 0) -> datetime | None:
    """First allowed slot strictly after `after_utc`, honouring days/window/interval.
    Overnight windows (end < start) are supported and belong to the weekday they start on."""
    if not schedule.get("enabled", True):
        return None
    allowed_days = set(schedule.get("days") or range(7))
    interval = max(1, int(schedule.get("interval_minutes", 180)))
    try:
        start_t = parse_hhmm(schedule.get("start_time", "00:00"))
        end_t = parse_hhmm(schedule.get("end_time", "23:59"))
    except ValueError:
        start_t, end_t = time(0, 0), time(23, 59)
    after_local = after_utc.astimezone(TZ)

    for offset in range(-1, 15):
        day = after_local.date() + timedelta(days=offset)
        if day.weekday() not in allowed_days:
            continue
        start_dt = datetime.combine(day, start_t, tzinfo=TZ)
        end_dt = datetime.combine(day, end_t, tzinfo=TZ)
        overnight = end_dt < start_dt
        if overnight:
            end_dt += timedelta(days=1)
        elif offset == -1:
            continue  # yesterday's same-day window can never contain a slot after "now"
        slot = start_dt
        while slot <= end_dt:
            if slot > after_local:
                result = slot.astimezone(UTC)
                if jitter_minutes > 0:
                    cap = min(jitter_minutes * 60, interval * 30)  # never more than half an interval
                    result += timedelta(seconds=random.uniform(0, cap))
                return result
            slot += timedelta(minutes=interval)
    return None


def chat_next_run(chat: dict[str, Any], after_utc: datetime) -> datetime | None:
    return compute_next_run(after_utc, chat.get("schedule", {}) or default_schedule(), int(chat_opts(chat)["jitter_minutes"]))


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #
def strip_html(text: str | None) -> str:
    return html.unescape(_TAG_RE.sub("", text or ""))


def visible_len(text: str | None) -> int:
    return len(strip_html(text))


def trim(text: str | None, limit: int = 42) -> str:
    if not text:
        return "(empty)"
    text = " ".join(strip_html(text).split())
    if not text:
        return "(empty)"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def progress_bar(done: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "░" * width
    filled = min(width, round((done / total) * width))
    return "█" * filled + "░" * (width - filled)


def spark_bar(value: int, peak: int, width: int = 10) -> str:
    if peak <= 0 or value <= 0:
        return ""
    return "▇" * max(1, round(value / peak * width))


def text_fingerprint(text: str | None) -> str:
    norm = " ".join(strip_html(text).lower().split())
    return "t:" + hashlib.sha1(norm.encode("utf-8")).hexdigest()


def normalize_url(raw: str) -> str | None:
    url = raw.strip()
    if not url:
        return None
    if url.lower().startswith(("http://", "https://", "tg://")):
        return url
    if url.lower().startswith(("t.me/", "telegram.me/")) or re.match(r"^[\w-]+(\.[\w-]+)+(/\S*)?$", url):
        return "https://" + url
    if url.startswith("@") and len(url) > 1:
        return "https://t.me/" + url[1:]
    return None


FLAG_TAGS = {
    "#silent": "silent",
    "#protect": "protect_content",
    "#spoiler": "spoiler",
    "#pin": "pin",
    "#nofooter": "nofooter",
}


def parse_post_markup(raw: str | None) -> tuple[str, dict[str, Any]]:
    """Splits user text into (body, flags). Supports #silent #protect #spoiler #pin #nofooter
    and a trailing ---buttons--- section: `Label|https://url ; Label2|https://url2` per row."""
    text = (raw or "").replace("\r\n", "\n").strip()
    flags: dict[str, Any] = {"silent": False, "protect_content": False, "spoiler": False, "pin": False, "nofooter": False, "buttons": []}
    if not text:
        return "", flags
    content_lines: list[str] = []
    button_mode = False
    button_lines: list[str] = []
    for line in text.split("\n"):
        clean = line.strip()
        lowered = clean.lower()
        if lowered == "---buttons---":
            button_mode = True
            continue
        if button_mode:
            if clean:
                button_lines.append(clean)
            continue
        if lowered in FLAG_TAGS:
            flags[FLAG_TAGS[lowered]] = True
            continue
        content_lines.append(line)

    buttons: list[list[dict[str, str]]] = []
    for line in button_lines:
        row: list[dict[str, str]] = []
        for part in [p.strip() for p in line.split(";") if p.strip()]:
            if "|" not in part:
                continue
            label, url = [x.strip() for x in part.split("|", 1)]
            url = normalize_url(url) if url else None
            if label and url:
                row.append({"text": label[:MAX_BUTTON_LABEL], "url": url})
        if row:
            buttons.append(row[:8])
    flags["buttons"] = buttons[:10]
    return "\n".join(content_lines).strip(), flags


def parse_schedule_text(text: str) -> dict[str, Any]:
    """Parses the advanced `key=value` schedule editor. Missing keys keep sensible defaults."""
    data: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            data[key.strip().lower()] = value.strip()
    if not data:
        raise ValueError("Nothing to apply. Send lines like `interval=180`, `batch=5`, `start=09:00`, `end=21:00`, `days=0,1,2,3,4,5,6`.")
    interval = parse_duration_minutes(data["interval"]) if "interval" in data else SETTINGS.default_interval_minutes
    try:
        batch = int(data.get("batch", SETTINGS.default_batch_size))
    except ValueError as exc:
        raise ValueError("batch must be a whole number") from exc
    start = data.get("start", SETTINGS.default_start_time)
    end = data.get("end", SETTINGS.default_end_time)
    days = parse_days(data.get("days", ",".join(map(str, SETTINGS.default_days))))
    parse_hhmm(start)
    parse_hhmm(end)
    if interval < 1 or batch < 1:
        raise ValueError("Interval and batch must be positive")
    if batch > 50:
        raise ValueError("Batch size can be at most 50")
    return {"interval_minutes": interval, "batch_size": batch, "start_time": start, "end_time": end, "days": days}


def compose_text(body: str | None, footer: str | None, skip_footer: bool = False) -> str:
    body = (body or "").strip()
    footer = (footer or "").strip()
    if skip_footer or not footer:
        return body
    return f"{body}\n\n{footer}" if body else footer


def coll_suffix(chat_id: int) -> str:
    return f"m_{abs(chat_id)}" if chat_id < 0 else f"p_{chat_id}"
