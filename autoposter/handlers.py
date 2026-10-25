"""Telegram update handlers: commands, button callbacks and the admin's private-chat conversation."""
from __future__ import annotations

import asyncio
import gzip
import html
import io
from datetime import UTC, timedelta
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, Conflict, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import ContextTypes

from .config import SETTINGS, logger
from .database import loads_json
from .jobs import build_backup_file
from .payloads import UnsupportedMessage, album_fingerprint, message_to_post_payload, parse_bulk_text
from .sender import (
    after_posts_added,
    build_post_markup,
    notify_admins,
    process_chat_batch,
    rebuild_and_ensure_schedule,
    send_saved_post,
    send_single_now,
)
from .state import STATE, is_admin
from .ui import (
    HELP_TEXT,
    SCHEDULE_PRESETS,
    ChatMissing,
    auto_delete_keyboard,
    back_to_dashboard,
    backup_keyboard,
    backup_text,
    chat_keyboard,
    confirm_keyboard,
    dashboard_keyboard,
    dashboard_text,
    esc,
    get_target_chat,
    post_label,
    preset_menu,
    render_chat_overview,
    render_database_text,
    render_logs_text,
    render_options_text,
    render_post_detail,
    render_posts_page,
    render_queue_text,
    render_schedule_text,
    render_settings_text,
    render_stats_text,
    require_chat,
    schedule_keyboard,
    targets_keyboard,
    upload_keyboard,
    upload_picker_keyboard,
    upload_text,
)
from .utils import (
    AUTO_DELETE_PRESETS,
    REACTION_EMOJIS,
    chat_opts,
    format_dt,
    parse_post_markup,
    parse_schedule_text,
    utcnow,
    visible_len,
)

IGNORABLE_BADREQUEST = (
    "message is not modified",
    "message to edit not found",
    "query is too old",
    "message can't be edited",
    "message_id_invalid",
    "message to delete not found",
)
NOISY_ERRORS = (Conflict, NetworkError, TimedOut, RetryAfter)
KNOWN_MODES = {"await_password", "restore_backup", "edit_schedule", "force_sub_channel", "footer_text", "add_target", "bulk_upload", "promo_text"}
MAX_FOOTER_LEN = 400
MAX_BACKUP_BYTES = 40 * 1024 * 1024
BACKGROUND_TASKS: set[asyncio.Task] = set()


# --------------------------------------------------------------------------- #
# Admin state helpers
# --------------------------------------------------------------------------- #
async def admin_doc(admin_id: int) -> dict[str, Any]:
    return await STATE.db.admins.find_one({"admin_id": admin_id}) or {}


async def set_admin_meta(admin_id: int, **fields: Any) -> None:
    fields["updated_at"] = utcnow()
    await STATE.db.admins.update_one({"admin_id": admin_id}, {"$set": fields}, upsert=True)


async def clear_admin_mode(admin_id: int) -> None:
    await STATE.db.admins.update_one(
        {"admin_id": admin_id},
        {
            "$unset": {"mode": "", "mode_chat_id": "", "mode_extra": "", "pending_action": "", "password_deadline": ""},
            "$set": {"updated_at": utcnow()},
        },
        upsert=True,
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="mode:cancel")]])


async def upsert_target_from_chat(chat_obj) -> dict[str, Any]:
    chat_type = "channel" if chat_obj.type == ChatType.CHANNEL else "group"
    title = chat_obj.title or str(chat_obj.id)
    doc = await STATE.db.ensure_chat(chat_obj.id, title, chat_type, getattr(chat_obj, "username", None))
    STATE.title_cache[chat_obj.id] = title
    return doc


def parse_chat_ref(raw: str) -> int | str:
    raw = raw.strip()
    if "t.me/" in raw:
        raw = raw.split("t.me/", 1)[1].split("/")[0].split("?")[0]
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw if raw.startswith("@") else f"@{raw}"


def forwarded_chat_id(message) -> int | None:
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        origin_chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if origin_chat is not None:
            return origin_chat.id
    return None


# --------------------------------------------------------------------------- #
# Dashboard + commands
# --------------------------------------------------------------------------- #
async def show_dashboard(chat_id: int, user_id: int, bot) -> None:
    await bot.send_message(chat_id=chat_id, text=await dashboard_text(user_id), reply_markup=dashboard_keyboard(), parse_mode="HTML")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await update.effective_message.reply_text("Unauthorized.")
        return
    await clear_admin_mode(user.id)
    await show_dashboard(update.effective_chat.id, user.id, context.bot)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_admin(update.effective_user.id if update.effective_user else None):
        await update.effective_message.reply_text(HELP_TEXT, parse_mode="HTML")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id if user else None):
        return
    target = await get_target_chat(user.id)
    await update.effective_message.reply_text(await render_stats_text(target["chat_id"] if target else None), parse_mode="HTML")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_admin(update.effective_user.id if update.effective_user else None):
        await update.effective_message.reply_text(await render_settings_text(), parse_mode="HTML")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id if user else None):
        return
    await clear_admin_mode(user.id)
    await update.effective_message.reply_text("❌ Cancelled. Any in-progress action (password, upload, restore, …) has been stopped.")


async def upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id if user else None) or update.effective_chat.type != ChatType.PRIVATE:
        return
    target = await get_target_chat(user.id)
    if not target:
        await update.effective_message.reply_text("⬆️ Which target do you want to upload to?", reply_markup=await upload_picker_keyboard())
        return
    await begin_upload(context.bot, user.id, target["chat_id"])


async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id if user else None):
        return
    text = await finish_upload(user.id)
    await update.effective_message.reply_text(text or "Nothing to finish — you're not in upload mode.", parse_mode="HTML")


async def register_current_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id if user else None):
        return
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text("Use this command inside a channel or group where the bot is present.")
        return
    saved = await upsert_target_from_chat(chat)
    await update.effective_message.reply_text(f"Registered: {saved['title']}")


