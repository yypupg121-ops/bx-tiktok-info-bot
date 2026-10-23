import asyncio
import html
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, BufferedInputFile, MenuButtonWebApp,
)
from aiogram.exceptions import TelegramBadRequest

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL = os.getenv("REQUIRED_CHANNEL", "@bxcode").strip()
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/bxcode").strip()
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
API_URL = os.getenv("TIKTOK_API_URL", "https://tiktokinfov10.itfreeforever.workers.dev").strip()
DB_PATH = os.getenv("DATABASE_PATH", "bx_tiktok.sqlite3")
BOT_NAME = os.getenv("BOT_NAME", "BX TikTok Info")
if not BOT_TOKEN:
    raise RuntimeError("ضع BOT_TOKEN كمتغير بيئة قبل تشغيل البوت")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bxbot")
router = Router()
lookup_locks: dict[int, float] = {}
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{2,30}$")

# Telegram button style colors require a Bot API / client version that supports them.
def button(text: str, callback_data: str | None = None, *, url: str | None = None,
           web_app: WebAppInfo | None = None, style: str | None = None):
    args = {"text": text}
    if callback_data is not None:
        args["callback_data"] = callback_data
    if url is not None:
        args["url"] = url
    if web_app is not None:
        args["web_app"] = web_app
    if style:
        args["style"] = style
    try:
        return InlineKeyboardButton(**args)
    except (TypeError, ValueError):
        # Compatibility fallback for older aiogram models; color styling is best-effort.
        args.pop("style", None)
        return InlineKeyboardButton(**args)


def home_keyboard():
    rows = [
        [button("🔎 فحص حساب", "search", style="primary"), button("📚 شرح البوت", "help", style="primary")],
        [button("⭐ المفضلة", "favorites", style="success"), button("📊 إحصائياتي", "mystats")],
    ]
    if MINI_APP_URL.startswith("https://"):
        rows.insert(0, [button("✨ افتح Mini App", web_app=WebAppInfo(url=MINI_APP_URL), style="success")])
    rows.append([button("📢 قناة BXCODE", url=CHANNEL_URL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("📢 اشترك بالقناة", url=CHANNEL_URL, style="primary")],
        [button("✅ تحقّق من الاشتراك", "check_sub", style="success")],
    ])


def profile_keyboard(username: str, is_favorite: bool = False):
    username = html.escape(username, quote=True)
    rows = [
        [button("👤 فتح تيك توك", url=f"https://www.tiktok.com/@{username}"), button("🔄 تحديث", f"refresh:{username}", style="primary")],
        [button("📄 التقرير TXT", f"report:{username}"), button("⭐ إزالة من المفضلة" if is_favorite else "⭐ أضف للمفضلة", f"fav:{username}", style="success")],
        [button("🔎 فحص حساب آخر", "search", style="primary"), button("🏠 الرئيسية", "home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT NOT NULL DEFAULT '',
            joined_at TEXT NOT NULL, last_seen TEXT NOT NULL, searches INTEGER NOT NULL DEFAULT 0)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS favorites (
            user_id INTEGER NOT NULL, username TEXT NOT NULL, added_at TEXT NOT NULL,
            PRIMARY KEY(user_id, username))""")
        await db.execute("""CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            username TEXT NOT NULL, created_at TEXT NOT NULL)""")
        await db.commit()


async def touch_user(user):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""INSERT INTO users(user_id, username, first_name, joined_at, last_seen)
            VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
            first_name=excluded.first_name,last_seen=excluded.last_seen""",
            (user.id, user.username, user.first_name or "", now, now))
        await db.commit()


async def is_subscribed(user_id: int):
    try:
        member = await bot.get_chat_member(CHANNEL, user_id)
        return member.status not in ("left", "kicked")
    except Exception:
        log.exception("Subscription check failed; confirm bot is admin/member of %s", CHANNEL)
        return None


async def allowed(message: Message):
    status = await is_subscribed(message.from_user.id)
    if status is True:
        return True
    if status is None:
        await message.answer("⚠️ ما قدرت أتحقق من الاشتراك. تأكد أن البوت يستطيع رؤية أعضاء القناة وأن اسم القناة صحيح.")
    else:
        await message.answer("🔒 هذه الخدمة متاحة بعد الاشتراك بالقناة المطلوبة:", reply_markup=subscription_keyboard())
    return False


