# main.py
# -*- coding: utf-8 -*-

import os
import re
import asyncio
from secrets import token_urlsafe
from urllib.parse import quote as urlquote
from datetime import datetime, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputTextMessageContent,
    InlineQueryResultArticle,
)
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    CommandHandler,
    InlineQueryHandler,
    ChosenInlineResultHandler,
    ChatMemberHandler,
    filters,
)
import asyncpg

# --------- تنظیمات از محیط ---------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# سقف نصب در گروه‌ها
MAX_GROUPS = int(os.environ.get("MAX_GROUPS", "100"))
SUPPORT_CONTACT = os.environ.get("SUPPORT_CONTACT", "soulsownerbot")  # بدون @

# کانال‌های اجباری (دوگانه)
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "SLSHEXED")
CHANNEL_USERNAME_2 = os.environ.get("CHANNEL_USERNAME_2", "dr_gooshad")

def _norm(ch: str) -> str:
    return ch.replace("@", "").strip()

MANDATORY_CHANNELS = []
if _norm(CHANNEL_USERNAME):
    MANDATORY_CHANNELS.append(_norm(CHANNEL_USERNAME))
if _norm(CHANNEL_USERNAME_2) and _norm(CHANNEL_USERNAME_2).lower() != _norm(CHANNEL_USERNAME).lower():
    MANDATORY_CHANNELS.append(_norm(CHANNEL_USERNAME_2))

# ---------- ثوابت ----------
TRIGGERS = {"نجوا", "درگوشی", "سکرت"}
GUIDE_DELETE_AFTER_SEC = 180
ALERT_SNIPPET = 190

# تاریخ خیلی دور برای «بدون انقضا»
FAR_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)

broadcast_wait_for_banner = set()

# ---------- ابزارک‌های عمومی ----------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def sanitize(name: str) -> str:
    return (name or "کاربر").replace("<", "").replace(">", "")

