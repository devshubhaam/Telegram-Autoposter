"""Sending posts to Telegram + the batch engine that drives the schedule."""
from __future__ import annotations

import asyncio
import html
import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo
from telegram.error import BadRequest, ChatMigrated, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut

from .config import SETTINGS
from .state import STATE
from .utils import (
    DEFAULT_OPTIONS,
    INVISIBLE,
    MAX_CAPTION_LEN,
    REACTION_EMOJIS,
    chat_next_run,
    chat_opts,
    compose_text,
    strip_html,
    utcnow,
    visible_len,
)

# media_type -> (bot method, argument name, supports caption, supports spoiler)
MEDIA_METHODS: dict[str, tuple[str, str, bool, bool]] = {
    "photo": ("send_photo", "photo", True, True),
    "video": ("send_video", "video", True, True),
    "animation": ("send_animation", "animation", True, True),
    "document": ("send_document", "document", True, False),
    "audio": ("send_audio", "audio", True, False),
    "voice": ("send_voice", "voice", True, False),
    "video_note": ("send_video_note", "video_note", False, False),
    "sticker": ("send_sticker", "sticker", False, False),
}
COPY_TYPES = ("forward", "copy")  # "forward" is the legacy name used by the removed channel-import feature
VALID_MEDIA_TYPES = {"text", "album", *MEDIA_METHODS, *COPY_TYPES}

MAX_FAILURES_PER_BATCH = 5
QUARANTINE_AFTER = 3


@dataclass(slots=True)
class SendResult:
    message_id: int          # the message that carries the reaction buttons
    message_ids: list[int]   # everything that was posted (used for auto-delete)


# --------------------------------------------------------------------------- #
# Keyboards attached to published posts
# --------------------------------------------------------------------------- #
def reaction_row(counts: dict[int, int] | None = None) -> list[InlineKeyboardButton]:
    counts = counts or {}
    row = []
    for i, emoji in enumerate(REACTION_EMOJIS):
        n = counts.get(i, 0)
        row.append(InlineKeyboardButton(f"{emoji} {n}" if n else emoji, callback_data=f"react:{i}"))
    return row


def render_buttons(buttons: list[list[dict[str, str]]] | None) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for row in buttons or []:
        rendered = [InlineKeyboardButton(item["text"], url=item["url"]) for item in row if item.get("url") and item.get("text")]
        if rendered:
            rows.append(rendered)
    return rows


def build_post_markup(post: dict[str, Any], counts: dict[int, int] | None = None) -> InlineKeyboardMarkup:
    rows = render_buttons(post.get("buttons"))
    rows.append(reaction_row(counts))
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------- #
# Low level send
# --------------------------------------------------------------------------- #
async def send_saved_post(bot, target_chat_id: int, post: dict[str, Any], opts: dict[str, Any] | None = None, plain: bool = False) -> SendResult:
    opts = opts or DEFAULT_OPTIONS
    media_type = post.get("media_type", "text")
    silent = bool(post.get("silent", False))
    protect = bool(post.get("protect_content", False))
    markup = build_post_markup(post)
    parse_mode = None if plain else post.get("parse_mode", "HTML")
    footer = opts.get("footer")
    skip_footer = bool(post.get("nofooter", False))

    def prep(value: str | None) -> str:
        composed = compose_text(value, footer, skip_footer)
        return strip_html(composed) if plain else composed

    if media_type in COPY_TYPES:
        msg = await bot.copy_message(
            chat_id=target_chat_id,
            from_chat_id=post["source_chat_id"],
            message_id=post["source_message_id"],
            disable_notification=silent,
            protect_content=protect,
            reply_markup=markup,
        )
        return SendResult(msg.message_id, [msg.message_id])

    if media_type == "album":
        items = post.get("media_group") or []
        if not items:
            raise ValueError("album has no items")
        if len(items) == 1:  # Telegram refuses 1-item albums
            single = {**post, "media_type": items[0].get("media_type", "photo"), "file_id": items[0]["file_id"], "spoiler": items[0].get("spoiler", False)}
            return await send_saved_post(bot, target_chat_id, single, opts, plain)
        caption = prep(post.get("caption"))
        long_caption = visible_len(caption) > MAX_CAPTION_LEN
        input_media = []
        for idx, item in enumerate(items[:10]):
            cls = InputMediaPhoto if item.get("media_type") == "photo" else InputMediaVideo
            media_kwargs: dict[str, Any] = {"media": item["file_id"]}
            if item.get("spoiler"):
                media_kwargs["has_spoiler"] = True
            if idx == 0 and caption and not long_caption:
                media_kwargs["caption"] = caption
                media_kwargs["parse_mode"] = parse_mode
            input_media.append(cls(**media_kwargs))
        msgs = await bot.send_media_group(chat_id=target_chat_id, media=input_media, disable_notification=silent, protect_content=protect)
        ids = [m.message_id for m in msgs]
        # Albums can't carry buttons, so a follow-up message holds reactions (and an over-long caption).
        anchor = await bot.send_message(
            chat_id=target_chat_id,
            text=caption if long_caption else INVISIBLE,
            parse_mode=parse_mode if long_caption else None,
            reply_markup=markup,
            disable_notification=True,
        )
        return SendResult(anchor.message_id, ids + [anchor.message_id])

    common = {"chat_id": target_chat_id, "disable_notification": silent, "protect_content": protect, "reply_markup": markup}

    if media_type in MEDIA_METHODS:
        method, field, has_caption, has_spoiler = MEDIA_METHODS[media_type]
        kwargs: dict[str, Any] = {field: post["file_id"], **common}
        caption = prep(post.get("caption")) if has_caption else ""
        long_caption = visible_len(caption) > MAX_CAPTION_LEN
        if has_caption and caption and not long_caption:
            kwargs["caption"] = caption
            kwargs["parse_mode"] = parse_mode
        if has_spoiler:
            kwargs["has_spoiler"] = bool(post.get("spoiler", False))
        msg = await getattr(bot, method)(**kwargs)
        ids = [msg.message_id]
        if long_caption:
            extra = await bot.send_message(chat_id=target_chat_id, text=caption, parse_mode=parse_mode, disable_notification=True)
            ids.append(extra.message_id)
        return SendResult(msg.message_id, ids)

    msg = await bot.send_message(text=prep(post.get("text")) or INVISIBLE, parse_mode=parse_mode, **common)
    return SendResult(msg.message_id, [msg.message_id])