# --------------------------------------------------------------------------- #
# Upload mode
# --------------------------------------------------------------------------- #
async def begin_upload(bot, admin_id: int, chat_id: int, edit_message_id: int | None = None) -> None:
    chat = await STATE.db.get_chat(chat_id)
    if not chat:
        await bot.send_message(admin_id, "❌ That target no longer exists.", reply_markup=await upload_picker_keyboard())
        return
    await STATE.db.set_active_target(admin_id, chat_id)
    await set_admin_meta(admin_id, mode="bulk_upload", mode_chat_id=chat_id, upload_count=0, upload_dups=0, upload_bad=0)
    text = upload_text(chat["title"], 0, 0, 0)
    message_id = edit_message_id
    if edit_message_id is not None:
        try:
            await bot.edit_message_text(chat_id=admin_id, message_id=edit_message_id, text=text, reply_markup=upload_keyboard(), parse_mode="HTML")
        except BadRequest:
            message_id = None
    if message_id is None:
        sent = await bot.send_message(admin_id, text, reply_markup=upload_keyboard(), parse_mode="HTML")
        message_id = sent.message_id
    await set_admin_meta(admin_id, upload_msg_id=message_id)


async def finish_upload(admin_id: int) -> str | None:
    state = await admin_doc(admin_id)
    if state.get("mode") != "bulk_upload":
        return None
    chat = await STATE.db.get_chat(state.get("mode_chat_id")) if state.get("mode_chat_id") else None
    title = esc(chat.get("title")) if chat else "target"
    await clear_admin_mode(admin_id)
    await STATE.db.admins.update_one({"admin_id": admin_id}, {"$unset": {"upload_count": "", "upload_dups": "", "upload_bad": "", "upload_msg_id": ""}})
    extra = ""
    if state.get("upload_dups"):
        extra += f"\n🧬 Duplicates skipped: <b>{int(state['upload_dups'])}</b>"
    if state.get("upload_bad"):
        extra += f"\n⚠️ Invalid items skipped: <b>{int(state['upload_bad'])}</b>"
    when = format_dt(chat.get("next_post_time")) if chat else "—"
    return f"✅ Upload finished — <b>{int(state.get('upload_count', 0))}</b> post(s) added to {title}.{extra}\n⏰ Next run: <b>{esc(when)}</b>"


async def refresh_upload_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = context.job.data
    state = await admin_doc(admin_id)
    if state.get("mode") != "bulk_upload" or not state.get("upload_msg_id"):
        return
    chat = await STATE.db.get_chat(state.get("mode_chat_id"))
    title = chat["title"] if chat else "target"
    try:
        await context.bot.edit_message_text(
            chat_id=admin_id,
            message_id=state["upload_msg_id"],
            text=upload_text(title, int(state.get("upload_count", 0)), int(state.get("upload_dups", 0)), int(state.get("upload_bad", 0))),
            reply_markup=upload_keyboard(),
            parse_mode="HTML",
        )
    except TelegramError:
        pass  # "not modified", message deleted, flood wait — the final summary is authoritative anyway


async def bump_upload(admin_id: int, context: ContextTypes.DEFAULT_TYPE, added: int = 0, dups: int = 0, bad: int = 0) -> None:
    inc = {k: v for k, v in (("upload_count", added), ("upload_dups", dups), ("upload_bad", bad)) if v}
    if inc:
        await STATE.db.admins.update_one({"admin_id": admin_id}, {"$inc": inc}, upsert=True)
    name = f"upl:{admin_id}"
    for job in context.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    context.job_queue.run_once(refresh_upload_message, 1.2, data=admin_id, name=name)  # debounced edit: no flood limits


# --------------------------------------------------------------------------- #
# Albums
# --------------------------------------------------------------------------- #
async def buffer_media_group_item(update: Update, context: ContextTypes.DEFAULT_TYPE, target: dict[str, Any], upload_mode: bool) -> None:
    message = update.effective_message
    gid = message.media_group_id
    spoiler = bool(getattr(message, "has_media_spoiler", False))
    if message.photo:
        item = {"media_type": "photo", "file_id": message.photo[-1].file_id, "file_unique_id": message.photo[-1].file_unique_id, "spoiler": spoiler}
    elif message.video:
        item = {"media_type": "video", "file_id": message.video.file_id, "file_unique_id": message.video.file_unique_id, "spoiler": spoiler}
    else:
        return
    buffers = STATE.media_group_buffers
    buf = buffers.get(gid)
    if buf is None:
        buf = buffers[gid] = {"chat_id": target["chat_id"], "title": target["title"], "admin_id": update.effective_user.id, "items": [], "caption": None, "flags": None, "job": None, "upload_mode": upload_mode}
    buf["items"].append(item)
    raw_caption = message.caption_html if message.caption else None
    if raw_caption and buf["caption"] is None:
        buf["caption"], buf["flags"] = parse_post_markup(raw_caption)
    if buf["job"] is not None:
        buf["job"].schedule_removal()
    buf["job"] = context.job_queue.run_once(flush_media_group, 1.5, data=gid, name=f"mg:{gid}")


async def flush_media_group(context: ContextTypes.DEFAULT_TYPE) -> None:
    gid = context.job.data
    buf = STATE.media_group_buffers.pop(gid, None)
    if not buf or not buf["items"]:
        return
    flags = buf["flags"] or {}
    items = buf["items"][:10]
    payload: dict[str, Any] = {
        "media_type": "album",
        "media_group": items,
        "caption": buf["caption"],
        "parse_mode": "HTML",
        "silent": flags.get("silent", False),
        "protect_content": flags.get("protect_content", False),
        "pin": flags.get("pin", False),
        "nofooter": flags.get("nofooter", False),
        "buttons": flags.get("buttons", []),
        "fingerprint": album_fingerprint(items),
    }
    chat_id, admin_id = buf["chat_id"], buf["admin_id"]
    try:
        chat = await STATE.db.get_chat(chat_id)
        if not chat:
            return
        post_id = await STATE.db.add_post(chat_id, payload, chat_opts(chat)["dedupe"])
        await after_posts_added(chat_id)
        state = await admin_doc(admin_id)
        if state.get("mode") == "bulk_upload" and state.get("mode_chat_id") == chat_id:
            await bump_upload(admin_id, context, added=1 if post_id else 0, dups=0 if post_id else 1)
        elif post_id:
            await context.bot.send_message(admin_id, f"Saved to {buf['title']} ✅\n🖼 Album ({len(items)} items)")
        else:
            await context.bot.send_message(admin_id, f"🧬 Duplicate album — already in {buf['title']}, skipped.")
    except Exception as exc:  # noqa: BLE001 - runs as a job; report instead of silently losing the album
        await STATE.db.log("error", "album_save_failed", chat_id=chat_id, error=f"{type(exc).__name__}: {exc}")
        try:
            await context.bot.send_message(admin_id, f"⚠️ Couldn't save that album: {exc}")
        except TelegramError:
            pass