def mention_html(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{sanitize(name)}</a>'

def group_link_title(title: str) -> str:
    return sanitize(title or "گروه")

def avatar_url(label: str) -> str:
    # آواتار امن عمومی (بدون نیاز به توکن)
    return f"https://api.dicebear.com/7.x/initials/svg?seed={urlquote(label or 'user')}"

async def safe_delete(bot, chat_id: int, message_id: int, attempts: int = 3, delay: float = 0.6):
    for _ in range(attempts):
        try:
            await bot.delete_message(chat_id, message_id)
            return True
        except Exception:
            await asyncio.sleep(delay)
    return False

async def delete_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id, message_id = context.job.data
    try:
        await context.bot.delete_message(chat_id, message_id)
    except Exception:
        pass

# ---------- دیتابیس ----------
pool: asyncpg.Pool = None

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS users (
  user_id BIGINT PRIMARY KEY,
  username TEXT,
  first_name TEXT,
  last_seen TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS chats (
  chat_id BIGINT PRIMARY KEY,
  title TEXT,
  type TEXT,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  last_seen TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS whispers (
  id BIGSERIAL PRIMARY KEY,
  group_id BIGINT NOT NULL,
  sender_id BIGINT NOT NULL,
  receiver_id BIGINT NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'sent',  -- 'sent' | 'read'
  created_at TIMESTAMPTZ DEFAULT NOW(),
  message_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_whispers_group ON whispers(group_id);
CREATE INDEX IF NOT EXISTS idx_whispers_sr ON whispers(sender_id, receiver_id);

CREATE TABLE IF NOT EXISTS pending (
  sender_id BIGINT PRIMARY KEY,
  group_id BIGINT NOT NULL,
  receiver_id BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  guide_message_id INTEGER,
  reply_to_msg_id BIGINT
);

CREATE TABLE IF NOT EXISTS watchers (
  group_id BIGINT NOT NULL,
  watcher_id BIGINT NOT NULL,
  PRIMARY KEY (group_id, watcher_id)
);

CREATE TABLE IF NOT EXISTS iwhispers (
  token TEXT PRIMARY KEY,
  sender_id BIGINT NOT NULL,
  receiver_id BIGINT,
  receiver_username TEXT,
  text TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  expires_at TIMESTAMPTZ NOT NULL,
  reported BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS whisper_contacts (
  owner_id BIGINT NOT NULL,
  peer_key TEXT NOT NULL,
  peer_id BIGINT,
  peer_username TEXT,
  peer_name TEXT,
  last_used TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (owner_id, peer_key)
);
"""

ALTER_SQL = """
ALTER TABLE chats ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE pending ADD COLUMN IF NOT EXISTS guide_message_id INTEGER;
ALTER TABLE pending ADD COLUMN IF NOT EXISTS reply_to_msg_id BIGINT;
ALTER TABLE iwhispers ADD COLUMN IF NOT EXISTS receiver_id BIGINT;
ALTER TABLE iwhispers ADD COLUMN IF NOT EXISTS receiver_username TEXT;
ALTER TABLE iwhispers ADD COLUMN IF NOT EXISTS reported BOOLEAN NOT NULL DEFAULT FALSE;
"""

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as con:
        await con.execute(CREATE_SQL)
        await con.execute(ALTER_SQL)

async def upsert_user(u):
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO users (user_id, username, first_name, last_seen)
               VALUES ($1,$2,$3,NOW())
               ON CONFLICT (user_id) DO UPDATE SET
                 username=EXCLUDED.username, first_name=EXCLUDED.first_name, last_seen=NOW();""",
            u.id, u.username, u.first_name or u.full_name
        )

async def upsert_chat(c, active: bool = True):
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO chats (chat_id, title, type, is_active, last_seen)
               VALUES ($1,$2,$3,$4,NOW())
               ON CONFLICT (chat_id) DO UPDATE SET
                 title=EXCLUDED.title, type=EXCLUDED.type, is_active=$4, last_seen=NOW();""",
            c.id, getattr(c, "title", None), c.type, active
        )

async def mark_chat_active(chat_id: int, active: bool):
    async with pool.acquire() as con:
        await con.execute("UPDATE chats SET is_active=$1, last_seen=NOW() WHERE chat_id=$2;", active, chat_id)

async def get_active_group_count() -> int:
    async with pool.acquire() as con:
        return await con.fetchval("SELECT COUNT(*) FROM chats WHERE type IN ('group','supergroup') AND is_active=TRUE;")

async def get_name_for(user_id: int, fallback: str = "کاربر") -> str:
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT COALESCE(NULLIF(first_name,''), NULLIF(username,'')) AS n FROM users WHERE user_id=$1;",
            user_id
        )
    if row and row["n"]:
        return str(row["n"])
    try:
        return sanitize((await app.bot.get_chat(user_id)).first_name)  # type: ignore
    except Exception:
        return sanitize(fallback)

async def get_username_for(user_id: int) -> str:
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT username FROM users WHERE user_id=$1;", user_id)
    if row and row["username"]:
        return str(row["username"]).lstrip("@")
    try:
        ch = await app.bot.get_chat(user_id)  # type: ignore
        if getattr(ch, "username", None):
            return ch.username.lstrip("@")
    except Exception:
        pass
    return ""

async def try_resolve_user_id_by_username(context: ContextTypes.DEFAULT_TYPE, username: str):
    if not username:
        return None
    try:
        ch = await context.bot.get_chat(f"@{username}")
        return int(getattr(ch, "id", 0)) or None
    except Exception:
        return None

async def upsert_contact(owner_id: int, peer_id: int | None, peer_username: str | None, peer_name: str | None):
    if not peer_id and not peer_username:
        return
    key = f"@{peer_username.lower()}" if peer_username else f"id:{peer_id}"
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO whisper_contacts(owner_id, peer_key, peer_id, peer_username, peer_name, last_used)
               VALUES ($1,$2,$3,$4,$5,NOW())
               ON CONFLICT (owner_id, peer_key) DO UPDATE SET
                 peer_id=COALESCE(EXCLUDED.peer_id, whisper_contacts.peer_id),
                 peer_username=COALESCE(EXCLUDED.peer_username, whisper_contacts.peer_username),
                 peer_name=COALESCE(EXCLUDED.peer_name, whisper_contacts.peer_name),
                 last_used=NOW();""",
            owner_id, key, peer_id, (peer_username or None), (peer_name or None)
        )

async def get_recent_contacts(owner_id: int, limit: int = 8):
    async with pool.acquire() as con:
        rows = await con.fetch(
            "SELECT peer_id, peer_username, peer_name FROM whisper_contacts WHERE owner_id=$1 ORDER BY last_used DESC LIMIT $2;",
            owner_id, limit
        )
    return rows

# ---------- عضویت ----------
async def is_member_required_channel(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        for ch in MANDATORY_CHANNELS:
            m = await context.bot.get_chat_member(f"@{ch}", user_id)
            if getattr(m, "status", "") not in ("member", "administrator", "creator"):
                return False
        return True
    except Exception:
        return False

def _channels_text():
    return "، ".join([f"@{ch}" for ch in MANDATORY_CHANNELS])

def start_keyboard_pre():
    rows = [[InlineKeyboardButton("عضو شدم ✅", callback_data="checksub")]]
    if len(MANDATORY_CHANNELS) >= 1:
        rows.append([InlineKeyboardButton("عضویت در کانال یک", url=f"https://t.me/{MANDATORY_CHANNELS[0]}")])
    if len(MANDATORY_CHANNELS) >= 2:
        rows.append([InlineKeyboardButton("عضویت در کانال دو", url=f"https://t.me/{MANDATORY_CHANNELS[1]}")])
    rows.append([InlineKeyboardButton("افزودن ربات به گروه ➕", url="https://t.me/DareGushi_BOT?startgroup=true")])
    rows.append([InlineKeyboardButton("ارتباط با پشتیبان 👨🏻‍💻", url="https://t.me/SOULSOWNERBOT")])
    return InlineKeyboardMarkup(rows)

def start_keyboard_post():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("افزودن ربات به گروه ➕", url="https://t.me/DareGushi_BOT?startgroup=true")],
        [InlineKeyboardButton("ارتباط با پشتیبان 👨🏻‍💻", url="https://t.me/SOULSOWNERBOT")],
    ])

START_TEXT = (
    "سلام! 👋\n\n"
    "برای استفاده ابتدا عضو کانال(های) زیر شوید:\n"
    f"👉 {_channels_text()}\n\n"
    "بعد روی «عضو شدم ✅» بزنید."
)

INTRO_TEXT = (
    "به «درگوشی» خوش آمدید!\n\n"
    "در گروه‌ها روی پیام فرد هدف **Reply** کنید و یکی از کلمات «نجوا / درگوشی / سکرت» را بفرستید؛ "
    "سپس متن نجوا را در خصوصی ربات ارسال کنید (فقط متن).\n\n"
    "حالت اینلاین هم فعال است: داخل چت بنویسید `@Bot متن @username` یا فقط `@Bot` تا لیست مخاطبین اخیر بیاید."
)

# ---------- /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    await upsert_user(update.effective_user)

    ok = await is_member_required_channel(context, update.effective_user.id)
    if ok:
        await update.message.reply_text(INTRO_TEXT, reply_markup=start_keyboard_post())
        # اگر پندینگ فعال دارد، پیام انتظار بفرست
        async with pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT group_id, receiver_id FROM pending WHERE sender_id=$1 AND expires_at>NOW();",
                update.effective_user.id
            )
        if row:
            group_id = int(row["group_id"])
            receiver_id = int(row["receiver_id"])
            try:
                chatobj = await context.bot.get_chat(group_id)
                gtitle = group_link_title(getattr(chatobj, "title", "گروه"))
            except Exception:
                gtitle = "گروه"
            receiver_name = await get_name_for(receiver_id, "گیرنده")
            await update.message.reply_text(
                f"⌛️ در انتظارِ متنِ نجوای شما…\n"
                f"هدف: {mention_html(receiver_id, receiver_name)} در «{gtitle}»\n"
                f"لطفاً فقط متن را ارسال کنید.",
                parse_mode=ParseMode.HTML
            )
    else:
        await update.message.reply_text(START_TEXT, reply_markup=start_keyboard_pre())

