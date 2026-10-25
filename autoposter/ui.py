"""Keyboards and message renderers for the admin control panel."""
from __future__ import annotations

import asyncio
import html
from typing import Any

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup

from . import __version__
from .config import SETTINGS
from .state import STATE
from .utils import (
    AUTO_DELETE_PRESETS,
    REACTION_EMOJIS,
    aware,
    chat_opts,
    coll_suffix,
    day_labels,
    format_ago,
    format_dt,
    format_minutes,
    progress_bar,
    spark_bar,
    trim,
    utcnow,
)

SEP = "━━━━━━━━━━━━━━━━━━"
TARGETS_PER_PAGE = 8
POSTS_PER_PAGE = 6


class ChatMissing(Exception):
    """Raised by renderers when the target was removed while a button for it was still on screen."""


def esc(value: Any) -> str:
    return html.escape(str(value))


def b(label: str, data: str) -> Btn:
    return Btn(label, callback_data=data)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def chat_icon(chat: dict[str, Any]) -> str:
    return "📣" if chat.get("type") == "channel" else "👥"


def handle_of(chat: dict[str, Any]) -> str:
    return f"@{chat['username']}" if chat.get("username") else str(chat["chat_id"])


def target_name(chat: dict[str, Any] | None) -> str:
    if not chat:
        return "No target selected"
    return f"{chat_icon(chat)} {chat.get('title', 'Untitled')} • {handle_of(chat)}"


def post_label(post: dict[str, Any]) -> str:
    media = post.get("media_type", "text")
    icon = {
        "text": "📝", "photo": "🖼️", "video": "🎬", "animation": "✨", "document": "📄", "audio": "🎵",
        "voice": "🎤", "video_note": "⭕", "sticker": "🙂", "album": "🖼️🎬", "forward": "↪️", "copy": "↪️",
    }.get(media, "📦")
    if media == "album":
        count = len(post.get("media_group", []))
        return f"{icon} {trim(post.get('caption') or f'Album ({count} items)', 28)} ({count})"
    if media in ("forward", "copy"):
        return f"{icon} Copied message"
    base = post.get("text") or post.get("caption") or post.get("file_name") or media
    return f"{icon} {trim(base, 28)}"


async def require_chat(chat_id: int) -> dict[str, Any]:
    chat = await STATE.db.get_chat(chat_id)
    if not chat:
        raise ChatMissing(chat_id)
    return chat


async def get_target_chat(admin_id: int) -> dict[str, Any] | None:
    target_chat_id = await STATE.db.get_active_target(admin_id)
    if target_chat_id is None:
        return None
    return await STATE.db.get_chat(target_chat_id)


def back_to_dashboard() -> Markup:
    return Markup([[b("⬅️ Back", "nav:dashboard")]])


def confirm_keyboard(yes_data: str, no_data: str, yes_label: str = "✅ Yes, confirm") -> Markup:
    return Markup([[b(yes_label, yes_data), b("❌ Cancel", no_data)]])


# --------------------------------------------------------------------------- #
# Dashboard / targets
# --------------------------------------------------------------------------- #
def dashboard_keyboard() -> Markup:
    return Markup(
        [
            [b("📊 Dashboard", "nav:dashboard"), b("🧭 Targets", "nav:targets")],
            [b("⬆️ Upload posts", "up:start"), b("📝 Posts", "nav:posts")],
            [b("🗂 Queue", "nav:queue"), b("📅 Schedule", "nav:schedule")],
            [b("📈 Stats", "nav:stats"), b("⚙️ Settings", "nav:settings")],
            [b("📜 Logs", "nav:logs"), b("💾 Backup", "nav:backup")],
            [b("🗃 Database", "nav:database"), b("➕ Add Channel/Group", "target:add")],
        ]
    )


async def dashboard_text(admin_id: int) -> str:
    stats, target = await asyncio.gather(STATE.db.global_stats(), get_target_chat(admin_id))
    attention = f"\n⚠️ Paused targets: <b>{stats['paused']}</b>" if stats["paused"] else ""
    return (
        "🌙 <b>Auto Posting Control Center</b>\n"
        f"{SEP}\n"
        f"🎯 <b>Active target</b>\n{esc(target_name(target))}\n\n"
        f"📣 Channels: <b>{stats['total_channels']}</b>   👥 Groups: <b>{stats['total_groups']}</b>\n"
        f"📝 Total posts: <b>{stats['total_posts']}</b>\n"
        f"📦 Remaining this cycle: <b>{stats['remaining_posts']}</b>\n"
        f"🚀 Published today: <b>{stats['published_today']}</b>\n"
        f"🔁 Cycles completed: <b>{stats['total_cycles_completed']}</b>\n"
        f"⏰ Next post: <b>{esc(format_dt(stats['next_post_time']))}</b>"
        f"{attention}\n\n"
        "Tip: add the bot as admin in a channel/group, open 🧭 Targets, then tap ⬆️ Upload posts."
    )