# --------------------------------------------------------------------------- #
# Callback plumbing
# --------------------------------------------------------------------------- #
class Cb:
    """Thin wrapper so every branch can answer the callback query exactly once."""

    def __init__(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.query = update.callback_query
        self.user = self.query.from_user
        self.context = context
        self.bot = context.bot
        self.answered = False

    @property
    def chat_id(self) -> int:
        return self.query.message.chat_id if self.query.message else self.user.id

    @property
    def message_id(self) -> int | None:
        return self.query.message.message_id if self.query.message else None

    async def ack(self, text: str | None = None, alert: bool = False) -> None:
        if self.answered:
            return
        self.answered = True
        try:
            await self.query.answer(text, show_alert=alert)
        except BadRequest:
            pass  # query too old / already answered

    async def edit(self, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        try:
            await self.query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def send(self, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        await self.bot.send_message(self.chat_id, text, reply_markup=markup, parse_mode="HTML")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not is_admin(query.from_user.id if query.from_user else None):
        try:
            await query.answer("Unauthorized", show_alert=True)
        except BadRequest:
            pass
        return
    cb = Cb(update, context)
    try:
        await route_callback(cb, query.data or "")
    except ChatMissing:
        await cb.ack("That target no longer exists.", alert=True)
        try:
            await cb.edit("🧭 <b>Targets</b>\nThat target was removed. Pick another one.", await targets_keyboard())
        except BadRequest:
            pass
    except BadRequest as exc:
        if not any(marker in str(exc).lower() for marker in IGNORABLE_BADREQUEST):
            raise
    finally:
        await cb.ack()


async def route_callback(cb: Cb, data: str) -> None:
    db = STATE.db
    user_id = cb.user.id
    parts = data.split(":")
    head = parts[0]

    if data == "noop":
        return
    if data == "mode:cancel":
        await clear_admin_mode(user_id)
        await cb.ack("Cancelled")
        await cb.edit(await dashboard_text(user_id), dashboard_keyboard())
        return

    # ---- navigation -------------------------------------------------------
    if head == "nav":
        await route_nav(cb, parts)
        return

    # ---- upload -----------------------------------------------------------
    if head == "up":
        action = parts[1]
        if action == "start":
            target = await get_target_chat(user_id)
            if not target:
                await cb.edit("⬆️ <b>Upload posts</b>\nWhich target do you want to upload to?", await upload_picker_keyboard())
                return
            await begin_upload(cb.bot, user_id, target["chat_id"], edit_message_id=cb.message_id)
        elif action == "pick":
            await require_chat(int(parts[2]))
            await begin_upload(cb.bot, user_id, int(parts[2]), edit_message_id=cb.message_id)
        elif action == "change":
            await clear_admin_mode(user_id)
            await cb.edit("⬆️ <b>Upload posts</b>\nWhich target do you want to upload to?", await upload_picker_keyboard())
        elif action == "finish":
            summary = await finish_upload(user_id)
            await cb.edit(summary or "Upload mode is not active.", back_to_dashboard())
        return

    if data == "target:add":
        await set_admin_meta(user_id, mode="add_target")
        await cb.send(
            "➕ <b>Add Channel / Group</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "1. Pehle bot ko us channel/group me <b>admin</b> banao (channel me Post messages permission ke saath).\n"
            "2. Phir yaha bhejo:\n"
            "   • Channel ka koi bhi message <b>forward</b> karo, ya\n"
            "   • <code>@username</code> / <code>t.me/username</code> bhejo, ya\n"
            "   • numeric ID bhejo (jaise <code>-1001234567890</code>)\n\n"
            "Group ke liye @username / ID bhejo, ya group me jaakar <code>/register</code> likho.",
            cancel_keyboard(),
        )
        return

    if data == "backup:export":
        await cb.ack("Preparing backup…")
        blob, filename = build_backup_file(await db.backup_payload())
        await cb.bot.send_document(cb.chat_id, document=InputFile(io.BytesIO(blob), filename=filename), caption="Backup exported ✅")
        return
    if data == "backup:restore":
        await set_admin_meta(user_id, mode="restore_backup")
        await cb.send("♻️ <b>Restore mode</b>\nUpload a backup file (.json or .json.gz) in this chat. Existing posts of the targets in the file are replaced.", cancel_keyboard())
        return
    if data == "db:wipe":
        await cb.edit(
            "🧨 <b>Wipe ALL data — are you sure?</b>\n━━━━━━━━━━━━━━━━━━\n"
            "This deletes every target, post, history entry and log. The bot resets to a brand-new state. This cannot be undone.",
            confirm_keyboard("db:wipeconfirm", "nav:database", "🧨 Yes, wipe everything"),
        )
        return
    if data == "db:wipeconfirm":
        await handle_dangerous_confirm(data, cb)
        return

    if head == "sc":  # schedule preset menu
        text, kb = await preset_menu(parts[1], int(parts[2]))
        await cb.edit(text, kb)
        return
    if head == "ss":  # apply preset
        await apply_schedule_preset(cb, parts[1], int(parts[2]), int(parts[3]))
        return
    if head == "op":
        await route_option(cb, parts[1], int(parts[2]))
        return
    if head == "od":
        chat_id, idx = int(parts[1]), int(parts[2])
        if not 0 <= idx < len(AUTO_DELETE_PRESETS):
            await cb.ack("Expired button", alert=True)
            return
        minutes, label = AUTO_DELETE_PRESETS[idx]
        await require_chat(chat_id)
        await db.set_options(chat_id, auto_delete_minutes=minutes)
        await cb.ack(f"Auto-delete: {label}")
        text, kb = await render_options_text(chat_id)
        await cb.edit(text, kb)
        return
    if head == "pl":
        text, kb = await render_posts_page(int(parts[1]), int(parts[2]))
        await cb.edit(text, kb)
        return
    if head == "pv":
        text, kb = await render_post_detail(int(parts[1]), parts[2], int(parts[3]))
        await cb.edit(text, kb)
        return
    if head == "pa":
        await route_post_action(cb, parts[1], int(parts[2]), parts[3], int(parts[4]))
        return
    if head == "chat" and len(parts) >= 3:
        await route_chat_action(cb, parts[1], int(parts[2]), data)
        return


async def route_nav(cb: Cb, parts: list[str]) -> None:
    user_id = cb.user.id
    where = parts[1]
    if where == "dashboard":
        await cb.edit(await dashboard_text(user_id), dashboard_keyboard())
    elif where == "targets":
        page = int(parts[2]) if len(parts) > 2 else 0
        await cb.edit("🧭 <b>Targets</b>\nSelect a channel or group.", await targets_keyboard(page))
    elif where in ("queue", "schedule", "posts"):
        target = await get_target_chat(user_id)
        if not target:
            await cb.ack("Select a target first (🧭 Targets)", alert=True)
            return
        cid = target["chat_id"]
        if where == "queue":
            await cb.edit(await render_queue_text(cid), await chat_keyboard(cid))
        elif where == "schedule":
            await cb.edit(await render_schedule_text(cid), schedule_keyboard(cid))
        else:
            text, kb = await render_posts_page(cid, 0)
            await cb.edit(text, kb)
    elif where == "stats":
        await cb.edit(await render_stats_text(None), back_to_dashboard())
    elif where == "settings":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❓ Help", callback_data="nav:help"), InlineKeyboardButton("⬅️ Back", callback_data="nav:dashboard")]])
        await cb.edit(await render_settings_text(), kb)
    elif where == "help":
        await cb.edit(HELP_TEXT, back_to_dashboard())
    elif where == "logs":
        await cb.edit(await render_logs_text(), back_to_dashboard())
    elif where == "database":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🧨 Wipe ALL data", callback_data="db:wipe")], [InlineKeyboardButton("⬅️ Back", callback_data="nav:dashboard")]])
        await cb.edit(await render_database_text(), kb)
    elif where == "backup":
        await cb.edit(backup_text(), backup_keyboard())


async def apply_schedule_preset(cb: Cb, key: str, chat_id: int, idx: int) -> None:
    preset = SCHEDULE_PRESETS.get(key)
    if not preset or not 0 <= idx < len(preset["values"]):
        await cb.ack("Expired button", alert=True)
        return
    value, label = preset["values"][idx]
    await require_chat(chat_id)
    if key == "int":
        await STATE.db.update_schedule(chat_id, interval_minutes=value)
    elif key == "bat":
        await STATE.db.update_schedule(chat_id, batch_size=value)
    elif key == "win":
        await STATE.db.update_schedule(chat_id, start_time=value[0], end_time=value[1])
    elif key == "day":
        await STATE.db.update_schedule(chat_id, days=list(value))
    elif key == "jit":
        await STATE.db.set_options(chat_id, jitter_minutes=value)
    await cb.ack(f"{preset['title'].split(' ', 1)[1]}: {label}")
    await cb.edit(await render_schedule_text(chat_id), schedule_keyboard(chat_id))


async def route_option(cb: Cb, key: str, chat_id: int) -> None:
    chat = await require_chat(chat_id)
    opts = chat_opts(chat)
    if key in ("loop", "dedupe", "notify"):
        field = {"loop": "loop", "dedupe": "dedupe", "notify": "notify_cycle"}[key]
        await STATE.db.set_options(chat_id, **{field: not opts[field]})
        text, kb = await render_options_text(chat_id)
        await cb.edit(text, kb)
    elif key == "ad":
        await cb.edit(
            "🕓 <b>Auto-delete</b>\n━━━━━━━━━━━━━━━━━━\nPublished posts are removed from the channel after this time. Great for offers and time-limited content.",
            auto_delete_keyboard(chat_id, int(opts["auto_delete_minutes"])),
        )
    elif key == "footer":
        await set_admin_meta(cb.user.id, mode="footer_text", mode_chat_id=chat_id)
        current = f"\n\nCurrent footer:\n{opts['footer']}" if opts["footer"] else ""
        await cb.send(
            "✍️ <b>Footer</b>\nSend the text that should be added under every post (HTML formatting like <b>bold</b> and links works). "
            f"Send <code>off</code> to remove it.{current}",
            cancel_keyboard(),
        )


async def route_post_action(cb: Cb, action: str, chat_id: int, post_id: str, page: int) -> None:
    db = STATE.db
    await require_chat(chat_id)
    post = await db.get_post(chat_id, post_id)
    if not post:
        await cb.ack("That post no longer exists.", alert=True)
        text, kb = await render_posts_page(chat_id, page)
        await cb.edit(text, kb)
        return
    chat = await db.get_chat(chat_id)
    if action == "prev":
        try:
            await send_saved_post(cb.bot, cb.user.id, post, chat_opts(chat))
        except TelegramError as exc:
            await cb.ack(f"Preview failed: {exc}", alert=True)
            return
        await cb.ack("Preview sent 👆")
    elif action == "now":
        try:
            await send_single_now(cb.bot, chat_id, post)
        except TelegramError as exc:
            await cb.ack(f"Couldn't send: {exc}", alert=True)
            return
        await cb.ack("🚀 Sent to the channel")
    elif action == "del":
        await cb.edit(
            "🗑 <b>Delete this post?</b>\nIt will be removed from the rotation permanently.",
            confirm_keyboard(f"pa:delok:{chat_id}:{post_id}:{page}", f"pv:{chat_id}:{post_id}:{page}", "🗑 Yes, delete"),
        )
    elif action == "delok":
        await db.delete_post(chat_id, post_id)
        await cb.ack("Deleted")
        text, kb = await render_posts_page(chat_id, page)
        await cb.edit(text, kb)


async def route_chat_action(cb: Cb, action: str, chat_id: int, data: str) -> None:
    db = STATE.db
    user_id = cb.user.id
    chat = await require_chat(chat_id)
    title = esc(chat.get("title", chat_id))

    if action == "open":
        await cb.edit(await render_chat_overview(chat_id), await chat_keyboard(chat_id))
    elif action == "pick":
        await db.set_active_target(user_id, chat_id)
        await cb.ack("Target selected ✅")
        await cb.edit(await render_chat_overview(chat_id), await chat_keyboard(chat_id))
    elif action == "queue":
        await cb.edit(await render_queue_text(chat_id), await chat_keyboard(chat_id))
    elif action == "schedule":
        await cb.edit(await render_schedule_text(chat_id), schedule_keyboard(chat_id))
    elif action == "stats":
        await cb.edit(await render_stats_text(chat_id), await chat_keyboard(chat_id))
    elif action == "options":
        text, kb = await render_options_text(chat_id)
        await cb.edit(text, kb)
    elif action == "edit_schedule":
        await set_admin_meta(user_id, mode="edit_schedule", mode_chat_id=chat_id)
        await cb.send(
            f"✏️ <b>Advanced schedule • {title}</b>\nSend the values you want to change (anything you omit resets to the default):\n"
            "<code>interval=2h\nbatch=5\nstart=09:00\nend=21:00\ndays=0,1,2,3,4,5,6</code>\n"
            "interval accepts minutes (<code>90</code>), <code>90m</code>, <code>2h</code> or <code>1d</code>. Days: 0=Mon … 6=Sun.",
            cancel_keyboard(),
        )
    elif action == "reshuffle":
        await rebuild_and_ensure_schedule(chat_id)
        await cb.ack("Queue reshuffled 🔀")
        await cb.edit(await render_queue_text(chat_id), await chat_keyboard(chat_id))
    elif action == "run":
        if not chat.get("is_active", True):
            await cb.ack("This target is paused — resume it first.", alert=True)
            return
        if STATE.lock_for(chat_id).locked():
            await cb.ack("A batch is already being published…", alert=True)
            return
        await cb.ack("Publishing the next batch…")
        message_id = cb.message_id
        bot, admin_chat = cb.bot, cb.chat_id

        async def run_batch() -> None:
            sent = await process_chat_batch(bot, chat_id, manual=True)
            note = f"\n\n✅ Manual batch sent: <b>{sent}</b>"
            try:
                await bot.edit_message_text(chat_id=admin_chat, message_id=message_id, text=await render_queue_text(chat_id) + note, reply_markup=await chat_keyboard(chat_id), parse_mode="HTML")
            except (BadRequest, ChatMissing):
                pass

        task = cb.context.application.create_task(run_batch(), update=None, name=f"manual:{chat_id}")
        BACKGROUND_TASKS.add(task)
        task.add_done_callback(BACKGROUND_TASKS.discard)
    elif action == "toggle":
        new_state = not chat.get("is_active", True)
        await db.set_active(chat_id, new_state, reason=None if new_state else "Paused by admin.")
        await cb.ack("Resumed ▶️" if new_state else "Paused ⏸")
        await cb.edit(await render_chat_overview(chat_id), await chat_keyboard(chat_id))
    elif action == "undo":
        removed = await db.undo_last_post(chat_id)
        if removed:
            await cb.ack(f"Removed: {post_label(removed)}", alert=True)
        else:
            await cb.ack("No posts to undo.", alert=True)
        text, kb = await render_posts_page(chat_id, 0)
        await cb.edit(text, kb)
    elif action == "wipe":
        await cb.edit(
            f"🧹 <b>Wipe posts for {title}?</b>\n━━━━━━━━━━━━━━━━━━\n"
            "This deletes all posts, history and queue progress for this target only. Other targets are not affected. This cannot be undone.",
            confirm_keyboard(f"chat:wipeconfirm:{chat_id}", f"chat:open:{chat_id}", "🧹 Yes, wipe posts"),
        )
    elif action in ("wipeconfirm", "removeconfirm"):
        await handle_dangerous_confirm(data, cb)
    elif action == "remove":
        await cb.edit(
            f"🗑 <b>Remove {title} from the bot?</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Its posts, history, schedule and queue are all deleted and auto-posting stops. "
            "The bot stays in the channel/group unless you also remove it there. This cannot be undone.",
            confirm_keyboard(f"chat:removeconfirm:{chat_id}", f"chat:open:{chat_id}", "🗑 Yes, remove it"),
        )
    elif action == "promo":
        await set_admin_meta(user_id, mode="promo_text", mode_chat_id=chat_id)
        await cb.send(f"📌 Send the prompt/promo message for <b>{title}</b> now — it will be posted and pinned there. A previously pinned prompt is unpinned automatically.", cancel_keyboard())
    elif action == "forcesub":
        current = chat.get("force_sub_channel")
        current_line = f"\nCurrently set: <code>{esc(current)}</code>" if current else "\nCurrently: <i>not set (join requests are not gated)</i>"
        await set_admin_meta(user_id, mode="force_sub_channel", mode_chat_id=chat_id)
        await cb.send(
            f"🔒 <b>Force-Sub Gate for {title}</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"Send the @username or numeric ID of the channel people must join first before their join request to <b>{title}</b> is approved.{current_line}\n\n"
            "⚠️ Requirements:\n"
            f"1. <b>{title}</b> needs 'Approve new members' turned ON.\n"
            "2. This bot must be admin in BOTH channels.\n\n"
            "Send <code>off</code> to remove the gate.",
            cancel_keyboard(),
        )


# --------------------------------------------------------------------------- #
# Destructive actions (optionally password-gated)
# --------------------------------------------------------------------------- #
async def run_dangerous_action(action: str) -> tuple[str, InlineKeyboardMarkup]:
    if action == "db:wipeconfirm":
        await STATE.db.wipe_all()
        STATE.title_cache.clear()
        return "✅ Database wiped. The bot is now in a fresh state.\n\nUse 🧭 Targets to add channels/groups again.", dashboard_keyboard()
    parts = action.split(":")
    if len(parts) == 3 and parts[0] == "chat":
        chat_id = int(parts[2])
        if parts[1] == "wipeconfirm":
            if not await STATE.db.get_chat(chat_id):
                return "⚠️ That target no longer exists.", await targets_keyboard()
            await STATE.db.wipe_chat(chat_id)
            return (await render_chat_overview(chat_id)) + "\n\n✅ Posts wiped for this target.", await chat_keyboard(chat_id)
        if parts[1] == "removeconfirm":
            chat = await STATE.db.get_chat(chat_id)
            title = esc(chat.get("title", chat_id)) if chat else str(chat_id)
            await STATE.db.delete_chat(chat_id)
            STATE.title_cache.pop(chat_id, None)
            return f"✅ <b>{title}</b> has been removed from the bot's target list.", await targets_keyboard()
    return "⚠️ Unknown or expired action.", dashboard_keyboard()


async def handle_dangerous_confirm(action: str, cb: Cb) -> None:
    if SETTINGS.admin_password:
        await set_admin_meta(
            cb.user.id,
            mode="await_password",
            pending_action=action,
            password_deadline=utcnow() + timedelta(seconds=SETTINGS.password_timeout_seconds),
        )
        await cb.edit(
            "🔑 <b>Password required</b>\n━━━━━━━━━━━━━━━━━━\n"
            "This is a destructive action. Reply here with the admin password to confirm, or tap Cancel.\n"
            "Your password message is deleted automatically.",
            cancel_keyboard(),
        )
        return
    text, kb = await run_dangerous_action(action)
    await cb.edit(text, kb)


# --------------------------------------------------------------------------- #
# Reactions on published posts (anyone can tap)
# --------------------------------------------------------------------------- #
async def handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    msg = query.message
    try:
        if not msg or not user:
            return
        try:
            emoji_idx = int(query.data.split(":", 1)[1])
        except (ValueError, IndexError):
            return
        if not 0 <= emoji_idx < len(REACTION_EMOJIS):
            return
        counts = await STATE.db.toggle_reaction(msg.chat_id, msg.message_id, user.id, emoji_idx)
        custom_rows: list[list[dict[str, str]]] = []
        if msg.reply_markup and msg.reply_markup.inline_keyboard:
            for row in msg.reply_markup.inline_keyboard[:-1]:
                cleaned = [{"text": btn.text, "url": btn.url} for btn in row if btn.url]
                if cleaned:
                    custom_rows.append(cleaned)
        try:
            await query.edit_message_reply_markup(reply_markup=build_post_markup({"buttons": custom_rows}, counts))
        except BadRequest:
            pass
        await query.answer(f"You reacted {REACTION_EMOJIS[emoji_idx]}")
        return
    except Exception:
        await _safe_answer(query)
        raise
    finally:
        await _safe_answer(query)


async def _safe_answer(query) -> None:
    try:
        await query.answer()
    except TelegramError:
        pass


# --------------------------------------------------------------------------- #
# Chat membership events
# --------------------------------------------------------------------------- #
async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change = update.my_chat_member
    chat = change.chat
    if chat.type not in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
        return
    new_status = change.new_chat_member.status
    existing = await STATE.db.get_chat(chat.id)
    label = html.escape(chat.title or str(chat.id))
    if new_status in ("left", "kicked"):
        await STATE.db.log("info", "bot_removed_from_chat", chat_id=chat.id, status=new_status)
        if existing and existing.get("is_active", True):
            await STATE.db.set_active(chat.id, False, reason="The bot was removed from this chat.")
            await notify_admins(context.bot, f"⏸ The bot was removed from <b>{label}</b> — auto-posting there is paused.")
        return
    adder = change.from_user
    if not existing and not is_admin(adder.id if adder else None):
        await STATE.db.log("info", "ignored_unknown_chat_add", chat_id=chat.id, by=adder.id if adder else None)
        return
    try:
        saved = await upsert_target_from_chat(chat)
    except Exception as exc:  # noqa: BLE001
        await STATE.db.log("error", "register_chat_failed", chat_id=chat.id, error=str(exc))
        await notify_admins(context.bot, f"❌ <b>{label}</b> register nahi ho paya: <code>{html.escape(str(exc))}</code>")
        return
    if not existing:
        await STATE.db.log("info", "chat_registered", chat_id=chat.id, status=new_status)
        await notify_admins(
            context.bot,
            f"✅ Registered: <b>{html.escape(saved['title'])}</b> ({'channel' if saved['type'] == 'channel' else 'group'})\nAb /start → 🧭 Targets me dikhega.",
        )
    elif chat.type == ChatType.CHANNEL and new_status == "member":
        await notify_admins(context.bot, f"⚠️ The bot is no longer an admin in <b>{label}</b>, so it can't post there until you promote it again.")


async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    req = update.chat_join_request
    main_chat_id = req.chat.id
    applicant = req.from_user
    chat_doc = await STATE.db.get_chat(main_chat_id)
    required = chat_doc.get("force_sub_channel") if chat_doc else None
    if not required:
        return  # no gate configured; leave the request for manual/native handling
    try:
        member = await context.bot.get_chat_member(required, applicant.id)
        is_member = member.status in ("member", "administrator", "creator", "restricted")
    except TelegramError:
        is_member = False
    if is_member:
        try:
            await context.bot.approve_chat_join_request(main_chat_id, applicant.id)
        except TelegramError:
            pass
        return
    try:
        await context.bot.decline_chat_join_request(main_chat_id, applicant.id)
    except TelegramError:
        pass
    link: str | None = None
    label = str(required)
    if label.startswith("@"):
        link = f"https://t.me/{label.lstrip('@')}"
    else:
        try:
            info = await context.bot.get_chat(required)
            label = info.title or label
            link = f"https://t.me/{info.username}" if info.username else info.invite_link
        except TelegramError:
            pass
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Join required channel", url=link)]]) if link else None
    try:
        await context.bot.send_message(
            getattr(req, "user_chat_id", None) or applicant.id,
            f"🔒 To join <b>{html.escape(chat_doc.get('title', str(main_chat_id)))}</b>, please join {html.escape(label)} first, then send the join request again.",
            parse_mode="HTML",
            reply_markup=kb,
        )
    except TelegramError:
        pass


async def any_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Keeps a registered target's title/username fresh. Cheap: one DB hit per chat per process."""
    chat = update.effective_chat
    if not chat or chat.type not in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if chat.id not in STATE.title_cache:
        doc = await STATE.db.get_chat(chat.id)
        STATE.title_cache[chat.id] = doc.get("title") if doc else None
    cached = STATE.title_cache[chat.id]
    if cached is None:
        return
    new_title = chat.title or str(chat.id)
    if new_title != cached:
        await STATE.db.chats.update_one({"chat_id": chat.id}, {"$set": {"title": new_title, "username": getattr(chat, "username", None), "updated_at": utcnow()}})
        STATE.title_cache[chat.id] = new_title


# --------------------------------------------------------------------------- #
# The admin's private-chat conversation
# --------------------------------------------------------------------------- #
async def download_document_text(message, context: ContextTypes.DEFAULT_TYPE, max_bytes: int = MAX_BACKUP_BYTES) -> tuple[str, str]:
    doc = message.document
    if doc.file_size and doc.file_size > max_bytes:
        raise ValueError(f"That file is too large ({doc.file_size // (1024 * 1024)} MB).")
    file = await context.bot.get_file(doc.file_id)
    blob = bytes(await file.download_as_bytearray())
    name = (doc.file_name or "").lower()
    if name.endswith(".gz") or blob[:2] == b"\x1f\x8b":
        blob = gzip.decompress(blob)
        name = name[:-3] if name.endswith(".gz") else name
    return name, blob.decode("utf-8-sig")


async def ingest_message(update: Update, context: ContextTypes.DEFAULT_TYPE, target: dict[str, Any], upload_mode: bool) -> None:
    """Saves whatever the admin sent as post(s) into `target`."""
    message = update.effective_message
    user_id = update.effective_user.id
    chat_id = target["chat_id"]
    dedupe = chat_opts(target)["dedupe"]
    title = target["title"]

    async def report(text: str) -> None:
        await message.reply_text(text, parse_mode="HTML")

    if message.media_group_id and (message.photo or message.video):
        await buffer_media_group_item(update, context, target, upload_mode)
        return

    file_name = (message.document.file_name or "").lower() if message.document else ""
    if message.document and file_name.endswith((".json", ".jsonl", ".txt")):
        try:
            name, text = await download_document_text(message, context, 20 * 1024 * 1024)
            posts, bad = parse_bulk_text(file_name, text)
        except (ValueError, UnicodeDecodeError, OSError, TelegramError) as exc:
            if upload_mode:
                await bump_upload(user_id, context, bad=1)
            await report(f"❌ Couldn't read that file: <code>{esc(exc)}</code>")
            return
        inserted, dups = await STATE.db.bulk_add_posts(chat_id, posts, dedupe)
        await after_posts_added(chat_id)
        if upload_mode:
            await bump_upload(user_id, context, added=inserted, dups=dups, bad=bad)
        else:
            extra = (f"\n🧬 Duplicates skipped: {dups}" if dups else "") + (f"\n⚠️ Invalid entries skipped: {bad}" if bad else "")
            await report(f"Bulk import complete ✅\nInserted posts: <b>{inserted}</b>{extra}")
        return

    try:
        payload = message_to_post_payload(message)
    except UnsupportedMessage as exc:
        if upload_mode:
            await bump_upload(user_id, context, bad=1)
        await report(f"⚠️ {esc(exc)}")
        return
    post_id = await STATE.db.add_post(chat_id, payload, dedupe)
    await after_posts_added(chat_id)
    if upload_mode:
        await bump_upload(user_id, context, added=1 if post_id else 0, dups=0 if post_id else 1)
    elif post_id:
        await report(f"Saved to <b>{esc(title)}</b> ✅\n{esc(post_label(payload))}")
    else:
        await report(f"🧬 Duplicate — that post is already in <b>{esc(title)}</b>, skipped.")


async def private_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not message or not is_admin(user.id if user else None):
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    state = await admin_doc(user.id)
    mode = state.get("mode")
    if mode and mode not in KNOWN_MODES:
        await clear_admin_mode(user.id)  # stale mode from an older version
        mode = None

    handler = MODE_HANDLERS.get(mode)
    if handler:
        await handler(update, context, state)
        return

    target = await get_target_chat(user.id)
    if not target:
        await message.reply_text("Select a target first — open /start → 🧭 Targets (or ⬆️ Upload posts).")
        return
    await ingest_message(update, context, target, upload_mode=False)


async def mode_password(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any]) -> None:
    message, user = update.effective_message, update.effective_user
    deadline = state.get("password_deadline")
    if deadline is not None and deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    if deadline and utcnow() > deadline:
        await clear_admin_mode(user.id)
        await message.reply_text("⌛ Password prompt expired. Please start the action again.")
        return
    entered = (message.text or "").strip()
    try:
        await message.delete()
    except TelegramError:
        pass
    if not SETTINGS.admin_password or entered != SETTINGS.admin_password:
        await context.bot.send_message(user.id, "❌ Wrong password. Try again, or send /cancel to abort.")
        return
    pending = state.get("pending_action", "")
    await clear_admin_mode(user.id)
    text, kb = await run_dangerous_action(pending)
    await context.bot.send_message(user.id, text, reply_markup=kb, parse_mode="HTML")


async def mode_restore(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any]) -> None:
    message, user = update.effective_message, update.effective_user
    if not message.document:
        await message.reply_text("Please upload the backup file (.json or .json.gz), or send /cancel.")
        return
    try:
        _, text = await download_document_text(message, context)
        payload = loads_json(text)
        chats, posts = await STATE.db.restore_payload(payload)
    except (ValueError, UnicodeDecodeError, OSError, TelegramError, KeyError, TypeError) as exc:
        await message.reply_text(f"❌ Restore failed: <code>{esc(exc)}</code>\nThe file must be a backup exported by this bot. Try again or /cancel.", parse_mode="HTML")
        return
    STATE.title_cache.clear()
    await clear_admin_mode(user.id)
    await message.reply_text(f"Restore complete ✅\nTargets: {chats}\nPosts: {posts}\nTip: tap 🔀 Reshuffle on each restored target so the queue matches the restored posts.")


async def mode_edit_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any]) -> None:
    message, user = update.effective_message, update.effective_user
    if not message.text:
        await message.reply_text("Send the schedule as text, e.g. <code>interval=2h</code>, or /cancel.", parse_mode="HTML")
        return
    chat_id = int(state["mode_chat_id"])
    try:
        parsed = parse_schedule_text(message.text)
    except ValueError as exc:
        await message.reply_text(f"❌ {esc(exc)}\nFix it and send again, or /cancel.", parse_mode="HTML")
        return
    if not await STATE.db.update_schedule(chat_id, **parsed):
        await clear_admin_mode(user.id)
        await message.reply_text("❌ That target no longer exists.")
        return
    await clear_admin_mode(user.id)
    await message.reply_text("Schedule updated ✅\n\n" + await render_schedule_text(chat_id), reply_markup=schedule_keyboard(chat_id), parse_mode="HTML")