async def on_checksub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    user = update.effective_user
    ok = await is_member_required_channel(context, user.id)
    if ok:
        await update.callback_query.answer("عضویت تایید شد ✅", show_alert=False)
        await update.callback_query.message.reply_text(INTRO_TEXT, reply_markup=start_keyboard_post())
    else:
        await update.callback_query.answer("هنوز عضویت تکمیل نیست. لطفاً عضو شوید و دوباره امتحان کنید.", show_alert=True)

# ---------- راهنمای متنی داخل گروه ----------
async def group_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    text = (
        "راهنمای سریع:\n"
        "• روی پیام شخصِ هدف «Reply» کرده و «نجوا / درگوشی / سکرت» بفرستید؛ سپس متن را در خصوصی ارسال کنید.\n"
        f"• حالت اینلاین: @{BOT_USERNAME or 'DareGushi_BOT'} <متن> @username  یا فقط @{BOT_USERNAME or 'DareGushi_BOT'} برای نمایش مخاطبین اخیر."
    )
    rows = [[InlineKeyboardButton("✍️ ارسال متن در خصوصی", url=f"https://t.me/{BOT_USERNAME or 'DareGushi_BOT'}?start=go")]]
    if len(MANDATORY_CHANNELS) >= 1:
        rows.append([InlineKeyboardButton("عضویت در کانال یک", url=f"https://t.me/{MANDATORY_CHANNELS[0]}")])
    if len(MANDATORY_CHANNELS) >= 2:
        rows.append([InlineKeyboardButton("عضویت در کانال دو", url=f"https://t.me/{MANDATORY_CHANNELS[1]}")])

    sent = await update.effective_message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(rows),
        disable_web_page_preview=True
    )
    context.job_queue.run_once(delete_job, when=GUIDE_DELETE_AFTER_SEC, data=(chat.id, sent.message_id))

# ---------- Inline Mode ----------
BOT_USERNAME: str = ""
INLINE_HELP = "فرمت: «@{bot} متن نجوا @username»\nمثال: @{bot} سلام @ali123".format

def _preview(s: str, n: int = 50) -> str:
    return s if len(s) <= n else (s[:n] + "…")

async def on_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iq = update.inline_query
    q = (iq.query or "").strip()
    user = iq.from_user

    # شرط عضویت
    if not await is_member_required_channel(context, user.id):
        await iq.answer(
            results=[
                InlineQueryResultArticle(
                    id="join",
                    title="🔒 برای ارسال نجوا عضو شوید",
                    description=_channels_text(),
                    input_message_content=InputTextMessageContent("برای ارسال نجوا ابتدا عضو کانال‌ها شوید."),
                )
            ],
            cache_time=1,
            is_personal=True,
            switch_pm_text="عضو شدم ✅",
            switch_pm_parameter="join"
        )
        return

    results = []

    # @username را در هر جای کوئری پیدا کن (آخرین مورد معتبر)
    uname_match = None
    for m in re.finditer(r"@([A-Za-z0-9_]{5,})", q):
        uname_match = m

    if uname_match:
        uname = uname_match.group(1).lower()
        # متن = کل کوئری به‌جز آخرین @username
        text = (q[:uname_match.start()] + q[uname_match.end():]).strip()

        rid = await try_resolve_user_id_by_username(context, uname)

        if rid:
            rname = await get_name_for(rid, "گیرنده")
            title = rname
            thumb = avatar_url(rname)
        else:
            title = f"@{uname}"
            thumb = avatar_url(uname)

        token = token_urlsafe(12)
        async with pool.acquire() as con:
            await con.execute(
                "INSERT INTO iwhispers(token, sender_id, receiver_id, receiver_username, text, expires_at, reported) VALUES ($1,$2,$3,$4,$5,$6,FALSE);",
                token, user.id, rid, uname, text, FAR_FUTURE
            )

        results.append(
            InlineQueryResultArticle(
                id=token,
                title=title,
                description=_preview(text) if text else "بدون متن",
                input_message_content=InputTextMessageContent(f"🔒 نجوا برای {title if rid else '@'+uname}"),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔒 نمایش پیام", callback_data=f"iws:{token}")]]),
                thumbnail_url=thumb,
                thumbnail_width=64,
                thumbnail_height=64,
            )
        )
    else:
        # اگر @username نیست → از مخاطبین اخیر پیشنهاد بده (حتی با q خالی)
        recents = await get_recent_contacts(user.id, limit=8)
        base_text = q
        for r in recents:
            rid = int(r["peer_id"]) if r["peer_id"] is not None else None
            run = (r["peer_username"] or "").lower() if r["peer_username"] else None
            pname = r["peer_name"] or (run and f"@{run}") or (rid and f"id:{rid}") or "کاربر"

            token = token_urlsafe(12)
            async with pool.acquire() as con:
                await con.execute(
                    "INSERT INTO iwhispers(token, sender_id, receiver_id, receiver_username, text, expires_at, reported) VALUES ($1,$2,$3,$4,$5,$6,FALSE);",
                    token, user.id, rid, run, base_text, FAR_FUTURE
                )

            results.append(
                InlineQueryResultArticle(
                    id=token,
                    title=pname,
                    description=_preview(base_text) if base_text else "بدون متن",
                    input_message_content=InputTextMessageContent(f"🔒 نجوا برای {pname}"),
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔒 نمایش پیام", callback_data=f"iws:{token}")]]),
                    thumbnail_url=avatar_url(pname),
                    thumbnail_width=64,
                    thumbnail_height=64,
                )
            )

    # اگر هیچ نتیجه‌ای نشد، راهنما بده تا باکس باز شود
    if not results:
        help_result = InlineQueryResultArticle(
            id="help",
            title="راهنما",
            description="متن بنویسید و هرجا @username را اضافه کنید (یا خالی بگذارید تا مخاطبین اخیر بیاید).",
            input_message_content=InputTextMessageContent(INLINE_HELP(BOT_USERNAME)),
            thumbnail_url=avatar_url("help"),
            thumbnail_width=64,
            thumbnail_height=64,
        )
        await iq.answer([help_result], cache_time=1, is_personal=True)
    else:
        await iq.answer(results, cache_time=0, is_personal=True)

