"""Turning Telegram messages and bulk files into saved-post documents (and validating them)."""
from __future__ import annotations

import hashlib
from typing import Any

from bson import json_util as _json_util  # only for ObjectId/date aware JSON in bulk files

from .database import JSON_OPTIONS
from .sender import COPY_TYPES, MEDIA_METHODS, VALID_MEDIA_TYPES
from .utils import MAX_TEXT_LEN, normalize_url, parse_post_markup, text_fingerprint, visible_len, MAX_BUTTON_LABEL


class UnsupportedMessage(Exception):
    """The admin sent something we can't (or shouldn't) store as a post. The message is user-facing."""


NO_FLAGS: dict[str, Any] = {"silent": False, "protect_content": False, "spoiler": False, "pin": False, "nofooter": False, "buttons": []}


def media_fingerprint(media_type: str, unique_id: str | None) -> str | None:
    return f"m:{media_type}:{unique_id}" if unique_id else None


def album_fingerprint(items: list[dict[str, Any]]) -> str | None:
    ids = [i.get("file_unique_id") or i.get("file_id") for i in items]
    if not ids or not all(ids):
        return None
    return "a:" + hashlib.sha1("|".join(map(str, ids)).encode()).hexdigest()


def check_text_length(body: str) -> None:
    if visible_len(body) > MAX_TEXT_LEN:
        raise UnsupportedMessage(f"That text is {visible_len(body)} characters; Telegram allows at most {MAX_TEXT_LEN} per message. Please split it.")


def message_to_post_payload(message) -> dict[str, Any]:
    """Maps an admin's private message to a post document. Raises UnsupportedMessage if nothing storable."""
    spoiler_media = bool(getattr(message, "has_media_spoiler", False))
    caption_raw = message.caption_html if message.caption else None
    text_raw = message.text_html if message.text else None
    body, flags = parse_post_markup(text_raw or caption_raw or "")
    base = {"parse_mode": "HTML", **flags}

    def media(media_type: str, obj, *, file_name: str | None = None, with_caption: bool = True, spoilerable: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "media_type": media_type,
            "file_id": obj.file_id,
            "file_unique_id": getattr(obj, "file_unique_id", None),
            "file_name": file_name,
            "fingerprint": media_fingerprint(media_type, getattr(obj, "file_unique_id", None)),
            **base,
            "caption": body if with_caption else None,
        }
        payload["spoiler"] = bool(flags["spoiler"] or (spoilerable and spoiler_media))
        return payload

    if message.photo:
        return media("photo", message.photo[-1], file_name="photo", spoilerable=True)
    if message.video:
        return media("video", message.video, file_name=message.video.file_name, spoilerable=True)
    if message.animation:
        return media("animation", message.animation, file_name=message.animation.file_name, spoilerable=True)
    if message.video_note:
        return media("video_note", message.video_note, with_caption=False)
    if message.voice:
        return media("voice", message.voice)
    if message.audio:
        return media("audio", message.audio, file_name=message.audio.file_name)
    if message.sticker:
        return media("sticker", message.sticker, with_caption=False)
    if message.document:
        return media("document", message.document, file_name=message.document.file_name)
    if any(getattr(message, attr, None) for attr in ("poll", "location", "venue", "contact", "dice")):
        # These can't be re-sent from a file_id, so they are copied from the admin's chat when it is their turn.
        return {
            "media_type": "copy",
            "source_chat_id": message.chat_id,
            "source_message_id": message.message_id,
            "fingerprint": f"c:{message.chat_id}:{message.message_id}",
            "caption": None,
            **{k: v for k, v in NO_FLAGS.items()},
        }
    if not body:
        raise UnsupportedMessage("I can't save that kind of message. Send text, a photo, video, GIF, document, audio, voice, sticker, album, poll or a bulk file.")
    check_text_length(body)
    return {"media_type": "text", "text": body, "fingerprint": text_fingerprint(body), **base}


# --------------------------------------------------------------------------- #
# Bulk files (.json / .jsonl / .txt)
# --------------------------------------------------------------------------- #
def _clean_buttons(raw: Any) -> list[list[dict[str, str]]]:
    rows: list[list[dict[str, str]]] = []
    if not isinstance(raw, list):
        return rows
    for row in raw:
        if not isinstance(row, list):
            continue
        cleaned = []
        for item in row:
            if not isinstance(item, dict):
                continue
            url = normalize_url(str(item.get("url", "")))
            label = str(item.get("text", "")).strip()[:MAX_BUTTON_LABEL]
            if url and label:
                cleaned.append({"text": label, "url": url})
        if cleaned:
            rows.append(cleaned[:8])
    return rows[:10]


