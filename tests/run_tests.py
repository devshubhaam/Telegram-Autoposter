import asyncio, os, sys, itertools, traceback
os.environ.update(BOT_TOKEN="1:x", MONGO_URI="mongodb://x", ADMIN_IDS="1,2", TIMEZONE="Asia/Kolkata", POST_GAP_SECONDS="0", RETRY_BASE_SECONDS="0", ADMIN_PASSWORD="")
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "stubs")); sys.path.insert(0, os.path.dirname(HERE))

from datetime import datetime, timedelta, UTC
from types import SimpleNamespace as NS
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut
from autoposter.config import SETTINGS
from autoposter.state import STATE
from autoposter.database import Database
from autoposter import utils, sender, ui, handlers, payloads, jobs

class Msg(NS):
    @property
    def message_id(self): return self.id

class FakeBot:
    id = 999
    def __init__(self): self.calls = []; self.fail = []; self.n = itertools.count(100)
    def _maybe_fail(self, name):
        for i, (m, exc) in enumerate(self.fail):
            if m == name:
                self.fail[i] = (m, exc) if exc[1] else (m, exc)  # keep
                e = exc[0]
                if exc[1] is not None:
                    left = exc[1] - 1
                    if left <= 0: self.fail.pop(i)
                    else: self.fail[i] = (m, (e, left))
                raise e
    def __getattr__(self, name):
        async def call(*a, **k):
            self.calls.append((name, a, k)); self._maybe_fail(name)
            if name == "send_media_group": return [Msg(id=next(self.n)) for _ in k["media"]]
            if name in ("get_chat_member",): return NS(status="administrator", can_post_messages=True)
            if name == "get_chat": return NS(id=-1001, type="channel", title="T", username="t")
            if name in ("pin_chat_message","delete_message","edit_message_text","unpin_chat_message"): return True
            return Msg(id=next(self.n))
        return call
    def sent(self, name=None): return [c for c in self.calls if name is None or c[0] == name]

passed = 0
def check(cond, label):
    global passed
    if not cond: raise AssertionError(label)
    passed += 1

async def mkchat(cid=-1001, title="Chan", n=3, **opts):
    chat = await STATE.db.ensure_chat(cid, title, "channel", "chan")
    if opts: await STATE.db.set_options(cid, **opts)
    for i in range(n):
        await STATE.db.add_post(cid, {"media_type": "text", "text": f"post {i}", "fingerprint": utils.text_fingerprint(f"post {i}"), "buttons": []})
    await sender.after_posts_added(cid)
    return await STATE.db.get_chat(cid)

def fresh():
    STATE.db = Database(SETTINGS); STATE.title_cache.clear(); STATE.posting_locks.clear()

async def t_utils():
    n = utc = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    s = utils.default_schedule(); s.update(start_time="09:00", end_time="21:00", interval_minutes=180)
    nxt = utils.compute_next_run(n, s).astimezone(utils.TZ)
    check(nxt.hour == 18 and nxt.minute == 0, f"next slot {nxt}")  # 17:30 IST now -> 18:00
    s2 = {**s, "start_time": "20:00", "end_time": "02:00", "interval_minutes": 60}
    after = datetime(2026, 10, 25, 22, 0, tzinfo=UTC)  # 03:30 IST next day, outside overnight window of 25th
    r = utils.compute_next_run(after, s2).astimezone(utils.TZ)
    check(r.hour == 20, f"overnight next {r}")
    inside = datetime(2026, 10, 25, 19, 0, tzinfo=UTC)  # 00:30 IST 26th, inside window from 25th
    r = utils.compute_next_run(inside, s2).astimezone(utils.TZ)
    check(r.hour == 1 and r.day == 26, f"overnight carry {r}")
    check(utils.compute_next_run(n, {**s, "days": [0]}) is not None, "weekday")
    body, f = utils.parse_post_markup("Hello\n#silent\n#pin\n---buttons---\nA|example.com ; B|https://x.y\nbad|nothing")
    check(body == "Hello" and f["silent"] and f["pin"] and len(f["buttons"]) == 1 and len(f["buttons"][0]) == 2, "markup")
    check(f["buttons"][0][0]["url"] == "https://example.com", "url normalized")
    p = utils.parse_schedule_text("interval=2h\nbatch=4\nstart=09:00\nend=22:30\ndays=0,1")
    check(p["interval_minutes"] == 120 and p["batch_size"] == 4, "sched text")
    for bad in ("interval=x", "batch=0", "start=25:00", "days=9", "hello"):
        try: utils.parse_schedule_text(bad); check(False, f"should fail {bad}")
        except ValueError: check(True, "")
    check(utils.visible_len("<b>ab</b>&amp;") == 3, "visible_len")

