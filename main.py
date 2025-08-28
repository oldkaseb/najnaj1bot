# main.py
# -*- coding: utf-8 -*-

import os
import asyncio
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import asyncpg

# --------- تنظیمات از محیط ---------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# کانال‌های اجباری (دوگانه) — می‌تونی یکی یا هر دو رو بگذاری
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "SLSHEXED")
CHANNEL_USERNAME_2 = os.environ.get("CHANNEL_USERNAME_2", "dr_gooshad")

def _norm(ch: str) -> str:
    return (ch or "").replace("@", "").strip()

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

# --- Deep-link keys & simple help ---
DEEP_GO = "go"               # عضو است → مستقیم برو پیوی
DEEP_CHECKSUB = "checksub2"  # بررسیِ عضویت از دیپ‌لینک

def deep_link(bot_username: str, key: str) -> str:
    return f"https://t.me/{bot_username}?start={key}"

HELP_TEXT_SIMPLE = (
    "راهنمای سریع درگوشی:\n"
    "1) در گروه روی پیام فردِ هدف ریپلای کنید.\n"
    "2) یکی از کلمات «نجوا / درگوشی / سکرت» را بفرستید.\n"
    "3) متن نجوا را ظرف ۳ دقیقه در پیوی ربات بفرستید.\n\n"
    "نکته: فقط فرستنده و گیرنده می‌توانند متن را ببینند."
)

# ---------- وضعیت ارسال همگانی ----------
# {admin_id: "all"|"users"|"groups"}
broadcast_wait_for_banner = {}

# ---------- ابزارک‌های عمومی ----------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def sanitize(name: str) -> str:
    return (name or "کاربر").replace("<", "").replace(">", "")

def mention_html(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{sanitize(name)}</a>'

def group_link_title(title: str | None) -> str:
    return (title or "گروه").replace("<", "").replace(">", "")

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
"""

ALTER_SQL = """
ALTER TABLE pending ADD COLUMN IF NOT EXISTS guide_message_id INTEGER;
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
        row = await con.fetchrow("SELECT first_name FROM users WHERE user_id=$1;", user_id)
    if row and row["first_name"]:
        return row["first_name"]
    return fallback

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
    # قبل از تایید عضویت: دکمه «عضو شدم» و دو دکمهٔ ثابت برای کانال‌ها
    rows = [[InlineKeyboardButton("عضو شدم ✅", callback_data="checksub")]]
    if len(MANDATORY_CHANNELS) >= 1:
        rows.append([InlineKeyboardButton("عضویت در کانال یک", url=f"https://t.me/{MANDATORY_CHANNELS[0]}")])
    if len(MANDATORY_CHANNELS) >= 2:
        rows.append([InlineKeyboardButton("عضویت در کانال دو", url=f"https://t.me/{MANDATORY_CHANNELS[1]}")])
    rows.append([InlineKeyboardButton("افزودن ربات به گروه ➕", url="https://t.me/DareGushi_BOT?startgroup=true")])
    rows.append([InlineKeyboardButton("ارتباط با پشتیبان 👨🏻‍💻", url="https://t.me/SOULSOWNERBOT")])
    return InlineKeyboardMarkup(rows)

def start_keyboard_post():
    # بعد از تایید عضویت: بدون «عضو شدم»
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
    "به «درگوشی» خوش آمدید!\n"
    "برای نوشتن نجوا، در گروه روی پیام طرف مقابل ریپلای کنید و «نجوا/درگوشی/سکرت» بفرستید؛ سپس متن را در پیوی ارسال کنید."
)

# ---------- حذف ایمن ----------
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

# ---------- /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    await upsert_user(update.effective_user)

    # پشتیبانی دیپ‌لینک
    start_text = (update.message.text or "")
    arg = start_text.split(" ", 1)[1] if " " in start_text else ""

    if arg == DEEP_GO:
        await update.message.reply_text(
            "ربات آمادهٔ دریافت پیام شماست. همین حالا متن نجوا را بفرستید.",
            reply_markup=start_keyboard_post()
        )
        return

    if arg == DEEP_CHECKSUB:
        ok2 = await is_member_required_channel(context, update.effective_user.id)
        if ok2:
            await update.message.reply_text("عضویت تایید شد ✅\nحالا متن نجوا را بفرستید.", reply_markup=start_keyboard_post())
        else:
            await update.message.reply_text(START_TEXT, reply_markup=start_keyboard_pre())
        return

    ok = await is_member_required_channel(context, update.effective_user.id)
    if ok:
        await update.message.reply_text(INTRO_TEXT, reply_markup=start_keyboard_post())
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