async def mode_footer(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any]) -> None:
    message, user = update.effective_message, update.effective_user
    if not message.text:
        await message.reply_text("Send the footer as text, or /cancel.")
        return
    chat_id = int(state["mode_chat_id"])
    raw = message.text_html.strip()
    footer: str | None = None if raw.lower() == "off" else raw
    if footer and visible_len(footer) > MAX_FOOTER_LEN:
        await message.reply_text(f"❌ Footer is too long ({visible_len(footer)} characters, max {MAX_FOOTER_LEN}). Shorten it and send again, or /cancel.")
        return
    if not await STATE.db.set_options(chat_id, footer=footer):
        await clear_admin_mode(user.id)
        await message.reply_text("❌ That target no longer exists.")
        return
    await clear_admin_mode(user.id)
    text, kb = await render_options_text(chat_id)
    await message.reply_text(("✍️ Footer removed ✅" if footer is None else "✍️ Footer saved ✅") + "\n\n" + text, reply_markup=kb, parse_mode="HTML")


async def mode_force_sub(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any]) -> None:
    message, user = update.effective_message, update.effective_user
    if not message.text:
        await message.reply_text("Send an @username or numeric ID, or <code>off</code>.", parse_mode="HTML")
        return
    target_chat_id = int(state["mode_chat_id"])
    raw = message.text.strip()
    if raw.lower() == "off":
        await STATE.db.chats.update_one({"chat_id": target_chat_id}, {"$unset": {"force_sub_channel": ""}, "$set": {"updated_at": utcnow()}})
        await clear_admin_mode(user.id)
        await message.reply_text("🔓 Force-sub gate removed for this target.")
        return
    value = parse_chat_ref(raw)
    try:
        await context.bot.get_chat_member(value, context.bot.id)
    except TelegramError as exc:
        await message.reply_text(f"❌ Bot can't access {raw} — make sure the bot is an admin there. ({exc})")
        return
    await STATE.db.chats.update_one({"chat_id": target_chat_id}, {"$set": {"force_sub_channel": value, "updated_at": utcnow()}})
    await clear_admin_mode(user.id)
    await message.reply_text(f"🔒 Force-sub gate set to {raw} ✅\nNew join requests will now be checked against this channel.")