# گزارش فوریِ «لحظهٔ ارسال اینلاین»
async def on_chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cir = update.chosen_inline_result
    token = cir.result_id
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT sender_id, receiver_id, receiver_username, text FROM iwhispers WHERE token=$1;",
            token
        )
    if not row:
        return
    sender_id = int(row["sender_id"])
    receiver_id = row["receiver_id"] and int(row["receiver_id"])
    receiver_username = row["receiver_username"]

    s_label = mention_html(sender_id, await get_name_for(sender_id, "کاربر"))
    if receiver_id:
        r_label = mention_html(receiver_id, await get_name_for(receiver_id, "کاربر"))
    else:
        r_label = f"@{receiver_username}" if receiver_username else "گیرنده"

    msg = f"📝 نجوای اینلاین: {s_label} ➜ {r_label} + {row['text']}"
    try:
        await context.bot.send_message(ADMIN_ID, msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        pass

async def on_inline_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    user = update.effective_user

    try:
        _, token = cq.data.split(":")
    except Exception:
        return

    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT token, sender_id, receiver_id, receiver_username, text, reported FROM iwhispers WHERE token=$1;",
            token
        )
    if not row:
        await cq.answer("این نجوا نامعتبر است.", show_alert=True)
        return

    sender_id = int(row["sender_id"])
    receiver_id = row["receiver_id"] and int(row["receiver_id"])
    recv_un = (row["receiver_username"] or "").lower() or None
    text = row["text"]
    already_reported = bool(row["reported"])

    allowed = (user.id == sender_id) or (receiver_id and user.id == receiver_id) or ((user.username or "").lower() == (recv_un or "")) or (user.id == ADMIN_ID)
    if not allowed:
        await cq.answer("این پیام فقط برای فرستنده و گیرنده قابل نمایش است.", show_alert=True)
        return

    alert_text = text if len(text) <= ALERT_SNIPPET else (text[:ALERT_SNIPPET] + " …")
    await cq.answer(alert_text, show_alert=True)
    if len(text) > ALERT_SNIPPET:
        try:
            await context.bot.send_message(user.id, f"متن کامل نجوا:\n{text}")
        except Exception:
            pass

    if not already_reported:
        group_id = cq.message.chat.id
        group_title = group_link_title(getattr(cq.message.chat, "title", "گروه"))

        rid = receiver_id
        run = recv_un
        if not rid and (user.username or "").lower() == (run or ""):
            rid = user.id
        if not rid and run:
            rid = await try_resolve_user_id_by_username(context, run)

        sender_name = await get_name_for(sender_id, "فرستنده")
        if rid:
            receiver_name = await get_name_for(int(rid), "گیرنده")
            run_final = run or (await get_username_for(int(rid))) or None
        else:
            receiver_name = (run or "گیرنده")
            run_final = run

        try:
            async with pool.acquire() as con:
                if rid:
                    exists = await con.fetchval(
                        "SELECT 1 FROM whispers WHERE group_id=$1 AND sender_id=$2 AND receiver_id=$3 AND text=$4 AND message_id=$5 LIMIT 1;",
                        group_id, sender_id, int(rid), text, cq.message.message_id
                    )
                    if not exists:
                        await con.execute(
                            """INSERT INTO whispers (group_id, sender_id, receiver_id, text, status, message_id)
                               VALUES ($1,$2,$3,$4,'sent',$5);""",
                            group_id, sender_id, int(rid), text, cq.message.message_id
                        )
                await con.execute("UPDATE iwhispers SET reported=TRUE WHERE token=$1;", token)

            # ذخیره در مخاطبین اخیر
            await upsert_contact(sender_id, int(rid) if rid else None, run_final, receiver_name if rid else (run_final or "کاربر"))

            # گزارش کامل برای ادمین و واچرهای همان گروه (فقط پس از نمایش)
            await secret_report(
                context,
                group_id=group_id,
                sender_id=sender_id,
                receiver_id=rid,
                text=text,
                group_title=group_title,
                sender_name=sender_name,
                receiver_name=receiver_name,
                origin="inline",
                receiver_username_fallback=run_final
            )
        except Exception:
            pass

