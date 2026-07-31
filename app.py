import asyncio
import html
import io
import json
import logging
import os
import random
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bson import ObjectId
from bson import json_util
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, ReturnDocument
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, InputMediaPhoto, InputMediaVideo, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger("telegram_autoposter")


@dataclass(slots=True)
class Settings:
    bot_token: str
    mongo_uri: str
    mongo_db: str
    admin_ids: set[int]
    timezone: str
    scan_interval_seconds: int
    default_interval_minutes: int
    default_batch_size: int
    default_start_time: str
    default_end_time: str
    default_days: list[int]
    scheduler_lock_seconds: int
    retry_attempts: int
    retry_base_seconds: float
    admin_password: str | None = None
    password_timeout_seconds: int = 300

    @classmethod
    def from_env(cls) -> "Settings":
        admin_ids = {
            int(part.strip())
            for part in os.getenv("ADMIN_IDS", "").split(",")
            if part.strip()
        }
        if not admin_ids:
            raise RuntimeError("ADMIN_IDS is required")
        return cls(
            bot_token=os.environ["BOT_TOKEN"],
            mongo_uri=os.environ["MONGO_URI"],
            mongo_db=os.getenv("MONGO_DB", "telegram_autoposter"),
            admin_ids=admin_ids,
            timezone=os.getenv("TIMEZONE", "UTC"),
            scan_interval_seconds=int(os.getenv("SCAN_INTERVAL_SECONDS", "30")),
            default_interval_minutes=int(os.getenv("DEFAULT_INTERVAL_MINUTES", "180")),
            default_batch_size=int(os.getenv("DEFAULT_BATCH_SIZE", "5")),
            default_start_time=os.getenv("DEFAULT_START_TIME", "00:00"),
            default_end_time=os.getenv("DEFAULT_END_TIME", "21:00"),
            default_days=[
                int(x)
                for x in os.getenv("DEFAULT_DAYS", "0,1,2,3,4,5,6").split(",")
                if x.strip()
            ],
            scheduler_lock_seconds=int(os.getenv("SCHEDULER_LOCK_SECONDS", "300")),
            retry_attempts=int(os.getenv("RETRY_ATTEMPTS", "3")),
            retry_base_seconds=float(os.getenv("RETRY_BASE_SECONDS", "2")),
            admin_password=(os.getenv("ADMIN_PASSWORD", "").strip() or None),
            password_timeout_seconds=int(os.getenv("PASSWORD_TIMEOUT_SECONDS", "300")),
        )


SETTINGS = Settings.from_env()
TZ = ZoneInfo(SETTINGS.timezone)
BOT_USERNAME = ""


def utcnow() -> datetime:
    return datetime.now(UTC)


def coll_suffix(chat_id: int) -> str:
    return f"m_{abs(chat_id)}" if chat_id < 0 else f"p_{chat_id}"