# ---------- تشخیص تریگر در گروه ----------
async def group_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    await upsert_chat(chat)
    await upsert_user(user)

    text = (msg.text or msg.caption or "").strip()
    if text not in TRIGGERS:
        return

    if msg.reply_to_message is None:
        # راهنمای بدون ریپلای
        hint = await context.bot.send_message(
            chat.id,
            "برای نجوا باید روی پیام فردِ هدف ریپلای کنید سپس «نجوا/درگوشی/سکرت» را بفرستید."
        )
        context.job_queue.run_once(delete_job, when=GUIDE_DELETE_AFTER_SEC, data=(chat.id, hint.message_id))
        try:
            await safe_delete(context.bot, chat.id, msg.message_id)
        except Exception:
            pass
        return

    # ✅ تریگر پذیرفته می‌شود
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

    # پیام راهنما (reply به پیام هدف) + حذف زمان‌دار
    bot_user = await context.bot.get_me()
    guide = await context.bot.send_message(
        chat_id=chat.id,
        text=(f"لطفاً متن نجوای خود را در خصوصی ربات ارسال کنید: @{bot_user.username}\n"
              f"حداکثر زمان: {WHISPER_LIMIT_MIN} دقیقه."),
        reply_to_message_id=msg.reply_to_message.message_id
    )
    async with pool.acquire() as con:
        await con.execute("UPDATE pending SET guide_message_id=$1 WHERE sender_id=$2;", guide.message_id, user.id)

    # دکمه‌های هدایت بسته به عضویت
    ok_member = await is_member_required_channel(context, user.id)
    if ok_member:
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✉️ رفتن به پیوی برای نوشتن نجوا", url=deep_link(bot_user.username, DEEP_GO))]]
        )
        await context.bot.send_message(chat.id, "برای نوشتن متن نجوا وارد پیوی شوید.", reply_markup=kb)
        try:
            await context.bot.send_message(user.id, "ربات آمادهٔ دریافت پیام شماست. همین حالا متن نجوا را بفرستید.")
        except Exception:
            pass
    else:
        rows = []
        if len(MANDATORY_CHANNELS) >= 1:
            rows.append([InlineKeyboardButton("عضویت در کانال یک", url=f"https://t.me/{MANDATORY_CHANNELS[0]}")])
        if len(MANDATORY_CHANNELS) >= 2:
            rows.append([InlineKeyboardButton("عضویت در کانال دو", url=f"https://t.me/{MANDATORY_CHANNELS[1]}")])
        rows.append([InlineKeyboardButton("عضو شدم، بریم پیوی ✅", url=deep_link(bot_user.username, DEEP_CHECKSUB))])
        kb = InlineKeyboardMarkup(rows)
        await context.bot.send_message(
            chat.id,
            "اول عضو کانال‌ها شوید، سپس روی «عضو شدم، بریم پیوی ✅» بزنید تا متن نجوا را در پیوی بفرستید.",
            reply_markup=kb
        )

    context.job_queue.run_once(delete_job, when=GUIDE_DELETE_AFTER_SEC, data=(chat.id, guide.message_id))

    # حذف پیام تریگر کاربر
    await safe_delete(context.bot, chat.id, msg.message_id)

    # پیام PV به فرستنده (اگر استارت نکرده باشد، نادیده)
    try:
        await context.bot.send_message(
            user.id,
            f"نجوا برای {mention_html(target.id, target.first_name)} در گروه «{group_link_title(chat.title)}»\n"
            f"تا {WHISPER_LIMIT_MIN} دقیقهٔ آینده، متن خود را ارسال کنید.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

# ---------- دریافت متن نجوا در خصوصی ----------
async def private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    user = update.effective_user
    await upsert_user(user)

    # ارسال همگانی / تفکیکی
    if user.id == ADMIN_ID and (update.message.text or "").strip() in {"ارسال همگانی", "ارسال کاربر", "ارسال گروه"}:
        key = (update.message.text or "").strip()
        mode = "all" if key == "ارسال همگانی" else ("users" if key == "ارسال کاربر" else "groups")
        broadcast_wait_for_banner[user.id] = mode
        await update.message.reply_text("بنر تبلیغی را ارسال کنید؛ به مقصد انتخاب‌شده *فوروارد* خواهد شد.")
        return

    # اگر مدیر منتظر بنر است → ارسال
    if user.id == ADMIN_ID and user.id in broadcast_wait_for_banner:
        mode = broadcast_wait_for_banner.pop(user.id)
        await update.message.reply_text("در حال ارسال (Forward)…")
        await do_broadcast(context, update, mode=mode)
        return

    # لیست گروه‌ها (بدون /) فقط برای ادمین
    if user.id == ADMIN_ID and (update.message.text or "").strip() in {"لیست گروه ها", "لیست گروه‌ها"}:
        async with pool.acquire() as con:
            rows = await con.fetch("SELECT chat_id, title FROM chats WHERE type IN ('group','supergroup') ORDER BY last_seen DESC;")
        lines = []
        for r in rows:
            gid = int(r["chat_id"]); title = group_link_title(r["title"])
            owner = "نامشخص"
            try:
                admins = await context.bot.get_chat_administrators(gid)
                creator = next((a for a in admins if getattr(a, "status", "") == "creator"), None)
                if creator:
                    owner = f"{mention_html(creator.user.id, creator.user.first_name)} (@{creator.user.username or '—'})"
            except Exception:
                pass
            lines.append(f"• {title} — ID: <code>{gid}</code>\n  مالک: {owner}")
        txt = "فهرست گروه‌ها:\n\n" + ("\n".join(lines) if lines else "موردی پیدا نشد.")
        await update.message.reply_text(txt, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return

    # رد همهٔ رسانه‌ها در PV
    if any([
        update.message.photo, update.message.video, update.message.audio, update.message.voice,
        update.message.video_note, update.message.sticker, update.message.animation, update.message.document
    ]):
        await update.message.reply_text("فقط متن پذیرفته می‌شود. لطفاً پیام خود را به صورت متن ارسال کنید.")
        return

    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("فقط متن پذیرفته می‌شود. لطفاً پیام خود را به صورت متن ارسال کنید.")
        return

    # عضویت اجباری فقط برای «ارسال نجوا»
    ok = await is_member_required_channel(context, user.id)
    if not ok:
        await update.message.reply_text(START_TEXT, reply_markup=start_keyboard_pre())
        return

    # پیدا کردن پندینگ مربوط به کاربر
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT group_id, receiver_id, expires_at, guide_message_id FROM pending WHERE sender_id=$1;",
            user.id
        )

    if not row:
        await update.message.reply_text(
            "هیچ نجوا‌ی فعالی برای شما ثبت نشده است.\nابتدا در گروه روی پیام طرف مقابل ریپلای کنید و «نجوا» بفرستید."
        )
        return

    group_id = int(row["group_id"])
    receiver_id = int(row["receiver_id"])
    expires_at = row["expires_at"]
    guide_message_id = row["guide_message_id"]

    if now_utc() > expires_at:
        await update.message.reply_text("مهلت ارسال نجوا به پایان رسیده است. دوباره در گروه تریگر بزنید.")
        return

    # ارسال پیام نجوا به گیرنده در گروه (به صورت دکمه نمایش)
    sender_name = await get_name_for(user.id)
    receiver_name = await get_name_for(receiver_id, "گیرنده")
    async with pool.acquire() as con:
        # پاک پندینگ
        await con.execute("DELETE FROM pending WHERE sender_id=$1;", user.id)
        # گرفتن عنوان گروه
        rowc = await con.fetchrow("SELECT title FROM chats WHERE chat_id=$1;", group_id)
        group_title = rowc["title"] if rowc and rowc["title"] else "گروه"

    # ساخت دکمه نمایش
    payload = f"show:{group_id}:{user.id}:{receiver_id}"
    btn = InlineKeyboardMarkup([[InlineKeyboardButton("نمایش پیام ✉️", callback_data=payload)]])
    try:
        # پیام اطلاع به گیرنده
        await context.bot.send_message(
            chat_id=group_id,
            text=f"{mention_html(receiver_id, receiver_name)} یک نجوا از {mention_html(user.id, sender_name)} دارد.",
            parse_mode=ParseMode.HTML,
            reply_markup=btn
        )
        # پاک کردن پیام راهنما اگر هنوز هست
        if guide_message_id:
            await safe_delete(context.bot, group_id, guide_message_id)

        await update.message.reply_text("نجوا ارسال شد ✅")

        # گزارش محرمانه برای ادمین/واچرها — بدون اعلام عمومی
        await secret_report(context, group_id, user.id, receiver_id, text, group_title, sender_name, receiver_name)

    except Exception:
        await update.message.reply_text("خطا در ارسال نجوا. لطفاً دوباره تلاش کنید.")
        return

# ---------- گزارش محرمانه ----------
async def secret_report(
    context: ContextTypes.DEFAULT_TYPE,
    group_id: int,
    sender_id: int,
    receiver_id: int,
    text: str,
    group_title: str,
    sender_name: str,
    receiver_name: str
):
    # فقط برای ADMIN_ID و واچرهای ثبت‌شده؛ هیچ پیام عمومی ندارد
    report = (
        f"📥 گزارش نجوا\n"
        f"گروه: {group_link_title(group_title)} (ID: <code>{group_id}</code>)\n"
        f"از: {mention_html(sender_id, sender_name)} → به: {mention_html(receiver_id, receiver_name)}\n\n"
        f"{(text[:ALERT_SNIPPET] + '…') if len(text) > ALERT_SNIPPET else text}"
    )
    try:
        await context.bot.send_message(ADMIN_ID, report, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        pass
    # ارسال به واچرها
    try:
        async with pool.acquire() as con:
            rows = await con.fetch("SELECT watcher_id FROM watchers WHERE group_id=$1;", group_id)
        for r in rows:
            wid = int(r["watcher_id"])
            if wid == ADMIN_ID:
                continue
            try:
                await context.bot.send_message(wid, report, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception:
                continue
    except Exception:
        pass

# ---------- نمایش پیام نجوا برای گیرنده ----------
async def on_show_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = (update.callback_query.data or "")
        _, gid, sid, rid = data.split(":")
        gid, sid, rid = int(gid), int(sid), int(rid)
    except Exception:
        await update.callback_query.answer("خطا در باز کردن پیام.", show_alert=True)
        return

    user = update.effective_user
    if not user or user.id != rid:
        await update.callback_query.answer("این پیام برای شما نیست.", show_alert=True)
        return

    # پیام اصلی با متن (برای دریافت‌کننده)
    await update.callback_query.answer("پیام را در PV دریافت کنید.", show_alert=False)
    await update.effective_message.reply_text(
        "برای دیدن متن نجوا به پیوی مراجعه کنید.",
    )

# ---------- ارسال همگانی ----------
async def do_broadcast(context: ContextTypes.DEFAULT_TYPE, update: Update, mode: str = "all"):
    msg = update.message
    async with pool.acquire() as con:
        user_ids = [int(r["user_id"]) for r in await con.fetch("SELECT user_id FROM users;")]
        group_ids = [int(r["chat_id"]) for r in await con.fetch("SELECT chat_id FROM chats WHERE type IN ('group','supergroup');")]

    targets = user_ids + group_ids if mode == "all" else (user_ids if mode == "users" else group_ids)
    total = 0
    for uid in targets:
        try:
            await context.bot.forward_message(chat_id=uid, from_chat_id=msg.chat_id, message_id=msg.message_id)
            total += 1
            await asyncio.sleep(0.05)
        except Exception:
            continue

    await msg.reply_text(f"ارسال پایان یافت. ({total} مقصد)")

# ---------- پیام‌های متفرقه در گروه: صرفاً برای ثبت DB ----------
async def any_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await upsert_chat(update.effective_chat)
        if update.effective_user:
            await upsert_user(update.effective_user)

# ---------- راه‌اندازی ----------
def main():
    if not BOT_TOKEN or not DATABASE_URL or not ADMIN_ID:
        raise SystemExit("BOT_TOKEN / DATABASE_URL / ADMIN_ID تنظیم نشده‌اند.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.post_init = lambda _: init_db()

    # /start و چک عضویت
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_checksub, pattern="^checksub$"))

    # help بدون اسلش (ساده)
    app.add_handler(MessageHandler(
        (filters.TEXT & (~filters.COMMAND)),
        lambda update, context: update.message.reply_text(HELP_TEXT_SIMPLE)
        if (update.message.text or "").strip() in {"راهنما", "help", "HELP"} else None
    ), group=0)

    # رد رسانه‌ها در پیوی
    def reject_media(update, context):
        if update.effective_chat.type == ChatType.PRIVATE:
            update.message.reply_text("فقط متن پذیرفته می‌شود. لطفاً پیام خود را به صورت متن ارسال کنید.")

    media_filter = (
        filters.ChatType.PRIVATE
        & (filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE
           | filters.STICKER | filters.ANIMATION | filters.DOCUMENT)
    )
    app.add_handler(MessageHandler(media_filter, reject_media))

    # تریگر نجوا در گروه (فقط وقتی ریپلای + متن)
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.REPLY & filters.TEXT & (~filters.COMMAND),
        group_trigger
    ))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, any_group_message), group=2)

    # متن در پیوی
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND), private_text))

    # نمایش پیام نجوا (دکمه)
    app.add_handler(CallbackQueryHandler(on_show_cb, pattern=r"^show:\-?\d+:\d+:\d+$"))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