async def targets_keyboard(page: int = 0) -> Markup:
    chats = await STATE.db.list_chats()
    pages = max(1, -(-len(chats) // TARGETS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    rows: list[list[Btn]] = []
    for chat in chats[page * TARGETS_PER_PAGE : (page + 1) * TARGETS_PER_PAGE]:
        flag = "" if chat.get("is_active", True) else "⏸ "
        rows.append([b(f"{flag}{chat_icon(chat)} {trim(chat['title'], 34)}", f"chat:open:{chat['chat_id']}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(b("◀️", f"nav:targets:{page - 1}"))
        nav.append(b(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(b("▶️", f"nav:targets:{page + 1}"))
        rows.append(nav)
    rows.append([b("➕ Add Channel/Group", "target:add")])
    rows.append([b("⬅️ Back", "nav:dashboard")])
    return Markup(rows)


async def upload_picker_keyboard() -> Markup:
    chats = await STATE.db.list_chats()
    rows = [[b(f"{chat_icon(c)} {trim(c['title'], 36)}", f"up:pick:{c['chat_id']}")] for c in chats[:40]]
    rows.append([b("➕ Add Channel/Group", "target:add")])
    rows.append([b("⬅️ Back", "nav:dashboard")])
    return Markup(rows)


# --------------------------------------------------------------------------- #
# Target panel
# --------------------------------------------------------------------------- #
async def chat_keyboard(chat_id: int) -> Markup:
    chat = await STATE.db.get_chat(chat_id)
    active = bool(chat.get("is_active", True)) if chat else True
    return Markup(
        [
            [b("🎯 Select target", f"chat:pick:{chat_id}"), b("🗂 Queue", f"chat:queue:{chat_id}")],
            [b("⬆️ Upload posts", f"up:pick:{chat_id}"), b("📝 Posts", f"pl:{chat_id}:0")],
            [b("📅 Schedule", f"chat:schedule:{chat_id}"), b("📈 Stats", f"chat:stats:{chat_id}")],
            [b("🔀 Reshuffle", f"chat:reshuffle:{chat_id}"), b("⏭ Post now", f"chat:run:{chat_id}")],
            [b("⚙️ Options", f"chat:options:{chat_id}"), b("↩️ Undo last upload", f"chat:undo:{chat_id}")],
            [b("⏸ Pause" if active else "▶️ Resume", f"chat:toggle:{chat_id}")],
            [b("🧹 Wipe posts", f"chat:wipe:{chat_id}"), b("🗑 Remove target", f"chat:remove:{chat_id}")],
            [b("⬅️ Targets", "nav:targets")],
        ]
    )


def option_summary(chat: dict[str, Any]) -> str:
    o = chat_opts(chat)
    return (
        f"Mode: <b>{'🔁 loop' if o['loop'] else '➡️ one pass'}</b> • Duplicates: <b>{'skip' if o['dedupe'] else 'allow'}</b>\n"
        f"Auto-delete: <b>{esc(format_minutes(int(o['auto_delete_minutes'])))}</b> • Jitter: <b>{'±' + str(o['jitter_minutes']) + ' min' if o['jitter_minutes'] else 'off'}</b>\n"
        f"Footer: <b>{'set' if o['footer'] else 'none'}</b>"
    )


async def render_chat_overview(chat_id: int) -> str:
    chat = await require_chat(chat_id)
    snap = await STATE.db.queue_snapshot(chat_id)
    schedule = chat.get("schedule") or {}
    total = max(snap["total"], 1)
    state = "active ✅" if chat.get("is_active", True) else "paused ⏸"
    reason = f"\n<i>{esc(chat['pause_reason'])}</i>" if chat.get("pause_reason") and not chat.get("is_active", True) else ""
    error = f"\n⚠️ Last error: <code>{esc(trim(chat.get('last_error'), 160))}</code>" if chat.get("last_error") else ""
    return (
        f"{chat_icon(chat)} <b>{esc(chat['title'])}</b>\n"
        f"<code>{esc(handle_of(chat))}</code>\n"
        f"{SEP}\n"
        f"State: <b>{state}</b>{reason}\n"
        f"Cycle: <b>{snap['cycle']}</b>\n"
        f"Progress: <code>{progress_bar(snap['used'], total)}</code> <b>{snap['used']}/{snap['total']}</b>\n"
        f"Queue: <b>{snap['queue_size']}</b> • Remaining: <b>{snap['remaining']}</b>\n"
        f"Every <b>{esc(format_minutes(int(schedule.get('interval_minutes', 180))))}</b> • batch <b>{schedule.get('batch_size', 5)}</b>\n"
        f"Window: <b>{esc(schedule.get('start_time'))} → {esc(schedule.get('end_time'))}</b> • <b>{esc(day_labels(schedule.get('days', [])))}</b>\n"
        f"{option_summary(chat)}\n"
        f"Last posted: <b>{esc(format_ago(snap['last_posted_at']))}</b>\n"
        f"Next run: <b>{esc(format_dt(snap['next_post_time']))}</b>"
        f"{error}"
    )


async def render_queue_text(chat_id: int) -> str:
    chat = await require_chat(chat_id)
    snap = await STATE.db.queue_snapshot(chat_id)
    queue = (chat.get("queue_state") or {}).get("queue", [])
    cursor = snap["current_cursor"]
    upcoming: list[str] = []
    for post_id in queue[cursor : cursor + 10]:
        post = await STATE.db.get_post(chat_id, post_id)
        if post:
            upcoming.append(esc(post_label(post)))
    batch = int((chat.get("schedule") or {}).get("batch_size", 5))
    lines = [f"{'▶️' if i < batch else '•'} {label}" for i, label in enumerate(upcoming)]
    return (
        f"🗂 <b>Queue • {esc(chat['title'])}</b>\n"
        f"{SEP}\n"
        f"Remaining: <b>{snap['remaining']}</b> • Used: <b>{snap['used']}</b>\n"
        f"Cycle: <b>{snap['cycle']}</b> • Cursor: <b>{cursor}/{snap['queue_size']}</b>\n"
        f"Status: <b>{esc(snap['shuffle_status'])}</b>\n"
        f"Last posted: <b>{esc(format_dt(snap['last_posted_at']))}</b>\n"
        f"Next run: <b>{esc(format_dt(snap['next_post_time']))}</b>\n\n"
        f"<b>Up next</b> (▶️ = goes out in the next batch)\n{chr(10).join(lines) if lines else '<i>empty</i>'}"
    )


# --------------------------------------------------------------------------- #
# Schedule (preset menus)
# --------------------------------------------------------------------------- #
SCHEDULE_PRESETS: dict[str, dict[str, Any]] = {
    "int": {"title": "⏱ Interval", "hint": "How often a batch goes out.", "values": [(15, "15 min"), (30, "30 min"), (60, "1 hour"), (120, "2 hours"), (180, "3 hours"), (360, "6 hours"), (720, "12 hours"), (1440, "24 hours")]},
    "bat": {"title": "📦 Batch size", "hint": "Posts published per slot.", "values": [(1, "1"), (2, "2"), (3, "3"), (5, "5"), (8, "8"), (10, "10"), (15, "15"), (20, "20")]},
    "win": {"title": "🕐 Time window", "hint": f"Posting hours ({SETTINGS.timezone}).", "values": [(("00:00", "23:59"), "All day"), (("06:00", "12:00"), "Morning 06–12"), (("09:00", "21:00"), "Day 09–21"), (("12:00", "18:00"), "Afternoon 12–18"), (("17:00", "23:00"), "Evening 17–23"), (("20:00", "02:00"), "Night 20–02")]},
    "day": {"title": "📆 Days", "hint": "Which weekdays are allowed.", "values": [(list(range(7)), "Every day"), ([0, 1, 2, 3, 4], "Mon–Fri"), ([5, 6], "Weekends"), ([0, 2, 4], "Mon/Wed/Fri"), ([1, 3, 5], "Tue/Thu/Sat")]},
    "jit": {"title": "🎲 Random jitter", "hint": "Adds a random 0–N minute delay to every slot so timing looks organic.", "values": [(0, "Off"), (5, "up to 5 min"), (10, "up to 10 min"), (20, "up to 20 min"), (30, "up to 30 min"), (60, "up to 60 min")]},
}


def preset_is_current(key: str, value: Any, chat: dict[str, Any]) -> bool:
    schedule = chat.get("schedule") or {}
    if key == "int":
        return int(schedule.get("interval_minutes", 0)) == value
    if key == "bat":
        return int(schedule.get("batch_size", 0)) == value
    if key == "win":
        return (schedule.get("start_time"), schedule.get("end_time")) == tuple(value)
    if key == "day":
        return sorted(schedule.get("days", [])) == value
    if key == "jit":
        return int(chat_opts(chat)["jitter_minutes"]) == value
    return False


def schedule_keyboard(chat_id: int) -> Markup:
    return Markup(
        [
            [b("⏱ Interval", f"sc:int:{chat_id}"), b("📦 Batch", f"sc:bat:{chat_id}")],
            [b("🕐 Window", f"sc:win:{chat_id}"), b("📆 Days", f"sc:day:{chat_id}")],
            [b("🎲 Jitter", f"sc:jit:{chat_id}"), b("✏️ Advanced (type)", f"chat:edit_schedule:{chat_id}")],
            [b("🗂 Preview queue", f"chat:queue:{chat_id}"), b("⬅️ Target panel", f"chat:open:{chat_id}")],
        ]
    )


async def render_schedule_text(chat_id: int) -> str:
    chat = await require_chat(chat_id)
    s = chat.get("schedule") or {}
    jitter = int(chat_opts(chat)["jitter_minutes"])
    return (
        f"📅 <b>Schedule • {esc(chat['title'])}</b>\n"
        f"{SEP}\n"
        f"⏱ Every <b>{esc(format_minutes(int(s.get('interval_minutes', 180))))}</b>\n"
        f"📦 Batch <b>{s.get('batch_size', 5)}</b> posts\n"
        f"🕐 Window <b>{esc(s.get('start_time', '00:00'))} → {esc(s.get('end_time', '21:00'))}</b> ({esc(SETTINGS.timezone)})\n"
        f"📆 <b>{esc(day_labels(s.get('days', [])))}</b>\n"
        f"🎲 Jitter <b>{'up to ' + str(jitter) + ' min' if jitter else 'off'}</b>\n"
        f"⏰ Next run <b>{esc(format_dt(chat.get('next_post_time')))}</b>\n\n"
        "Tap a setting to change it with one tap. Need something custom? Use ✏️ Advanced and send:\n"
        "<code>interval=2h\nbatch=5\nstart=09:00\nend=21:00\ndays=0,1,2,3,4,5,6</code>"
    )


async def preset_menu(key: str, chat_id: int) -> tuple[str, Markup]:
    chat = await require_chat(chat_id)
    preset = SCHEDULE_PRESETS[key]
    rows: list[list[Btn]] = []
    current_row: list[Btn] = []
    for idx, (value, label) in enumerate(preset["values"]):
        mark = "✅ " if preset_is_current(key, value, chat) else ""
        current_row.append(b(f"{mark}{label}", f"ss:{key}:{chat_id}:{idx}"))
        if len(current_row) == (2 if key in ("win", "day", "jit") else 4):
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    rows.append([b("⬅️ Back", f"chat:schedule:{chat_id}")])
    return f"{preset['title']} • {esc(chat['title'])}\n{SEP}\n{preset['hint']}", Markup(rows)


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #
def options_keyboard(chat: dict[str, Any]) -> Markup:
    cid = chat["chat_id"]
    o = chat_opts(chat)
    return Markup(
        [
            [b(f"🔁 Mode: {'loop' if o['loop'] else 'one pass'}", f"op:loop:{cid}")],
            [b(f"🧬 Duplicates: {'skip' if o['dedupe'] else 'allow'}", f"op:dedupe:{cid}")],
            [b(f"🔔 Cycle alerts: {'on' if o['notify_cycle'] else 'off'}", f"op:notify:{cid}")],
            [b(f"🕓 Auto-delete: {format_minutes(int(o['auto_delete_minutes']))}", f"op:ad:{cid}")],
            [b("✍️ Footer" + (" (set)" if o["footer"] else ""), f"op:footer:{cid}")],
            [b("📌 Send & pin prompt", f"chat:promo:{cid}"), b("🔒 Force-sub gate", f"chat:forcesub:{cid}")],
            [b("⬅️ Target panel", f"chat:open:{cid}")],
        ]
    )


async def render_options_text(chat_id: int) -> tuple[str, Markup]:
    chat = await require_chat(chat_id)
    o = chat_opts(chat)
    footer = f"\n<b>Footer preview</b>\n{o['footer']}" if o["footer"] else ""
    text = (
        f"⚙️ <b>Options • {esc(chat['title'])}</b>\n"
        f"{SEP}\n"
        "🔁 <b>Mode</b> — loop forever (reshuffle each cycle) or one pass (pause after every post is published once)\n"
        "🧬 <b>Duplicates</b> — skip identical uploads automatically\n"
        "🔔 <b>Cycle alerts</b> — DM when a cycle completes\n"
        "🕓 <b>Auto-delete</b> — remove published posts after a set time\n"
        "✍️ <b>Footer</b> — signature added to every post (use <code>#nofooter</code> in a post to skip it)"
        f"{footer}"
    )
    return text, options_keyboard(chat)


def auto_delete_keyboard(chat_id: int, current: int) -> Markup:
    rows, row = [], []
    for idx, (minutes, label) in enumerate(AUTO_DELETE_PRESETS):
        row.append(b(("✅ " if minutes == current else "") + label, f"od:{chat_id}:{idx}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([b("⬅️ Back", f"chat:options:{chat_id}")])
    return Markup(rows)


# --------------------------------------------------------------------------- #
# Posts browser
# --------------------------------------------------------------------------- #
async def render_posts_page(chat_id: int, page: int) -> tuple[str, Markup]:
    chat = await require_chat(chat_id)
    rows_data, total = await STATE.db.page_posts(chat_id, page, POSTS_PER_PAGE)
    pages = max(1, -(-total // POSTS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    keyboard: list[list[Btn]] = []
    for post in rows_data:
        times = f" ×{post['posted_count']}" if post.get("posted_count") else ""
        keyboard.append([b(f"{post_label(post)}{times}", f"pv:{chat_id}:{post['_id']}:{page}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(b("◀️ Newer", f"pl:{chat_id}:{page - 1}"))
        nav.append(b(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(b("Older ▶️", f"pl:{chat_id}:{page + 1}"))
        keyboard.append(nav)
    keyboard.append([b("⬆️ Upload posts", f"up:pick:{chat_id}"), b("⬅️ Target panel", f"chat:open:{chat_id}")])
    text = (
        f"📝 <b>Posts • {esc(chat['title'])}</b>\n"
        f"{SEP}\n"
        f"Total: <b>{total}</b> • newest first (page {page + 1}/{pages})\n\n"
        + ("Tap a post to preview, send it now or delete it." if total else "<i>No posts yet — tap ⬆️ Upload posts.</i>")
    )
    return text, Markup(keyboard)


async def render_post_detail(chat_id: int, post_id: str, page: int) -> tuple[str, Markup]:
    post = await STATE.db.get_post(chat_id, post_id)
    if not post:
        text, kb = await render_posts_page(chat_id, page)
        return "⚠️ That post no longer exists.\n\n" + text, kb
    flags = [name for name, key in (("silent", "silent"), ("protected", "protect_content"), ("spoiler", "spoiler"), ("pin", "pin"), ("no-footer", "nofooter")) if post.get(key)]
    body = post.get("text") or post.get("caption") or ""
    text = (
        f"{esc(post_label(post))}\n"
        f"{SEP}\n"
        f"Added: <b>{esc(format_dt(post.get('created_at')))}</b>\n"
        f"Published: <b>{post.get('posted_count', 0)}×</b> • last <b>{esc(format_ago(post.get('last_posted_at')))}</b>\n"
        f"Buttons: <b>{sum(len(r) for r in post.get('buttons') or [])}</b> • Flags: <b>{esc(', '.join(flags) or 'none')}</b>\n"
        + (f"\n<i>{esc(trim(body, 300))}</i>" if body else "")
    )
    cid, pid = chat_id, post_id
    kb = Markup(
        [
            [b("👁 Preview to me", f"pa:prev:{cid}:{pid}:{page}"), b("🚀 Send now", f"pa:now:{cid}:{pid}:{page}")],
            [b("🗑 Delete", f"pa:del:{cid}:{pid}:{page}")],
            [b("⬅️ Back to list", f"pl:{cid}:{page}")],
        ]
    )
    return text, kb


# --------------------------------------------------------------------------- #
# Stats / logs / database / backup / settings / help
# --------------------------------------------------------------------------- #
async def render_stats_text(chat_id: int | None = None) -> str:
    db = STATE.db
    if chat_id is None:
        s = await db.global_stats()
        paused = " • <b>" + str(s["paused"]) + "</b> paused" if s["paused"] else ""
        return (
            "📈 <b>Global statistics</b>\n"
            f"{SEP}\n"
            f"Targets: <b>{s['total_channels']}</b> channels • <b>{s['total_groups']}</b> groups{paused}\n"
            f"Total posts: <b>{s['total_posts']}</b>\n"
            f"Remaining this cycle: <b>{s['remaining_posts']}</b>\n"
            f"Published today: <b>{s['published_today']}</b>\n"
            f"Cycles completed: <b>{s['total_cycles_completed']}</b>\n"
            f"Next post: <b>{esc(format_dt(s['next_post_time']))}</b>"
        )
    chat = await require_chat(chat_id)
    total, used, today, history_count = await asyncio.gather(
        db.count_posts(chat_id), db.count_used_posts(chat_id), db.published_today(chat_id), db.history_coll(chat_id).count_documents({})
    )
    created = aware(chat.get("created_at"))
    age_days = max(1, (utcnow() - created).days + 1) if created else 1
    days, (totals, best) = await asyncio.gather(db.history_by_day(chat_id, 7), db.reaction_summary(chat_id, 3))
    peak = max((n for _, n in days), default=0)
    chart = "\n".join(f"<code>{label}</code> {spark_bar(n, peak)} {n}" for label, n in days)
    reactions_line = "  ".join(f"{emoji} {n}" for emoji, n in zip(REACTION_EMOJIS, totals))
    top_lines = "\n".join(f"{i}. {esc(post_label(post)) if post else '(deleted post)'} — <b>{n}</b>" for i, (n, post) in enumerate(best, 1))
    return (
        f"📈 <b>Statistics • {esc(chat['title'])}</b>\n"
        f"{SEP}\n"
        f"Posts: <b>{total}</b> • remaining <b>{max(total - used, 0)}</b>\n"
        f"Published today: <b>{today}</b> • all time <b>{history_count}</b> (~{round(history_count / age_days, 1)}/day)\n"
        f"Cycle: <b>{(chat.get('queue_state') or {}).get('cycle', 0)}</b> • completed <b>{(chat.get('stats') or {}).get('total_cycles_completed', 0)}</b>\n"
        f"Next: <b>{esc(format_dt(chat.get('next_post_time')))}</b>\n\n"
        f"<b>Last 7 days</b>\n{chart}\n\n"
        f"<b>Reactions</b> {reactions_line}\n"
        + (f"\n<b>Top posts</b>\n{top_lines}" if top_lines else "")
    )


async def render_logs_text() -> str:
    rows = await STATE.db.logs.find({}).sort("created_at", -1).to_list(length=12)
    lines = [f"• <b>{esc(row.get('level', 'INFO'))}</b> {esc(row.get('event', 'event'))} <code>{esc(format_dt(row.get('created_at')))}</code>" for row in rows]
    return f"📜 <b>Recent logs</b>\n{SEP}\n" + ("\n".join(lines) if lines else "<i>No logs yet</i>")


async def render_database_text() -> str:
    chats = await STATE.db.list_chats()
    lines = []
    for chat in chats[:20]:
        count = await STATE.db.count_posts(chat["chat_id"])
        lines.append(f"• <b>{esc(chat['title'])}</b> → <code>posts_{coll_suffix(chat['chat_id'])}</code> ({count})")
    log_count = await STATE.db.logs.count_documents({})
    pending = await STATE.db.deletes.count_documents({})
    return (
        "🗃 <b>Database overview</b>\n"
        f"{SEP}\n"
        f"Targets: <b>{len(chats)}</b> • Admin states: <b>{await STATE.db.admins.count_documents({})}</b>\n"
        f"Log rows: <b>{log_count}</b> • Pending auto-deletes: <b>{pending}</b>\n\n"
        f"<b>Per-target post collections</b>\n{chr(10).join(lines) if lines else '<i>empty</i>'}"
    )


def backup_text() -> str:
    return (
        "💾 <b>Backup & restore</b>\n"
        f"{SEP}\n"
        "• Export a full backup of targets, schedules, options, queues and posts.\n"
        "• Restore: tap ♻️ Restore mode, then upload a backup file (.json or .json.gz) here.\n"
        "• 🗓 An automatic backup is DM'd to every admin daily at 03:00."
    )


def backup_keyboard() -> Markup:
    return Markup([[b("⬇️ Export backup", "backup:export"), b("♻️ Restore mode", "backup:restore")], [b("⬅️ Back", "nav:dashboard")]])


async def render_settings_text() -> str:
    try:
        ping = f"{await STATE.db.ping():.0f} ms"
    except Exception as exc:  # noqa: BLE001
        ping = f"❌ {type(exc).__name__}"
    up = STATE.uptime_seconds
    uptime = f"{up // 86400}d {up % 86400 // 3600}h {up % 3600 // 60}m"
    return (
        "⚙️ <b>Settings & health</b>\n"
        f"{SEP}\n"
        f"Version: <b>{__version__}</b> • Uptime: <b>{uptime}</b>\n"
        f"Database ping: <b>{ping}</b>\n"
        f"Timezone: <b>{esc(SETTINGS.timezone)}</b> (now {esc(format_dt(utcnow()))})\n"
        f"Scan interval: <b>{SETTINGS.scan_interval_seconds}s</b> • Retries: <b>{SETTINGS.retry_attempts}</b> • Gap: <b>{SETTINGS.post_gap_seconds}s</b>\n"
        f"New-target defaults: every <b>{SETTINGS.default_interval_minutes} min</b>, batch <b>{SETTINGS.default_batch_size}</b>, "
        f"<b>{SETTINGS.default_start_time}–{SETTINGS.default_end_time}</b>\n"
        f"Password gate: <b>{'on' if SETTINGS.admin_password else 'off'}</b> • Log retention: <b>{SETTINGS.log_retention_days}d</b>\n\n"
        "<i>These come from environment variables; per-target options live in each target's panel.</i>"
    )


HELP_TEXT = (
    "<b>Admin quick start</b>\n"
    "1. Add the bot as admin to a channel/group, then 🧭 Targets → pick it (or ➕ Add).\n"
    "2. Tap ⬆️ Upload posts and send anything: text, photo, video, GIF, document, audio, voice, sticker, album, poll — or a .json/.jsonl/.txt bulk file. Tap ✅ Finish when done.\n"
    "3. Open 📅 Schedule to set interval, batch, window and days with one tap. The shuffle engine does the rest.\n\n"
    "<b>Commands</b>\n"
    "/start dashboard • /upload start uploading • /done finish upload • /stats • /status health • /cancel abort any prompt\n\n"
    "<b>Per-post flags</b> (own line in the text/caption)\n"
    "<code>#silent</code> <code>#protect</code> <code>#spoiler</code> <code>#pin</code> <code>#nofooter</code>\n\n"
    "<b>Buttons</b>\n"
    "<code>---buttons---</code> then <code>Label|https://example.com</code>; use <code>;</code> for several buttons on one row.\n\n"
    "<b>Bulk .txt</b>: separate posts with a line containing only <code>---</code>.\n"
    "<b>Bulk .json</b>: an array of <code>{\"media_type\":\"text\",\"text\":\"…\"}</code> objects."
)


# --------------------------------------------------------------------------- #
# Upload mode
# --------------------------------------------------------------------------- #
def upload_keyboard() -> Markup:
    return Markup([[b("📥 Import range from channel", "up:import")], [b("✅ Finish upload", "up:finish"), b("🔄 Change target", "up:change")]])


def upload_text(title: str, added: int, dups: int, bad: int) -> str:
    extra = ""
    if dups:
        extra += f"\n🧬 Duplicates skipped: <b>{dups}</b>"
    if bad:
        extra += f"\n⚠️ Invalid items skipped: <b>{bad}</b>"
    return (
        f"⬆️ <b>Upload mode → {esc(title)}</b>\n"
        f"{SEP}\n"
        "Send your posts now: text, photo, video, GIF, document, audio, voice, sticker, album, poll, "
        "or a .json/.jsonl/.txt bulk file.\nOr tap 📥 Import range to copy a range of posts from another channel.\n"
        f"Posts added: <b>{added}</b>{extra}"
    )

