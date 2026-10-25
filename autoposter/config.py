"""Environment-driven settings. Fails early with a readable message instead of a bare KeyError."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger("telegram_autoposter")


def _clean(value: str | None) -> str:
    return (value or "").strip().strip("\"'").strip()


def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer (got {raw!r})") from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum} (got {value})")
    return value


def _env_float(name: str, default: float, minimum: float | None = None) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number (got {raw!r})") from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum} (got {value})")
    return value


def _env_hhmm(name: str, default: str) -> str:
    raw = os.getenv(name, default).strip() or default
    try:
        hour, minute = raw.split(":", 1)
        time(hour=int(hour), minute=int(minute))
    except ValueError as exc:
        raise RuntimeError(f"{name} must look like HH:MM (got {raw!r})") from exc
    return raw


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
    post_gap_seconds: float
    log_retention_days: int
    admin_password: str | None = None
    password_timeout_seconds: int = 300

    @classmethod
    def from_env(cls) -> "Settings":
        missing = [name for name in ("BOT_TOKEN", "MONGO_URI", "ADMIN_IDS") if not _clean(os.getenv(name))]
        if missing:
            raise RuntimeError("Missing required environment variable(s): " + ", ".join(missing))

        admin_ids: set[int] = set()
        for part in os.getenv("ADMIN_IDS", "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                admin_ids.add(int(part))
            except ValueError as exc:
                raise RuntimeError(f"ADMIN_IDS must be comma separated numeric Telegram user IDs (bad value: {part!r})") from exc

        timezone = os.getenv("TIMEZONE", "UTC").strip() or "UTC"
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise RuntimeError(f"TIMEZONE {timezone!r} is not a valid IANA timezone (example: Asia/Kolkata)") from exc

        days_raw = os.getenv("DEFAULT_DAYS", "0,1,2,3,4,5,6")
        try:
            days = sorted({int(x) for x in days_raw.split(",") if x.strip()})
        except ValueError as exc:
            raise RuntimeError(f"DEFAULT_DAYS must be comma separated 0-6 (got {days_raw!r})") from exc
        if not days or any(d not in range(7) for d in days):
            raise RuntimeError(f"DEFAULT_DAYS must be comma separated 0-6 (got {days_raw!r})")

        return cls(
            bot_token=_clean(os.environ["BOT_TOKEN"]),
            mongo_uri=_clean(os.environ["MONGO_URI"]),
            mongo_db=os.getenv("MONGO_DB", "telegram_autoposter").strip() or "telegram_autoposter",
            admin_ids=admin_ids,
            timezone=timezone,
            scan_interval_seconds=_env_int("SCAN_INTERVAL_SECONDS", 30, 5),
            default_interval_minutes=_env_int("DEFAULT_INTERVAL_MINUTES", 180, 1),
            default_batch_size=_env_int("DEFAULT_BATCH_SIZE", 5, 1),
            default_start_time=_env_hhmm("DEFAULT_START_TIME", "00:00"),
            default_end_time=_env_hhmm("DEFAULT_END_TIME", "21:00"),
            default_days=days,
            scheduler_lock_seconds=_env_int("SCHEDULER_LOCK_SECONDS", 300, 30),
            retry_attempts=_env_int("RETRY_ATTEMPTS", 3, 1),
            retry_base_seconds=_env_float("RETRY_BASE_SECONDS", 2.0, 0.0),
            post_gap_seconds=_env_float("POST_GAP_SECONDS", 1.0, 0.0),
            log_retention_days=_env_int("LOG_RETENTION_DAYS", 14, 1),
            admin_password=(os.getenv("ADMIN_PASSWORD", "").strip() or None),
            password_timeout_seconds=_env_int("PASSWORD_TIMEOUT_SECONDS", 300, 30),
        )


SETTINGS = Settings.from_env()
TZ = ZoneInfo(SETTINGS.timezone)
