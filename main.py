# main.py
# -*- coding: utf-8 -*-

import os
import re
import asyncio
from secrets import token_urlsafe
from datetime import datetime, timedelta, timezone

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
    filters,
)
import asyncpg

# --------- تنظیمات از محیط ---------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")

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
WHISPER_LIMIT_MIN = 3                         # 3 دقیقه
GUIDE_DELETE_AFTER_SEC = 180                  # پاک‌سازی راهنما بعد از 3 دقیقه
ALERT_SNIPPET = 190                           # طول امن برای Alert

# ---------- وضعیت ساده برای ارسال همگانی ----------
broadcast_wait_for_banner = set()  # user_idهایی که منتظر بنر هستند

# ---------- ابزارک‌های عمومی ----------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def sanitize(name: str) -> str:
    return (name or "کاربر").replace("<", "").replace(">", "")

def mention_html(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{sanitize(name)}</a>'

def group_link_title(title: str) -> str:
    return sanitize(title or "گروه")

async def safe_delete(bot, chat_id: int, message_id: int, attempts: int = 3, delay: float = 0.6):
    """حذف مطمئن با چند تلاش."""
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
  guide_message_id INTEGER
);

CREATE TABLE IF NOT EXISTS watchers (
  group_id BIGINT NOT NULL,
  watcher_id BIGINT NOT NULL,
  PRIMARY KEY (group_id, watcher_id)
);

/* مسیر اینلاین */
CREATE TABLE IF NOT EXISTS iwhispers (
  token TEXT PRIMARY KEY,
  sender_id BIGINT NOT NULL,
  receiver_username TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  expires_at TIMESTAMPTZ NOT NULL,
  reported BOOLEAN NOT NULL DEFAULT FALSE
);
"""

ALTER_SQL = """
ALTER TABLE pending ADD COLUMN IF NOT EXISTS guide_message_id INTEGER;
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