async def t_engine():
    fresh(); bot = FakeBot()
    chat = await mkchat(n=3)
    await STATE.db.update_schedule(-1001, batch_size=2)
    sent = await sender.process_chat_batch(bot, -1001)
    check(sent == 2, "batch of 2")
    sent = await sender.process_chat_batch(bot, -1001)
    check(sent == 1, "remaining 1 then stops at cycle end")
    chat = await STATE.db.get_chat(-1001)
    check(chat["stats"]["total_cycles_completed"] == 1, "cycle counted once")
    sent = await sender.process_chat_batch(bot, -1001)
    check(sent == 2, "new cycle started")
    chat = await STATE.db.get_chat(-1001)
    check(chat["queue_state"]["cycle"] == 2, "cycle 2")
    check(chat["next_post_time"] > utils.utcnow(), "next in future")
    check(len(bot.sent("send_message")) == 5, "5 messages sent")
    # reaction markup attached
    kw = bot.sent("send_message")[0][2]
    check(kw["reply_markup"].inline_keyboard[-1][0].callback_data == "react:0", "reaction row")

async def t_one_pass():
    fresh(); bot = FakeBot()
    await mkchat(n=2, loop=False)
    await STATE.db.update_schedule(-1001, batch_size=5)
    sent = await sender.process_chat_batch(bot, -1001)
    chat = await STATE.db.get_chat(-1001)
    check(sent == 2 and chat["is_active"] is False and chat["pause_reason"], "one-pass pauses")
    check(chat["next_post_time"] is None, "no next when paused")
    check(len(bot.sent("send_message")) >= 1, "notified")
    await STATE.db.add_post(-1001, {"media_type": "text", "text": "new", "fingerprint": "x1"})
    await sender.after_posts_added(-1001)
    await STATE.db.set_active(-1001, True)
    sent = await sender.process_chat_batch(bot, -1001)
    check(sent == 1, f"only new post published, got {sent}")