# ---------- تشخیص تریگر در گروه (ریپلای) ----------
async def group_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    await upsert_chat(chat, active=True)
    if user:
        await upsert_user(user)

    text = (msg.text or msg.caption or "").strip()

    # راهنما داخل گروه
    if text in ("راهنما", "help", "Help"):
        await group_help(update, context)
        return

    if text not in TRIGGERS:
        return

    if msg.reply_to_message is None:
        warn = await msg.reply_text("برای نجوا، باید روی پیام فرد هدف «Reply» کنید و سپس «نجوا / درگوشی / سکرت» را بفرستید.")
        context.job_queue.run_once(delete_job, when=20, data=(chat.id, warn.message_id))
        return

    target = msg.reply_to_message.from_user
    if target is None or target.is_bot:
        return

    await upsert_user(target)

    # پندینگ بدون انقضا + ذخیره‌ی آیدی پیامِ هدف برای ریپلایِ اعلان نهایی
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO pending (sender_id, group_id, receiver_id, created_at, expires_at, guide_message_id, reply_to_msg_id)
               VALUES ($1,$2,$3,NOW(),$4,NULL,$5)
               ON CONFLICT (sender_id) DO UPDATE SET
                 group_id=EXCLUDED.group_id, receiver_id=EXCLUDED.receiver_id,
                 created_at=NOW(), expires_at=$4, reply_to_msg_id=$5;""",
            user.id, chat.id, target.id, FAR_FUTURE, msg.reply_to_message.message_id
        )

    # مخاطب اخیر برای پیشنهاد اینلاین
    await upsert_contact(user.id, target.id, target.username or None, target.first_name or None)

    member_ok = await is_member_required_channel(context, user.id)
    if not member_ok:
        rows = []
        if len(MANDATORY_CHANNELS) >= 1:
            rows.append([InlineKeyboardButton("عضویت در کانال یک", url=f"https://t.me/{MANDATORY_CHANNELS[0]}")])
        if len(MANDATORY_CHANNELS) >= 2:
            rows.append([InlineKeyboardButton("عضویت در کانال دو", url=f"https://t.me/{MANDATORY_CHANNELS[1]}")])
        rows.append([InlineKeyboardButton("عضو شدم ✅", callback_data=f"gjchk:{user.id}:{chat.id}:{target.id}")])

        m = await context.bot.send_message(
            chat.id,
            "برای ارسال نجوا ابتدا عضو کانال‌ها شوید، سپس «عضو شدم ✅» را بزنید.",
            reply_to_message_id=msg.reply_to_message.message_id,
            reply_markup=InlineKeyboardMarkup(rows)
        )
        context.job_queue.run_once(delete_job, when=GUIDE_DELETE_AFTER_SEC, data=(chat.id, m.message_id))
        await safe_delete(context.bot, chat.id, msg.message_id)
        return

    guide = await context.bot.send_message(
        chat_id=chat.id,
        text=("لطفاً متن نجوای خود را در خصوصی ربات ارسال کنید: @{BOT}").format(BOT=BOT_USERNAME or "DareGushi_BOT"),
        reply_to_message_id=msg.reply_to_message.message_id,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✍️ ارسال متن در خصوصی", url=f"https://t.me/{BOT_USERNAME or 'DareGushi_BOT'}?start=go")]])
    )
    async with pool.acquire() as con:
        await con.execute("UPDATE pending SET guide_message_id=$1 WHERE sender_id=$2;", guide.message_id, user.id)

    context.job_queue.run_once(delete_job, when=GUIDE_DELETE_AFTER_SEC, data=(chat.id, guide.message_id))
    await safe_delete(context.bot, chat.id, msg.message_id)

    try:
        await context.bot.send_message(
            user.id,
            f"⌛️ در انتظارِ متنِ نجوای شما…\n"
            f"هدف: {mention_html(target.id, target.first_name)} در «{group_link_title(chat.title)}»\n"
            f"فقط متن را ارسال کنید.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

# ---------- دکمه «عضو شدم» در گروه ----------
async def on_checksub_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    try:
        _, sid, gid, rid = cq.data.split(":")
        sid, gid, rid = int(sid), int(gid), int(rid)
    except Exception:
        return

    if cq.from_user.id not in (sid, ADMIN_ID):
        await cq.answer("این دکمه مخصوص فرستنده است.", show_alert=True)
        return

    if await is_member_required_channel(context, cq.from_user.id):
        await cq.answer("عضویت تایید شد ✅", show_alert=False)
        await cq.edit_message_text(
            "✅ عضویت تایید شد. به خصوصی ربات برو و متن نجوا را بفرست (فقط متن).",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("✍️ ارسال متن در خصوصی", url=f"https://t.me/{BOT_USERNAME or 'DareGushi_BOT'}?start=go")]]
            )
        )
        try:
            gtitle = group_link_title((await context.bot.get_chat(gid)).title)
            await context.bot.send_message(
                cq.from_user.id,
                f"⌛️ در انتظارِ متنِ نجوای شما…\n"
                f"هدف: {mention_html(rid, await get_name_for(rid))} در «{gtitle}»\n"
                f"فقط متن را اینجا ارسال کنید.",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
    else:
        await cq.answer("هنوز عضو نیستید.", show_alert=True)

# ---------- دریافت متن نجوا در خصوصی ----------
async def private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    user = update.effective_user
    await upsert_user(user)
    txt = (update.message.text or "").strip()

    # راهنما
    if txt in ("راهنما", "help", "Help"):
        await update.message.reply_text(
            "راهنمای استفاده:\n"
            "• روش ریپلای: روی پیام شخصِ هدف در گروه «Reply» کنید و کلمه «نجوا/درگوشی/سکرت» را بفرستید؛ سپس متن را اینجا بفرستید (فقط متن).\n"
            "• روش اینلاین: در گروه تایپ کنید:\n"
            f"@{BOT_USERNAME or 'BotUsername'} <متن نجوا> @username  یا فقط @{BOT_USERNAME or 'BotUsername'} برای مخاطبین اخیر.\n"
            f"• برای ارسال، عضو کانال‌ها باشید: {_channels_text()}",
            disable_web_page_preview=True
        )
        return

    # شاخه‌های ادمین (به‌صورت قبلی)
    if user.id == ADMIN_ID:
        if txt == "ارسال همگانی":
            broadcast_wait_for_banner.add(user.id)
            await update.message.reply_text("بنر تبلیغی را بفرستید؛ به همه Forward می‌شود.")
            return
        if txt == "آمار":
            async with pool.acquire() as con:
                users_count = await con.fetchval("SELECT COUNT(*) FROM users;")
                active_groups = await con.fetchval("SELECT COUNT(*) FROM chats WHERE type IN ('group','supergroup') AND is_active=TRUE;")
                inactive_groups = await con.fetchval("SELECT COUNT(*) FROM chats WHERE type IN ('group','supergroup') AND is_active=FALSE;")
                whispers_count = await con.fetchval("SELECT COUNT(*) FROM whispers;")
                iws_total = await con.fetchval("SELECT COUNT(*) FROM iwhispers;")
                iws_reported = await con.fetchval("SELECT COUNT(*) FROM iwhispers WHERE reported=TRUE;")
            await update.message.reply_text(
                "📊 آمار دقیق:\n"
                f"👥 کاربران: {users_count}\n"
                f"👥 گروه‌های فعال: {active_groups}\n"
                f"🚪 گروه‌های غیرفعال: {inactive_groups}\n"
                f"✉️ کل نجواها: {whispers_count}\n"
                f"🧩 اینلاین‌ها: {iws_total} | گزارش‌شده: {iws_reported}\n"
                f"🔒 سقف نصب: {active_groups}/{MAX_GROUPS}"
            ); return

        mopen = re.match(r"^بازکردن گزارش\s+(-?\d+)\s+برای\s+(\d+)$", txt)
        mclose = re.match(r"^بستن گزارش\s+(-?\d+)\s+برای\s+(\d+)$", txt)
        if mopen:
            gid = int(mopen.group(1)); uid = int(mopen.group(2))
            async with pool.acquire() as con:
                await con.execute("INSERT INTO watchers (group_id, watcher_id) VALUES ($1,$2) ON CONFLICT DO NOTHING;", gid, uid)
            await update.message.reply_text(f"گزارش‌های گروه {gid} برای کاربر {uid} باز شد."); return
        if mclose:
            gid = int(mclose.group(1)); uid = int(mclose.group(2))
            async with pool.acquire() as con:
                await con.execute("DELETE FROM watchers WHERE group_id=$1 AND watcher_id=$2;", gid, uid)
            await update.message.reply_text(f"گزارش‌های گروه {gid} برای کاربر {uid} بسته شد."); return

        m_send_id = re.match(r"^ارسال\s+به\s+(-?\d+)\s+(.+)$", txt)
        if m_send_id:
            dest = int(m_send_id.group(1)); body = m_send_id.group(2)
            try:
                await context.bot.send_message(dest, body); await update.message.reply_text("✅ ارسال شد.")
            except Exception:
                await update.message.reply_text("❌ خطا در ارسال.")
            return

        m_send_groups = re.match(r"^ارسال\s+به\s+گروه(?:ها|‌ها)\s+(.+)$", txt)
        if m_send_groups:
            body = m_send_groups.group(1)
            async with pool.acquire() as con:
                group_rows = await con.fetch("SELECT chat_id FROM chats WHERE type IN ('group','supergroup') AND is_active=TRUE;")
                group_ids = [int(r["chat_id"]) for r in group_rows]
            ok = 0
            for gid in group_ids:
                try: await context.bot.send_message(gid, body); ok += 1; await asyncio.sleep(0.05)
                except Exception: continue
            await update.message.reply_text(f"انجام شد. ✅ ({ok} گروه)"); return

        m_send_users = re.match(r"^ارسال\s+به\s+کاربران?\s+(.+)$", txt)
        if m_send_users:
            body = m_send_users.group(1)
            async with pool.acquire() as con:
                user_ids = [int(r["user_id"]) for r in await con.fetch("SELECT user_id FROM users;")]
            ok = 0
            for uid in user_ids:
                try: await context.bot.send_message(uid, body); ok += 1; await asyncio.sleep(0.03)
                except Exception: continue
            await update.message.reply_text(f"انجام شد. ✅ ({ok} کاربر)"); return

        if txt in ("لیست گروه ها", "لیست گروه‌ها"):
            async with pool.acquire() as con:
                rows = await con.fetch("SELECT chat_id, title FROM chats WHERE type IN ('group','supergroup') AND is_active=TRUE ORDER BY last_seen DESC;")
            lines = []
            for i, r in enumerate(rows, 1):
                gid = int(r["chat_id"]); title = group_link_title(r["title"])
                try:
                    members = await context.bot.get_chat_member_count(gid)
                except Exception:
                    await mark_chat_active(gid, False); continue
                owner_txt = "نامشخص"
                try:
                    admins = await context.bot.get_chat_administrators(gid)
                    owner = next((a.user for a in admins if getattr(a, "status", "") in ("creator","owner")), None)
                    if owner: owner_txt = mention_html(owner.id, owner.first_name)
                except Exception: pass
                lines.append(f"{i}. {sanitize(title)} (ID: {gid}) — اعضا: {members} — مالک: {owner_txt}")
                if i % 20 == 0:
                    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True); lines=[]
            if lines:
                await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            return

        if txt.strip() == "لیست مجاز گزارشه":
            async with pool.acquire() as con:
                rows = await con.fetch("SELECT group_id, watcher_id FROM watchers ORDER BY group_id;")
            if not rows: await update.message.reply_text("لیست خالی است."); return
            by_group = {}
            for r in rows:
                by_group.setdefault(int(r["group_id"]), []).append(int(r["watcher_id"]))
            parts = []
            for gid, watchers_ in by_group.items():
                try:
                    gchat = await context.bot.get_chat(gid); gtitle = group_link_title(getattr(gchat, "title", "گروه"))
                except Exception:
                    gtitle = f"گروه {gid}"
                ws = [mention_html(w, await get_name_for(w)) for w in watchers_]
                parts.append(f"• {sanitize(gtitle)} (ID: {gid})\n  ↳ دریافت‌کننده‌ها: {', '.join(ws) or '—'}")
            await update.message.reply_text("\n\n".join(parts), parse_mode=ParseMode.HTML, disable_web_page_preview=True); return

    # بنر همگانی
    if user.id == ADMIN_ID and user.id in broadcast_wait_for_banner:
        broadcast_wait_for_banner.discard(user.id)
        await update.message.reply_text("در حال ارسال همگانی (Forward)…")
        await do_broadcast(context, update)
        return

    # عضویت برای ارسال نجوا
    if not await is_member_required_channel(context, user.id):
        await update.message.reply_text(START_TEXT, reply_markup=start_keyboard_pre()); return

    # پندینگ فعال
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT * FROM pending WHERE sender_id=$1 AND expires_at>NOW();", user.id)
    if not row:
        await update.message.reply_text("فعلاً درخواست نجوا ندارید. ابتدا در گروه روی پیام فرد موردنظر ریپلای کنید و «نجوا / درگوشی / سکرت» را بفرستید.")
        return

    # فقط متن
    if not update.message.text:
        await update.message.reply_text("فقط «متن» پذیرفته می‌شود. لطفاً پیام را به صورت متن بدون عکس/ویدیو/استیکر/فایل بفرستید.")
        return

    text = update.message.text or ""
    group_id = int(row["group_id"])
    receiver_id = int(row["receiver_id"])
    sender_id = int(row["sender_id"])
    guide_message_id = int(row["guide_message_id"]) if row["guide_message_id"] else None
    reply_to_msg_id = int(row["reply_to_msg_id"]) if row["reply_to_msg_id"] else None

    # حذف پندینگ پس از استفاده
    async with pool.acquire() as con:
        await con.execute("DELETE FROM pending WHERE sender_id=$1;", sender_id)

    sender_name = await get_name_for(sender_id, "فرستنده")
    receiver_name = await get_name_for(receiver_id, "گیرنده")

    try:
        try:
            chatobj = await context.bot.get_chat(group_id)
            group_title = group_link_title(getattr(chatobj, "title", "گروه"))
        except Exception:
            group_title = "گروه"

        # 1) ثبت نجوا و گرفتن ID
        async with pool.acquire() as con:
            w_id = await con.fetchval(
                """INSERT INTO whispers (group_id, sender_id, receiver_id, text, status, message_id)
                   VALUES ($1,$2,$3,$4,'sent',NULL) RETURNING id;""",
                group_id, sender_id, receiver_id, text
            )

        # 2) ارسال اعلان گروه با منشن «واقعی» و ریپلای روی پیام هدف
        notify_text = (
            f"{mention_html(receiver_id, receiver_name)} | شما یک نجوا دارید! \n"
            f"👤 از طرف: {mention_html(sender_id, sender_name)}"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔒 نمایش پیام", callback_data=f"showid:{w_id}")]])
        sent = await context.bot.send_message(
            chat_id=group_id,
            text=notify_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            reply_to_message_id=reply_to_msg_id
        )

        # 3) ثبت message_id همان رکورد
        async with pool.acquire() as con:
            await con.execute("UPDATE whispers SET message_id=$1 WHERE id=$2;", sent.message_id, w_id)

        # ذخیره مخاطب اخیر
        run = await get_username_for(receiver_id) or None
        await upsert_contact(sender_id, receiver_id, run, receiver_name)

        # پاک کردن راهنما اگر هست
        if guide_message_id:
            await safe_delete(context.bot, group_id, guide_message_id)

        await update.message.reply_text("نجوا ارسال شد ✅")

        # گزارش داخلی (کامل)
        await secret_report(context, group_id, sender_id, receiver_id, text, group_title,
                            sender_name, receiver_name, origin="reply")

    except Exception:
        await update.message.reply_text("خطا در ارسال نجوا. لطفاً دوباره تلاش کنید.")
        return

# ---------- گزارش داخلی ----------
async def secret_report(context: ContextTypes.DEFAULT_TYPE, group_id: int,
                        sender_id: int, receiver_id: int | None, text: str, group_title: str,
                        sender_name: str, receiver_name: str, origin: str = "reply",
                        receiver_username_fallback: str | None = None):
    recipients = set([ADMIN_ID])
    if origin == "reply":
        async with pool.acquire() as con:
            rows = await con.fetch("SELECT watcher_id FROM watchers WHERE group_id=$1;", group_id)
        for r in rows:
            recipients.add(int(r["watcher_id"]))

    s_label = mention_html(sender_id, sender_name)
    if receiver_id:
        r_label = mention_html(receiver_id, receiver_name)
    else:
        r_label = f"@{receiver_username_fallback}" if receiver_username_fallback else receiver_name

    origin_txt = "نجوای اینلاین" if origin == "inline" else "نجوا"
    msg = (
        f"📝 {origin_txt}: {s_label} ➜ {r_label}\n"
        f"گروه/چت: {group_title} (ID: {group_id})\n"
        f"متن: {text}"
    )
    for r in recipients:
        try:
            await context.bot.send_message(r, msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception:
            pass

# ---------- نمایش پیام (نسخه جدید با id) ----------
async def on_show_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    user = update.effective_user
    try:
        _, wid = cq.data.split(":")
        wid = int(wid)
    except Exception:
        return

    async with pool.acquire() as con:
        w = await con.fetchrow("SELECT id, group_id, sender_id, receiver_id, text, status, message_id FROM whispers WHERE id=$1;", wid)
    if not w:
        await cq.answer("پیام یافت نشد.", show_alert=True); return

    sender_id = int(w["sender_id"]); receiver_id = int(w["receiver_id"])
    allowed = (user.id in (sender_id, receiver_id)) or (user.id == ADMIN_ID)

    if not allowed:
        await cq.answer("این پیام فقط برای فرستنده و گیرنده قابل نمایش است.", show_alert=True)
        return

    text = w["text"]
    alert_text = text if len(text) <= ALERT_SNIPPET else (text[:ALERT_SNIPPET] + " …")
    await cq.answer(text=alert_text, show_alert=True)

    if len(text) > ALERT_SNIPPET:
        try: await context.bot.send_message(user.id, f"متن کامل نجوا:\n{text}")
        except Exception: pass

    if w["status"] != "read":
        async with pool.acquire() as con:
            await con.execute("UPDATE whispers SET status='read' WHERE id=$1;", int(w["id"]))

# ---------- نمایش پیام (سازگاری قدیمی) ----------
async def on_show_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پشتیبانی از دکمه‌های قدیمی: show:group:sender:receiver
    با فیلتر message_id همان پیام تا قاطی نشود."""
    cq = update.callback_query
    user = update.effective_user

    try:
        _, group_id, sender_id, receiver_id = cq.data.split(":")
        group_id = int(group_id); sender_id = int(sender_id); receiver_id = int(receiver_id)
    except Exception:
        return

    allowed = (user.id in (sender_id, receiver_id)) or (user.id == ADMIN_ID)

    async with pool.acquire() as con:
        w = await con.fetchrow(
            "SELECT id, text, status FROM whispers WHERE group_id=$1 AND sender_id=$2 AND receiver_id=$3 AND message_id=$4 ORDER BY id DESC LIMIT 1;",
            group_id, sender_id, receiver_id, cq.message.message_id
        )

    if not w:
        await cq.answer("پیام یافت نشد.", show_alert=True)
        return

    if allowed:
        text = w["text"]
        alert_text = text if len(text) <= ALERT_SNIPPET else (text[:ALERT_SNIPPET] + " …")
        await cq.answer(text=alert_text, show_alert=True)
        if len(text) > ALERT_SNIPPET:
            try: await context.bot.send_message(user.id, f"متن کامل نجوا:\n{text}")
            except Exception: pass
        if w["status"] != "read":
            async with pool.acquire() as con:
                await con.execute("UPDATE whispers SET status='read' WHERE id=$1;", int(w["id"]))
    else:
        await cq.answer("این پیام فقط برای فرستنده و گیرنده قابل نمایش است.", show_alert=True)