def progress_bar(done: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "░" * width
    filled = min(width, round((done / total) * width))
    return "█" * filled + "░" * (width - filled)


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(hour=int(hour), minute=int(minute))


def parse_days(value: str) -> list[int]:
    result = sorted({int(part.strip()) for part in value.split(",") if part.strip()})
    if not result or any(day not in range(7) for day in result):
        raise ValueError("Days must be comma separated weekday numbers 0-6")
    return result


def parse_post_link_or_id(value: str) -> int | None:
    value = (value or "").strip().split("?", 1)[0].rstrip("/")
    if not value:
        return None
    if value.isdigit():
        return int(value)
    parts = [p for p in value.split("/") if p]
    if parts and parts[-1].isdigit():
        return int(parts[-1])
    return None


def day_labels(days: Iterable[int]) -> str:
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return ", ".join(names[d] for d in sorted(days))


def format_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M %Z")


def default_schedule() -> dict[str, Any]:
    return {
        "enabled": True,
        "interval_minutes": SETTINGS.default_interval_minutes,
        "batch_size": SETTINGS.default_batch_size,
        "start_time": SETTINGS.default_start_time,
        "end_time": SETTINGS.default_end_time,
        "days": SETTINGS.default_days,
    }


def render_buttons(buttons: list[list[dict[str, str]]] | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    rows = []
    for row in buttons:
        rendered = []
        for item in row:
            if item.get("url"):
                rendered.append(InlineKeyboardButton(item["text"], url=item["url"]))
        if rendered:
            rows.append(rendered)
    return InlineKeyboardMarkup(rows) if rows else None


def parse_post_markup(raw: str | None) -> tuple[str, dict[str, Any]]:
    text = (raw or "").replace("\r\n", "\n").strip()
    flags = {"silent": False, "protect_content": False, "spoiler": False, "buttons": []}
    if not text:
        return "", flags
    lines = text.split("\n")
    content_lines: list[str] = []
    button_mode = False
    button_lines: list[str] = []
    for line in lines:
        clean = line.strip()
        lowered = clean.lower()
        if lowered == "---buttons---":
            button_mode = True
            continue
        if button_mode:
            if clean:
                button_lines.append(clean)
            continue
        if lowered == "#silent":
            flags["silent"] = True
            continue
        if lowered == "#protect":
            flags["protect_content"] = True
            continue
        if lowered == "#spoiler":
            flags["spoiler"] = True
            continue
        content_lines.append(line)

    buttons: list[list[dict[str, str]]] = []
    for line in button_lines:
        row: list[dict[str, str]] = []
        for part in [p.strip() for p in line.split(";") if p.strip()]:
            if "|" not in part:
                continue
            label, url = [x.strip() for x in part.split("|", 1)]
            if label and url:
                row.append({"text": label[:64], "url": url})
        if row:
            buttons.append(row)
    flags["buttons"] = buttons
    return "\n".join(content_lines).strip(), flags


def compute_next_run(after_utc: datetime, schedule: dict[str, Any]) -> datetime | None:
    if not schedule.get("enabled", True):
        return None
    allowed_days = set(schedule.get("days") or list(range(7)))
    interval = max(1, int(schedule.get("interval_minutes", 180)))
    start_t = parse_hhmm(schedule.get("start_time", "00:00"))
    end_t = parse_hhmm(schedule.get("end_time", "23:59"))
    after_local = after_utc.astimezone(TZ)

    for offset in range(15):
        day = after_local.date() + timedelta(days=offset)
        if day.weekday() not in allowed_days:
            continue
        start_dt = datetime.combine(day, start_t, tzinfo=TZ)
        end_dt = datetime.combine(day, end_t, tzinfo=TZ)
        if end_dt < start_dt:
            end_dt += timedelta(days=1)
        slot = start_dt
        while slot <= end_dt:
            if slot > after_local:
                return slot.astimezone(UTC)
            slot += timedelta(minutes=interval)
    return None


class Database:
    def __init__(self, settings: Settings) -> None:
        self.client = AsyncIOMotorClient(settings.mongo_uri)
        self.db = self.client[settings.mongo_db]
        self.chats = self.db["chats"]
        self.admins = self.db["admins"]
        self.logs = self.db["logs"]

    def posts_coll(self, chat_id: int):
        return self.db[f"posts_{coll_suffix(chat_id)}"]

    def history_coll(self, chat_id: int):
        return self.db[f"history_{coll_suffix(chat_id)}"]

    async def init(self) -> None:
        await self.chats.create_index([("chat_id", ASCENDING)], unique=True)
        await self.chats.create_index([("next_post_time", ASCENDING)])
        await self.chats.create_index([("processing_until", ASCENDING)])
        await self.admins.create_index([("admin_id", ASCENDING)], unique=True)
        await self.logs.create_index([("created_at", ASCENDING)])

    async def log(self, level: str, event: str, **payload: Any) -> None:
        doc = {"level": level, "event": event, "payload": payload, "created_at": utcnow()}
        await self.logs.insert_one(doc)
        getattr(logger, level.lower(), logger.info)("%s | %s", event, payload)

    async def ensure_chat(self, chat_id: int, title: str, chat_type: str, username: str | None = None) -> dict[str, Any]:
        now = utcnow()
        doc = await self.chats.find_one_and_update(
            {"chat_id": chat_id},
            {
                "$set": {
                    "title": title,
                    "type": chat_type,
                    "username": username,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "created_at": now,
                    "is_active": True,
                    "schedule": default_schedule(),
                    "queue_state": {
                        "cycle": 0,
                        "cursor": 0,
                        "queue": [],
                        "shuffle_status": "idle",
                        "last_batch_size": 0,
                        "last_posted_at": None,
                        "last_reshuffled_at": None,
                    },
                    "stats": {"total_cycles_completed": 0},
                    "next_post_time": compute_next_run(now, default_schedule()),
                    "processing_until": None,
                    "last_error": None,
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        await self.posts_coll(chat_id).create_index([("created_at", ASCENDING)])
        await self.posts_coll(chat_id).create_index([("used_in_cycle", ASCENDING)])
        await self.history_coll(chat_id).create_index([("posted_at", ASCENDING)])
        return doc

    async def list_chats(self, include_hidden: bool = True) -> list[dict[str, Any]]:
        query = {} if include_hidden else {"hidden_source_only": {"$ne": True}}
        cursor = self.chats.find(query).sort("title", ASCENDING)
        return await cursor.to_list(length=500)

    async def get_chat(self, chat_id: int) -> dict[str, Any] | None:
        return await self.chats.find_one({"chat_id": chat_id})

    async def set_active_target(self, admin_id: int, chat_id: int | None) -> None:
        await self.admins.find_one_and_update(
            {"admin_id": admin_id},
            {"$set": {"target_chat_id": chat_id, "updated_at": utcnow()}},
            upsert=True,
        )

    async def get_active_target(self, admin_id: int) -> int | None:
        row = await self.admins.find_one({"admin_id": admin_id})
        return row.get("target_chat_id") if row else None

    async def count_posts(self, chat_id: int) -> int:
        return await self.posts_coll(chat_id).count_documents({"active": True})

    async def count_used_posts(self, chat_id: int) -> int:
        return await self.posts_coll(chat_id).count_documents({"active": True, "used_in_cycle": True})

    async def undo_last_post(self, chat_id: int) -> dict[str, Any] | None:
        post = await self.posts_coll(chat_id).find_one({"active": True}, sort=[("created_at", -1)])
        if not post:
            return None
        await self.posts_coll(chat_id).delete_one({"_id": post["_id"]})
        return post

    async def wipe_chat(self, chat_id: int) -> None:
        await self.posts_coll(chat_id).delete_many({})
        await self.history_coll(chat_id).delete_many({})
        await self.chats.update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "queue_state": {
                        "cycle": 0,
                        "cursor": 0,
                        "queue": [],
                        "shuffle_status": "idle",
                        "last_batch_size": 0,
                        "last_posted_at": None,
                        "last_reshuffled_at": None,
                    },
                    "stats.total_cycles_completed": 0,
                    "next_post_time": None,
                    "updated_at": utcnow(),
                }
            },
        )

    async def delete_chat(self, chat_id: int) -> None:
        await self.posts_coll(chat_id).drop()
        await self.history_coll(chat_id).drop()
        await self.chats.delete_one({"chat_id": chat_id})
        await self.admins.update_many({"target_chat_id": chat_id}, {"$set": {"target_chat_id": None}})

    async def mark_hidden_source(self, chat_id: int) -> None:
        """Hides a channel from Targets/Upload pickers and stops it from being scheduled.
        Used for channels only added so the bot can copy their existing posts elsewhere."""
        await self.chats.update_one(
            {"chat_id": chat_id},
            {"$set": {"hidden_source_only": True, "is_active": False, "next_post_time": None, "updated_at": utcnow()}},
        )

    async def wipe_all(self) -> None:
        chats = await self.list_chats()
        for chat in chats:
            await self.posts_coll(chat["chat_id"]).drop()
            await self.history_coll(chat["chat_id"]).drop()
        await self.chats.delete_many({})
        await self.admins.delete_many({})
        await self.logs.delete_many({})
        await self.init()

    async def enqueue_new_posts(self, chat_id: int, post_ids: list[str]) -> None:
        if not post_ids:
            return
        chat = await self.get_chat(chat_id)
        if not chat:
            return
        queue_state = chat.get("queue_state", {}) or {}
        queue: list[str] = list(queue_state.get("queue") or [])
        cursor = int(queue_state.get("cursor", 0))
        if not queue or cursor >= len(queue):
            return  # no cycle currently in progress; the next rebuild will pick these up fresh
        already_sent = queue[:cursor]
        remaining = queue[cursor:] + [str(pid) for pid in post_ids]
        random.shuffle(remaining)
        await self.chats.update_one(
            {"chat_id": chat_id},
            {"$set": {"queue_state.queue": already_sent + remaining, "queue_state.shuffle_status": "ready", "updated_at": utcnow()}},
        )

    async def add_post(self, chat_id: int, payload: dict[str, Any]) -> str:
        payload.update(
            {
                "active": True,
                "used_in_cycle": False,
                "posted_count": payload.get("posted_count", 0),
                "created_at": payload.get("created_at", utcnow()),
                "updated_at": utcnow(),
            }
        )
        result = await self.posts_coll(chat_id).insert_one(payload)
        await self.enqueue_new_posts(chat_id, [str(result.inserted_id)])
        await self.chats.update_one({"chat_id": chat_id}, {"$set": {"updated_at": utcnow()}})
        return str(result.inserted_id)

    async def bulk_add_posts(self, chat_id: int, posts: list[dict[str, Any]]) -> int:
        if not posts:
            return 0
        now = utcnow()
        for post in posts:
            post.setdefault("active", True)
            post.setdefault("used_in_cycle", False)
            post.setdefault("posted_count", 0)
            post.setdefault("created_at", now)
            post["updated_at"] = now
        result = await self.posts_coll(chat_id).insert_many(posts)
        await self.enqueue_new_posts(chat_id, [str(i) for i in result.inserted_ids])
        await self.chats.update_one({"chat_id": chat_id}, {"$set": {"updated_at": now}})
        return len(result.inserted_ids)

    async def get_post(self, chat_id: int, post_id: str) -> dict[str, Any] | None:
        return await self.posts_coll(chat_id).find_one({"_id": ObjectId(post_id), "active": True})

    async def recent_posts(self, chat_id: int, limit: int = 10) -> list[dict[str, Any]]:
        return await self.posts_coll(chat_id).find({"active": True}).sort("created_at", -1).to_list(length=limit)

    async def update_schedule(
        self,
        chat_id: int,
        *,
        interval_minutes: int,
        batch_size: int,
        start_time: str,
        end_time: str,
        days: list[int],
        enabled: bool = True,
    ) -> dict[str, Any]:
        schedule = {
            "enabled": enabled,
            "interval_minutes": interval_minutes,
            "batch_size": batch_size,
            "start_time": start_time,
            "end_time": end_time,
            "days": days,
        }
        next_run = compute_next_run(utcnow(), schedule)
        doc = await self.chats.find_one_and_update(
            {"chat_id": chat_id},
            {"$set": {"schedule": schedule, "next_post_time": next_run, "updated_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
        return doc

    async def acquire_processing(self, chat_id: int) -> bool:
        now = utcnow()
        until = now + timedelta(seconds=SETTINGS.scheduler_lock_seconds)
        result = await self.chats.find_one_and_update(
            {
                "chat_id": chat_id,
                "$or": [
                    {"processing_until": None},
                    {"processing_until": {"$lt": now}},
                ],
            },
            {"$set": {"processing_until": until}},
            return_document=ReturnDocument.AFTER,
        )
        return result is not None

    async def release_processing(self, chat_id: int) -> None:
        await self.chats.update_one({"chat_id": chat_id}, {"$set": {"processing_until": None}})

    async def rebuild_cycle(self, chat_id: int) -> dict[str, Any]:
        chat = await self.get_chat(chat_id)
        post_ids = [str(row["_id"]) async for row in self.posts_coll(chat_id).find({"active": True}, {"_id": 1})]
        random.shuffle(post_ids)
        previous_cycle = int(chat.get("queue_state", {}).get("cycle", 0)) if chat else 0
        previous_queue = chat.get("queue_state", {}).get("queue", []) if chat else []
        previous_cursor = int(chat.get("queue_state", {}).get("cursor", 0)) if chat else 0
        cycle_completed = previous_cycle > 0 and previous_cursor >= len(previous_queue)
        if cycle_completed:
            await self.chats.update_one({"chat_id": chat_id}, {"$inc": {"stats.total_cycles_completed": 1}})
        await self.posts_coll(chat_id).update_many({"active": True}, {"$set": {"used_in_cycle": False}})
        next_cycle = previous_cycle + 1 if post_ids else previous_cycle
        queue_state = {
            "cycle": next_cycle,
            "cursor": 0,
            "queue": post_ids,
            "shuffle_status": "ready" if post_ids else "empty",
            "last_batch_size": 0,
            "last_posted_at": chat.get("queue_state", {}).get("last_posted_at") if chat else None,
            "last_reshuffled_at": utcnow(),
        }
        updated = await self.chats.find_one_and_update(
            {"chat_id": chat_id},
            {"$set": {"queue_state": queue_state, "updated_at": utcnow(), "last_error": None}},
            return_document=ReturnDocument.AFTER,
        )
        return updated

    async def advance_after_success(self, chat_id: int, post_id: str, telegram_message_id: int) -> dict[str, Any]:
        chat = await self.get_chat(chat_id)
        queue_state = chat["queue_state"]
        new_cursor = int(queue_state.get("cursor", 0)) + 1
        await self.posts_coll(chat_id).update_one(
            {"_id": ObjectId(post_id)},
            {
                "$set": {"used_in_cycle": True, "last_posted_at": utcnow(), "updated_at": utcnow()},
                "$inc": {"posted_count": 1},
            },
        )
        await self.history_coll(chat_id).insert_one(
            {"post_id": post_id, "telegram_message_id": telegram_message_id, "posted_at": utcnow(), "cycle": queue_state.get("cycle", 0)}
        )
        updated = await self.chats.find_one_and_update(
            {"chat_id": chat_id},
            {
                "$set": {
                    "queue_state.cursor": new_cursor,
                    "queue_state.last_posted_at": utcnow(),
                    "updated_at": utcnow(),
                    "last_error": None,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        return updated

    async def finalize_batch(self, chat_id: int, sent_count: int, next_run: datetime | None) -> dict[str, Any]:
        chat = await self.get_chat(chat_id)
        queue_state = chat.get("queue_state", {})
        queue = queue_state.get("queue", [])
        cursor = int(queue_state.get("cursor", 0))
        shuffle_status = "cycle complete • awaiting next shuffle" if queue and cursor >= len(queue) else "in progress"
        updated = await self.chats.find_one_and_update(
            {"chat_id": chat_id},
            {
                "$set": {
                    "next_post_time": next_run,
                    "queue_state.last_batch_size": sent_count,
                    "queue_state.shuffle_status": shuffle_status,
                    "updated_at": utcnow(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        return updated

    async def due_chats(self, now: datetime) -> list[dict[str, Any]]:
        cursor = self.chats.find({"is_active": True, "next_post_time": {"$ne": None, "$lte": now}}).sort("next_post_time", ASCENDING)
        return await cursor.to_list(length=200)

    async def published_today(self, chat_id: int) -> int:
        today_local = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        return await self.history_coll(chat_id).count_documents({"posted_at": {"$gte": today_local.astimezone(UTC)}})

    async def queue_snapshot(self, chat_id: int) -> dict[str, Any]:
        chat = await self.get_chat(chat_id)
        total = await self.count_posts(chat_id)
        used = await self.count_used_posts(chat_id)
        queue_state = chat.get("queue_state", {})
        cursor = int(queue_state.get("cursor", 0))
        queue = queue_state.get("queue", [])
        next_batch = queue[cursor : cursor + int(chat.get("schedule", {}).get("batch_size", 5))]
        return {
            "total": total,
            "used": used,
            "remaining": max(total - used, 0),
            "cycle": int(queue_state.get("cycle", 0)),
            "current_cursor": cursor,
            "next_batch_ids": next_batch,
            "queue_size": len(queue),
            "shuffle_status": queue_state.get("shuffle_status", "idle"),
            "last_posted_at": queue_state.get("last_posted_at"),
            "next_post_time": chat.get("next_post_time"),
        }

    async def global_stats(self) -> dict[str, Any]:
        chats = await self.list_chats(include_hidden=False)
        total_channels = sum(1 for c in chats if c.get("type") == "channel")
        total_groups = sum(1 for c in chats if c.get("type") != "channel")
        total_cycles_completed = sum(int(c.get("stats", {}).get("total_cycles_completed", 0)) for c in chats)
        next_times = [c["next_post_time"] for c in chats if c.get("next_post_time")]

        async def per_chat_stats(chat_id: int) -> tuple[int, int, int]:
            posts, used, today = await asyncio.gather(
                self.count_posts(chat_id),
                self.count_used_posts(chat_id),
                self.published_today(chat_id),
            )
            return posts, used, today

        results = await asyncio.gather(*(per_chat_stats(chat["chat_id"]) for chat in chats)) if chats else []
        total_posts = sum(r[0] for r in results)
        total_used = sum(r[1] for r in results)
        published_today = sum(r[2] for r in results)
        return {
            "total_posts": total_posts,
            "remaining_posts": max(total_posts - total_used, 0),
            "published_today": published_today,
            "total_cycles_completed": total_cycles_completed,
            "total_channels": total_channels,
            "total_groups": total_groups,
            "next_post_time": min(next_times) if next_times else None,
        }

    async def backup_payload(self) -> dict[str, Any]:
        chats = await self.list_chats()
        payload: dict[str, Any] = {"exported_at": utcnow().isoformat(), "chats": [], "posts": {}}
        for chat in chats:
            cid = str(chat["chat_id"])
            payload["chats"].append({k: v for k, v in chat.items() if k != "_id"})
            rows = []
            async for row in self.posts_coll(chat["chat_id"]).find({}):
                row["_id"] = str(row["_id"])
                rows.append(row)
            payload["posts"][cid] = rows
        return payload

    async def restore_payload(self, payload: dict[str, Any]) -> tuple[int, int]:
        restored_chats = 0
        restored_posts = 0
        for chat in payload.get("chats", []):
            chat_id = int(chat["chat_id"])
            chat.pop("_id", None)
            await self.chats.replace_one({"chat_id": chat_id}, chat, upsert=True)
            restored_chats += 1
        for raw_chat_id, posts in payload.get("posts", {}).items():
            chat_id = int(raw_chat_id)
            await self.posts_coll(chat_id).delete_many({})
            to_insert = []
            for row in posts:
                row = dict(row)
                row["_id"] = ObjectId(row["_id"]) if row.get("_id") else ObjectId()
                to_insert.append(row)
            if to_insert:
                await self.posts_coll(chat_id).insert_many(to_insert)
                restored_posts += len(to_insert)
        return restored_chats, restored_posts


class AppState:
    def __init__(self) -> None:
        self.db = Database(SETTINGS)
        self.scheduler = AsyncIOScheduler(timezone=TZ)
        self.posting_locks: dict[int, asyncio.Lock] = {}

    def lock_for(self, chat_id: int) -> asyncio.Lock:
        lock = self.posting_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self.posting_locks[chat_id] = lock
        return lock


STATE = AppState()


def is_admin(user_id: int | None) -> bool:
    return bool(user_id and user_id in SETTINGS.admin_ids)


async def admin_doc(admin_id: int) -> dict[str, Any]:
    return await STATE.db.admins.find_one({"admin_id": admin_id}) or {}


async def set_admin_meta(admin_id: int, **fields: Any) -> None:
    fields["updated_at"] = utcnow()
    await STATE.db.admins.update_one({"admin_id": admin_id}, {"$set": fields}, upsert=True)


async def clear_admin_mode(admin_id: int) -> None:
    await STATE.db.admins.update_one(
        {"admin_id": admin_id},
        {"$unset": {"mode": "", "mode_chat_id": "", "mode_extra": ""}, "$set": {"updated_at": utcnow()}},
        upsert=True,
    )


async def get_target_chat(admin_id: int) -> dict[str, Any] | None:
    target_chat_id = await STATE.db.get_active_target(admin_id)
    if target_chat_id is None:
        return None
    return await STATE.db.get_chat(target_chat_id)


def target_name(chat: dict[str, Any] | None) -> str:
    if not chat:
        return "No target selected"
    handle = f"@{chat['username']}" if chat.get("username") else str(chat["chat_id"])
    icon = "📣" if chat.get("type") == "channel" else "👥"
    return f"{icon} {chat.get('title', 'Untitled')} • {handle}"


def trim(text: str | None, limit: int = 42) -> str:
    if not text:
        return "(empty)"
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def post_label(post: dict[str, Any]) -> str:
    media = post.get("media_type", "text")
    icon = {
        "text": "📝",
        "photo": "🖼️",
        "video": "🎬",
        "animation": "✨",
        "document": "📄",
        "audio": "🎵",
        "album": "🖼️🎬",
    }.get(media, "📦")
    if media == "album":
        count = len(post.get("media_group", []))
        base = post.get("caption") or f"Album ({count} items)"
        return f"{icon} {trim(base, 28)} ({count})"
    base = post.get("text") or post.get("caption") or post.get("file_name") or media
    return f"{icon} {trim(base, 28)}"


def dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Dashboard", callback_data="nav:dashboard"), InlineKeyboardButton("🧭 Targets", callback_data="nav:targets")],
            [InlineKeyboardButton("🗂 Queue", callback_data="nav:queue"), InlineKeyboardButton("📅 Schedule", callback_data="nav:schedule")],
            [InlineKeyboardButton("📝 Posts", callback_data="nav:posts"), InlineKeyboardButton("📈 Stats", callback_data="nav:stats")],
            [InlineKeyboardButton("⚙️ Settings", callback_data="nav:settings"), InlineKeyboardButton("📜 Logs", callback_data="nav:logs")],
            [InlineKeyboardButton("🗃 Database", callback_data="nav:database"), InlineKeyboardButton("💾 Backup", callback_data="nav:backup")],
            [InlineKeyboardButton("⬆️ Upload Mode", callback_data="nav:upload")],
        ]
    )


async def targets_keyboard() -> InlineKeyboardMarkup:
    chats = await STATE.db.list_chats(include_hidden=False)
    rows: list[list[InlineKeyboardButton]] = []
    for chat in chats[:40]:
        icon = "📣" if chat.get("type") == "channel" else "👥"
        rows.append([InlineKeyboardButton(f"{icon} {trim(chat['title'], 36)}", callback_data=f"chat:open:{chat['chat_id']}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="nav:dashboard")])
    return InlineKeyboardMarkup(rows)


async def upload_targets_keyboard() -> InlineKeyboardMarkup:
    chats = await STATE.db.list_chats(include_hidden=False)
    rows: list[list[InlineKeyboardButton]] = []
    for chat in chats[:40]:
        icon = "📣" if chat.get("type") == "channel" else "👥"
        rows.append([InlineKeyboardButton(f"{icon} {trim(chat['title'], 36)}", callback_data=f"up:pick:{chat['chat_id']}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="nav:dashboard")])
    return InlineKeyboardMarkup(rows)


def confirm_keyboard(yes_data: str, no_data: str, yes_label: str = "✅ Yes, confirm") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(yes_label, callback_data=yes_data), InlineKeyboardButton("❌ Cancel", callback_data=no_data)]])


async def chat_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    chat = await STATE.db.get_chat(chat_id)
    active = bool(chat.get("is_active", True)) if chat else True
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎯 Select target", callback_data=f"chat:pick:{chat_id}"), InlineKeyboardButton("🗂 Queue", callback_data=f"chat:queue:{chat_id}")],
            [InlineKeyboardButton("📅 Schedule", callback_data=f"chat:schedule:{chat_id}"), InlineKeyboardButton("📝 Posts", callback_data=f"chat:posts:{chat_id}")],
            [InlineKeyboardButton("🔀 Reshuffle", callback_data=f"chat:reshuffle:{chat_id}"), InlineKeyboardButton("⏭ Post now", callback_data=f"chat:run:{chat_id}")],
            [InlineKeyboardButton("↩️ Undo last post", callback_data=f"chat:undo:{chat_id}"), InlineKeyboardButton("🧹 Wipe this channel", callback_data=f"chat:wipe:{chat_id}")],
            [InlineKeyboardButton("📌 Send & Pin Prompt", callback_data=f"chat:promo:{chat_id}")],
            [InlineKeyboardButton("🔒 Force-Sub Gate", callback_data=f"chat:forcesub:{chat_id}")],
            [InlineKeyboardButton("⏸ Pause" if active else "▶️ Resume", callback_data=f"chat:toggle:{chat_id}"), InlineKeyboardButton("🗑 Remove channel/group", callback_data=f"chat:remove:{chat_id}")],
            [InlineKeyboardButton("⬅️ Targets", callback_data="nav:targets")],
        ]
    )


async def dashboard_text(admin_id: int) -> str:
    stats, target, chats = await asyncio.gather(
        STATE.db.global_stats(),
        get_target_chat(admin_id),
        STATE.db.list_chats(include_hidden=False),
    )
    return (
        "🌙 <b>Auto Posting Control Center</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Active target</b>\n{html.escape(target_name(target))}\n\n"
        f"📣 Channels: <b>{stats['total_channels']}</b>\n"
        f"👥 Groups: <b>{stats['total_groups']}</b>\n"
        f"📝 Total posts: <b>{stats['total_posts']}</b>\n"
        f"📦 Remaining posts: <b>{stats['remaining_posts']}</b>\n"
        f"🚀 Published today: <b>{stats['published_today']}</b>\n"
        f"🔁 Cycles completed: <b>{stats['total_cycles_completed']}</b>\n"
        f"⏰ Next post: <b>{html.escape(format_dt(stats['next_post_time']))}</b>\n\n"
        f"✨ Registered targets: <b>{len(chats)}</b>\n"
        "Tip: add the bot as admin to channels/groups, then open 🧭 Targets."
    )


async def render_chat_overview(chat_id: int) -> str:
    chat = await STATE.db.get_chat(chat_id)
    snap = await STATE.db.queue_snapshot(chat_id)
    schedule = chat.get("schedule", {})
    handle = f"@{chat['username']}" if chat.get("username") else str(chat['chat_id'])
    done = snap["used"]
    total = max(snap["total"], 1)
    return (
        f"{'📣' if chat.get('type') == 'channel' else '👥'} <b>{html.escape(chat['title'])}</b>\n"
        f"<code>{html.escape(handle)}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Cycle: <b>{snap['cycle']}</b>\n"
        f"Progress: <code>{progress_bar(done, total)}</code> <b>{done}/{snap['total']}</b>\n"
        f"Remaining: <b>{snap['remaining']}</b>\n"
        f"Queue size: <b>{snap['queue_size']}</b>\n"
        f"Shuffle: <b>{html.escape(snap['shuffle_status'])}</b>\n"
        f"Batch size: <b>{schedule.get('batch_size', 5)}</b>\n"
        f"Interval: <b>{schedule.get('interval_minutes', 180)} min</b>\n"
        f"Window: <b>{schedule.get('start_time')} → {schedule.get('end_time')}</b>\n"
        f"Days: <b>{html.escape(day_labels(schedule.get('days', [])))}</b>\n"
        f"Last posted: <b>{html.escape(format_dt(snap['last_posted_at']))}</b>\n"
        f"Next run: <b>{html.escape(format_dt(snap['next_post_time']))}</b>\n"
        f"State: <b>{'active' if chat.get('is_active', True) else 'paused'}</b>"
    )


async def render_queue_text(chat_id: int) -> str:
    chat = await STATE.db.get_chat(chat_id)
    snap = await STATE.db.queue_snapshot(chat_id)
    next_posts: list[str] = []
    for post_id in snap["next_batch_ids"][:8]:
        post = await STATE.db.get_post(chat_id, post_id)
        if post:
            next_posts.append(f"• {html.escape(post_label(post))}")
    queue_preview: list[str] = []
    for post_id in chat.get("queue_state", {}).get("queue", [])[snap['current_cursor'] : snap['current_cursor'] + 12]:
        post = await STATE.db.get_post(chat_id, post_id)
        if post:
            queue_preview.append(html.escape(post_label(post)))
    return (
        f"🗂 <b>Queue • {html.escape(chat['title'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Remaining posts: <b>{snap['remaining']}</b>\n"
        f"Used posts: <b>{snap['used']}</b>\n"
        f"Current cycle: <b>{snap['cycle']}</b>\n"
        f"Current cursor: <b>{snap['current_cursor']}</b>\n"
        f"Queue size: <b>{snap['queue_size']}</b>\n"
        f"Last posted: <b>{html.escape(format_dt(snap['last_posted_at']))}</b>\n"
        f"Next posting time: <b>{html.escape(format_dt(snap['next_post_time']))}</b>\n"
        f"Shuffle status: <b>{html.escape(snap['shuffle_status'])}</b>\n\n"
        f"<b>Next batch</b>\n{('<i>empty</i>' if not next_posts else chr(10).join(next_posts))}\n\n"
        f"<b>Current random queue</b>\n{('<i>empty</i>' if not queue_preview else chr(10).join(f'• {x}' for x in queue_preview))}"
    )


async def render_schedule_text(chat_id: int) -> str:
    chat = await STATE.db.get_chat(chat_id)
    schedule = chat.get("schedule", {})
    return (
        f"📅 <b>Schedule • {html.escape(chat['title'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Enabled: <b>{'yes' if schedule.get('enabled', True) else 'no'}</b>\n"
        f"Interval: <b>{schedule.get('interval_minutes', 180)} minutes</b>\n"
        f"Batch size: <b>{schedule.get('batch_size', 5)} posts</b>\n"
        f"Start: <b>{schedule.get('start_time', '00:00')}</b>\n"
        f"End: <b>{schedule.get('end_time', '21:00')}</b>\n"
        f"Days: <b>{html.escape(day_labels(schedule.get('days', [])))}</b>\n"
        f"Next run: <b>{html.escape(format_dt(chat.get('next_post_time')))}</b>\n\n"
        "Send a new schedule in this format after tapping <b>✏️ Edit schedule</b>:\n"
        "<code>interval=180\nbatch=5\nstart=00:00\nend=21:00\ndays=0,1,2,3,4,5,6</code>"
    )


async def render_posts_text(chat_id: int) -> str:
    chat = await STATE.db.get_chat(chat_id)
    posts = await STATE.db.recent_posts(chat_id, limit=12)
    lines = [f"• <code>{str(p['_id'])[-6:]}</code> {html.escape(post_label(p))}" for p in posts]
    return (
        f"📝 <b>Posts • {html.escape(chat['title'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Total posts: <b>{await STATE.db.count_posts(chat_id)}</b>\n\n"
        "<b>Recent posts</b>\n"
        f"{('<i>No posts yet</i>' if not lines else chr(10).join(lines))}\n\n"
        "Send any text/photo/video/animation/document/audio in this private chat while this target is selected to save it.\n"
        "For bulk upload, send a JSON/JSONL/TXT file."
    )


async def render_stats_text(chat_id: int | None = None) -> str:
    if chat_id is None:
        stats = await STATE.db.global_stats()
        avg = stats["published_today"] if not stats["total_cycles_completed"] else round(stats["published_today"] / max(1, stats["total_channels"] + stats["total_groups"]), 2)
        return (
            "📈 <b>Global statistics</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Total posts: <b>{stats['total_posts']}</b>\n"
            f"Remaining posts: <b>{stats['remaining_posts']}</b>\n"
            f"Published today: <b>{stats['published_today']}</b>\n"
            f"Total cycles completed: <b>{stats['total_cycles_completed']}</b>\n"
            f"Average posts/day snapshot: <b>{avg}</b>\n"
            f"Next post time: <b>{html.escape(format_dt(stats['next_post_time']))}</b>\n"
            f"Total channels: <b>{stats['total_channels']}</b>\n"
            f"Total groups: <b>{stats['total_groups']}</b>"
        )
    chat = await STATE.db.get_chat(chat_id)
    published_today = await STATE.db.published_today(chat_id)
    total_posts = await STATE.db.count_posts(chat_id)
    used_posts = await STATE.db.count_used_posts(chat_id)
    history_count = await STATE.db.history_coll(chat_id).count_documents({})
    age_days = max(1, (utcnow() - chat.get("created_at", utcnow())).days + 1)
    avg = round(history_count / age_days, 2)
    return (
        f"📈 <b>Statistics • {html.escape(chat['title'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Total posts: <b>{total_posts}</b>\n"
        f"Remaining posts: <b>{max(total_posts - used_posts, 0)}</b>\n"
        f"Published today: <b>{published_today}</b>\n"
        f"Current cycle: <b>{chat.get('queue_state', {}).get('cycle', 0)}</b>\n"
        f"Total cycles completed: <b>{chat.get('stats', {}).get('total_cycles_completed', 0)}</b>\n"
        f"Average posts per day: <b>{avg}</b>\n"
        f"Next post time: <b>{html.escape(format_dt(chat.get('next_post_time')))}</b>\n"
        f"Last posted: <b>{html.escape(format_dt(chat.get('queue_state', {}).get('last_posted_at')))}</b>"
    )


async def render_logs_text() -> str:
    rows = await STATE.db.logs.find({}).sort("created_at", -1).to_list(length=12)
    lines = []
    for row in rows:
        when = format_dt(row.get("created_at"))
        lines.append(f"• <b>{row.get('level','INFO')}</b> {html.escape(row.get('event','event'))} <code>{html.escape(when)}</code>")
    return "📜 <b>Recent logs</b>\n━━━━━━━━━━━━━━━━━━\n" + ("\n".join(lines) if lines else "<i>No logs yet</i>")


async def render_database_text() -> str:
    chats = await STATE.db.list_chats()
    lines = []
    for chat in chats[:20]:
        post_count = await STATE.db.count_posts(chat["chat_id"])
        tag = " <i>(import source only — hidden)</i>" if chat.get("hidden_source_only") else ""
        lines.append(f"• <b>{html.escape(chat['title'])}</b>{tag} → <code>posts_{coll_suffix(chat['chat_id'])}</code> ({post_count})")
    log_count = await STATE.db.logs.count_documents({})
    return (
        "🗃 <b>Database overview</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Chats collection: <b>{len(chats)}</b>\n"
        f"Admin states: <b>{await STATE.db.admins.count_documents({})}</b>\n"
        f"Log rows: <b>{log_count}</b>\n\n"
        f"<b>Per-target post collections</b>\n{('<i>empty</i>' if not lines else chr(10).join(lines))}"
    )


async def render_backup_text() -> str:
    return (
        "💾 <b>Backup & restore</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "• Export a full JSON backup of chats, schedules, queues, and posts.\n"
        "• Restore by tapping ♻️ Restore mode and then uploading a backup JSON file in this private chat.\n"
        "• 🗓 An automatic backup is also sent here every day at 3:00 AM — no action needed."
    )


async def upsert_target_from_chat(chat_obj) -> dict[str, Any]:
    chat_type = "channel" if chat_obj.type == ChatType.CHANNEL else "group"
    username = getattr(chat_obj, "username", None)
    return await STATE.db.ensure_chat(chat_obj.id, chat_obj.title or str(chat_obj.id), chat_type, username)


async def parse_bulk_document(message, context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, Any]]:
    if not message.document:
        return []
    file = await context.bot.get_file(message.document.file_id)
    blob = await file.download_as_bytearray()
    text = bytes(blob).decode("utf-8")
    name = (message.document.file_name or "").lower()
    posts: list[dict[str, Any]] = []
    if name.endswith(".json"):
        raw = json_util.loads(text)
        if isinstance(raw, dict) and "posts" in raw:
            raw = raw["posts"]
        if not isinstance(raw, list):
            raise ValueError("JSON bulk file must contain an array of posts")
        for item in raw:
            posts.append(normalize_bulk_post(item))
        return posts
    if name.endswith(".jsonl"):
        for line in text.splitlines():
            if line.strip():
                posts.append(normalize_bulk_post(json_util.loads(line)))
        return posts
    chunks = [chunk.strip() for chunk in text.split("\n---\n") if chunk.strip()]
    for chunk in chunks:
        body, flags = parse_post_markup(chunk)
        posts.append({"media_type": "text", "text": body, **flags})
    return posts


def normalize_bulk_post(item: dict[str, Any]) -> dict[str, Any]:
    media_type = item.get("media_type", "text")
    return {
        "media_type": media_type,
        "text": item.get("text"),
        "caption": item.get("caption"),
        "parse_mode": item.get("parse_mode", "HTML"),
        "file_id": item.get("file_id"),
        "file_name": item.get("file_name"),
        "silent": bool(item.get("silent", False)),
        "protect_content": bool(item.get("protect_content", False)),
        "spoiler": bool(item.get("spoiler", False)),
        "buttons": item.get("buttons", []),
    }


def message_to_post_payload(message) -> dict[str, Any]:
    text = message.text_html if message.text else None
    caption = message.caption_html if message.caption else None
    body, flags = parse_post_markup(text or caption or "")
    if message.photo:
        return {
            "media_type": "photo",
            "file_id": message.photo[-1].file_id,
            "caption": body,
            "parse_mode": "HTML",
            "file_name": "photo",
            **flags,
            "spoiler": flags["spoiler"] or bool(getattr(message, "has_media_spoiler", False)),
        }
    if message.video:
        return {
            "media_type": "video",
            "file_id": message.video.file_id,
            "caption": body,
            "parse_mode": "HTML",
            "file_name": message.video.file_name,
            **flags,
            "spoiler": flags["spoiler"] or bool(getattr(message, "has_media_spoiler", False)),
        }
    if message.animation:
        return {
            "media_type": "animation",
            "file_id": message.animation.file_id,
            "caption": body,
            "parse_mode": "HTML",
            "file_name": message.animation.file_name,
            **flags,
            "spoiler": flags["spoiler"] or bool(getattr(message, "has_media_spoiler", False)),
        }
    if message.document:
        return {
            "media_type": "document",
            "file_id": message.document.file_id,
            "caption": body,
            "parse_mode": "HTML",
            "file_name": message.document.file_name,
            **flags,
        }
    if message.audio:
        return {
            "media_type": "audio",
            "file_id": message.audio.file_id,
            "caption": body,
            "parse_mode": "HTML",
            "file_name": message.audio.file_name,
            **flags,
        }
    return {"media_type": "text", "text": body, "parse_mode": "HTML", **flags}


def schedule_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ Edit schedule", callback_data=f"chat:edit_schedule:{chat_id}"), InlineKeyboardButton("🧪 Preview queue", callback_data=f"chat:queue:{chat_id}")],
            [InlineKeyboardButton("⬅️ Target panel", callback_data=f"chat:open:{chat_id}")],
        ]
    )


def backup_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬇️ Export backup", callback_data="backup:export"), InlineKeyboardButton("♻️ Restore mode", callback_data="backup:restore")],
            [InlineKeyboardButton("⬅️ Back", callback_data="nav:dashboard")],
        ]
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="nav:dashboard")]])


async def send_saved_post(bot, target_chat_id: int, post: dict[str, Any]) -> int:
    media_type = post.get("media_type", "text")
    if media_type == "forward":
        msg = await bot.copy_message(
            chat_id=target_chat_id,
            from_chat_id=post["source_chat_id"],
            message_id=post["source_message_id"],
            disable_notification=bool(post.get("silent", False)),
            protect_content=bool(post.get("protect_content", False)),
        )
        return msg.message_id
    if media_type == "album":
        items = post.get("media_group", [])
        input_media = []
        for idx, item in enumerate(items):
            cls = InputMediaPhoto if item.get("media_type") == "photo" else InputMediaVideo
            media_kwargs: dict[str, Any] = {"media": item["file_id"]}
            if item.get("spoiler"):
                media_kwargs["has_spoiler"] = True
            if idx == 0:
                media_kwargs["caption"] = post.get("caption") or None
                media_kwargs["parse_mode"] = post.get("parse_mode", "HTML")
            input_media.append(cls(**media_kwargs))
        msgs = await bot.send_media_group(
            chat_id=target_chat_id,
            media=input_media,
            disable_notification=bool(post.get("silent", False)),
            protect_content=bool(post.get("protect_content", False)),
        )
        markup = render_buttons(post.get("buttons"))
        if markup:
            await bot.send_message(chat_id=target_chat_id, text="\u2063", reply_markup=markup, disable_notification=True)
        return msgs[0].message_id
    markup = render_buttons(post.get("buttons"))
    kwargs = {
        "chat_id": target_chat_id,
        "disable_notification": bool(post.get("silent", False)),
        "protect_content": bool(post.get("protect_content", False)),
        "reply_markup": markup,
    }
    if media_type == "photo":
        msg = await bot.send_photo(
            photo=post["file_id"],
            caption=post.get("caption") or None,
            parse_mode=post.get("parse_mode", "HTML"),
            has_spoiler=bool(post.get("spoiler", False)),
            **kwargs,
        )
    elif media_type == "video":
        msg = await bot.send_video(
            video=post["file_id"],
            caption=post.get("caption") or None,
            parse_mode=post.get("parse_mode", "HTML"),
            has_spoiler=bool(post.get("spoiler", False)),
            **kwargs,
        )
    elif media_type == "animation":
        msg = await bot.send_animation(
            animation=post["file_id"],
            caption=post.get("caption") or None,
            parse_mode=post.get("parse_mode", "HTML"),
            has_spoiler=bool(post.get("spoiler", False)),
            **kwargs,
        )
    elif media_type == "document":
        msg = await bot.send_document(
            document=post["file_id"],
            caption=post.get("caption") or None,
            parse_mode=post.get("parse_mode", "HTML"),
            **kwargs,
        )
    elif media_type == "audio":
        msg = await bot.send_audio(
            audio=post["file_id"],
            caption=post.get("caption") or None,
            parse_mode=post.get("parse_mode", "HTML"),
            **kwargs,
        )
    else:
        msg = await bot.send_message(
            text=post.get("text") or "‎",
            parse_mode=post.get("parse_mode", "HTML"),
            link_preview_options=None,
            **kwargs,
        )
    return msg.message_id


async def send_post_with_retry(bot, target_chat_id: int, post: dict[str, Any]) -> int:
    last_error: Exception | None = None
    for attempt in range(1, SETTINGS.retry_attempts + 1):
        try:
            return await send_saved_post(bot, target_chat_id, post)
        except RetryAfter as exc:
            wait_for = float(getattr(exc, "retry_after", 3))
            await asyncio.sleep(wait_for + 0.5)
            last_error = exc
        except (TimedOut, NetworkError) as exc:
            await asyncio.sleep(SETTINGS.retry_base_seconds * attempt)
            last_error = exc
        except (Forbidden, BadRequest) as exc:
            raise exc
    if last_error:
        raise last_error
    raise RuntimeError("send_post_with_retry failed without captured exception")


async def process_chat_batch(bot, chat_id: int, manual: bool = False) -> int:
    lock = STATE.lock_for(chat_id)
    async with lock:
        if not await STATE.db.acquire_processing(chat_id):
            return 0
        sent_count = 0
        try:
            chat = await STATE.db.get_chat(chat_id)
            if not chat or not chat.get("is_active", True):
                return 0
            if not chat.get("queue_state", {}).get("queue") or int(chat.get("queue_state", {}).get("cursor", 0)) >= len(chat.get("queue_state", {}).get("queue", [])):
                chat = await STATE.db.rebuild_cycle(chat_id)
            schedule = chat.get("schedule", {})
            batch_size = int(schedule.get("batch_size", 5))
            while sent_count < batch_size:
                chat = await STATE.db.get_chat(chat_id)
                queue_state = chat.get("queue_state", {})
                queue = queue_state.get("queue", [])
                cursor = int(queue_state.get("cursor", 0))
                if cursor >= len(queue):
                    break
                post_id = queue[cursor]
                post = await STATE.db.get_post(chat_id, post_id)
                if not post:
                    await STATE.db.chats.update_one({"chat_id": chat_id}, {"$inc": {"queue_state.cursor": 1}})
                    continue
                try:
                    telegram_message_id = await send_post_with_retry(bot, chat_id, post)
                    await STATE.db.advance_after_success(chat_id, post_id, telegram_message_id)
                    sent_count += 1
                except Forbidden as exc:
                    await STATE.db.chats.update_one(
                        {"chat_id": chat_id},
                        {"$set": {"is_active": False, "next_post_time": None, "last_error": f"Forbidden: {exc}", "updated_at": utcnow()}},
                    )
                    await STATE.db.log("error", "chat_disabled_forbidden", chat_id=chat_id, error=str(exc))
                    break
                except BadRequest as exc:
                    await STATE.db.chats.update_one(
                        {"chat_id": chat_id},
                        {"$set": {"last_error": f"BadRequest: {exc}", "updated_at": utcnow()}},
                    )
                    await STATE.db.log("warning", "post_skipped_bad_request", chat_id=chat_id, post_id=post_id, error=str(exc))
                    await STATE.db.chats.update_one({"chat_id": chat_id}, {"$inc": {"queue_state.cursor": 1}})
                    continue

            next_run = compute_next_run(utcnow() + timedelta(seconds=1), schedule)
            await STATE.db.finalize_batch(chat_id, sent_count, next_run)
            if sent_count or manual:
                await STATE.db.log("info", "batch_processed", chat_id=chat_id, sent_count=sent_count)
            return sent_count
        finally:
            await STATE.db.release_processing(chat_id)


async def scheduler_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    due = await STATE.db.due_chats(utcnow())
    for chat in due:
        await process_chat_batch(context.bot, chat["chat_id"])


async def show_dashboard(chat_id: int, user_id: int, bot, message_id: int | None = None) -> None:
    text = await dashboard_text(user_id)
    if message_id:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=dashboard_keyboard(), parse_mode="HTML")
    else:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=dashboard_keyboard(), parse_mode="HTML")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await update.effective_message.reply_text("Unauthorized.")
        return
    await clear_admin_mode(user.id)
    await show_dashboard(update.effective_chat.id, user.id, context.bot)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id if user else None):
        return
    text = (
        "<b>Admin quick start</b>\n"
        "1. Add the bot as admin to a channel/group.\n"
        "2. Open 🧭 Targets and pick it.\n"
        "3. Send posts here privately or upload JSON/JSONL/TXT bulk files.\n"
        "4. Configure the schedule and let the shuffle engine run.\n\n"
        "Special flags inside text/caption:\n"
        "<code>#silent</code> <code>#protect</code> <code>#spoiler</code>\n"
        "Buttons section:\n"
        "<code>---buttons---</code> then <code>Label|https://example.com</code>"
    )
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id if user else None):
        return
    target = await get_target_chat(user.id)
    text = await render_stats_text(target["chat_id"] if target else None)
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def register_current_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id if user else None):
        return
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text("Use this command in a channel or group where the bot is present.")
        return
    saved = await upsert_target_from_chat(chat)
    await update.effective_message.reply_text(f"Registered: {saved['title']}")