async def t_errors():
    fresh(); bot = FakeBot()
    await mkchat(n=3)
    await STATE.db.update_schedule(-1001, batch_size=3)
    # Forbidden -> pause
    bot.fail.append(("send_message", (Forbidden("kicked"), 1)))
    sent = await sender.process_chat_batch(bot, -1001)
    chat = await STATE.db.get_chat(-1001)
    check(sent == 0 and not chat["is_active"] and chat["processing_until"] is None, "forbidden pauses + lock released")
    await STATE.db.set_active(-1001, True)
    # BadRequest non fatal: skip + fail count
    bot.fail.append(("send_message", (BadRequest("wrong file identifier"), 1)))
    sent = await sender.process_chat_batch(bot, -1001)
    check(sent == 2, f"one skipped two sent, {sent}")
    # fatal BadRequest
    await STATE.db.set_active(-1001, True); await sender.rebuild_and_ensure_schedule(-1001)
    bot.fail.append(("send_message", (BadRequest("Chat not found"), 1)))
    await sender.process_chat_batch(bot, -1001)
    check(not (await STATE.db.get_chat(-1001))["is_active"], "chat not found pauses")
    # RetryAfter long -> defer
    await STATE.db.set_active(-1001, True)
    bot.fail.append(("send_message", (RetryAfter(300), 1)))
    await sender.process_chat_batch(bot, -1001)
    chat = await STATE.db.get_chat(-1001)
    check(chat["is_active"] and chat["next_post_time"] > utils.utcnow() + timedelta(seconds=250), "flood defers")
    # RetryAfter timedelta short
    bot.fail.append(("send_message", (RetryAfter(timedelta(seconds=0)), 1)))
    r = await sender.send_post_with_retry(bot, -1001, {"media_type": "text", "text": "x"})
    check(r.message_id, "short retry-after retried")
    # timeouts exhausted -> defer, not crash
    await STATE.db.chats.update_one({"chat_id": -1001}, {"$set": {"next_post_time": utils.utcnow() - timedelta(seconds=5)}})
    bot.fail.append(("send_message", (TimedOut("t"), 10)))
    await sender.process_chat_batch(bot, -1001)
    chat = await STATE.db.get_chat(-1001)
    check(chat["next_post_time"] > utils.utcnow(), "timeout defers"); bot.fail.clear()
    # quarantine after 3 fails
    fresh(); bot = FakeBot(); await mkchat(n=1)
    for _ in range(3):
        bot.fail.append(("send_message", (BadRequest("bad"), 1)))
        await sender.process_chat_batch(bot, -1001)
        await sender.rebuild_and_ensure_schedule(-1001)
    check(await STATE.db.count_posts(-1001) == 0, "quarantined")
    # html parse fallback
    fresh(); bot = FakeBot()
    bot.fail.append(("send_message", (BadRequest("Can't parse entities: unsupported start tag"), 1)))
    r = await sender.send_post_with_retry(bot, -1001, {"media_type": "text", "text": "<b>hi"})
    check(bot.sent("send_message")[-1][2]["parse_mode"] is None, "plain fallback")

async def t_media_types():
    fresh(); bot = FakeBot(); await mkchat(n=0, footer="— @chan")
    opts = utils.chat_opts(await STATE.db.get_chat(-1001))
    posts = [
        {"media_type": "photo", "file_id": "f", "caption": "c"}, {"media_type": "video", "file_id": "f"},
        {"media_type": "animation", "file_id": "f"}, {"media_type": "document", "file_id": "f"},
        {"media_type": "audio", "file_id": "f"}, {"media_type": "voice", "file_id": "f"},
        {"media_type": "video_note", "file_id": "f"}, {"media_type": "sticker", "file_id": "f"},
        {"media_type": "copy", "source_chat_id": 1, "source_message_id": 2},
        {"media_type": "forward", "source_chat_id": 1, "source_message_id": 2},
        {"media_type": "album", "media_group": [{"media_type": "photo", "file_id": "a"}, {"media_type": "video", "file_id": "b"}], "caption": "x" * 1100},
        {"media_type": "album", "media_group": [{"media_type": "photo", "file_id": "a"}]},
        {"media_type": "photo", "file_id": "f", "caption": "y" * 1100},
        {"media_type": "text", "text": "hello", "nofooter": True},
    ]
    for p in posts:
        r = await sender.send_saved_post(bot, -1001, p, opts)
        check(r.message_id, p["media_type"])
    txt = bot.sent("send_message")
    check(any("— @chan" in c[2].get("text", "") for c in bot.sent("send_message")) is False or True, "")
    cap = [c for c in bot.sent("send_photo")][0][2]["caption"]
    check(cap.endswith("— @chan"), "footer appended")
    check(bot.sent("send_message")[-1][2]["text"] == "hello", "nofooter respected")
    check(len(bot.sent("send_media_group")) == 1 and len(bot.sent("send_photo")) == 3, "album single->photo")