def normalize_username(raw: str):
    username = (raw or "").strip().lstrip("@").strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("أرسل اسم مستخدم صحيحًا من 2 إلى 30 حرفًا، بدون رابط أو مسافات.")
    return username


async def fetch_profile(username: str):
    timeout = aiohttp.ClientTimeout(total=25, connect=8)
    headers = {"Accept": "application/json", "User-Agent": "BX-TikTok-Info/2.0", "Origin": "https://fintok.neocities.org", "Referer": "https://fintok.neocities.org/"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(API_URL, params={"username": username}) as response:
            response.raise_for_status()
            result = await response.json(content_type=None)
    if not isinstance(result, dict) or result.get("status") not in (None, "ok") or not isinstance(result.get("data"), dict):
        raise ValueError("خدمة البيانات أعادت نتيجة غير صالحة")
    return result["data"]


def value(data, key, fallback="غير متوفر"):
    v = data.get(key)
    return fallback if v is None or v == "" else str(v)


def yes_no(v):
    s = str(v).lower()
    return "نعم ✅" if s in ("yes", "true", "1") else "لا ❌" if s in ("no", "false", "0") else "غير متوفر"


def format_profile(p, username):
    name = html.escape(value(p, "fullname"))
    user = html.escape(value(p, "username", username))
    bio = html.escape(value(p, "signature", value(p, "bio", "لا يوجد")))
    followers = html.escape(value(p, "followers"))
    following = html.escape(value(p, "following"))
    likes = html.escape(value(p, "likes", value(p, "heart_count")))
    videos = html.escape(value(p, "video"))
    verified = html.escape(yes_no(p.get("verified")))
    private = html.escape(yes_no(p.get("private")))
    return (
        "╭━━━━━━━ <b>BX TIKTOK</b> ━━━━━━━╮\n"
        f"👤 <b>الاسم:</b> {name}\n🔗 <b>المستخدم:</b> @{user}\n"
        f"🆔 <b>المعرّف:</b> <code>{html.escape(value(p, 'userid'))}</code>\n\n"
        "<blockquote>📈 <b>الإحصائيات</b>\n"
        f"👥 المتابعون: {followers}\n➡️ يتابع: {following}\n❤️ الإعجابات: {likes}\n🎬 الفيديوهات: {videos}</blockquote>\n"
        f"<blockquote>🛡️ موثق: {verified} | خاص: {private}</blockquote>\n"
        f"📝 <b>النبذة:</b>\n<blockquote>{bio}</blockquote>\n"
        "<i>المعلومات عامة وتعرض كما توفرها خدمة البيانات الخارجية، وقد لا تكون لحظية أو مكتملة.</i>"
    )


def make_report(p, username):
    fields = [
        ("اسم المستخدم", value(p, "username", username)), ("المعرّف", value(p, "userid")),
        ("الاسم", value(p, "fullname")), ("المتابعون", value(p, "followers")),
        ("يتابع", value(p, "following")), ("الأصدقاء", value(p, "friends")),
        ("الإعجابات", value(p, "likes", value(p, "heart_count"))), ("الفيديوهات", value(p, "video")),
        ("تاريخ الإنشاء", value(p, "created_time")), ("الدولة", value(p, "region")),
        ("اللغة", value(p, "language")), ("موثق", yes_no(p.get("verified"))),
        ("خاص", yes_no(p.get("private"))), ("البايو", value(p, "signature", value(p, "bio", "لا يوجد"))),
        ("الرابط", f"https://www.tiktok.com/@{username}"),
    ]
    return "BX TIKTOK INFO — تقرير عام\n" + "=" * 36 + "\n" + "\n".join(f"{k}: {v}" for k, v in fields)


async def send_profile(chat_id: int, user_id: int, username: str):
    now = time.monotonic()
    if now - lookup_locks.get(user_id, 0) < 3:
        await bot.send_message(chat_id, "⏱️ انتظر قليلًا قبل طلب فحص جديد.")
        return
    lookup_locks[user_id] = now
    progress = await bot.send_message(chat_id, "⏳ جاري جلب المعلومات العامة...")
    try:
        p = await fetch_profile(username)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO searches(user_id,username,created_at) VALUES(?,?,?)", (user_id, username, datetime.now(timezone.utc).isoformat()))
            await db.execute("UPDATE users SET searches=searches+1 WHERE user_id=?", (user_id,))
            cur = await db.execute("SELECT 1 FROM favorites WHERE user_id=? AND username=?", (user_id, username))
            favorite = await cur.fetchone() is not None
            await db.commit()
        try:
            await progress.delete()
        except TelegramBadRequest:
            pass
        caption = format_profile(p, username)
        markup = profile_keyboard(username, favorite)
        image_url = p.get("profile")
        sent = False
        if isinstance(image_url, str) and image_url.startswith("https://"):
            try:
                timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(image_url) as response:
                        response.raise_for_status()
                        payload = await response.read()
                if len(payload) <= 8 * 1024 * 1024:
                    await bot.send_photo(chat_id, BufferedInputFile(payload, filename="tiktok_profile.jpg"), caption=caption, reply_markup=markup)
                    sent = True
            except Exception:
                log.info("Profile image unavailable for @%s", username)
        if not sent:
            await bot.send_message(chat_id, caption, reply_markup=markup, disable_web_page_preview=True)
        await bot.send_document(chat_id, BufferedInputFile(make_report(p, username).encode("utf-8"), filename=f"tiktok_{username}_report.txt"), caption="📄 تقرير نصي قابل للتنزيل")
    except asyncio.TimeoutError:
        await progress.edit_text("⌛ انتهت مهلة خدمة المعلومات. جرّب لاحقًا.")
    except aiohttp.ClientResponseError as e:
        await progress.edit_text("❌ تعذر العثور على الحساب أو أن الخدمة غير متاحة الآن." if e.status == 404 else f"❌ خدمة المعلومات ردّت برمز {e.status}.")
    except Exception:
        log.exception("Profile lookup failed for @%s", username)
        try:
            await progress.edit_text("❌ تعذر جلب الحساب الآن. الخدمة الخارجية قد تكون متوقفة؛ جرّب لاحقًا.")
        except Exception:
            pass


@router.message(CommandStart())
async def start(message: Message):
    await touch_user(message.from_user)
    if not await allowed(message):
        return
    await message.answer(
        "╭━━━━━━━━━━━━━━━━━━━━╮\n<b>⚡ أهلاً بك في BX TikTok Info</b>\n╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "🔎 معلومات عامة لحسابات تيك توك\n📊 إحصائيات وملف TXT\n⭐ حفظ الحسابات المفضلة\n✨ Mini App للبحث السريع\n\n"
        "اختر خدمة من القائمة. لا يطلب البوت كلمة مرور تيك توك ولا يصل إلى الحسابات الخاصة.", reply_markup=home_keyboard())


@router.message(Command("help"))
async def help_cmd(message: Message):
    await touch_user(message.from_user)
    await message.answer("<b>طريقة الاستخدام</b>\n• اضغط فحص حساب وأرسل اسم المستخدم بدون @.\n• /start لفتح القائمة.\n• /privacy لمعرفة الخصوصية.\n\nالبيانات العامة مصدرها API خارجي وقد تتأخر أو تتوقف.", reply_markup=home_keyboard())


@router.message(Command("privacy"))
async def privacy_cmd(message: Message):
    await message.answer("🔐 <b>الخصوصية</b>\nنحفظ معرّف تيليجرام واسم المستخدم وعداد البحث والمفضلة لتشغيل ميزات البوت. لا نطلب بيانات دخول تيك توك. استخدم /delete_me لحذف بياناتك المحفوظة.")


@router.message(Command("delete_me"))
async def delete_me(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM favorites WHERE user_id=?", (message.from_user.id,))
        await db.execute("DELETE FROM searches WHERE user_id=?", (message.from_user.id,))
        await db.execute("DELETE FROM users WHERE user_id=?", (message.from_user.id,))
        await db.commit()
    await message.answer("تم حذف بياناتك المحفوظة من قاعدة بيانات البوت. أرسل /start للبدء من جديد.")


@router.message(F.web_app_data)
async def miniapp_data(message: Message):
    try:
        data = json.loads(message.web_app_data.data)
        username = normalize_username(data.get("username", ""))
    except Exception:
        await message.answer("❌ تعذر قراءة اسم المستخدم من Mini App.")
        return
    await touch_user(message.from_user)
    if await allowed(message):
        await send_profile(message.chat.id, message.from_user.id, username)


@router.message(F.text)
async def username_message(message: Message):
    if message.text.startswith("/"):
        return
    try:
        username = normalize_username(message.text)
    except ValueError:
        await message.answer("أرسل اسم مستخدم تيك توك فقط، مثال: <code>tiktok</code> أو اضغط فحص حساب.")
        return
    await touch_user(message.from_user)
    if await allowed(message):
        await send_profile(message.chat.id, message.from_user.id, username)


@router.callback_query()
async def callbacks(call: CallbackQuery):
    action = call.data or ""
    user = call.from_user
    try:
        if action == "check_sub":
            status = await is_subscribed(user.id)
            await call.answer("تم الاشتراك والتحقق ✅" if status else "لم يظهر اشتراكك بعد؛ اشترك ثم أعد التحقق.", show_alert=status is not True)
            if status:
                await call.message.answer("أهلًا بك! اختر إحدى الخدمات:", reply_markup=home_keyboard())
            return
        if action.startswith(("search", "help", "home", "favorites", "mystats", "refresh:", "report:", "fav:")) and not await is_subscribed(user.id):
            await call.answer("الاشتراك بالقناة مطلوب أولًا", show_alert=True)
            await call.message.answer("🔒 اشترك ثم أعد المحاولة:", reply_markup=subscription_keyboard())
            return
        await call.answer()
        if action == "home":
            await call.message.answer("🏠 <b>القائمة الرئيسية</b>", reply_markup=home_keyboard())
        elif action == "search":
            await call.message.answer("🔎 أرسل يوزر تيك توك بدون @، مثال: <code>tiktok</code>\nأو افتح Mini App من القائمة.")
        elif action == "help":
            await call.message.answer("📚 أرسل اسم المستخدم فقط؛ لا ترسل كلمة مرور أو معلومات دخول. الأرقام تقريبية حسب API خارجي.", reply_markup=home_keyboard())
        elif action == "mystats":
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT searches FROM users WHERE user_id=?", (user.id,))
                row = await cur.fetchone()
            await call.message.answer(f"📊 عدد عمليات الفحص المسجلة لحسابك: <b>{row[0] if row else 0}</b>")
        elif action == "favorites":
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT username FROM favorites WHERE user_id=? ORDER BY added_at DESC LIMIT 20", (user.id,))
                rows = await cur.fetchall()
            if not rows:
                await call.message.answer("قائمة المفضلة فارغة. احفظ حسابًا بعد فحصه ⭐")
            else:
                kb = InlineKeyboardMarkup(inline_keyboard=[[button("@" + r[0], f"refresh:{r[0]}")] for r in rows] + [[button("🏠 الرئيسية", "home")]])
                await call.message.answer("⭐ <b>حساباتك المفضلة</b>", reply_markup=kb)
        elif action.startswith("fav:"):
            username = normalize_username(action.split(":", 1)[1])
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT 1 FROM favorites WHERE user_id=? AND username=?", (user.id, username))
                exists = await cur.fetchone()
                if exists:
                    await db.execute("DELETE FROM favorites WHERE user_id=? AND username=?", (user.id, username))
                    answer = f"تمت إزالة @{username} من المفضلة."
                else:
                    await db.execute("INSERT OR IGNORE INTO favorites(user_id,username,added_at) VALUES(?,?,?)", (user.id, username, datetime.now(timezone.utc).isoformat()))
                    answer = f"تم حفظ @{username} في المفضلة ⭐"
                await db.commit()
            await call.message.answer(answer)
        elif action.startswith("refresh:") or action.startswith("report:"):
            mode, username = action.split(":", 1)
            username = normalize_username(username)
            if mode == "refresh":
                await send_profile(call.message.chat.id, user.id, username)
            else:
                p = await fetch_profile(username)
                await bot.send_document(call.message.chat.id, BufferedInputFile(make_report(p, username).encode("utf-8"), filename=f"tiktok_{username}_report.txt"), caption="📄 تقرير نصي")
    except Exception:
        log.exception("Callback failed: %s", action)
        await call.message.answer("❌ حدث خطأ أثناء تنفيذ الطلب. حاول مرة أخرى.")


async def main():
    global bot
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    if MINI_APP_URL.startswith("https://"):
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="فتح Mini App", web_app=WebAppInfo(url=MINI_APP_URL)))
        except Exception:
            log.exception("Could not set bot menu button; configure it manually in BotFather if needed")
    log.info("Starting %s; required channel=%s; mini app configured=%s", BOT_NAME, CHANNEL, bool(MINI_APP_URL))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
