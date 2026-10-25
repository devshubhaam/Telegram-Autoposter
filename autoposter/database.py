"""MongoDB access layer (motor). All datetimes are timezone-aware UTC (client uses tz_aware=True)."""
from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from typing import Any

from bson import ObjectId
from bson import json_util
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from .config import SETTINGS, TZ, Settings, logger
from .utils import (
    REACTION_EMOJIS,
    aware,
    chat_next_run,
    coll_suffix,
    compute_next_run,
    default_schedule,
    utcnow,
)

# Explicit options so restored/exported datetimes are always tz-aware UTC.
JSON_OPTIONS = json_util.JSONOptions(json_mode=json_util.JSONMode.RELAXED, tz_aware=True, tzinfo=UTC)

BLANK_QUEUE_STATE: dict[str, Any] = {
    "cycle": 0,
    "cursor": 0,
    "queue": [],
    "shuffle_status": "idle",
    "last_batch_size": 0,
    "last_posted_at": None,
    "last_reshuffled_at": None,
    "last_post_id": None,
    "cycle_counted": False,
}


def dumps_json(payload: Any) -> bytes:
    return json_util.dumps(payload, indent=2, json_options=JSON_OPTIONS).encode("utf-8")


def loads_json(text: str) -> Any:
    return json_util.loads(text, json_options=JSON_OPTIONS)


