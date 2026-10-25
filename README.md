# Telegram Autoposter v2

Channels/groups me posts ko shuffle karke schedule par auto-post karne wala bot (MongoDB + python-telegram-bot 22.5).

## Env variables
| Name | Required | Default |
|---|---|---|
| `BOT_TOKEN`, `MONGO_URI`, `ADMIN_IDS` (comma separated) | yes | – |
| `TIMEZONE` | no | `UTC` (e.g. `Asia/Kolkata`) |
| `MONGO_DB` | no | `telegram_autoposter` |
| `ADMIN_PASSWORD` | no | – (gates destructive actions) |
| `DEFAULT_INTERVAL_MINUTES` / `DEFAULT_BATCH_SIZE` / `DEFAULT_START_TIME` / `DEFAULT_END_TIME` / `DEFAULT_DAYS` | no | 180 / 5 / 00:00 / 21:00 / 0-6 |
| `SCAN_INTERVAL_SECONDS`, `RETRY_ATTEMPTS`, `RETRY_BASE_SECONDS`, `POST_GAP_SECONDS`, `LOG_RETENTION_DAYS`, `SCHEDULER_LOCK_SECONDS`, `PASSWORD_TIMEOUT_SECONDS`, `LOG_LEVEL`, `PORT` | no | 30 / 3 / 2 / 1 / 14 / 300 / 300 / INFO / 8080 |

## Features
Upload mode (one tap), one-tap schedule presets, jitter, loop/one-pass, duplicate skipping, per-channel footer,
auto-delete, post browser (preview / send now / delete), 7-day stats + top reactions, auto-quarantine of broken posts,
flood-wait handling, daily gzip-aware backup, force-sub gate, pinned promo, `#silent #protect #spoiler #pin #nofooter`.

## Tests
`python tests/run_tests.py` (uses in-memory stubs in `tests/stubs`, no network/MongoDB needed).