async def mode_add_target(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any]) -> None:
    message, user = update.effective_message, update.effective_user
    chat_ref: int | str | None = forwarded_chat_id(message)
    if chat_ref is None and message.text:
        chat_ref = parse_chat_ref(message.text)
    if chat_ref is None:
        await message.reply_text("Channel ka message forward karo, ya @username / ID bhejo. /cancel se band karo.")
        return
    try:
        chat_obj = await context.bot.get_chat(chat_ref)
        member = await context.bot.get_chat_member(chat_obj.id, context.bot.id)
    except TelegramError as exc:
        await message.reply_text(f"❌ Chat access nahi mil raha: {exc}\nBot ko wahan admin banao, phir dobara bhejo (ya /cancel).")
        return
    if chat_obj.type not in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("❌ Ye channel ya group nahi hai. Dobara bhejo ya /cancel.")
        return
    if member.status in ("left", "kicked"):
        await message.reply_text("❌ Bot is chat me member nahi hai. Pehle bot ko add/admin karo, phir dobara bhejo.")
        return
    if chat_obj.type == ChatType.CHANNEL:
        if member.status not in ("administrator", "creator"):
            await message.reply_text("❌ Channel me bot ko admin banana zaroori hai (Post messages permission ke saath). Phir dobara bhejo.")
            return
        if getattr(member, "can_post_messages", None) is False:
            await message.reply_text("❌ Bot admin hai lekin 'Post messages' permission off hai. Permission on karke dobara bhejo.")
            return
    saved = await upsert_target_from_chat(chat_obj)
    await STATE.db.set_active_target(user.id, saved["chat_id"])
    await clear_admin_mode(user.id)
    await message.reply_text(
        "✅ Added & selected!\n\n" + await render_chat_overview(saved["chat_id"]),
        reply_markup=await chat_keyboard(saved["chat_id"]),
        parse_mode="HTML",
    )


