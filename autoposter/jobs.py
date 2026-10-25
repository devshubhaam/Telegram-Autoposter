"""Background jobs run by PTB's JobQueue."""
from __future__ import annotations

import asyncio
import gzip
import io

from telegram import InputFile
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .config import SETTINGS
from .database import dumps_json
from .sender import process_chat_batch
from .state import STATE
from .utils import utcnow

MAX_PARALLEL_CHATS = 4
GZIP_ABOVE_BYTES = 15 * 1024 * 1024  # Telegram bots can upload up to 50 MB; compress big backups


async def scheduler_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every SCAN_INTERVAL_SECONDS: publishes for every chat whose slot has arrived."""
    db = STATE.db
    due = await db.due_chats(utcnow())
    if not due:
        return
    sem = asyncio.Semaphore(MAX_PARALLEL_CHATS)

    async def run(chat_id: int) -> None:
        async with sem:
            try:
                await process_chat_batch(context.bot, chat_id)
            except Exception as exc:  # noqa: BLE001 - one broken chat must never stop the others
                await db.log("error", "batch_crashed", chat_id=chat_id, error=f"{type(exc).__name__}: {exc}")
                await db.defer_chat(chat_id, 120, error=f"{type(exc).__name__}: {exc}")

    await asyncio.gather(*(run(chat["chat_id"]) for chat in due))


async def housekeeping_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every minute: removes posts whose auto-delete time has come."""
    db = STATE.db
    for row in await db.due_deletes(utcnow()):
        for message_id in row.get("message_ids", []):
            try:
                await context.bot.delete_message(chat_id=row["chat_id"], message_id=message_id)
            except TelegramError:
                pass  # already deleted by an admin, or too old / no rights
        await db.remove_delete(row["_id"])


def build_backup_file(payload: dict) -> tuple[bytes, str]:
    blob = dumps_json(payload)
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    if len(blob) > GZIP_ABOVE_BYTES:
        return gzip.compress(blob), f"backup-{stamp}.json.gz"
    return blob, f"backup-{stamp}.json"


async def daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """03:00 local time: DM a backup to every admin and trim old logs."""
    db = STATE.db
    try:
        removed = await db.cleanup_logs(SETTINGS.log_retention_days)
        payload = await db.backup_payload()
        blob, filename = build_backup_file(payload)
        for admin_id in SETTINGS.admin_ids:
            try:
                await context.bot.send_document(
                    admin_id,
                    document=InputFile(io.BytesIO(blob), filename="auto" + filename),
                    caption=f"🗓 Automatic daily backup ✅\nTargets: {len(payload.get('chats', []))}",
                )
            except TelegramError:
                pass  # admin hasn't started the bot privately, or blocked it
        await db.log("info", "auto_backup_sent", admins=len(SETTINGS.admin_ids), logs_removed=removed)
    except Exception as exc:  # noqa: BLE001 - never let a backup failure kill the scheduler
        await db.log("error", "auto_backup_failed", error=f"{type(exc).__name__}: {exc}")