class Database:
    def __init__(self, settings: Settings) -> None:
        self.client = AsyncIOMotorClient(settings.mongo_uri, tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=20000)
        self.db = self.client[settings.mongo_db]
        self.chats = self.db["chats"]
        self.admins = self.db["admins"]
        self.logs = self.db["logs"]
        self.reactions = self.db["reactions"]
        self.deletes = self.db["scheduled_deletes"]
        self._indexed_chats: set[int] = set()
        self._reaction_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Collections / bootstrap
    # ------------------------------------------------------------------ #
    def posts_coll(self, chat_id: int):
        return self.db[f"posts_{coll_suffix(chat_id)}"]

    def history_coll(self, chat_id: int):
        return self.db[f"history_{coll_suffix(chat_id)}"]

    async def ping(self) -> float:
        started = asyncio.get_running_loop().time()
        await self.client.admin.command("ping")
        return (asyncio.get_running_loop().time() - started) * 1000

    async def init(self) -> None:
        await self.chats.create_index([("chat_id", ASCENDING)], unique=True)
        await self.chats.create_index([("next_post_time", ASCENDING)])
        await self.chats.create_index([("processing_until", ASCENDING)])
        await self.admins.create_index([("admin_id", ASCENDING)], unique=True)
        await self.logs.create_index([("created_at", ASCENDING)])
        await self.reactions.create_index([("chat_id", ASCENDING), ("message_id", ASCENDING)], unique=True)
        await self.deletes.create_index([("delete_at", ASCENDING)])
        await self.migrate_legacy()

    async def migrate_legacy(self) -> None:
        """The old "Import Range by channel" feature hid its source channels. That feature is gone:
        empty hidden sources are deleted, anything else is simply un-hidden."""
        async for chat in self.chats.find({"hidden_source_only": True}):
            chat_id = chat["chat_id"]
            if await self.count_posts(chat_id) == 0:
                await self.delete_chat(chat_id)
            else:
                await self.chats.update_one(
                    {"chat_id": chat_id},
                    {"$unset": {"hidden_source_only": ""}, "$set": {"is_active": True, "updated_at": utcnow()}},
                )
        # Admins that were mid-way through the removed import wizard.
        await self.admins.update_many(
            {"mode": {"$in": ["import_source", "import_from_link", "import_to_link"]}},
            {"$unset": {"mode": "", "mode_chat_id": "", "mode_extra": "", "source_chat_id": "", "from_message_id": ""}},
        )

    async def log(self, level: str, event: str, **payload: Any) -> None:
        level = level.lower()
        try:
            await self.logs.insert_one({"level": level.upper(), "event": event, "payload": payload, "created_at": utcnow()})
        except Exception:  # noqa: BLE001 - logging must never break the caller
            logger.exception("could not persist log row")
        getattr(logger, level if level in ("debug", "info", "warning", "error", "critical") else "info")("%s | %s", event, payload)

    async def cleanup_logs(self, days: int) -> int:
        res = await self.logs.delete_many({"created_at": {"$lt": utcnow() - timedelta(days=days)}})
        return int(getattr(res, "deleted_count", 0))

    # ------------------------------------------------------------------ #
    # Chats
    # ------------------------------------------------------------------ #
    async def ensure_chat(self, chat_id: int, title: str, chat_type: str, username: str | None = None) -> dict[str, Any]:
        try:
            doc = await self._ensure_chat_once(chat_id, title, chat_type, username)
        except DuplicateKeyError:
            doc = await self._ensure_chat_once(chat_id, title, chat_type, username)
        await self._ensure_chat_indexes(chat_id)
        return doc

    async def _ensure_chat_indexes(self, chat_id: int) -> None:
        if chat_id in self._indexed_chats:
            return
        posts, history = self.posts_coll(chat_id), self.history_coll(chat_id)
        await posts.create_index([("created_at", ASCENDING)])
        await posts.create_index([("used_in_cycle", ASCENDING)])
        await posts.create_index([("fingerprint", ASCENDING)])
        await history.create_index([("posted_at", ASCENDING)])
        self._indexed_chats.add(chat_id)

    async def _ensure_chat_once(self, chat_id: int, title: str, chat_type: str, username: str | None = None) -> dict[str, Any]:
        now = utcnow()
        schedule = default_schedule()
        return await self.chats.find_one_and_update(
            {"chat_id": chat_id},
            {
                "$set": {"title": title, "type": chat_type, "username": username, "updated_at": now},
                "$setOnInsert": {
                    "created_at": now,
                    "is_active": True,
                    "schedule": schedule,
                    "options": {},
                    "queue_state": dict(BLANK_QUEUE_STATE),
                    "stats": {"total_cycles_completed": 0},
                    "next_post_time": compute_next_run(now, schedule),
                    "processing_until": None,
                    "last_error": None,
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

    async def list_chats(self) -> list[dict[str, Any]]:
        return await self.chats.find({}).sort("title", ASCENDING).to_list(length=1000)

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

    async def set_options(self, chat_id: int, **options: Any) -> dict[str, Any] | None:
        updates = {f"options.{k}": v for k, v in options.items()}
        updates["updated_at"] = utcnow()
        return await self.chats.find_one_and_update({"chat_id": chat_id}, {"$set": updates}, return_document=ReturnDocument.AFTER)

    async def set_active(self, chat_id: int, active: bool, *, reason: str | None = None, error: str | None = None) -> dict[str, Any] | None:
        """Pause / resume. Resuming recomputes the next slot; pausing clears it."""
        chat = await self.get_chat(chat_id)
        if not chat:
            return None
        fields: dict[str, Any] = {
            "is_active": active,
            "next_post_time": chat_next_run(chat, utcnow()) if active else None,
            "pause_reason": None if active else reason,
            "updated_at": utcnow(),
        }
        if active:
            fields["last_error"] = None
        elif error:
            fields["last_error"] = error[:300]
        return await self.chats.find_one_and_update({"chat_id": chat_id}, {"$set": fields}, return_document=ReturnDocument.AFTER)

    async def update_schedule(self, chat_id: int, **fields: Any) -> dict[str, Any] | None:
        chat = await self.get_chat(chat_id)
        if not chat:
            return None
        schedule = {**default_schedule(), **(chat.get("schedule") or {})}
        for key in ("interval_minutes", "batch_size", "start_time", "end_time", "days", "enabled"):
            if key in fields and fields[key] is not None:
                schedule[key] = fields[key]
        preview = {**chat, "schedule": schedule}
        next_run = chat_next_run(preview, utcnow()) if chat.get("is_active", True) else None
        return await self.chats.find_one_and_update(
            {"chat_id": chat_id},
            {"$set": {"schedule": schedule, "next_post_time": next_run, "updated_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )

    async def touch_schedule(self, chat_id: int) -> dict[str, Any] | None:
        """If an active chat has no next slot (e.g. after a wipe) give it one."""
        chat = await self.get_chat(chat_id)
        if chat and chat.get("is_active", True) and not chat.get("next_post_time"):
            nxt = chat_next_run(chat, utcnow())
            await self.chats.update_one({"chat_id": chat_id}, {"$set": {"next_post_time": nxt, "updated_at": utcnow()}})
            chat["next_post_time"] = nxt
        return chat

    async def wipe_chat(self, chat_id: int) -> None:
        await self.posts_coll(chat_id).delete_many({})
        await self.history_coll(chat_id).delete_many({})
        await self.reactions.delete_many({"chat_id": chat_id})
        await self.chats.update_one(
            {"chat_id": chat_id},
            {"$set": {"queue_state": dict(BLANK_QUEUE_STATE), "stats.total_cycles_completed": 0, "next_post_time": None, "last_error": None, "updated_at": utcnow()}},
        )

    async def delete_chat(self, chat_id: int) -> None:
        await self.posts_coll(chat_id).drop()
        await self.history_coll(chat_id).drop()
        await self.reactions.delete_many({"chat_id": chat_id})
        await self.chats.delete_one({"chat_id": chat_id})
        await self.admins.update_many({"target_chat_id": chat_id}, {"$set": {"target_chat_id": None}})
        self._indexed_chats.discard(chat_id)

    async def wipe_all(self) -> None:
        for chat in await self.list_chats():
            await self.posts_coll(chat["chat_id"]).drop()
            await self.history_coll(chat["chat_id"]).drop()
        await self.chats.delete_many({})
        await self.admins.delete_many({})
        await self.logs.delete_many({})
        await self.reactions.delete_many({})
        await self.deletes.delete_many({})
        self._indexed_chats.clear()
        await self.init()

    # ------------------------------------------------------------------ #
    # Reactions
    # ------------------------------------------------------------------ #
    async def toggle_reaction(self, chat_id: int, message_id: int, user_id: int, emoji_idx: int) -> dict[int, int]:
        """One reaction per user per post; tapping the same emoji again removes it."""
        async with self._reaction_lock:
            key = {"chat_id": chat_id, "message_id": message_id}
            doc = await self.reactions.find_one(key) or {}
            counts: dict[str, int] = dict(doc.get("counts", {}))
            voters: dict[str, int] = dict(doc.get("voters", {}))
            uid = str(user_id)
            previous = voters.get(uid)
            if previous is not None:
                counts[str(previous)] = max(0, int(counts.get(str(previous), 0)) - 1)
            if previous == emoji_idx:
                voters.pop(uid, None)
            else:
                counts[str(emoji_idx)] = int(counts.get(str(emoji_idx), 0)) + 1
                voters[uid] = emoji_idx
            await self.reactions.update_one(key, {"$set": {"counts": counts, "voters": voters, "updated_at": utcnow()}}, upsert=True)
            return {int(k): int(v) for k, v in counts.items()}

    async def reaction_summary(self, chat_id: int, top: int = 3) -> tuple[list[int], list[tuple[int, dict[str, Any] | None]]]:
        totals = [0] * len(REACTION_EMOJIS)
        per_message: list[tuple[int, int]] = []
        async for doc in self.reactions.find({"chat_id": chat_id}):
            msg_total = 0
            for k, v in (doc.get("counts") or {}).items():
                idx, n = int(k), int(v)
                if 0 <= idx < len(totals):
                    totals[idx] += n
                msg_total += n
            if msg_total > 0:
                per_message.append((msg_total, int(doc["message_id"])))
        per_message.sort(reverse=True)
        best: list[tuple[int, dict[str, Any] | None]] = []
        for total, message_id in per_message[:top]:
            hist = await self.history_coll(chat_id).find_one({"telegram_message_id": message_id})
            post = None
            if hist:
                try:
                    post = await self.get_post(chat_id, hist["post_id"])
                except Exception:  # noqa: BLE001
                    post = None
            best.append((total, post))
        return totals, best

    # ------------------------------------------------------------------ #
    # Posts
    # ------------------------------------------------------------------ #
    async def count_posts(self, chat_id: int) -> int:
        return await self.posts_coll(chat_id).count_documents({"active": True})

    async def count_used_posts(self, chat_id: int) -> int:
        return await self.posts_coll(chat_id).count_documents({"active": True, "used_in_cycle": True})

    async def get_post(self, chat_id: int, post_id: str) -> dict[str, Any] | None:
        try:
            oid = ObjectId(post_id)
        except Exception:  # noqa: BLE001 - malformed ids simply don't exist
            return None
        return await self.posts_coll(chat_id).find_one({"_id": oid, "active": True})

    async def recent_posts(self, chat_id: int, limit: int = 10) -> list[dict[str, Any]]:
        return await self.posts_coll(chat_id).find({"active": True}).sort("created_at", DESCENDING).to_list(length=limit)

    async def page_posts(self, chat_id: int, page: int, size: int = 6) -> tuple[list[dict[str, Any]], int]:
        total = await self.count_posts(chat_id)
        pages = max(1, -(-total // size))
        page = max(0, min(page, pages - 1))
        rows = await (
            self.posts_coll(chat_id).find({"active": True}).sort([("created_at", DESCENDING), ("_id", DESCENDING)]).skip(page * size).limit(size).to_list(length=size)
        )
        return rows, total

    async def delete_post(self, chat_id: int, post_id: str) -> bool:
        try:
            oid = ObjectId(post_id)
        except Exception:  # noqa: BLE001
            return False
        res = await self.posts_coll(chat_id).delete_one({"_id": oid})
        return bool(getattr(res, "deleted_count", 0))

    async def undo_last_post(self, chat_id: int) -> dict[str, Any] | None:
        post = await self.posts_coll(chat_id).find_one({"active": True}, sort=[("created_at", DESCENDING), ("_id", DESCENDING)])
        if not post:
            return None
        await self.posts_coll(chat_id).delete_one({"_id": post["_id"]})
        return post

    async def quarantine_post(self, chat_id: int, post_id: str, reason: str) -> None:
        await self.posts_coll(chat_id).update_one({"_id": ObjectId(post_id)}, {"$set": {"active": False, "quarantined": True, "last_error": reason, "updated_at": utcnow()}})

    async def record_post_failure(self, chat_id: int, post_id: str, reason: str) -> int:
        doc = await self.posts_coll(chat_id).find_one_and_update(
            {"_id": ObjectId(post_id)},
            {"$set": {"last_error": reason[:300], "updated_at": utcnow()}, "$inc": {"fail_count": 1}},
            return_document=ReturnDocument.AFTER,
        )
        return int((doc or {}).get("fail_count", 0))

    async def existing_fingerprints(self, chat_id: int, fingerprints: list[str]) -> set[str]:
        found: set[str] = set()
        fingerprints = [f for f in set(fingerprints) if f]
        for i in range(0, len(fingerprints), 500):
            chunk = fingerprints[i : i + 500]
            async for row in self.posts_coll(chat_id).find({"fingerprint": {"$in": chunk}, "active": True}, {"fingerprint": 1}):
                found.add(row["fingerprint"])
        return found

    async def enqueue_new_posts(self, chat_id: int, post_ids: list[str]) -> None:
        """Mixes new posts into the not-yet-sent part of a cycle that is currently running."""
        if not post_ids:
            return
        chat = await self.get_chat(chat_id)
        if not chat:
            return
        queue_state = chat.get("queue_state", {}) or {}
        queue: list[str] = list(queue_state.get("queue") or [])
        cursor = int(queue_state.get("cursor", 0))
        if not queue or cursor >= len(queue):
            return  # no running cycle; the next rebuild will include these
        already_sent = queue[:cursor]
        remaining = queue[cursor:] + [str(pid) for pid in post_ids]
        random.shuffle(remaining)
        await self.chats.update_one(
            {"chat_id": chat_id},
            {"$set": {"queue_state.queue": already_sent + remaining, "queue_state.shuffle_status": "ready", "updated_at": utcnow()}},
        )

    async def add_post(self, chat_id: int, payload: dict[str, Any], dedupe: bool = True) -> str | None:
        """Returns the new post id, or None when it was skipped as a duplicate."""
        fp = payload.get("fingerprint")
        if dedupe and fp and await self.existing_fingerprints(chat_id, [fp]):
            return None
        now = utcnow()
        payload = dict(payload)
        payload.update(
            {
                "active": True,
                "used_in_cycle": False,
                "posted_count": payload.get("posted_count", 0),
                "created_at": payload.get("created_at", now),
                "updated_at": now,
            }
        )
        result = await self.posts_coll(chat_id).insert_one(payload)
        await self.enqueue_new_posts(chat_id, [str(result.inserted_id)])
        await self.chats.update_one({"chat_id": chat_id}, {"$set": {"updated_at": now}})
        return str(result.inserted_id)

    async def bulk_add_posts(self, chat_id: int, posts: list[dict[str, Any]], dedupe: bool = True) -> tuple[int, int]:
        """Returns (inserted, duplicates_skipped)."""
        if not posts:
            return 0, 0
        now = utcnow()
        duplicates = 0
        to_insert: list[dict[str, Any]] = []
        known: set[str] = set()
        if dedupe:
            known = await self.existing_fingerprints(chat_id, [p.get("fingerprint") for p in posts if p.get("fingerprint")])
        for i, post in enumerate(posts):
            fp = post.get("fingerprint")
            if dedupe and fp:
                if fp in known:
                    duplicates += 1
                    continue
                known.add(fp)
            row = dict(post)
            row.setdefault("active", True)
            row.setdefault("used_in_cycle", False)
            row.setdefault("posted_count", 0)
            row.setdefault("created_at", now + timedelta(microseconds=i))  # keeps upload order stable
            row["updated_at"] = now
            to_insert.append(row)
        if not to_insert:
            return 0, duplicates
        result = await self.posts_coll(chat_id).insert_many(to_insert)
        await self.enqueue_new_posts(chat_id, [str(i) for i in result.inserted_ids])
        await self.chats.update_one({"chat_id": chat_id}, {"$set": {"updated_at": now}})
        return len(result.inserted_ids), duplicates

    # ------------------------------------------------------------------ #
    # Processing lock + queue
    # ------------------------------------------------------------------ #
    async def acquire_processing(self, chat_id: int) -> bool:
        now = utcnow()
        until = now + timedelta(seconds=SETTINGS.scheduler_lock_seconds)
        result = await self.chats.find_one_and_update(
            {"chat_id": chat_id, "$or": [{"processing_until": None}, {"processing_until": {"$lt": now}}]},
            {"$set": {"processing_until": until}},
            return_document=ReturnDocument.AFTER,
        )
        return result is not None

    async def refresh_processing(self, chat_id: int) -> None:
        until = utcnow() + timedelta(seconds=SETTINGS.scheduler_lock_seconds)
        await self.chats.update_one({"chat_id": chat_id}, {"$set": {"processing_until": until}})

    async def release_processing(self, chat_id: int) -> None:
        await self.chats.update_one({"chat_id": chat_id}, {"$set": {"processing_until": None}})

    async def rebuild_cycle(self, chat_id: int, only_unused: bool = False) -> dict[str, Any] | None:
        chat = await self.get_chat(chat_id)
        if not chat:
            return None
        qs = chat.get("queue_state", {}) or {}
        flt: dict[str, Any] = {"active": True}
        if only_unused:
            flt["used_in_cycle"] = {"$ne": True}
        post_ids = [str(row["_id"]) async for row in self.posts_coll(chat_id).find(flt, {"_id": 1})]
        random.shuffle(post_ids)
        last_id = qs.get("last_post_id")
        if len(post_ids) > 1 and post_ids[0] == last_id:  # never open a cycle with the post that just closed the last one
            j = random.randrange(1, len(post_ids))
            post_ids[0], post_ids[j] = post_ids[j], post_ids[0]
        if not only_unused:
            await self.posts_coll(chat_id).update_many({"active": True}, {"$set": {"used_in_cycle": False}})
        prev_cycle = int(qs.get("cycle", 0))
        if post_ids and not (only_unused and prev_cycle > 0):
            next_cycle = prev_cycle + 1
        else:
            next_cycle = prev_cycle
        queue_state = {
            "cycle": next_cycle,
            "cursor": 0,
            "queue": post_ids,
            "shuffle_status": "ready" if post_ids else "empty",
            "last_batch_size": 0,
            "last_posted_at": qs.get("last_posted_at"),
            "last_reshuffled_at": utcnow(),
            "last_post_id": last_id,
            "cycle_counted": False,
        }
        return await self.chats.find_one_and_update(
            {"chat_id": chat_id},
            {"$set": {"queue_state": queue_state, "updated_at": utcnow(), "last_error": None}},
            return_document=ReturnDocument.AFTER,
        )

    async def advance_after_success(self, chat_id: int, post_id: str, message_id: int, message_ids: list[int]) -> None:
        now = utcnow()
        chat = await self.get_chat(chat_id)
        cycle = int(((chat or {}).get("queue_state") or {}).get("cycle", 0))
        await self.posts_coll(chat_id).update_one(
            {"_id": ObjectId(post_id)},
            {"$set": {"used_in_cycle": True, "last_posted_at": now, "updated_at": now, "last_error": None}, "$inc": {"posted_count": 1}},
        )
        await self.history_coll(chat_id).insert_one(
            {"post_id": post_id, "telegram_message_id": message_id, "message_ids": message_ids, "posted_at": now, "cycle": cycle}
        )
        await self.chats.update_one(
            {"chat_id": chat_id},
            {
                "$inc": {"queue_state.cursor": 1},
                "$set": {"queue_state.last_posted_at": now, "queue_state.last_post_id": post_id, "updated_at": now, "last_error": None},
            },
        )

    async def skip_cursor(self, chat_id: int) -> None:
        await self.chats.update_one({"chat_id": chat_id}, {"$inc": {"queue_state.cursor": 1}})

    async def finalize_batch(self, chat_id: int, sent_count: int, next_run: datetime | None) -> dict[str, Any] | None:
        chat = await self.get_chat(chat_id)
        if not chat:
            return None
        qs = chat.get("queue_state", {}) or {}
        queue = qs.get("queue", [])
        exhausted = bool(queue) and int(qs.get("cursor", 0)) >= len(queue)
        status = "cycle complete • awaiting next shuffle" if exhausted else "in progress"
        return await self.chats.find_one_and_update(
            {"chat_id": chat_id},
            {"$set": {"next_post_time": next_run, "queue_state.last_batch_size": sent_count, "queue_state.shuffle_status": status, "updated_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )

    async def mark_cycle_counted(self, chat_id: int) -> bool:
        """Atomically counts a finished cycle exactly once. Returns True if this call counted it."""
        res = await self.chats.find_one_and_update(
            {"chat_id": chat_id, "queue_state.cycle_counted": {"$ne": True}},
            {"$set": {"queue_state.cycle_counted": True}, "$inc": {"stats.total_cycles_completed": 1}},
            return_document=ReturnDocument.AFTER,
        )
        return res is not None

    async def due_chats(self, now: datetime) -> list[dict[str, Any]]:
        cursor = self.chats.find({"is_active": True, "next_post_time": {"$ne": None, "$lte": now}}).sort("next_post_time", ASCENDING)
        return await cursor.to_list(length=200)

    async def defer_chat(self, chat_id: int, seconds: int, error: str | None = None) -> None:
        fields: dict[str, Any] = {"next_post_time": utcnow() + timedelta(seconds=seconds), "updated_at": utcnow()}
        if error:
            fields["last_error"] = error[:300]
        await self.chats.update_one({"chat_id": chat_id}, {"$set": fields})

    # ------------------------------------------------------------------ #
    # Auto-delete queue
    # ------------------------------------------------------------------ #
    async def schedule_delete(self, chat_id: int, message_ids: list[int], when: datetime) -> None:
        await self.deletes.insert_one({"chat_id": chat_id, "message_ids": message_ids, "delete_at": when})

    async def due_deletes(self, now: datetime, limit: int = 100) -> list[dict[str, Any]]:
        return await self.deletes.find({"delete_at": {"$lte": now}}).sort("delete_at", ASCENDING).to_list(length=limit)

    async def remove_delete(self, row_id: Any) -> None:
        await self.deletes.delete_one({"_id": row_id})

    # ------------------------------------------------------------------ #
    # Stats
    # ------------------------------------------------------------------ #
    async def published_today(self, chat_id: int) -> int:
        today_local = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        return await self.history_coll(chat_id).count_documents({"posted_at": {"$gte": today_local.astimezone(UTC)}})

    async def history_by_day(self, chat_id: int, days: int = 7) -> list[tuple[str, int]]:
        today_local = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        start_local = today_local - timedelta(days=days - 1)
        buckets: dict[str, int] = {}
        for i in range(days):
            buckets[(start_local + timedelta(days=i)).strftime("%a %d")] = 0
        async for row in self.history_coll(chat_id).find({"posted_at": {"$gte": start_local.astimezone(UTC)}}, {"posted_at": 1}):
            label = aware(row["posted_at"]).astimezone(TZ).strftime("%a %d")
            if label in buckets:
                buckets[label] += 1
        return list(buckets.items())

    async def queue_snapshot(self, chat_id: int) -> dict[str, Any]:
        chat = await self.get_chat(chat_id) or {}
        total = await self.count_posts(chat_id)
        used = await self.count_used_posts(chat_id)
        qs = chat.get("queue_state", {}) or {}
        cursor = int(qs.get("cursor", 0))
        queue = qs.get("queue", []) or []
        batch = int((chat.get("schedule") or {}).get("batch_size", 5))
        return {
            "total": total,
            "used": used,
            "remaining": max(total - used, 0),
            "cycle": int(qs.get("cycle", 0)),
            "current_cursor": cursor,
            "next_batch_ids": queue[cursor : cursor + batch],
            "queue_size": len(queue),
            "shuffle_status": qs.get("shuffle_status", "idle"),
            "last_posted_at": qs.get("last_posted_at"),
            "next_post_time": chat.get("next_post_time"),
        }

    async def global_stats(self) -> dict[str, Any]:
        chats = await self.list_chats()
        total_channels = sum(1 for c in chats if c.get("type") == "channel")
        total_groups = len(chats) - total_channels
        paused = sum(1 for c in chats if not c.get("is_active", True))
        cycles = sum(int((c.get("stats") or {}).get("total_cycles_completed", 0)) for c in chats)
        next_times = [aware(c["next_post_time"]) for c in chats if c.get("next_post_time") and c.get("is_active", True)]

        async def per_chat(chat_id: int) -> list[int]:
            return list(await asyncio.gather(self.count_posts(chat_id), self.count_used_posts(chat_id), self.published_today(chat_id)))

        results = await asyncio.gather(*(per_chat(c["chat_id"]) for c in chats)) if chats else []
        total_posts = sum(r[0] for r in results)
        total_used = sum(r[1] for r in results)
        return {
            "total_posts": total_posts,
            "remaining_posts": max(total_posts - total_used, 0),
            "published_today": sum(r[2] for r in results),
            "total_cycles_completed": cycles,
            "total_channels": total_channels,
            "total_groups": total_groups,
            "paused": paused,
            "next_post_time": min(next_times) if next_times else None,
        }

    # ------------------------------------------------------------------ #
    # Backup / restore
    # ------------------------------------------------------------------ #
    async def backup_payload(self) -> dict[str, Any]:
        chats = await self.list_chats()
        payload: dict[str, Any] = {"version": 2, "exported_at": utcnow().isoformat(), "chats": [], "posts": {}}
        for chat in chats:
            cid = str(chat["chat_id"])
            payload["chats"].append({k: v for k, v in chat.items() if k != "_id"})
            rows = []
            async for row in self.posts_coll(chat["chat_id"]).find({}):
                row["_id"] = str(row["_id"])
                rows.append(row)
            payload["posts"][cid] = rows
        return payload

    async def restore_payload(self, payload: Any) -> tuple[int, int]:
        if not isinstance(payload, dict) or not isinstance(payload.get("chats", []), list) or not isinstance(payload.get("posts", {}), dict):
            raise ValueError("This doesn't look like a backup file made by this bot.")
        restored_chats = 0
        restored_posts = 0
        known_ids: set[int] = set()
        for chat in payload.get("chats", []):
            if not isinstance(chat, dict) or "chat_id" not in chat:
                continue
            chat = dict(chat)
            chat_id = int(chat["chat_id"])
            chat.pop("_id", None)
            chat["processing_until"] = None
            await self.chats.replace_one({"chat_id": chat_id}, chat, upsert=True)
            await self._ensure_chat_indexes(chat_id)
            known_ids.add(chat_id)
            restored_chats += 1
        for raw_chat_id, posts in payload.get("posts", {}).items():
            chat_id = int(raw_chat_id)
            if chat_id not in known_ids and not await self.get_chat(chat_id):
                continue
            await self.posts_coll(chat_id).delete_many({})
            to_insert = []
            for row in posts or []:
                if not isinstance(row, dict):
                    continue
                row = dict(row)
                try:
                    row["_id"] = ObjectId(row["_id"]) if row.get("_id") else ObjectId()
                except Exception:  # noqa: BLE001
                    row["_id"] = ObjectId()
                to_insert.append(row)
            if to_insert:
                await self.posts_coll(chat_id).insert_many(to_insert)
                restored_posts += len(to_insert)
        return restored_chats, restored_posts