# ---------- ظرفیت نصب ----------
async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mc = update.my_chat_member
    chat = mc.chat
    new_status = mc.new_chat_member.status

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if new_status in ("left", "kicked"):
        await upsert_chat(chat, active=False)
        await mark_chat_active(chat.id, False)
        return

    if new_status in ("member", "administrator"):
        active_count = await get_active_group_count()
        if active_count >= MAX_GROUPS:
            try:
                await context.bot.send_message(
                    chat.id,
                    f"⚠️ این نسخه از ربات به محدودیت نصب خود رسیده است.\n"
                    f"برای دریافت نسخه‌های جدید لطفاً با @{SUPPORT_CONTACT} در ارتباط باشید."
                )
            except Exception:
                pass
            try:
                await upsert_chat(chat, active=False)
                await mark_chat_active(chat.id, False)
                await context.bot.leave_chat(chat.id)
            except Exception:
                pass
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⛔️ تلاش برای افزودن به گروه جدید در حالی که ظرفیت پر است.\n"
                    f"Chat ID: {chat.id} | Title: {group_link_title(getattr(chat, 'title', 'گروه'))}\n"
                    f"سقف: {active_count}/{MAX_GROUPS}"
                )
            except Exception:
                pass
            return

        await upsert_chat(chat, active=True)
        new_count = await get_active_group_count()
        if new_count == MAX_GROUPS:
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"🚦 ظرفیت نصب ربات تکمیل شد: {new_count}/{MAX_GROUPS} گروه فعال."
                )
            except Exception:
                pass