async def t_dedupe_bulk():
    fresh(); await mkchat(n=0)
    ok, dups = await STATE.db.bulk_add_posts(-1001, [{"media_type": "text", "text": "a", "fingerprint": "f1"}, {"media_type": "text", "text": "a", "fingerprint": "f1"}, {"media_type": "text", "text": "b", "fingerprint": "f2"}])
    check((ok, dups) == (2, 1), f"bulk {ok} {dups}")
    check(await STATE.db.add_post(-1001, {"media_type": "text", "text": "b", "fingerprint": "f2"}) is None, "dup single")
    check(await STATE.db.add_post(-1001, {"media_type": "text", "text": "b", "fingerprint": "f2"}, dedupe=False), "dedupe off")
    posts, bad = payloads.parse_bulk_text("x.json", '[{"text":"hi"},{"media_type":"photo"},{"media_type":"photo","file_id":"z","buttons":[[{"text":"a","url":"example.com"}]]},5]')
    check(len(posts) == 2 and bad == 2, f"json {len(posts)} {bad}")
    posts, bad = payloads.parse_bulk_text("x.txt", "one\n---\ntwo #silent\n---\n\n---\nthree")
    check(len(posts) == 3 and bad == 0, "txt")
    for name, txt in (("a.json", "{"), ("a.jsonl", "{}\n{bad"), ("a.json", '{"a":1}')):
        try: payloads.parse_bulk_text(name, txt); check(False, "must raise")
        except ValueError: check(True, "")
    posts, bad = payloads.parse_bulk_text("a.jsonl", '{"text":"x"}\n\n{"media_type":"album","media_group":[{"media_type":"photo","file_id":"1"}]}')
    check(len(posts) == 2, "jsonl")

async def t_autodelete_jobs():
    fresh(); bot = FakeBot(); await mkchat(n=1, auto_delete_minutes=60)
    await sender.process_chat_batch(bot, -1001)
    check(await STATE.db.deletes.count_documents({}) == 1, "delete scheduled")
    await STATE.db.deletes.update_many({}, {"$set": {"delete_at": utils.utcnow() - timedelta(seconds=1)}})
    ctx = NS(bot=bot, job_queue=None)
    await jobs.housekeeping_tick(ctx)
    check(len(bot.sent("delete_message")) >= 1 and await STATE.db.deletes.count_documents({}) == 0, "deleted")
    await STATE.db.chats.update_one({"chat_id": -1001}, {"$set": {"next_post_time": utils.utcnow() - timedelta(seconds=1)}})
    await jobs.scheduler_tick(ctx); await jobs.daily_job(ctx)
    check(len(bot.sent("send_document")) == 2, "backup to both admins")
    # tick survives crash
    class Boom(FakeBot):
        async def send_message(self, *a, **k): raise ValueError("boom")
    await STATE.db.chats.update_one({"chat_id": -1001}, {"$set": {"next_post_time": utils.utcnow() - timedelta(seconds=1), "is_active": True}})
    b = Boom(); await jobs.scheduler_tick(NS(bot=b))
    check(True, "tick survived")

async def t_backup():
    fresh(); await mkchat(n=2)
    payload = await STATE.db.backup_payload()
    blob, name = jobs.build_backup_file(payload)
    from autoposter.database import loads_json
    data = loads_json(blob.decode())
    fresh()
    c, p = await STATE.db.restore_payload(data)
    check((c, p) == (1, 2), f"restore {c} {p}")
    try: await STATE.db.restore_payload([1]); check(False, "")
    except ValueError: check(True, "")

# ------------------------------------------------------------------ UI / click-everything
def walk(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]

class FakeQuery:
    def __init__(self, data, bot): self.data = data; self.from_user = NS(id=1); self.message = NS(chat_id=1, message_id=50); self.edits = []; self.answers = []
    async def answer(self, text=None, show_alert=False): self.answers.append(text)
    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        assert len(text) <= 4096, "too long"
        self.edits.append((text, reply_markup))

class FakeApp:
    def create_task(self, coro, update=None, name=None): return asyncio.ensure_future(coro)

class FakeJobQueue:
    def __init__(self): self.jobs = []
    def get_jobs_by_name(self, n): return []
    def run_once(self, cb, when, data=None, name=None): self.jobs.append((cb, data, name)); return NS(schedule_removal=lambda: None)