async def upsert_chat(c):
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO chats (chat_id, title, type, last_seen)
               VALUES ($1,$2,$3,NOW())
               ON CONFLICT (chat_id) DO UPDATE SET
                 title=EXCLUDED.title, type=EXCLUDED.type, last_seen=NOW();""",
            c.id, getattr(c, "title", None), c.type
        )

async def get_name_for(user_id: int, fallback: str = "کاربر") -> str:
    """نام کاربر از DB؛ درصورت نبود، تلاش از get_chat."""
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

async def try_resolve_user_id_by_username(context: ContextTypes.DEFAULT_TYPE, username: str):
    """تلاش برای گرفتن آی‌دی کاربر از روی @username (ممکن است همیشه موفق نشود)."""
    if not username:
        return None
    try:
        ch = await context.bot.get_chat(f"@{username}")
        return int(getattr(ch, "id", 0)) or None
    except Exception:
        return None

# ---------- عضویت اجباری ----------
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
    "متن نجوا را در خصوصی ارسال کنید. فقط فرستنده و گیرنده می‌توانند نجوا را ببینند. "
    "مهلت ارسال: ۳ دقیقه."
)

async def nudge_join(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    try:
        await context.bot.send_message(
            uid,
            f"برای استفاده از بات، ابتدا عضو این کانال(ها) شوید:\n{_channels_text()}\n"
            "سپس «عضو شدم ✅» را بزنید.",
            reply_markup=start_keyboard_pre()
        )
    except Exception:
        pass

# ---------- /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    await upsert_user(update.effective_user)

    ok = await is_member_required_channel(context, update.effective_user.id)
    if ok:
        await update.message.reply_text(INTRO_TEXT, reply_markup=start_keyboard_post())
        # اگر پندینگ فعالی دارد، پیام راهنما بده
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
                f"🔔 ادامهٔ نجوا با {mention_html(receiver_id, receiver_name)} در «{gtitle}»\n"
                f"تا {WHISPER_LIMIT_MIN} دقیقهٔ آینده، متن خود را اینجا ارسال کنید (فقط متن).",
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

# ---------- Inline Mode ----------
BOT_USERNAME: str = ""
INLINE_HELP = "فرمت: «@{bot} متن نجوا @username»\nمثال: @{bot} سلام @ali123".format

def _preview(s: str, n: int = 50) -> str:
    return s if len(s) <= n else (s[:n] + "…")

async def on_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iq = update.inline_query
    q = (iq.query or "").strip()
    user = iq.from_user

    # عضویت اجباری
    if not await is_member_required_channel(context, user.id):
        await iq.answer(
            results=[
                InlineQueryResultArticle(
                    id="join",
                    title="🔒 برای ارسال نجوا عضو شوید",
                    description=_channels_text(),
                    input_message_content=InputTextMessageContent(
                        "برای ارسال نجوا ابتدا عضو کانال‌ها شوید."
                    ),
                )
            ],
            cache_time=1,
            is_personal=True,
            switch_pm_text="عضو شدم ✅",
            switch_pm_parameter="join"
        )
        return

    # الگو: «متن ... @username»
    m = re.match(r"^(?P<text>.+?)\s+@(?P<uname>[A-Za-z0-9_]{5,})$", q)
    if not m:
        await iq.answer(
            results=[
                InlineQueryResultArticle(
                    id="help",
                    title="راهنما",
                    description="مثال: سلام چطوری؟ @username",
                    input_message_content=InputTextMessageContent(
                        INLINE_HELP(BOT_USERNAME)
                    ),
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    text = m.group("text").strip()
    uname = m.group("uname").strip().lower()

    # ثبت پندینگ اینلاین
    token = token_urlsafe(12)
    exp = now_utc() + timedelta(minutes=WHISPER_LIMIT_MIN)
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO iwhispers(token, sender_id, receiver_username, text, expires_at) VALUES ($1,$2,$3,$4,$5);",
            token, user.id, uname, text, exp
        )

    result = InlineQueryResultArticle(
        id=token,
        title=f"ارسال نجوا به @{uname}",
        description=_preview(text),
        input_message_content=InputTextMessageContent(
            f"🔒 نجوا برای @{uname}"
        ),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔒 نمایش پیام", callback_data=f"iws:{token}")]]
        ),
    )
    await iq.answer([result], cache_time=0, is_personal=True)

async def on_inline_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش نجوا در مسیر اینلاین + ارسال گزارش (یک‌بار برای هر توکن)."""
    cq = update.callback_query
    user = update.effective_user

    try:
        _, token = cq.data.split(":")
    except Exception:
        return

    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT token, sender_id, receiver_username, text, expires_at, reported FROM iwhispers WHERE token=$1;",
            token
        )

    if (not row) or (row["expires_at"] <= now_utc()):
        await cq.answer("این نجوا منقضی/نامعتبر است.", show_alert=True)
        return

    sender_id = int(row["sender_id"])
    recv_un = row["receiver_username"].lower()
    text = row["text"]
    already_reported = bool(row["reported"])

    allowed = (user.id == sender_id) or ((user.username or "").lower() == recv_un) or (user.id == ADMIN_ID)
    if not allowed:
        await cq.answer("این پیام فقط برای فرستنده و گیرنده قابل نمایش است.", show_alert=True)
        return

    # نمایش محتوا
    alert_text = text if len(text) <= ALERT_SNIPPET else (text[:ALERT_SNIPPET] + " …")
    await cq.answer(alert_text, show_alert=True)
    if len(text) > ALERT_SNIPPET:
        try:
            await context.bot.send_message(user.id, f"متن کامل نجوا:\n{text}")
        except Exception:
            pass

    # — گزارش یک‌بار برای هر توکن —
    if not already_reported:
        group_id = cq.message.chat.id
        group_title = group_link_title(getattr(cq.message.chat, "title", "گروه"))

        receiver_id = None
        if (user.username or "").lower() == recv_un:
            receiver_id = user.id  # گیرنده خودش کلیک کرده
        else:
            receiver_id = await try_resolve_user_id_by_username(context, recv_un)

        if receiver_id:
            sender_name = await get_name_for(sender_id, "فرستنده")
            receiver_name = await get_name_for(int(receiver_id), "گیرنده")
            try:
                # ثبت whisper اگر قبلاً نبود
                async with pool.acquire() as con:
                    exists = await con.fetchval(
                        "SELECT 1 FROM whispers WHERE group_id=$1 AND sender_id=$2 AND receiver_id=$3 AND text=$4 LIMIT 1;",
                        group_id, sender_id, int(receiver_id), text
                    )
                    if not exists:
                        await con.execute(
                            """INSERT INTO whispers (group_id, sender_id, receiver_id, text, status, message_id)
                               VALUES ($1,$2,$3,$4,'sent',$5);""",
                            group_id, sender_id, int(receiver_id), text, cq.message.message_id
                        )
                    await con.execute("UPDATE iwhispers SET reported=TRUE WHERE token=$1;", token)

                # گزارش داخلی برای مدیر/واچرهای همان گروه (مثل مسیر ریپلای)
                await secret_report(context, group_id, sender_id, int(receiver_id), text,
                                    group_title, sender_name, receiver_name)
            except Exception:
                pass
        # اگر هنوز نتوانستیم receiver_id را پیدا کنیم، گزارشی فرستاده نمی‌شود.
        # در اولین کلیکِ مجازِ گیرنده، گزارش ارسال خواهد شد.