def normalize_bulk_post(item: Any) -> dict[str, Any] | None:
    """Returns a clean post document, or None when the entry is unusable."""
    if not isinstance(item, dict):
        return None
    media_type = str(item.get("media_type", "text")).strip().lower()
    if media_type not in VALID_MEDIA_TYPES:
        return None
    parse_mode = item.get("parse_mode", "HTML")
    if parse_mode not in ("HTML", "MarkdownV2", "Markdown", None):
        parse_mode = "HTML"
    post: dict[str, Any] = {
        "media_type": media_type,
        "parse_mode": parse_mode,
        "silent": bool(item.get("silent", False)),
        "protect_content": bool(item.get("protect_content", False)),
        "spoiler": bool(item.get("spoiler", False)),
        "pin": bool(item.get("pin", False)),
        "nofooter": bool(item.get("nofooter", False)),
        "buttons": _clean_buttons(item.get("buttons")),
    }
    caption = item.get("caption")
    if media_type == "text":
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        if visible_len(text) > MAX_TEXT_LEN:
            return None
        post.update(text=text.strip(), fingerprint=text_fingerprint(text))
        return post
    if media_type in COPY_TYPES:
        try:
            src_chat, src_msg = int(item["source_chat_id"]), int(item["source_message_id"])
        except (KeyError, TypeError, ValueError):
            return None
        post.update(source_chat_id=src_chat, source_message_id=src_msg, fingerprint=f"c:{src_chat}:{src_msg}")
        return post
    post["caption"] = caption if isinstance(caption, str) and caption.strip() else None
    if media_type == "album":
        group = item.get("media_group")
        if not isinstance(group, list):
            return None
        cleaned = []
        for entry in group:
            if isinstance(entry, dict) and entry.get("file_id") and entry.get("media_type") in ("photo", "video"):
                cleaned.append({k: entry.get(k) for k in ("media_type", "file_id", "file_unique_id", "spoiler") if entry.get(k) is not None})
        if not cleaned:
            return None
        post.update(media_group=cleaned, fingerprint=album_fingerprint(cleaned))
        return post
    if media_type in MEDIA_METHODS:
        file_id = item.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            return None
        unique = item.get("file_unique_id")
        post.update(
            file_id=file_id,
            file_unique_id=unique,
            file_name=item.get("file_name"),
            fingerprint=media_fingerprint(media_type, unique) or f"f:{media_type}:{file_id}",
        )
        return post
    return None


def parse_bulk_text(filename: str, text: str) -> tuple[list[dict[str, Any]], int]:
    """Returns (valid_posts, invalid_count). Raises ValueError for files that aren't parseable at all."""
    name = (filename or "").lower()
    text = text.lstrip("﻿")
    raw_items: list[Any] = []
    if name.endswith(".json"):
        try:
            raw = _json_util.loads(text, json_options=JSON_OPTIONS)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Invalid JSON: {exc}") from exc
        if isinstance(raw, dict) and "posts" in raw:
            raw = raw["posts"]
        if not isinstance(raw, list):
            raise ValueError("JSON bulk file must contain an array of posts (or an object with a \"posts\" array).")
        raw_items = raw
    elif name.endswith(".jsonl"):
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                raw_items.append(_json_util.loads(line, json_options=JSON_OPTIONS))
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"Invalid JSON on line {number}: {exc}") from exc
    else:
        posts: list[dict[str, Any]] = []
        bad = 0
        for chunk in text.replace("\r\n", "\n").split("\n---\n"):
            if not chunk.strip():
                continue
            body, flags = parse_post_markup(chunk)
            if not body or visible_len(body) > MAX_TEXT_LEN:
                bad += 1
                continue
            posts.append({"media_type": "text", "text": body, "parse_mode": "HTML", "fingerprint": text_fingerprint(body), **flags})
        return posts, bad
    posts, bad = [], 0
    for item in raw_items:
        clean = normalize_bulk_post(item)
        if clean:
            posts.append(clean)
        else:
            bad += 1
    return posts, bad