def _retry_seconds(exc: RetryAfter) -> float:
    value = exc.retry_after
    return float(value.total_seconds()) if isinstance(value, timedelta) else float(value)


async def send_post_with_retry(bot, target_chat_id: int, post: dict[str, Any], opts: dict[str, Any] | None = None) -> SendResult:
    last_error: Exception | None = None
    plain = False
    attempt = 0
    while attempt < SETTINGS.retry_attempts:
        attempt += 1
        try:
            return await send_saved_post(bot, target_chat_id, post, opts, plain=plain)
        except RetryAfter as exc:
            wait = _retry_seconds(exc)
            if wait > 60:
                raise  # don't hold the chat lock for minutes; the caller defers the chat instead
            await asyncio.sleep(wait + 0.5)
            last_error = exc
        except BadRequest as exc:  # NOTE: must precede NetworkError, BadRequest is a subclass of it
            message = str(exc).lower()
            if not plain and ("parse entities" in message or "can't parse" in message or "unsupported start tag" in message):
                plain = True  # broken HTML in the saved post: send it as plain text instead of losing it
                attempt -= 1
                continue
            raise
        except (TimedOut, NetworkError) as exc:
            await asyncio.sleep(SETTINGS.retry_base_seconds * attempt)
            last_error = exc
    if last_error:
        raise last_error
    raise RuntimeError("send_post_with_retry failed without captured exception")


FATAL_BADREQUEST_MARKERS = (
    "chat not found",
    "not enough rights",
    "have no rights",
    "need administrator rights",
    "chat_write_forbidden",
    "chat_admin_required",
    "peer_id_invalid",
    "bot is not a member",
    "channel_private",
    "group chat was upgraded",
)


def is_fatal_chat_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in FATAL_BADREQUEST_MARKERS)


# --------------------------------------------------------------------------- #
# Admin notifications
# --------------------------------------------------------------------------- #
async def notify_admins(bot, text: str) -> None:
    for admin_id in SETTINGS.admin_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except TelegramError:
            pass  # admin never started the bot / blocked it