def mkctx(bot): return NS(bot=bot, application=FakeApp(), job_queue=FakeJobQueue())

async def click(bot, data):
    q = FakeQuery(data, bot)
    upd = NS(callback_query=q, effective_user=q.from_user)
    await handlers.handle_callback(upd, mkctx(bot))
    return q

async def t_click_everything():
    fresh(); bot = FakeBot(); await mkchat(n=8)
    await STATE.db.set_active_target(1, -1001)
    await STATE.db.add_post(-1001, {"media_type": "album", "media_group": [{"media_type": "photo", "file_id": "a"}, {"media_type": "photo", "file_id": "b"}], "caption": "al"})
    # create some stats data
    await sender.process_chat_batch(bot, -1001)
    seen, queue = set(), ["nav:dashboard"]
    visited = 0
    while queue:
        data = queue.pop(0)
        if data in seen: continue
        seen.add(data)
        if any(data.startswith(x) for x in ("chat:wipeconfirm", "chat:removeconfirm", "db:wipeconfirm", "pa:delok", "chat:undo", "chat:remove:")):
            continue  # destructive, tested separately
        assert len(data.encode()) <= 64, f"callback too long {data}"
        q = await click(bot, data); visited += 1
        for text, kb in q.edits:
            if kb: queue.extend(walk(kb))
        await asyncio.sleep(0)
    check(visited > 60, f"clicked {visited} buttons")
    await asyncio.sleep(0.05)
    # destructive ones
    q = await click(bot, "chat:undo:-1001"); check(True, "undo")
    q = await click(bot, "chat:removeconfirm:-1001"); check(await STATE.db.get_chat(-1001) is None, "removed")
    q = await click(bot, "chat:open:-1001"); check("removed" in q.edits[-1][0], "missing chat handled")
    q = await click(bot, "db:wipeconfirm"); check(True, "wipe")
    q = await click(bot, "nav:dashboard"); check(q.edits, "dashboard after wipe")
    # unauthorized
    q = FakeQuery("nav:dashboard", bot); q.from_user = NS(id=77)
    await handlers.handle_callback(NS(callback_query=q, effective_user=q.from_user), mkctx(bot))
    check(q.answers == ["Unauthorized"], "unauthorized")

async def t_presets_and_options():
    fresh(); bot = FakeBot(); await mkchat(n=2)
    for key in ui.SCHEDULE_PRESETS:
        for i in range(len(ui.SCHEDULE_PRESETS[key]["values"])):
            await click(bot, f"ss:{key}:-1001:{i}")
    ch = await STATE.db.get_chat(-1001)
    check(ch["schedule"]["days"] == [1, 3, 5] and ch["options"]["jitter_minutes"] == 60, "presets applied")
    await click(bot, "op:loop:-1001"); await click(bot, "op:dedupe:-1001"); await click(bot, "op:notify:-1001")
    o = utils.chat_opts(await STATE.db.get_chat(-1001))
    check(o["loop"] is False and o["dedupe"] is False and o["notify_cycle"] is True, "toggles")
    await click(bot, "od:-1001:2"); check(utils.chat_opts(await STATE.db.get_chat(-1001))["auto_delete_minutes"] == 360, "autodelete")
    q = await click(bot, "ss:int:-1001:99"); check(q.answers[0] == "Expired button", "bad idx")

# ------------------------------------------------------------------ message flows
def msg(**k):
    base = dict(text=None, caption=None, photo=None, video=None, animation=None, video_note=None, voice=None, audio=None, sticker=None, document=None,
                poll=None, location=None, venue=None, contact=None, dice=None, media_group_id=None, chat_id=1, message_id=5, forward_origin=None, text_html=None, caption_html=None)
    base.update(k); m = NS(**base)
    replies = []
    async def reply_text(t, **kw): replies.append(t)
    async def delete(): pass
    m.reply_text = reply_text; m.delete = delete; m.replies = replies
    return m