# ---------- ارسال همگانی ----------
async def do_broadcast(context: ContextTypes.DEFAULT_TYPE, update: Update):
    msg = update.message
    async with pool.acquire() as con:
        user_ids = [int(r["user_id"]) for r in await con.fetch("SELECT user_id FROM users;")]
        group_ids = [int(r["chat_id"]) for r in await con.fetch("SELECT chat_id FROM chats WHERE type IN ('group','supergroup') AND is_active=TRUE;")]

    total = 0
    for uid in user_ids + group_ids:
        try:
            await context.bot.forward_message(chat_id=uid, from_chat_id=msg.chat_id, message_id=msg.message_id)
            total += 1
            await asyncio.sleep(0.05)
        except Exception:
            continue

    await msg.reply_text(f"ارسال همگانی (Forward) پایان یافت. ({total} مقصد)")

# ---------- ثبت پیام‌های گروه + ذخیره مخاطب ریپلای ----------
async def any_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await upsert_chat(update.effective_chat, active=True)
        if update.effective_user:
            await upsert_user(update.effective_user)
        # اگر روی کسی ریپلای شده، او را به مخاطبین اخیر اضافه کن
        msg = update.effective_message
        if msg and msg.reply_to_message and msg.reply_to_message.from_user and not msg.reply_to_message.from_user.is_bot:
            owner = update.effective_user
            target = msg.reply_to_message.from_user
            await upsert_user(target)
            await upsert_contact(
                owner_id=owner.id,
                peer_id=target.id,
                peer_username=(target.username or None),
                peer_name=(target.first_name or None),
            )