async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.my_chat_member.chat
    if chat.type in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
        await upsert_target_from_chat(chat)


async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    req = update.chat_join_request
    main_chat_id = req.chat.id
    applicant = req.from_user
    chat_doc = await STATE.db.get_chat(main_chat_id)
    required = chat_doc.get("force_sub_channel") if chat_doc else None
    if not required:
        return  # no gate configured for this chat; leave the request for manual/native handling
    try:
        member = await context.bot.get_chat_member(required, applicant.id)
        is_member = member.status in ("member", "administrator", "creator")
    except (BadRequest, Forbidden):
        is_member = False
    if is_member:
        try:
            await context.bot.approve_chat_join_request(main_chat_id, applicant.id)
        except (BadRequest, Forbidden):
            pass
        return
    try:
        await context.bot.decline_chat_join_request(main_chat_id, applicant.id)
    except (BadRequest, Forbidden):
        pass
    invite_label = str(required)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Join required channel", url=f"https://t.me/{invite_label.lstrip('@')}")]]) if invite_label.startswith("@") else None
    try:
        await context.bot.send_message(
            applicant.id,
            f"🔒 To join <b>{html.escape(chat_doc.get('title', str(main_chat_id)))}</b>, please join {html.escape(invite_label)} first, then send the join request again.",
            parse_mode="HTML",
            reply_markup=kb,
        )
    except (BadRequest, Forbidden):
        pass  # user hasn't started the bot privately; can't DM them