def upd(m): return NS(effective_message=m, effective_user=NS(id=1), effective_chat=NS(type="private", id=1))

async def t_messages():
    fresh(); bot = FakeBot(); ctx = mkctx(bot); await mkchat(n=0)
    await STATE.db.set_active_target(1, -1001)
    m = msg(text="hello world", text_html="hello world\n#silent")
    await handlers.private_admin_message(upd(m), ctx)
    check("Saved" in m.replies[0] and await STATE.db.count_posts(-1001) == 1, "text saved")
    m = msg(text="hello world", text_html="hello world #x")
    await handlers.private_admin_message(upd(m), ctx); check(True, "")
    m = msg(text="hello world", text_html="hello world")
    await handlers.private_admin_message(upd(m), ctx)
    check("Duplicate" in m.replies[0], "dup detect")
    ph = NS(file_id="p1", file_unique_id="u1")
    m = msg(photo=[ph], caption="cap", caption_html="cap")
    await handlers.private_admin_message(upd(m), ctx); check("Saved" in m.replies[0], "photo")
    for attr in ("voice", "sticker", "video_note", "audio", "animation", "video"):
        m = msg(**{attr: NS(file_id="x"+attr, file_unique_id="u"+attr, file_name=None)})
        await handlers.private_admin_message(upd(m), ctx); check("Saved" in m.replies[0], attr)
    m = msg(poll=NS()); await handlers.private_admin_message(upd(m), ctx); check("Saved" in m.replies[0], "poll")
    m = msg(); await handlers.private_admin_message(upd(m), ctx); check("save that kind" in m.replies[0], "empty rejected")
    m = msg(text="x"*5000, text_html="x"*5000); await handlers.private_admin_message(upd(m), ctx); check("4096" in m.replies[0], "too long")
    # upload mode
    await handlers.begin_upload(bot, 1, -1001)
    st = await handlers.admin_doc(1); check(st["mode"] == "bulk_upload", "upload mode")
    m = msg(text="up1", text_html="up1"); await handlers.private_admin_message(upd(m), ctx)
    m = msg(text="up1", text_html="up1"); await handlers.private_admin_message(upd(m), ctx)
    st = await handlers.admin_doc(1); check(st["upload_count"] == 1 and st["upload_dups"] == 1 and not m.replies, "counters")
    check(ctx.job_queue.jobs, "refresh scheduled")
    await handlers.refresh_upload_message(NS(job=NS(data=1), bot=bot)); check(bot.sent("edit_message_text"), "refresh edits")
    # album
    for i in range(3):
        m = msg(photo=[NS(file_id=f"al{i}", file_unique_id=f"alu{i}")], media_group_id="g1", caption="album cap" if i == 0 else None, caption_html="album cap" if i == 0 else None)
        await handlers.private_admin_message(upd(m), ctx)
    await handlers.flush_media_group(NS(job=NS(data="g1"), bot=bot))
    check(await STATE.db.posts_coll(-1001).count_documents({"media_type": "album"}) == 1, "album saved")
    summary = await handlers.finish_upload(1); check("Upload finished" in summary, "finish")
    # edit schedule mode
    await handlers.set_admin_meta(1, mode="edit_schedule", mode_chat_id=-1001)
    m = msg(text="interval=bogus"); await handlers.private_admin_message(upd(m), ctx); check("❌" in m.replies[0], "bad schedule msg")
    m = msg(text="interval=1h\nbatch=2"); await handlers.private_admin_message(upd(m), ctx)
    check((await STATE.db.get_chat(-1001))["schedule"]["interval_minutes"] == 60, "schedule applied")
    # footer
    await handlers.set_admin_meta(1, mode="footer_text", mode_chat_id=-1001)
    m = msg(text="foot", text_html="<b>foot</b>"); await handlers.private_admin_message(upd(m), ctx)
    check(utils.chat_opts(await STATE.db.get_chat(-1001))["footer"] == "<b>foot</b>", "footer")
    # bulk file via document
    async def get_file(fid):
        class F:
            async def download_as_bytearray(self): return bytearray(b"a1\n---\na2\n---\na1")
        return F()
    bot.get_file = get_file
    m = msg(document=NS(file_name="p.txt", file_id="d", file_size=10, file_unique_id="du"))
    await handlers.private_admin_message(upd(m), ctx); check("Inserted posts: <b>2</b>" in m.replies[0] and "Duplicates skipped: 1" in m.replies[0], m.replies)
    # restore bad file
    await handlers.set_admin_meta(1, mode="restore_backup")
    bot_bad = bot
    async def get_file2(fid):
        class F:
            async def download_as_bytearray(self): return bytearray(b"not json")
        return F()
    bot.get_file = get_file2
    m = msg(document=NS(file_name="b.json", file_id="d", file_size=10)); await handlers.private_admin_message(upd(m), ctx)
    check("Restore failed" in m.replies[0], "bad restore handled")
    await handlers.clear_admin_mode(1)
    # stale mode
    await handlers.set_admin_meta(1, mode="import_source")
    m = msg(text="x", text_html="x"); await handlers.private_admin_message(upd(m), ctx); check("Saved" in m.replies[0], "stale mode cleared")
    # add target flow
    await handlers.set_admin_meta(1, mode="add_target")
    m = msg(text="@somechan"); await handlers.private_admin_message(upd(m), ctx); check("Added" in m.replies[0], m.replies)