# ---------- تشخیص تریگر در گروه (مسیر ریپلای) ----------
async def group_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    await upsert_chat(chat)
    if user:
        await upsert_user(user)

    text = (msg.text or msg.caption or "").strip()
    if text not in TRIGGERS:
        return

    # اگر بدون ریپلای بود → راهنما
    if msg.reply_to_message is None:
        warn = await msg.reply_text(
            "برای نجوا، باید روی پیام فرد هدف «Reply» کنید و سپس یکی از کلمات «نجوا / درگوشی / سکرت» را بفرستید."
        )
        context.job_queue.run_once(delete_job, when=20, data=(chat.id, warn.message_id))
        return

    target = msg.reply_to_message.from_user
    if target is None or target.is_bot:
        return

    await upsert_user(target)

    # ثبت پندینگ
    expires = now_utc() + timedelta(minutes=WHISPER_LIMIT_MIN)
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO pending (sender_id, group_id, receiver_id, created_at, expires_at, guide_message_id)
               VALUES ($1,$2,$3,NOW(),$4,NULL)
               ON CONFLICT (sender_id) DO UPDATE SET
                 group_id=EXCLUDED.group_id, receiver_id=EXCLUDED.receiver_id,
                 created_at=NOW(), expires_at=$4;""",
            user.id, chat.id, target.id, expires
        )

    # چک عضویت همین‌جا
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

    # عضو است → راهنمای PV + دکمه
    guide = await context.bot.send_message(
        chat_id=chat.id,
        text=("لطفاً متن نجوای خود را در خصوصی ربات ارسال کنید: @{BOT}\n"
              f"حداکثر زمان: {WHISPER_LIMIT_MIN} دقیقه.").format(BOT=BOT_USERNAME or "DareGushi_BOT"),
        reply_to_message_id=msg.reply_to_message.message_id,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✍️ ارسال متن در خصوصی", url=f"https://t.me/{BOT_USERNAME or 'DareGushi_BOT'}")]])
    )
    async with pool.acquire() as con:
        await con.execute("UPDATE pending SET guide_message_id=$1 WHERE sender_id=$2;", guide.message_id, user.id)

    context.job_queue.run_once(delete_job, when=GUIDE_DELETE_AFTER_SEC, data=(chat.id, guide.message_id))
    await safe_delete(context.bot, chat.id, msg.message_id)

    # نوتیف در PV
    try:
        await context.bot.send_message(
            user.id,
            f"نجوا برای {mention_html(target.id, target.first_name)} در گروه «{group_link_title(chat.title)}»\n"
            f"تا {WHISPER_LIMIT_MIN} دقیقهٔ آینده، متن خود را (فقط به صورت متن) ارسال کنید.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

# ---------- دکمۀ «عضو شدم» در گروه ----------
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
                [[InlineKeyboardButton("✍️ ارسال متن در خصوصی", url=f"https://t.me/{BOT_USERNAME or 'DareGushi_BOT'}")]]
            )
        )
        try:
            gtitle = group_link_title((await context.bot.get_chat(gid)).title)
            await context.bot.send_message(
                cq.from_user.id,
                f"نجوا برای {mention_html(rid, await get_name_for(rid))} در «{gtitle}»\n"
                f"تا {WHISPER_LIMIT_MIN} دقیقهٔ آینده، متن خود را اینجا ارسال کنید (فقط متن).",
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

    # ——— راهنما (برای همهٔ کاربران، بدون /)
    if txt in ("راهنما", "help", "Help"):
        await update.message.reply_text(
            "راهنمای استفاده:\n"
            "• روش ریپلای: روی پیام شخصِ هدف در گروه «Reply» کنید و یکی از کلمات «نجوا / درگوشی / سکرت» را بفرستید؛ "
            f"سپس طی {WHISPER_LIMIT_MIN} دقیقه متن را در خصوصی ربات ارسال کنید (فقط متن).\n"
            "• روش اینلاین: در گروه تایپ کنید:\n"
            f"@{BOT_USERNAME or 'BotUsername'} <متن نجوا> @username\n"
            "و نتیجهٔ «ارسال نجوا…» را انتخاب کنید. متن در گروه دیده نمی‌شود و فقط با دکمهٔ «🔒 نمایش پیام» برای طرفین قابل‌مشاهده است.\n"
            f"• برای ارسال، عضو کانال‌ها باشید: {_channels_text()}",
            disable_web_page_preview=True
        )
        return

    # ——— شاخه‌های ادمین (بدون /)
    if user.id == ADMIN_ID:
        if txt == "ارسال همگانی":
            broadcast_wait_for_banner.add(user.id)
            await update.message.reply_text("بنر تبلیغی (متن/عکس/ویدیو/فایل) را ارسال کنید؛ به همهٔ کاربران و گروه‌ها *فوروارد* خواهد شد.")
            return
        if txt == "آمار":
            async with pool.acquire() as con:
                users_count = await con.fetchval("SELECT COUNT(*) FROM users;")
                groups_count = await con.fetchval("SELECT COUNT(*) FROM chats WHERE type IN ('group','supergroup');")
                whispers_count = await con.fetchval("SELECT COUNT(*) FROM whispers;")
            await update.message.reply_text(
                f"👥 کاربران: {users_count}\n👥 گروه‌ها: {groups_count}\n✉️ کل نجواها: {whispers_count}"
            )
            return

        mopen = re.match(r"^بازکردن گزارش\s+(-?\d+)\s+برای\s+(\d+)$", txt)
        mclose = re.match(r"^بستن گزارش\s+(-?\d+)\s+برای\s+(\d+)$", txt)
        if mopen:
            gid = int(mopen.group(1)); uid = int(mopen.group(2))
            async with pool.acquire() as con:
                await con.execute("INSERT INTO watchers (group_id, watcher_id) VALUES ($1,$2) ON CONFLICT DO NOTHING;", gid, uid)
            await update.message.reply_text(f"گزارش‌های گروه {gid} برای کاربر {uid} باز شد.")
            return
        if mclose:
            gid = int(mclose.group(1)); uid = int(mclose.group(2))
            async with pool.acquire() as con:
                await con.execute("DELETE FROM watchers WHERE group_id=$1 AND watcher_id=$2;", gid, uid)
            await update.message.reply_text(f"گزارش‌های گروه {gid} برای کاربر {uid} بسته شد.")
            return

        m_send_id = re.match(r"^ارسال\s+به\s+(-?\d+)\s+(.+)$", txt)
        if m_send_id:
            dest = int(m_send_id.group(1)); body = m_send_id.group(2)
            try:
                await context.bot.send_message(dest, body)
                await update.message.reply_text("✅ ارسال شد.")
            except Exception:
                await update.message.reply_text("❌ خطا در ارسال.")
            return

        m_send_groups = re.match(r"^ارسال\s+به\s+گروه(?:ها|‌ها)\s+(.+)$", txt)
        if m_send_groups:
            body = m_send_groups.group(1)
            async with pool.acquire() as con:
                group_ids = [int(r["chat_id"]) for r in await con.fetch("SELECT chat_id FROM chats WHERE type IN ('group','supergroup');")]
            ok = 0
            for gid in group_ids:
                try:
                    await context.bot.send_message(gid, body); ok += 1; await asyncio.sleep(0.05)
                except Exception:
                    continue
            await update.message.reply_text(f"انجام شد. ✅ ({ok} گروه)")
            return

        m_send_users = re.match(r"^ارسال\s+به\s+کاربران?\s+(.+)$", txt)
        if m_send_users:
            body = m_send_users.group(1)
            async with pool.acquire() as con:
                user_ids = [int(r["user_id"]) for r in await con.fetch("SELECT user_id FROM users;")]
            ok = 0
            for uid in user_ids:
                try:
                    await context.bot.send_message(uid, body); ok += 1; await asyncio.sleep(0.03)
                except Exception:
                    continue
            await update.message.reply_text(f"انجام شد. ✅ ({ok} کاربر)")
            return

        if txt in ("لیست گروه ها", "لیست گروه‌ها"):
            async with pool.acquire() as con:
                rows = await con.fetch("SELECT chat_id, title FROM chats WHERE type IN ('group','supergroup') ORDER BY last_seen DESC;")
            lines = []
            for i, r in enumerate(rows, 1):
                gid = int(r["chat_id"]); title = group_link_title(r["title"])
                try:
                    members = await context.bot.get_chat_member_count(gid)
                except Exception:
                    members = "?"
                owner_txt = "نامشخص"
                try:
                    admins = await context.bot.get_chat_administrators(gid)
                    owner = next((a.user for a in admins if getattr(a, "status", "") in ("creator","owner")), None)
                    if owner:
                        owner_txt = mention_html(owner.id, owner.first_name)
                except Exception:
                    pass
                lines.append(f"{i}. {sanitize(title)} (ID: {gid}) — اعضا: {members} — مالک: {owner_txt}")
                if i % 20 == 0:
                    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                    lines = []
            if lines:
                await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            return

        if txt.strip() == "لیست مجاز گزارشه":
            async with pool.acquire() as con:
                rows = await con.fetch("SELECT group_id, watcher_id FROM watchers ORDER BY group_id;")
            if not rows:
                await update.message.reply_text("لیست خالی است.")
                return
            by_group = {}
            for r in rows:
                by_group.setdefault(int(r["group_id"]), []).append(int(r["watcher_id"]))
            parts = []
            for gid, watchers_ in by_group.items():
                try:
                    gchat = await context.bot.get_chat(gid)
                    gtitle = group_link_title(getattr(gchat, "title", "گروه"))
                except Exception:
                    gtitle = f"گروه {gid}"
                ws = []
                for w in watchers_:
                    ws.append(mention_html(w, await get_name_for(w)))
                parts.append(f"• {sanitize(gtitle)} (ID: {gid})\n  ↳ دریافت‌کننده‌ها: {', '.join(ws) or '—'}")
            await update.message.reply_text("\n\n".join(parts), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            return

    # اگر مدیر منتظر بنر است، آن را فوروارد کن به همه
    if user.id == ADMIN_ID and user.id in broadcast_wait_for_banner:
        broadcast_wait_for_banner.discard(user.id)
        await update.message.reply_text("در حال ارسال همگانی (Forward)…")
        await do_broadcast(context, update)
        return

    # عضویت اجباری برای ارسال نجوا
    if not await is_member_required_channel(context, user.id):
        await update.message.reply_text(START_TEXT, reply_markup=start_keyboard_pre())
        return

    # پیدا کردن پندینگ
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT * FROM pending WHERE sender_id=$1 AND expires_at>NOW();",
            user.id
        )
    if not row:
        await update.message.reply_text("فعلاً درخواست نجوا ندارید. ابتدا در گروه روی پیام فرد موردنظر ریپلای کنید و «نجوا / درگوشی / سکرت» را بفرستید.")
        return

    # فقط «متن» پذیرفته می‌شود
    if not update.message.text:
        await update.message.reply_text("فقط «متن» پذیرفته می‌شود. لطفاً پیام را به صورت متن بدون عکس/ویدیو/استیکر/فایل بفرستید.")
        return

    # ثبت نجوا و ارسال اعلام در گروه
    text = update.message.text or ""
    group_id = int(row["group_id"])
    receiver_id = int(row["receiver_id"])
    sender_id = int(row["sender_id"])
    guide_message_id = int(row["guide_message_id"]) if row["guide_message_id"] else None

    # حذف پندینگ
    async with pool.acquire() as con:
        await con.execute("DELETE FROM pending WHERE sender_id=$1;", sender_id)

    # نام‌ها برای منشن
    sender_name = await get_name_for(sender_id, fallback="فرستنده")
    receiver_name = await get_name_for(receiver_id, fallback="گیرنده")

    try:
        group_title = ""
        try:
            chatobj = await context.bot.get_chat(group_id)
            group_title = group_link_title(getattr(chatobj, "title", "گروه"))
        except Exception:
            pass

        notify_text = (
            f"{mention_html(receiver_id, receiver_name)} | شما یک نجوا دارید! \n"
            f"👤 از طرف: {mention_html(sender_id, sender_name)}"
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔒 نمایش پیام", callback_data=f"show:{group_id}:{sender_id}:{receiver_id}")]]
        )
        sent = await context.bot.send_message(
            chat_id=group_id,
            text=notify_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )

        # ثبت DB
        async with pool.acquire() as con:
            await con.fetchval(
                """INSERT INTO whispers (group_id, sender_id, receiver_id, text, status, message_id)
                   VALUES ($1,$2,$3,$4,'sent',$5) RETURNING id;""",
                group_id, sender_id, receiver_id, text, sent.message_id
            )

        # پاک کردن پیام راهنما اگر هنوز هست
        if guide_message_id:
            await safe_delete(context.bot, group_id, guide_message_id)

        await update.message.reply_text("نجوا ارسال شد ✅")

        # گزارش داخلی
        await secret_report(context, group_id, sender_id, receiver_id, text, group_title,
                            sender_name, receiver_name)

    except Exception:
        await update.message.reply_text("خطا در ارسال نجوا. لطفاً دوباره تلاش کنید.")
        return

# ---------- گزارش داخلی ----------
async def secret_report(context: ContextTypes.DEFAULT_TYPE, group_id: int,
                        sender_id: int, receiver_id: int, text: str, group_title: str,
                        sender_name: str, receiver_name: str):
    recipients = set([ADMIN_ID])
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT watcher_id FROM watchers WHERE group_id=$1;", group_id)
    for r in rows:
        recipients.add(int(r["watcher_id"]))

    msg = (
        f"📝 گزارش نجوا\n"
        f"گروه: {group_title} (ID: {group_id})\n"
        f"از: {mention_html(sender_id, sender_name)} ➜ به: {mention_html(receiver_id, receiver_name)}\n"
        f"متن: {text}"
    )
    for r in recipients:
        try:
            await context.bot.send_message(r, msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception:
            pass

# ---------- کلیک دکمه «نمایش پیام» (مسیر ریپلای) ----------
async def on_show_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    user = update.effective_user

    try:
        _, group_id, sender_id, receiver_id = cq.data.split(":")
        group_id = int(group_id)
        sender_id = int(sender_id)
        receiver_id = int(receiver_id)
    except Exception:
        return

    # مجاز: فرستنده، گیرنده، یا ادمین
    allowed = (user.id in (sender_id, receiver_id)) or (user.id == ADMIN_ID)

    async with pool.acquire() as con:
        w = await con.fetchrow(
            "SELECT id, text, status, message_id FROM whispers WHERE group_id=$1 AND sender_id=$2 AND receiver_id=$3 ORDER BY id DESC LIMIT 1;",
            group_id, sender_id, receiver_id
        )

    if not w:
        await cq.answer("پیام یافت نشد.", show_alert=True)
        return

    if allowed:
        text = w["text"]
        alert_text = text if len(text) <= ALERT_SNIPPET else (text[:ALERT_SNIPPET] + " …")
        await cq.answer(text=alert_text, show_alert=True)

        if len(text) > ALERT_SNIPPET:
            try:
                await context.bot.send_message(user.id, f"متن کامل نجوا:\n{text}")
            except Exception:
                pass

        if w["status"] != "read":
            async with pool.acquire() as con:
                await con.execute("UPDATE whispers SET status='read' WHERE id=$1;", int(w["id"]))
    else:
        await cq.answer("این پیام فقط برای فرستنده و گیرنده قابل نمایش است.", show_alert=True)

# ---------- ارسال همگانی (Forward) ----------
async def do_broadcast(context: ContextTypes.DEFAULT_TYPE, update: Update):
    msg = update.message
    async with pool.acquire() as con:
        user_ids = [int(r["user_id"]) for r in await con.fetch("SELECT user_id FROM users;")]
        group_ids = [int(r["chat_id"]) for r in await con.fetch("SELECT chat_id FROM chats WHERE type IN ('group','supergroup');")]

    total = 0
    for uid in user_ids + group_ids:
        try:
            await context.bot.forward_message(chat_id=uid, from_chat_id=msg.chat_id, message_id=msg.message_id)
            total += 1
            await asyncio.sleep(0.05)
        except Exception:
            continue

    await msg.reply_text(f"ارسال همگانی (Forward) پایان یافت. ({total} مقصد)")

# ---------- هندلرهای پایه دیگر ----------
async def any_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await upsert_chat(update.effective_chat)
        if update.effective_user:
            await upsert_user(update.effective_user)

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

    # گروه: بررسی تریگرها
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.TEXT & (~filters.COMMAND),
        group_trigger
    ))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, any_group_message), group=2)

    # خصوصی
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND), private_text))

    # نمایش نجوا (ریپلای) و اینلاین
    app.add_handler(CallbackQueryHandler(on_show_cb, pattern=r"^show:\-?\d+:\d+:\d+$"))
    app.add_handler(InlineQueryHandler(on_inline_query))
    app.add_handler(CallbackQueryHandler(on_inline_show, pattern=r"^iws:.+"))
    app.add_handler(CallbackQueryHandler(on_checksub_group, pattern=r"^gjchk:\d+:-?\d+:\d+$"))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