async def mode_bulk_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any]) -> None:
    message, user = update.effective_message, update.effective_user
    target = await STATE.db.get_chat(int(state["mode_chat_id"]))
    if not target:
        await clear_admin_mode(user.id)
        await message.reply_text("❌ That target no longer exists. Upload mode ended.")
        return
    await ingest_message(update, context, target, upload_mode=True)


async def mode_promo(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any]) -> None:
    message, user = update.effective_message, update.effective_user
    target_chat_id = int(state["mode_chat_id"])
    chat = await STATE.db.get_chat(target_chat_id)
    if not chat:
        await clear_admin_mode(user.id)
        await message.reply_text("❌ That target no longer exists.")
        return
    old_promo_id = chat.get("promo_message_id")
    try:
        sent = await context.bot.copy_message(chat_id=target_chat_id, from_chat_id=message.chat_id, message_id=message.message_id)
        await context.bot.pin_chat_message(chat_id=target_chat_id, message_id=sent.message_id, disable_notification=True)
    except TelegramError as exc:
        await clear_admin_mode(user.id)
        await message.reply_text(f"❌ Could not send/pin: {exc}")
        return
    if old_promo_id:
        try:
            await context.bot.unpin_chat_message(chat_id=target_chat_id, message_id=old_promo_id)
        except TelegramError:
            pass
    await STATE.db.chats.update_one({"chat_id": target_chat_id}, {"$set": {"promo_message_id": sent.message_id, "updated_at": utcnow()}})
    await clear_admin_mode(user.id)
    await message.reply_text(f"📌 Prompt sent & pinned in {chat.get('title', target_chat_id)} ✅")