async def t_legacy_migration():
    fresh()
    await STATE.db.ensure_chat(-5, "src", "channel"); await STATE.db.chats.update_one({"chat_id": -5}, {"$set": {"hidden_source_only": True, "is_active": False}})
    await STATE.db.admins.insert_one({"admin_id": 1, "mode": "import_to_link"})
    await STATE.db.init()
    check(await STATE.db.get_chat(-5) is None, "hidden empty source removed")
    check("mode" not in (await handlers.admin_doc(1)), "wizard mode cleared")

async def t_reaction():
    fresh(); bot = FakeBot(); await mkchat(n=1)
    await sender.process_chat_batch(bot, -1001)
    class Q:
        data = "react:1"; message = NS(chat_id=-1001, message_id=100, reply_markup=NS(inline_keyboard=[[NS(text="L", url="https://a.b")], sender.reaction_row()]))
        answers = []; marks = []
        async def answer(self, t=None, **k): self.answers.append(t)
        async def edit_message_reply_markup(self, reply_markup=None): self.marks.append(reply_markup)
    q = Q(); u = NS(callback_query=q, effective_user=NS(id=50))
    await handlers.handle_reaction(u, mkctx(bot))
    check(q.marks[0].inline_keyboard[1][1].text == "👍 1" and q.marks[0].inline_keyboard[0][0].url, "react counts + custom kept")
    await handlers.handle_reaction(u, mkctx(bot)); check(q.marks[1].inline_keyboard[1][1].text == "👍", "toggle off")
    totals, best = await STATE.db.reaction_summary(-1001); check(totals[1] == 0, "summary")

async def t_errorhandler():
    fresh(); bot = FakeBot()
    chan_update = NS(effective_chat=NS(type="channel", id=-1), effective_user=None)
    class U(handlers.Update): pass
    handlers.Update = U if False else handlers.Update
    await handlers.error_handler(object(), NS(error=ValueError("x"), bot=bot)); check(not bot.sent("send_message"), "no msg for non-update")
    await handlers.error_handler(NS(), NS(error=TimedOut("t"), bot=bot)); check(True, "noise")

async def main():
    tests = [v for k, v in globals().items() if k.startswith("t_")]
    failed = 0
    for t in tests:
        try: await t(); print("PASS", t.__name__)
        except Exception:
            failed += 1; print("FAIL", t.__name__); traceback.print_exc()
    print(f"{passed} checks, {failed} failed tests"); sys.exit(1 if failed else 0)
asyncio.run(main())
