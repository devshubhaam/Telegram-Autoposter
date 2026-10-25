"""Telegram Autoposter — entry point.  Run:  python app.py"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from datetime import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from autoposter import __version__, handlers
from autoposter.config import SETTINGS, TZ, logger
from autoposter.database import Database
from autoposter.jobs import daily_job, housekeeping_tick, scheduler_tick
from autoposter.state import STATE

COMMANDS = [
    BotCommand("start", "Open the dashboard"),
    BotCommand("upload", "Upload posts to the selected target"),
    BotCommand("done", "Finish upload mode"),
    BotCommand("stats", "Statistics"),
    BotCommand("status", "Health & settings"),
    BotCommand("help", "How to use the bot"),
    BotCommand("cancel", "Cancel the current action"),
]


async def on_startup(application: Application) -> None:
    STATE.db = Database(SETTINGS)  # created here so motor binds to the running event loop
    last_error: Exception | None = None
    for attempt in range(1, 6):
        try:
            await STATE.db.init()
            break
        except Exception as exc:  # noqa: BLE001 - MongoDB may still be waking up
            last_error = exc
            logger.warning("MongoDB not ready (attempt %s/5): %s", attempt, exc)
            await asyncio.sleep(3 * attempt)
    else:
        raise RuntimeError(f"Could not connect to MongoDB: {last_error}") from last_error

    me = await application.bot.get_me()
    STATE.bot_username = me.username or ""
    try:
        await application.bot.set_my_commands(COMMANDS)
    except Exception as exc:  # noqa: BLE001 - cosmetic only
        logger.warning("set_my_commands failed: %s", exc)

    jq = application.job_queue
    job_kwargs = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 60}
    jq.run_repeating(scheduler_tick, interval=SETTINGS.scan_interval_seconds, first=5, name="scheduler_tick", job_kwargs=job_kwargs)
    jq.run_repeating(housekeeping_tick, interval=60, first=20, name="housekeeping", job_kwargs=job_kwargs)
    jq.run_daily(daily_job, time=time(3, 0, tzinfo=TZ), name="daily", job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 3600})
    await STATE.db.log("info", "startup_complete", bot_username=STATE.bot_username, version=__version__)


async def on_shutdown(application: Application) -> None:
    if STATE.db is not None:
        try:
            await STATE.db.log("info", "shutdown_complete")
        finally:
            STATE.db.client.close()


class _HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps({"ok": True, "version": __version__, "uptime": STATE.uptime_seconds}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - silence access logs
        pass


def start_health_server() -> None:
    port = int(os.getenv("PORT", "8080"))
    try:
        server = HTTPServer(("0.0.0.0", port), _HealthCheckHandler)
    except OSError as exc:
        logger.warning("health server not started on port %s: %s", port, exc)
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("health_check_server_started port=%s", port)


def build_application() -> Application:
    app = (
        Application.builder()
        .token(SETTINGS.bot_token)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .connect_timeout(15)
        .read_timeout(30)
        .write_timeout(60)
        .pool_timeout(15)
        .get_updates_read_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", handlers.start_cmd))
    app.add_handler(CommandHandler("help", handlers.help_cmd))
    app.add_handler(CommandHandler("stats", handlers.stats_cmd))
    app.add_handler(CommandHandler("status", handlers.status_cmd))
    app.add_handler(CommandHandler("upload", handlers.upload_cmd))
    app.add_handler(CommandHandler("done", handlers.done_cmd))
    app.add_handler(CommandHandler("register", handlers.register_current_chat))
    app.add_handler(CommandHandler("cancel", handlers.cancel_cmd))
    app.add_handler(CallbackQueryHandler(handlers.handle_reaction, pattern=r"^react:\d+$"))
    app.add_handler(CallbackQueryHandler(handlers.handle_callback))
    app.add_handler(ChatMemberHandler(handlers.my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatJoinRequestHandler(handlers.handle_join_request))
    # Only brand-new private messages; edited messages must not be re-saved as posts.
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE & ~filters.COMMAND, handlers.private_admin_message))
    app.add_handler(MessageHandler(~filters.ChatType.PRIVATE & ~filters.COMMAND & (filters.UpdateType.MESSAGE | filters.UpdateType.CHANNEL_POST), handlers.any_chat_message))
    app.add_error_handler(handlers.error_handler)
    return app


def main() -> None:
    logger.info("boot: starting v%s (python %s)", __version__, sys.version.split()[0])
    try:
        start_health_server()
        build_application().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
    except BaseException:
        logger.exception("FATAL: bot crashed during startup/run")
        sys.stdout.flush()
        sys.stderr.flush()
        raise


if __name__ == "__main__":
    main()