async def run_dangerous_action(action: str) -> tuple[str, InlineKeyboardMarkup]:
    """Executes a confirmed destructive action (wipe/remove) and returns text+keyboard to show."""
    if action == "db:wipeconfirm":
        await STATE.db.wipe_all()
        return (
            "✅ Database wiped. Bot is now in a fresh state.\n\nUse 🧭 Targets to add channels/groups again.",
            dashboard_keyboard(),
        )
    parts = action.split(":")
    if len(parts) == 3 and parts[0] == "chat":
        chat_id = int(parts[2])
        if parts[1] == "wipeconfirm":
            await STATE.db.wipe_chat(chat_id)
            return (
                (await render_chat_overview(chat_id)) + "\n\n✅ Posts wiped for this channel.",
                await chat_keyboard(chat_id),
            )
        if parts[1] == "removeconfirm":
            chat = await STATE.db.get_chat(chat_id)
            title = html.escape(chat.get("title", str(chat_id))) if chat else str(chat_id)
            await STATE.db.delete_chat(chat_id)
            return (
                f"✅ <b>{title}</b> has been removed from the bot's target list.",
                await targets_keyboard(),
            )
    return "⚠️ Unknown or expired action.", dashboard_keyboard()


async def handle_dangerous_confirm(action: str, query, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Gates a destructive action behind ADMIN_PASSWORD if one is configured, otherwise runs it directly."""
    if SETTINGS.admin_password:
        await set_admin_meta(
            user_id,
            mode="await_password",
            pending_action=action,
            password_deadline=utcnow() + timedelta(seconds=SETTINGS.password_timeout_seconds),
        )
        await query.edit_message_text(
            "🔑 <b>Password required</b>\n━━━━━━━━━━━━━━━━━━\n"
            "This is a destructive action. Reply here (in this private chat) with the admin "
            "password to confirm, or send /cancel to abort.\nYour password message will be deleted automatically.",
            parse_mode="HTML",
        )
        return
    text, kb = await run_dangerous_action(action)
    await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    if not is_admin(user.id if user else None):
        await query.answer("Unauthorized", show_alert=True)
        return
    await query.answer()
    data = query.data or ""

    if data == "nav:dashboard":
        await query.edit_message_text(await dashboard_text(user.id), reply_markup=dashboard_keyboard(), parse_mode="HTML")
        return
    if data == "nav:targets":
        await query.edit_message_text("🧭 <b>Targets</b>\nSelect a channel or group.", reply_markup=await targets_keyboard(), parse_mode="HTML")
        return
    if data == "nav:upload":
        await query.edit_message_text(
            "⬆️ <b>Upload Mode</b>\nSelect the channel/group you want to upload posts to.",
            reply_markup=await upload_targets_keyboard(),
            parse_mode="HTML",
        )
        return
    if data.startswith("up:pick:"):
        chat_id = int(data.split(":")[2])
        chat = await STATE.db.get_chat(chat_id)
        title = html.escape(chat.get("title", str(chat_id))) if chat else str(chat_id)
        await STATE.db.set_active_target(user.id, chat_id)
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📝 Manual", callback_data=f"up:manual:{chat_id}"), InlineKeyboardButton("📥 Import Range", callback_data=f"up:import:{chat_id}")],
                [InlineKeyboardButton("⬅️ Back", callback_data="nav:upload")],
            ]
        )
        await query.edit_message_text(
            f"⬆️ <b>Upload target: {title}</b>\nChoose how you want to add posts.",
            reply_markup=kb,
            parse_mode="HTML",
        )
        return
    if data.startswith("up:manual:"):
        chat_id = int(data.split(":")[2])
        chat = await STATE.db.get_chat(chat_id)
        title = html.escape(chat.get("title", str(chat_id))) if chat else str(chat_id)
        await STATE.db.set_active_target(user.id, chat_id)
        await set_admin_meta(user.id, mode="bulk_upload", mode_chat_id=chat_id, upload_count=0)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Finish Upload", callback_data="up:finish")]])
        sent = await context.bot.send_message(
            query.message.chat_id,
            f"⬆️ <b>Upload mode: {title}</b>\nSend your posts now (text, photo, video, album, or a .json/.jsonl/.txt bulk file).\nPosts added: <b>0</b>",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await set_admin_meta(user.id, upload_msg_id=sent.message_id)
        return
    if data.startswith("up:import:"):
        chat_id = int(data.split(":")[2])
        chat = await STATE.db.get_chat(chat_id)
        title = html.escape(chat.get("title", str(chat_id))) if chat else str(chat_id)
        await STATE.db.set_active_target(user.id, chat_id)
        await set_admin_meta(user.id, mode="import_source", mode_chat_id=chat_id)
        await context.bot.send_message(
            query.message.chat_id,
            (
                f"📥 <b>Import posts into {title}</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Step 1 of 3 — Forward any message from the SOURCE channel (the one that already "
                "has the posts), or send its @username / -100 numeric ID.\n\n"
                "⚠️ The bot must be an admin in the source channel to read/copy its posts.\n"
                "Send /cancel to abort."
            ),
            parse_mode="HTML",
        )
        return
    if data == "up:finish":
        admin_state = await admin_doc(user.id)
        count = int(admin_state.get("upload_count", 0))
        chat_id = admin_state.get("mode_chat_id")
        chat = await STATE.db.get_chat(chat_id) if chat_id else None
        title = html.escape(chat.get("title", str(chat_id))) if chat else "target"
        await clear_admin_mode(user.id)
        await STATE.db.admins.update_one({"admin_id": user.id}, {"$unset": {"upload_count": "", "upload_msg_id": ""}})
        await query.edit_message_text(f"✅ Upload finished — <b>{count}</b> post(s) added to {title}.", parse_mode="HTML")
        return
    if data == "nav:logs":
        await query.edit_message_text(await render_logs_text(), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="nav:dashboard")]]), parse_mode="HTML")
        return
    if data == "nav:database":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🧨 Wipe ALL data", callback_data="db:wipe")], [InlineKeyboardButton("⬅️ Back", callback_data="nav:dashboard")]])
        await query.edit_message_text(await render_database_text(), reply_markup=kb, parse_mode="HTML")
        return
    if data == "db:wipe":
        text = (
            "🧨 <b>Wipe ALL data — are you sure?</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "This deletes every chat, every post, every history entry, and all logs. "
            "The bot resets to a brand-new state. This cannot be undone."
        )
        await query.edit_message_text(text, reply_markup=confirm_keyboard("db:wipeconfirm", "nav:database", "🧨 Yes, wipe everything"), parse_mode="HTML")
        return
    if data == "db:wipeconfirm":
        await handle_dangerous_confirm(data, query, context, user.id)
        return
    if data == "nav:backup":
        await query.edit_message_text(await render_backup_text(), reply_markup=backup_keyboard(), parse_mode="HTML")
        return
    if data == "nav:settings":
        text = (
            "⚙️ <b>Settings</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"Timezone: <b>{SETTINGS.timezone}</b>\n"
            f"Scan interval: <b>{SETTINGS.scan_interval_seconds}s</b>\n"
            f"Retries: <b>{SETTINGS.retry_attempts}</b>\n"
            f"Default batch: <b>{SETTINGS.default_batch_size}</b>\n"
            f"Default interval: <b>{SETTINGS.default_interval_minutes} min</b>"
        )
        await query.edit_message_text(text, reply_markup=settings_keyboard(), parse_mode="HTML")
        return
    if data == "nav:stats":
        await query.edit_message_text(await render_stats_text(None), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="nav:dashboard")]]), parse_mode="HTML")
        return
    if data == "nav:queue":
        target = await get_target_chat(user.id)
        if not target:
            await query.answer("Select a target first", show_alert=True)
            return
        await query.edit_message_text(await render_queue_text(target["chat_id"]), reply_markup=await chat_keyboard(target["chat_id"]), parse_mode="HTML")
        return
    if data == "nav:schedule":
        target = await get_target_chat(user.id)
        if not target:
            await query.answer("Select a target first", show_alert=True)
            return
        await query.edit_message_text(await render_schedule_text(target["chat_id"]), reply_markup=schedule_keyboard(target["chat_id"]), parse_mode="HTML")
        return
    if data == "nav:posts":
        target = await get_target_chat(user.id)
        if not target:
            await query.answer("Select a target first", show_alert=True)
            return
        await query.edit_message_text(await render_posts_text(target["chat_id"]), reply_markup=await chat_keyboard(target["chat_id"]), parse_mode="HTML")
        return

    if data == "backup:export":
        payload = await STATE.db.backup_payload()
        blob = json_util.dumps(payload, indent=2).encode("utf-8")
        await context.bot.send_document(query.message.chat_id, document=InputFile(io.BytesIO(blob), filename=f"backup-{utcnow().strftime('%Y%m%d-%H%M%S')}.json"), caption="Backup exported ✅")
        return
    if data == "backup:restore":
        await set_admin_meta(user.id, mode="restore_backup")
        await context.bot.send_message(query.message.chat_id, "♻️ Restore mode enabled. Upload a backup JSON file in this private chat.")
        return

    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "chat":
        return
    action = parts[1]
    chat_id = int(parts[2])
    if action == "open":
        await query.edit_message_text(await render_chat_overview(chat_id), reply_markup=await chat_keyboard(chat_id), parse_mode="HTML")
    elif action == "pick":
        await STATE.db.set_active_target(user.id, chat_id)
        await query.edit_message_text(await render_chat_overview(chat_id), reply_markup=await chat_keyboard(chat_id), parse_mode="HTML")
    elif action == "queue":
        await query.edit_message_text(await render_queue_text(chat_id), reply_markup=await chat_keyboard(chat_id), parse_mode="HTML")
    elif action == "schedule":
        await query.edit_message_text(await render_schedule_text(chat_id), reply_markup=schedule_keyboard(chat_id), parse_mode="HTML")
    elif action == "posts":
        await query.edit_message_text(await render_posts_text(chat_id), reply_markup=await chat_keyboard(chat_id), parse_mode="HTML")
    elif action == "stats":
        await query.edit_message_text(await render_stats_text(chat_id), reply_markup=await chat_keyboard(chat_id), parse_mode="HTML")
    elif action == "edit_schedule":
        await set_admin_meta(user.id, mode="edit_schedule", mode_chat_id=chat_id)
        await context.bot.send_message(query.message.chat_id, f"📝 Send the new schedule for {chat_id} using the format shown in the schedule panel.")
    elif action == "reshuffle":
        current = await STATE.db.get_chat(chat_id)
        next_post_time = current.get("next_post_time")
        await STATE.db.rebuild_cycle(chat_id)
        await STATE.db.chats.update_one({"chat_id": chat_id}, {"$set": {"next_post_time": next_post_time, "updated_at": utcnow()}})
        await query.edit_message_text(await render_queue_text(chat_id), reply_markup=await chat_keyboard(chat_id), parse_mode="HTML")
    elif action == "run":
        sent = await process_chat_batch(context.bot, chat_id, manual=True)
        await query.edit_message_text((await render_queue_text(chat_id)) + f"\n\n✅ Manual batch sent: <b>{sent}</b>", reply_markup=await chat_keyboard(chat_id), parse_mode="HTML")
    elif action == "toggle":
        chat = await STATE.db.get_chat(chat_id)
        new_state = not chat.get("is_active", True)
        schedule = chat.get("schedule", {})
        next_run = compute_next_run(utcnow(), schedule) if new_state else None
        await STATE.db.chats.update_one({"chat_id": chat_id}, {"$set": {"is_active": new_state, "next_post_time": next_run, "updated_at": utcnow()}})
        await query.edit_message_text(await render_chat_overview(chat_id), reply_markup=await chat_keyboard(chat_id), parse_mode="HTML")
    elif action == "undo":
        removed = await STATE.db.undo_last_post(chat_id)
        if removed:
            preview = trim(removed.get("caption") or removed.get("text") or removed.get("type", "post"), 40)
            await query.answer(f"Removed last post: {preview}", show_alert=True)
        else:
            await query.answer("No posts to undo.", show_alert=True)
        await query.edit_message_text(await render_posts_text(chat_id), reply_markup=await chat_keyboard(chat_id), parse_mode="HTML")
    elif action == "wipe":
        chat = await STATE.db.get_chat(chat_id)
        title = html.escape(chat.get("title", str(chat_id))) if chat else str(chat_id)
        text = (
            f"🧹 <b>Wipe posts for {title}?</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "This deletes all posts, history, and queue progress for this channel/group only. "
            "Other channels are not affected. This cannot be undone."
        )
        await query.edit_message_text(text, reply_markup=confirm_keyboard(f"chat:wipeconfirm:{chat_id}", f"chat:open:{chat_id}", "🧹 Yes, wipe this channel"), parse_mode="HTML")
    elif action == "wipeconfirm":
        await handle_dangerous_confirm(data, query, context, user.id)
    elif action == "remove":
        chat = await STATE.db.get_chat(chat_id)
        title = html.escape(chat.get("title", str(chat_id))) if chat else str(chat_id)
        text = (
            f"🗑 <b>Remove {title} from the bot?</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "This removes the channel/group entirely from the target list — its posts, history, "
            "schedule, and queue are all deleted. The bot will stop auto-posting there. "
            "The bot itself stays in the channel/group unless you also remove it there manually. "
            "This cannot be undone."
        )
        await query.edit_message_text(text, reply_markup=confirm_keyboard(f"chat:removeconfirm:{chat_id}", f"chat:open:{chat_id}", "🗑 Yes, remove it"), parse_mode="HTML")
    elif action == "removeconfirm":
        await handle_dangerous_confirm(data, query, context, user.id)
    elif action == "promo":
        chat = await STATE.db.get_chat(chat_id)
        title = html.escape(chat.get("title", str(chat_id))) if chat else str(chat_id)
        await set_admin_meta(user.id, mode="promo_text", mode_chat_id=chat_id)
        await context.bot.send_message(
            query.message.chat_id,
            f"📌 Send the prompt/promo message for <b>{title}</b> now — it will be pinned there. Any previously pinned prompt will be unpinned automatically.",
            parse_mode="HTML",
        )
    elif action == "forcesub":
        chat = await STATE.db.get_chat(chat_id)
        title = html.escape(chat.get("title", str(chat_id))) if chat else str(chat_id)
        current = chat.get("force_sub_channel") if chat else None
        current_line = f"\nCurrently set: <code>{html.escape(str(current))}</code>" if current else "\nCurrently: <i>not set (join requests are not gated)</i>"
        await set_admin_meta(user.id, mode="force_sub_channel", mode_chat_id=chat_id)
        await context.bot.send_message(
            query.message.chat_id,
            (
                f"🔒 <b>Force-Sub Gate for {title}</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Send the @username or numeric ID of the channel people must join first "
                f"before their join request to <b>{title}</b> gets approved.{current_line}\n\n"
                "⚠️ Requirements:\n"
                f"1. <b>{title}</b> must have 'Approve new members' turned ON (Group/Channel settings → join requests).\n"
                "2. This bot must be admin in BOTH channels.\n\n"
                "Send <code>off</code> to remove the gate."
            ),
            parse_mode="HTML",
        )


def parse_schedule_text(text: str) -> dict[str, Any]:
    data: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            data[key.strip().lower()] = value.strip()
    interval = int(data.get("interval", SETTINGS.default_interval_minutes))
    batch = int(data.get("batch", SETTINGS.default_batch_size))
    start = data.get("start", SETTINGS.default_start_time)
    end = data.get("end", SETTINGS.default_end_time)
    days = parse_days(data.get("days", ",".join(map(str, SETTINGS.default_days))))
    parse_hhmm(start)
    parse_hhmm(end)
    if interval < 1 or batch < 1:
        raise ValueError("Interval and batch must be positive")
    return {"interval_minutes": interval, "batch_size": batch, "start_time": start, "end_time": end, "days": days}


MEDIA_GROUP_BUFFERS: dict[str, dict[str, Any]] = {}


async def buffer_media_group_item(update: Update, context: ContextTypes.DEFAULT_TYPE, target: dict[str, Any]) -> None:
    message = update.effective_message
    gid = message.media_group_id
    if message.photo:
        item = {"media_type": "photo", "file_id": message.photo[-1].file_id, "spoiler": bool(getattr(message, "has_media_spoiler", False))}
    elif message.video:
        item = {"media_type": "video", "file_id": message.video.file_id, "spoiler": bool(getattr(message, "has_media_spoiler", False))}
    else:
        return

    buf = MEDIA_GROUP_BUFFERS.get(gid)
    if buf is None:
        buf = {
            "chat_id": target["chat_id"],
            "title": target["title"],
            "admin_id": update.effective_user.id,
            "items": [],
            "caption": None,
            "flags": None,
            "job": None,
        }
        MEDIA_GROUP_BUFFERS[gid] = buf

    buf["items"].append(item)
    raw_caption = message.caption_html if message.caption else None
    if raw_caption and buf["caption"] is None:
        body, flags = parse_post_markup(raw_caption)
        buf["caption"] = body
        buf["flags"] = flags

    if buf["job"] is not None:
        buf["job"].schedule_removal()
    buf["job"] = context.job_queue.run_once(flush_media_group, 1.5, data=gid, name=f"mg:{gid}")


async def flush_media_group(context: ContextTypes.DEFAULT_TYPE) -> None:
    gid = context.job.data
    buf = MEDIA_GROUP_BUFFERS.pop(gid, None)
    if not buf or not buf["items"]:
        return
    flags = buf["flags"] or {"silent": False, "protect_content": False, "spoiler": False, "buttons": []}
    payload = {
        "media_type": "album",
        "media_group": buf["items"],
        "caption": buf["caption"],
        "parse_mode": "HTML",
        "silent": flags.get("silent", False),
        "protect_content": flags.get("protect_content", False),
        "buttons": flags.get("buttons", []),
    }
    chat_id = buf["chat_id"]
    await STATE.db.add_post(chat_id, payload)
    chat = await STATE.db.get_chat(chat_id)
    queue = chat.get("queue_state", {}).get("queue", [])
    cursor = int(chat.get("queue_state", {}).get("cursor", 0))
    if not queue or cursor >= len(queue):
        next_post_time = chat.get("next_post_time")
        await STATE.db.rebuild_cycle(chat_id)
        await STATE.db.chats.update_one({"chat_id": chat_id}, {"$set": {"next_post_time": next_post_time}})
    admin_id = buf["admin_id"]
    admin_state = await admin_doc(admin_id)
    if admin_state.get("mode") == "bulk_upload" and admin_state.get("mode_chat_id") == chat_id:
        await bump_upload_counter(admin_id, context, admin_id, added=1)
    else:
        await context.bot.send_message(admin_id, f"Saved to {buf['title']} ✅\n🖼 Album ({len(buf['items'])} items)")


async def bump_upload_counter(admin_id: int, context: ContextTypes.DEFAULT_TYPE, private_chat_id: int, added: int = 1) -> None:
    admin_state = await admin_doc(admin_id)
    if admin_state.get("mode") != "bulk_upload":
        return
    count = int(admin_state.get("upload_count", 0)) + added
    chat_id = admin_state.get("mode_chat_id")
    chat = await STATE.db.get_chat(chat_id) if chat_id else None
    title = html.escape(chat.get("title", str(chat_id))) if chat else "target"
    await set_admin_meta(admin_id, upload_count=count)
    msg_id = admin_state.get("upload_msg_id")
    if msg_id:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Finish Upload", callback_data="up:finish")]])
        try:
            await context.bot.edit_message_text(
                chat_id=private_chat_id,
                message_id=msg_id,
                text=f"⬆️ <b>Upload mode: {title}</b>\nSend your posts now (text, photo, video, album, or a .json/.jsonl/.txt bulk file).\nPosts added: <b>{count}</b>",
                parse_mode="HTML",
                reply_markup=kb,
            )
        except (BadRequest, Forbidden):
            pass


async def private_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not is_admin(user.id if user else None):
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    admin_state = await admin_doc(user.id)
    mode = admin_state.get("mode")

    if mode and message.text and message.text.strip().lower() == "/cancel":
        await clear_admin_mode(user.id)
        await message.reply_text("❌ Cancelled.")
        return

    if mode == "await_password":
        deadline = admin_state.get("password_deadline")
        if deadline and utcnow() > deadline:
            await clear_admin_mode(user.id)
            await message.reply_text("⌛ Password prompt expired. Please start the action again.")
            return
        entered = (message.text or "").strip()
        try:
            await message.delete()
        except (BadRequest, Forbidden):
            pass
        if not SETTINGS.admin_password or entered != SETTINGS.admin_password:
            await message.reply_text("❌ Wrong password. Try again, or send /cancel to abort.")
            return
        pending_action = admin_state.get("pending_action", "")
        await clear_admin_mode(user.id)
        result_text, result_kb = await run_dangerous_action(pending_action)
        await message.reply_text(result_text, reply_markup=result_kb, parse_mode="HTML")
        return

    if mode == "import_source":
        target_chat_id = int(admin_state.get("mode_chat_id"))
        source_chat_id: int | None = None
        origin = getattr(message, "forward_origin", None)
        if origin is not None:
            origin_chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
            if origin_chat is not None:
                source_chat_id = origin_chat.id
        if source_chat_id is None and getattr(message, "forward_from_chat", None):
            source_chat_id = message.forward_from_chat.id
        if source_chat_id is None and message.text:
            raw = message.text.strip()
            value: int | str = int(raw) if raw.lstrip("-").isdigit() else (raw if raw.startswith("@") else f"@{raw}")
            try:
                chat_obj = await context.bot.get_chat(value)
                source_chat_id = chat_obj.id
            except (BadRequest, Forbidden) as exc:
                await message.reply_text(f"❌ Can't access that channel: {exc}\nMake sure the bot is an admin there, then try again, or /cancel.")
                return
        if source_chat_id is None:
            await message.reply_text("Forward a message from the source channel, or send its @username / ID. Send /cancel to abort.")
            return
        existing_source_chat = await STATE.db.get_chat(source_chat_id)
        if existing_source_chat and not existing_source_chat.get("hidden_source_only"):
            existing_post_count = await STATE.db.count_posts(source_chat_id)
            if existing_post_count == 0:
                await STATE.db.mark_hidden_source(source_chat_id)
        await set_admin_meta(user.id, mode="import_from_link", mode_chat_id=target_chat_id, source_chat_id=source_chat_id)
        await message.reply_text(
            "🔗 Step 2 of 3 — Send the link (or plain message ID) of the FIRST post to import.\n"
            "e.g. <code>https://t.me/channelusername/120</code> or just <code>120</code>",
            parse_mode="HTML",
        )
        return

    if mode == "import_from_link" and message.text:
        from_id = parse_post_link_or_id(message.text)
        if from_id is None:
            await message.reply_text("Couldn't read that. Send a link like https://t.me/channel/120 or just the number 120, or /cancel.")
            return
        await set_admin_meta(user.id, from_message_id=from_id, mode="import_to_link")
        await message.reply_text(
            "🔗 Step 3 of 3 — Send the link (or plain message ID) of the LAST post to import.",
        )
        return

    if mode == "import_to_link" and message.text:
        to_id = parse_post_link_or_id(message.text)
        if to_id is None:
            await message.reply_text("Couldn't read that. Send a link like https://t.me/channel/130 or just the number 130, or /cancel.")
            return
        target_chat_id = int(admin_state.get("mode_chat_id"))
        source_chat_id = int(admin_state.get("source_chat_id"))
        from_id = int(admin_state.get("from_message_id"))
        lo, hi = sorted((from_id, to_id))
        total = hi - lo + 1
        if total > 1000:
            await message.reply_text(f"❌ Range too large ({total} posts). Please import in batches of 1000 or fewer, or /cancel.")
            return
        await clear_admin_mode(user.id)
        existing_source_chat = await STATE.db.get_chat(source_chat_id)
        if existing_source_chat and not existing_source_chat.get("hidden_source_only") and total > 0:
            existing_post_count = await STATE.db.count_posts(source_chat_id)
            if existing_post_count == 0:
                await STATE.db.mark_hidden_source(source_chat_id)
        status = await message.reply_text(f"⏳ Queuing {total} post(s) for import…")
        for msg_id in range(lo, hi + 1):
            payload = {
                "media_type": "forward",
                "source_chat_id": source_chat_id,
                "source_message_id": msg_id,
                "caption": None,
                "silent": False,
                "protect_content": False,
                "buttons": [],
            }
            await STATE.db.add_post(target_chat_id, payload)
        chat = await STATE.db.get_chat(target_chat_id)
        queue = chat.get("queue_state", {}).get("queue", [])
        cursor = int(chat.get("queue_state", {}).get("cursor", 0))
        if not queue or cursor >= len(queue):
            next_post_time = chat.get("next_post_time")
            await STATE.db.rebuild_cycle(target_chat_id)
            await STATE.db.chats.update_one({"chat_id": target_chat_id}, {"$set": {"next_post_time": next_post_time}})
        title = html.escape(chat.get("title", str(target_chat_id))) if chat else str(target_chat_id)
        await status.edit_text(
            f"✅ Import queued into <b>{title}</b>\n"
            f"📦 Posts added: <b>{total}</b>\n\n"
            "They'll go out on this channel's normal posting schedule. Any deleted/invalid "
            "message IDs in the range are skipped automatically when it's their turn to post.",
            parse_mode="HTML",
        )
        return

    if mode == "restore_backup" and message.document:
        file = await context.bot.get_file(message.document.file_id)
        blob = await file.download_as_bytearray()
        payload = json_util.loads(bytes(blob).decode("utf-8"))
        chats, posts = await STATE.db.restore_payload(payload)
        await clear_admin_mode(user.id)
        await message.reply_text(f"Restore complete ✅\nChats: {chats}\nPosts: {posts}")
        return
    if mode == "edit_schedule" and message.text:
        chat_id = int(admin_state.get("mode_chat_id"))
        parsed = parse_schedule_text(message.text)
        await STATE.db.update_schedule(chat_id, **parsed)
        await clear_admin_mode(user.id)
        await message.reply_text("Schedule updated ✅")
        return
    if mode == "force_sub_channel" and message.text:
        target_chat_id = int(admin_state.get("mode_chat_id"))
        raw = message.text.strip()
        if raw.lower() == "off":
            await STATE.db.chats.update_one({"chat_id": target_chat_id}, {"$unset": {"force_sub_channel": ""}, "$set": {"updated_at": utcnow()}})
            await clear_admin_mode(user.id)
            await message.reply_text("🔓 Force-sub gate removed for this channel.")
            return
        value: int | str = int(raw) if raw.lstrip("-").isdigit() else (raw if raw.startswith("@") else f"@{raw}")
        try:
            member = await context.bot.get_chat_member(value, context.bot.id)
        except (BadRequest, Forbidden) as exc:
            await message.reply_text(f"❌ Bot can't access {raw} — make sure the bot is an admin there. ({exc})")
            return
        await STATE.db.chats.update_one({"chat_id": target_chat_id}, {"$set": {"force_sub_channel": value, "updated_at": utcnow()}})
        await clear_admin_mode(user.id)
        await message.reply_text(f"🔒 Force-sub gate set to {raw} ✅\nNew join requests will now be checked against this channel.")
        return
    if mode == "bulk_upload":
        target_chat_id = int(admin_state.get("mode_chat_id"))
        target = await STATE.db.get_chat(target_chat_id)
        if not target:
            await clear_admin_mode(user.id)
            await message.reply_text("❌ Target channel no longer exists. Upload mode ended.")
            return
        if message.media_group_id and (message.photo or message.video):
            await buffer_media_group_item(update, context, target)
            return
        if message.document and (message.document.file_name or "").lower().endswith((".json", ".jsonl", ".txt")):
            posts = await parse_bulk_document(message, context)
            inserted = await STATE.db.bulk_add_posts(target_chat_id, posts)
            chat = await STATE.db.get_chat(target_chat_id)
            queue = chat.get("queue_state", {}).get("queue", [])
            cursor = int(chat.get("queue_state", {}).get("cursor", 0))
            if not queue or cursor >= len(queue):
                next_post_time = chat.get("next_post_time")
                await STATE.db.rebuild_cycle(target_chat_id)
                await STATE.db.chats.update_one({"chat_id": target_chat_id}, {"$set": {"next_post_time": next_post_time}})
            await bump_upload_counter(user.id, context, message.chat_id, added=inserted)
            return
        payload = message_to_post_payload(message)
        await STATE.db.add_post(target_chat_id, payload)
        await bump_upload_counter(user.id, context, message.chat_id, added=1)
        return
    if mode == "promo_text":
        target_chat_id = int(admin_state.get("mode_chat_id"))
        chat = await STATE.db.get_chat(target_chat_id)
        title = chat.get("title", str(target_chat_id)) if chat else str(target_chat_id)
        old_promo_id = chat.get("promo_message_id") if chat else None
        if old_promo_id:
            try:
                await context.bot.unpin_chat_message(chat_id=target_chat_id, message_id=old_promo_id)
            except (BadRequest, Forbidden):
                pass
        try:
            sent = await context.bot.copy_message(chat_id=target_chat_id, from_chat_id=message.chat_id, message_id=message.message_id)
            await context.bot.pin_chat_message(chat_id=target_chat_id, message_id=sent.message_id, disable_notification=True)
        except (BadRequest, Forbidden) as exc:
            await clear_admin_mode(user.id)
            await message.reply_text(f"❌ Could not send/pin: {exc}")
            return
        await STATE.db.chats.update_one({"chat_id": target_chat_id}, {"$set": {"promo_message_id": sent.message_id, "updated_at": utcnow()}})
        await clear_admin_mode(user.id)
        await message.reply_text(f"📌 Prompt sent & pinned in {title} ✅")
        return

    target = await get_target_chat(user.id)
    if not target:
        await message.reply_text("Select a target from the dashboard first.")
        return

    if message.media_group_id and (message.photo or message.video):
        await buffer_media_group_item(update, context, target)
        return

    if message.document and (message.document.file_name or "").lower().endswith((".json", ".jsonl", ".txt")):
        posts = await parse_bulk_document(message, context)
        inserted = await STATE.db.bulk_add_posts(target["chat_id"], posts)
        await message.reply_text(f"Bulk import complete ✅\nInserted posts: {inserted}")
        chat = await STATE.db.get_chat(target["chat_id"])
        queue = chat.get("queue_state", {}).get("queue", [])
        cursor = int(chat.get("queue_state", {}).get("cursor", 0))
        if not queue or cursor >= len(queue):
            next_post_time = chat.get("next_post_time")
            await STATE.db.rebuild_cycle(target["chat_id"])
            await STATE.db.chats.update_one({"chat_id": target["chat_id"]}, {"$set": {"next_post_time": next_post_time}})
        return

    payload = message_to_post_payload(message)
    await STATE.db.add_post(target["chat_id"], payload)
    await message.reply_text(f"Saved to {target['title']} ✅\n{post_label(payload)}")
    chat = await STATE.db.get_chat(target["chat_id"])
    queue = chat.get("queue_state", {}).get("queue", [])
    cursor = int(chat.get("queue_state", {}).get("cursor", 0))
    if not queue or cursor >= len(queue):
        next_post_time = chat.get("next_post_time")
        await STATE.db.rebuild_cycle(target["chat_id"])
        await STATE.db.chats.update_one({"chat_id": target["chat_id"]}, {"$set": {"next_post_time": next_post_time}})


async def any_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat and chat.type in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
        await upsert_target_from_chat(chat)


async def auto_backup_tick(application: Application) -> None:
    try:
        payload = await STATE.db.backup_payload()
        blob = json_util.dumps(payload, indent=2).encode("utf-8")
        filename = f"autobackup-{utcnow().strftime('%Y%m%d-%H%M%S')}.json"
        for admin_id in SETTINGS.admin_ids:
            try:
                await application.bot.send_document(
                    admin_id,
                    document=InputFile(io.BytesIO(blob), filename=filename),
                    caption=f"🗓 Automatic daily backup ✅\nChats: {len(payload.get('chats', []))}",
                )
            except (BadRequest, Forbidden):
                pass  # admin hasn't started the bot privately, or blocked it
        await STATE.db.log("info", "auto_backup_sent", admins=len(SETTINGS.admin_ids))
    except Exception as exc:  # noqa: BLE001 - never let a backup failure kill the scheduler
        await STATE.db.log("error", "auto_backup_failed", error=str(exc))


async def on_startup(application: Application) -> None:
    global BOT_USERNAME
    await STATE.db.init()
    me = await application.bot.get_me()
    BOT_USERNAME = me.username or ""
    STATE.scheduler.start(paused=False)
    STATE.scheduler.add_job(scheduler_tick, "interval", seconds=SETTINGS.scan_interval_seconds, args=[application], max_instances=1, coalesce=True)
    STATE.scheduler.add_job(auto_backup_tick, "cron", hour=3, minute=0, args=[application], max_instances=1, coalesce=True, id="auto_backup")
    await STATE.db.log("info", "startup_complete", bot_username=BOT_USERNAME)


async def on_shutdown(application: Application) -> None:
    if STATE.scheduler.running:
        STATE.scheduler.shutdown(wait=False)
    await STATE.db.log("info", "shutdown_complete")


class _HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format: str, *args: Any) -> None:  # silence default access logs
        pass


def start_health_server() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), _HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logging.getLogger(__name__).info("health_check_server_started", extra={"port": port})


def build_application() -> Application:
    app = Application.builder().token(SETTINGS.bot_token).build()
    app.post_init = on_startup
    app.post_shutdown = on_shutdown
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("register", register_current_chat))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatJoinRequestHandler(handle_join_request))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, private_admin_message))
    app.add_handler(MessageHandler(~filters.ChatType.PRIVATE & ~filters.COMMAND, any_chat_message))
    return app


def main() -> None:
    start_health_server()
    app = build_application()
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