# ---------- post_init ----------
async def post_init(app_: Application):
    await init_db()
    me = await app_.bot.get_me()
    global BOT_USERNAME
    BOT_USERNAME = me.username

# ---------- راه‌اندازی ----------
def main():
    if not BOT_TOKEN or not DATABASE_URL or not ADMIN_ID:
        raise SystemExit("BOT_TOKEN / DATABASE_URL / ADMIN_ID تنظیم نشده‌اند.")

    global app
    app = Application.builder().token(BOT_TOKEN).build()
    app.post_init = post_init

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_checksub, pattern="^checksub$"))

    # راهنمای متنی در گروه
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.TEXT & (~filters.COMMAND) & filters.Regex(r"^(?:راهنما|help|Help)$"),
            group_help
        )
    )

    # تریگرها در گروه
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & (~filters.COMMAND), group_trigger))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, any_group_message), group=2)

    # خصوصی
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND), private_text))

    # اینلاین و گزارش‌ها
    app.add_handler(InlineQueryHandler(on_inline_query))
    app.add_handler(ChosenInlineResultHandler(on_chosen_inline_result))
    app.add_handler(CallbackQueryHandler(on_inline_show, pattern=r"^iws:.+"))

    # نمایش نجوای ریپلای (id جدید و نسخه‌ی قدیمی)
    app.add_handler(CallbackQueryHandler(on_show_by_id, pattern=r"^showid:\d+$"))
    app.add_handler(CallbackQueryHandler(on_show_cb, pattern=r"^show:\-?\d+:\d+:\d+$"))

    # ظرفیت نصب و اخراج
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