MODE_HANDLERS = {
    "await_password": mode_password,
    "restore_backup": mode_restore,
    "edit_schedule": mode_edit_schedule,
    "footer_text": mode_footer,
    "force_sub_channel": mode_force_sub,
    "add_target": mode_add_target,
    "bulk_upload": mode_bulk_upload,
    "promo_text": mode_promo,
}


# --------------------------------------------------------------------------- #
# Global error handler
# --------------------------------------------------------------------------- #
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, BadRequest) and any(marker in str(error).lower() for marker in IGNORABLE_BADREQUEST):
        return
    if isinstance(error, NOISY_ERRORS):
        logger.warning("Transient Telegram problem: %s: %s", type(error).__name__, error)
        return
    logger.error("Unhandled exception while processing update", exc_info=error)
    try:
        await STATE.db.log("error", "unhandled_exception", error=f"{type(error).__name__}: {error}")
    except Exception:  # noqa: BLE001 - never let error logging itself crash the handler
        pass
    # Only ever talk back to an admin in their private chat — never post errors into a channel/group.
    if isinstance(update, Update) and update.effective_chat and update.effective_user:
        if update.effective_chat.type == ChatType.PRIVATE and is_admin(update.effective_user.id):
            try:
                await context.bot.send_message(
                    update.effective_chat.id,
                    f"⚠️ Something went wrong: <code>{html.escape(str(error))[:300]}</code>\nSend /start to get back to the dashboard.",
                    parse_mode="HTML",
                )
            except TelegramError:
                pass