# --------------------------------------------------------------------------- #
# Queue + batch engine
# --------------------------------------------------------------------------- #
async def ensure_queue(chat_id: int, chat: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Makes sure there's a running cycle. Loop mode reshuffles everything; one-pass mode only queues
    posts that haven't been published yet."""
    db = STATE.db
    chat = chat or await db.get_chat(chat_id)
    if not chat:
        return None
    qs = chat.get("queue_state") or {}
    queue = qs.get("queue") or []
    if queue and int(qs.get("cursor", 0)) < len(queue):
        return chat
    if chat_opts(chat)["loop"] or int(qs.get("cycle", 0)) == 0:
        return await db.rebuild_cycle(chat_id)
    return await db.rebuild_cycle(chat_id, only_unused=True)


async def rebuild_and_ensure_schedule(chat_id: int) -> dict[str, Any] | None:
    """Manual reshuffle: full rebuild, and make sure an active chat actually has a next slot."""
    await STATE.db.rebuild_cycle(chat_id)
    return await STATE.db.touch_schedule(chat_id)


async def after_posts_added(chat_id: int) -> None:
    """Called after any upload: starts a cycle if none is running and guarantees a next slot."""
    chat = await STATE.db.get_chat(chat_id)
    if not chat:
        return
    qs = chat.get("queue_state") or {}
    queue = qs.get("queue") or []
    if not queue or int(qs.get("cursor", 0)) >= len(queue):
        if chat_opts(chat)["loop"] or int(qs.get("cycle", 0)) == 0:
            await STATE.db.rebuild_cycle(chat_id)
        elif chat.get("is_active", True):
            await STATE.db.rebuild_cycle(chat_id, only_unused=True)
    await STATE.db.touch_schedule(chat_id)


async def _pause_chat(bot, chat_id: int, title: str, reason: str, error: str | None = None, level: str = "error") -> None:
    await STATE.db.set_active(chat_id, False, reason=reason, error=error)
    await STATE.db.log(level, "chat_paused", chat_id=chat_id, reason=reason, error=error)
    await notify_admins(bot, f"⏸ <b>{html.escape(title)}</b> paused\n{html.escape(reason)}")


async def _post_failed(bot, chat_id: int, title: str, post_id: str, exc: Exception, immediate: bool = False) -> None:
    fails = await STATE.db.record_post_failure(chat_id, post_id, f"{type(exc).__name__}: {exc}")
    await STATE.db.chats.update_one({"chat_id": chat_id}, {"$set": {"last_error": f"{type(exc).__name__}: {exc}"[:300], "updated_at": utcnow()}})
    await STATE.db.log("warning", "post_skipped", chat_id=chat_id, post_id=post_id, error=str(exc), fail_count=fails)
    if fails >= QUARANTINE_AFTER or immediate:
        await STATE.db.quarantine_post(chat_id, post_id, str(exc))
        await notify_admins(
            bot,
            f"🚧 A post in <b>{html.escape(title)}</b> failed {fails} times and was removed from rotation "
            f"(id …<code>{post_id[-6:]}</code>).\nReason: <code>{html.escape(str(exc)[:200])}</code>",
        )
    await STATE.db.skip_cursor(chat_id)


async def process_chat_batch(bot, chat_id: int, manual: bool = False) -> int:
    """Publishes up to `batch_size` posts. Never raises for Telegram/data problems — it logs, pauses
    the chat or defers it, and always releases the processing lock."""
    db = STATE.db
    async with STATE.lock_for(chat_id):
        if not await db.acquire_processing(chat_id):
            return 0
        sent = 0
        try:
            chat = await db.get_chat(chat_id)
            if not chat or not chat.get("is_active", True):
                return 0
            opts = chat_opts(chat)
            title = chat.get("title", str(chat_id))
            chat = await ensure_queue(chat_id, chat)
            if not chat:
                return 0
            qs = chat.get("queue_state") or {}
            queue = qs.get("queue") or []
            if not queue or int(qs.get("cursor", 0)) >= len(queue):
                if not opts["loop"] and await db.count_posts(chat_id) > 0:
                    await _pause_chat(bot, chat_id, title, "One-pass mode: every post has been published. Upload more posts and press Resume, or reshuffle to start over.", level="info")
                    return 0
                await db.finalize_batch(chat_id, 0, chat_next_run(chat, utcnow() + timedelta(seconds=1)))
                return 0

            batch_size = max(1, int((chat.get("schedule") or {}).get("batch_size", 5)))
            failures = 0
            defer_seconds = 0
            stopped = False  # chat paused/disabled during this batch

            while sent < batch_size:
                chat = await db.get_chat(chat_id)
                if not chat or not chat.get("is_active", True):
                    stopped = True
                    break
                qs = chat.get("queue_state") or {}
                queue = qs.get("queue") or []
                cursor = int(qs.get("cursor", 0))
                if cursor >= len(queue):
                    break
                post_id = queue[cursor]
                post = await db.get_post(chat_id, post_id)
                if not post:
                    await db.skip_cursor(chat_id)
                    continue
                try:
                    result = await send_post_with_retry(bot, chat_id, post, opts)
                except BadRequest as exc:
                    if is_fatal_chat_error(exc):
                        await _pause_chat(bot, chat_id, title, f"Telegram refused to post: {exc}", error=f"BadRequest: {exc}")
                        stopped = True
                        break
                    await _post_failed(bot, chat_id, title, post_id, exc, immediate=post.get("media_type") in COPY_TYPES and ("not found" in str(exc).lower() or "can't be copied" in str(exc).lower()))
                    failures += 1
                except Forbidden as exc:
                    if post.get("media_type") in COPY_TYPES:
                        await _post_failed(bot, chat_id, title, post_id, exc)
                        failures += 1
                    else:
                        await _pause_chat(bot, chat_id, title, "Bot lost access (removed, blocked or lacks permission to post).", error=f"Forbidden: {exc}")
                        stopped = True
                        break
                except ChatMigrated as exc:
                    await _pause_chat(bot, chat_id, title, f"This group was upgraded to a supergroup (new id {exc.new_chat_id}). Add the new one as a fresh target.", error=str(exc))
                    stopped = True
                    break
                except RetryAfter as exc:
                    defer_seconds = int(math.ceil(_retry_seconds(exc))) + 5
                    await db.log("warning", "flood_wait", chat_id=chat_id, seconds=defer_seconds)
                    break
                except (TimedOut, NetworkError, TelegramError) as exc:
                    defer_seconds = 120
                    await db.log("warning", "telegram_unavailable", chat_id=chat_id, error=f"{type(exc).__name__}: {exc}")
                    break
                except (KeyError, ValueError, TypeError, AttributeError) as exc:  # malformed saved post
                    await _post_failed(bot, chat_id, title, post_id, exc)
                    failures += 1
                else:
                    ids = result.message_ids
                    await db.advance_after_success(chat_id, post_id, result.message_id, ids)
                    sent += 1
                    if opts["auto_delete_minutes"] > 0:
                        await db.schedule_delete(chat_id, ids, utcnow() + timedelta(minutes=int(opts["auto_delete_minutes"])))
                    if post.get("pin"):
                        try:
                            await bot.pin_chat_message(chat_id=chat_id, message_id=result.message_id, disable_notification=True)
                        except TelegramError as exc:
                            await db.log("warning", "pin_failed", chat_id=chat_id, error=str(exc))
                    await db.refresh_processing(chat_id)
                    if sent < batch_size and SETTINGS.post_gap_seconds:
                        await asyncio.sleep(SETTINGS.post_gap_seconds)
                if failures >= MAX_FAILURES_PER_BATCH:
                    defer_seconds = 300
                    break

            if stopped:
                return sent

            chat = await db.get_chat(chat_id)
            if not chat:
                return sent
            qs = chat.get("queue_state") or {}
            queue = qs.get("queue") or []
            exhausted = bool(queue) and int(qs.get("cursor", 0)) >= len(queue)
            if exhausted and await db.mark_cycle_counted(chat_id):
                cycle_no = int(qs.get("cycle", 0))
                if opts["notify_cycle"] and opts["loop"]:
                    await notify_admins(bot, f"🔁 Cycle <b>{cycle_no}</b> completed in <b>{html.escape(title)}</b> — reshuffling for the next round.")
                if not opts["loop"]:
                    await _pause_chat(bot, chat_id, title, f"One-pass mode: all posts published (cycle {cycle_no}).", level="info")
                    return sent

            next_run = utcnow() + timedelta(seconds=defer_seconds) if defer_seconds else chat_next_run(chat, utcnow() + timedelta(seconds=1))
            await db.finalize_batch(chat_id, sent, next_run)
            if sent or manual:
                await db.log("info", "batch_processed", chat_id=chat_id, sent_count=sent)
            return sent
        finally:
            await db.release_processing(chat_id)


async def send_single_now(bot, chat_id: int, post: dict[str, Any]) -> SendResult:
    """Publishes one specific saved post immediately (outside the queue) and records it in history."""
    chat = await STATE.db.get_chat(chat_id)
    opts = chat_opts(chat)
    result = await send_post_with_retry(bot, chat_id, post, opts)
    now = utcnow()
    await STATE.db.posts_coll(chat_id).update_one({"_id": post["_id"]}, {"$set": {"last_posted_at": now, "updated_at": now}, "$inc": {"posted_count": 1}})
    await STATE.db.history_coll(chat_id).insert_one(
        {"post_id": str(post["_id"]), "telegram_message_id": result.message_id, "message_ids": result.message_ids, "posted_at": now, "cycle": int(((chat or {}).get("queue_state") or {}).get("cycle", 0)), "manual": True}
    )
    if opts["auto_delete_minutes"] > 0:
        await STATE.db.schedule_delete(chat_id, result.message_ids, now + timedelta(minutes=int(opts["auto_delete_minutes"])))
    return result
