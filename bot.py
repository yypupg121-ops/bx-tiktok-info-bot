import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import aiohttp
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL = os.getenv("REQUIRED_CHANNEL", "@bxcode").strip()
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/bxcode").strip()
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
API_URL = os.getenv("TIKTOK_API_URL", "https://tiktokinfov10.itfreeforever.workers.dev").strip()
DB_PATH = os.getenv("DATABASE_PATH", "bx_tiktok.sqlite3")
BOT_NAME = os.getenv("BOT_NAME", "BX TikTok Info")
if not BOT_TOKEN:
    raise RuntimeError("ضع BOT_TOKEN كمتغير بيئة قبل تشغيل البوت")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bxbot")
router = Router()
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{2,30}$")
LOOKUP_COOLDOWN = 3
pending_actions: dict[int, tuple[int, Message, str]] = {}
last_lookup: dict[int, float] = {}
active_lookups: set[int] = set()
bot: Bot
WEBAPP_AUTH_MAX_AGE = 24 * 60 * 60


def button(text: str, callback_data: str | None = None, *, url: str | None = None,
           web_app: WebAppInfo | None = None, style: str | None = None) -> InlineKeyboardButton:
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
        args.pop("style", None)
        return InlineKeyboardButton(**args)


def home_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [button("🔎 بحث", "search", style="primary"), button("⭐ المفضلة", "favorites", style="success")],
        [button("🔴 إحصائياتي", "mystats", style="danger"), button("📖 شرح البوت", "help", style="primary")],
    ]
    if MINI_APP_URL.startswith("https://"):
        rows.append([button("✨ افتح Mini App", web_app=WebAppInfo(url=MINI_APP_URL), style="success")])
    rows.append([button("📢 قناة BXCODE", url=CHANNEL_URL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("📢 اشترك بالقناة", url=CHANNEL_URL, style="primary")],
        [button("✅ تحقّق من الاشتراك", "check_sub", style="success")],
    ])


def back_keyboard(destination: str = "home", label: str = "⬅️ رجوع") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[button(label, destination)]])


def profile_keyboard(username: str, is_favorite: bool = False) -> InlineKeyboardMarkup:
    safe_username = html.escape(username, quote=True)
    rows = [
        [button("👤 فتح تيك توك", url=f"https://www.tiktok.com/@{safe_username}"),
         button("🔄 تحديث", f"refresh:{username}", style="primary")],
        [button("📄 ملف TXT", f"report:{username}"),
         button("⭐ إزالة من المفضلة" if is_favorite else "⭐ أضف للمفضلة", f"fav:{username}", style="success")],
        [button("🔎 بحث", "search", style="primary"), button("🏠 الرئيسية", "home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pending_keyboard() -> InlineKeyboardMarkup:
    return back_keyboard("cancel", "إلغاء والرجوع")


async def init_db() -> None:
    db_parent = Path(DB_PATH).expanduser().parent
    db_parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT NOT NULL DEFAULT '',
            joined_at TEXT NOT NULL, last_seen TEXT NOT NULL, searches INTEGER NOT NULL DEFAULT 0)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS favorites (
            user_id INTEGER NOT NULL, username TEXT NOT NULL, added_at TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '', PRIMARY KEY(user_id, username))""")
        columns = await (await db.execute("PRAGMA table_info(favorites)")).fetchall()
        if "display_name" not in {column[1] for column in columns}:
            await db.execute("ALTER TABLE favorites ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
        await db.execute("""CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            username TEXT NOT NULL, created_at TEXT NOT NULL)""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_searches_user_id ON searches(user_id, id DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_favorites_user_id ON favorites(user_id, added_at DESC)")
        await db.commit()


async def touch_user(user) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""INSERT INTO users(user_id, username, first_name, joined_at, last_seen)
            VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
            first_name=excluded.first_name,last_seen=excluded.last_seen""",
            (user.id, user.username, user.first_name or "", now, now))
        await db.commit()


async def is_subscribed(user_id: int) -> bool | None:
    try:
        member = await bot.get_chat_member(CHANNEL, user_id)
        return member.status not in ("left", "kicked")
    except Exception:
        log.exception("Subscription check failed; confirm bot is admin/member of %s", CHANNEL)
        return None


async def allowed(message: Message) -> bool:
    status = await is_subscribed(message.from_user.id)
    if status is True:
        return True
    if status is None:
        await message.answer("⚠️ ما قدرت أتحقق من الاشتراك. تأكد أن البوت يگدر يشوف أعضاء القناة واسم القناة صحيح.")
    else:
        await message.answer("🔒 اشترك بالقناة أول وبعدين ارجع:", reply_markup=subscription_keyboard())
    return False


def normalize_username(raw: str) -> str:
    username = (raw or "").strip()
    if username.startswith("@"):
        username = username[1:]
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("دز يوزر تيك توك صحيح، بدون رابط أو مسافات.")
    return username


def normalized_key(username: str) -> str:
    return username.casefold()


async def fetch_profile(username: str) -> dict:
    timeout = aiohttp.ClientTimeout(total=25, connect=8)
    headers = {
        "Accept": "application/json",
        "User-Agent": "BX-TikTok-Info/2.0",
        "Origin": "https://fintok.neocities.org",
        "Referer": "https://fintok.neocities.org/",
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(API_URL, params={"username": username}) as response:
            response.raise_for_status()
            result = await response.json(content_type=None)
    if not isinstance(result, dict) or result.get("status") not in (None, "ok") or not isinstance(result.get("data"), dict):
        raise ValueError("خدمة البيانات رجّعت نتيجة غير صالحة")
    return result["data"]


def raw_value(data: dict, *keys: str):
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def display_value(data: dict, *keys: str, fallback: str = "ماكو") -> str:
    value = raw_value(data, *keys)
    return fallback if value is None else str(value)


def truth_value(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        cleaned = value.strip().casefold()
        if cleaned in {"true", "1", "yes", "private", "on"}:
            return True
        if cleaned in {"false", "0", "no", "public", "off"}:
            return False
    return None


def account_privacy(data: dict) -> str:
    private = truth_value(raw_value(data, "private", "is_private"))
    if private is None:
        return "ماكو"
    return "خاص" if private else "عام"


def verification_label(data: dict) -> str | None:
    raw = raw_value(data, "verified", "is_verified")
    verified = truth_value(raw)
    if verified is None:
        return None
    return "موثق" if verified else "مو موثق"


def additional_fields(data: dict) -> list[tuple[str, str]]:
    candidates = [
        ("التوثيق", ("verified", "is_verified"), "verified"),
        ("الأصدقاء", ("friends", "friend_count"), "text"),
        ("تاريخ إنشاء الحساب", ("created_time", "create_time", "created_at"), "text"),
        ("آخر تغيير للاسم", ("last_change_name",), "text"),
        ("رابط النبذة", ("bio_link", "bio_url", "external_url"), "text"),
    ]
    extras = []
    for label, keys, kind in candidates:
        value = raw_value(data, *keys)
        if value is None:
            continue
        if kind == "verified":
            rendered = verification_label(data)
            if rendered:
                extras.append((label, rendered))
        else:
            extras.append((label, str(value)))
    return extras


def _safe_html(value, limit: int | None = None) -> str:
    text = str(value)
    if limit is not None and len(text) > limit:
        text = text[:limit - 1] + "…"
    return html.escape(text, quote=True)


def format_profile(data: dict, username: str) -> str:
    name = _safe_html(display_value(data, "fullname", "nickname"), 100)
    shown_username = _safe_html(display_value(data, "username", fallback=username), 30)
    user_id = _safe_html(display_value(data, "userid", "user_id", "id"), 50)
    country = _safe_html(display_value(data, "region", "country"), 50)
    language = _safe_html(display_value(data, "language", "lang"), 50)
    followers = _safe_html(display_value(data, "followers", "follower_count"), 50)
    following = _safe_html(display_value(data, "following", "following_count"), 50)
    likes = _safe_html(display_value(data, "likes", "heart_count", "total_favorited"), 50)
    videos = _safe_html(display_value(data, "video", "videos", "video_count"), 50)
    bio = _safe_html(display_value(data, "signature", "bio"), 700)
    lines = [
        "<blockquote><b>BX TikTok</b></blockquote>",
        f"👤 <b>الاسم:</b> {name}",
        f"🔗 <b>اليوزر:</b> @{shown_username}",
        f"🆔 <b>الايدي:</b> <code>{user_id}</code>",
        f"<b>الدولة:</b> {country}",
        f"<b>اللغة:</b> {language}",
        "",
        "<b>الإحصائيات</b>",
        f"👥 <b>المتابعين:</b> {followers}",
        f"➡️ <b>يتابع:</b> {following}",
        f"❤️ <b>اللايكات:</b> {likes}",
        f"🎬 <b>الفيديوهات:</b> {videos}",
        "",
        f"<b>الحساب:</b> {_safe_html(account_privacy(data))}",
        f"<b>النبذه:</b> {bio or 'ماكو'}",
    ]
    extras = additional_fields(data)
    if extras:
        lines.extend(["", "<b>معلومات إضافية</b>"])
        lines.extend(f"• <b>{_safe_html(label)}:</b> {_safe_html(value, 350)}" for label, value in extras)
    return "\n".join(lines)


def make_report(data: dict, username: str) -> str:
    fields = [
        ("الاسم", display_value(data, "fullname", "nickname")),
        ("اليوزر", "@" + display_value(data, "username", fallback=username)),
        ("الايدي", display_value(data, "userid", "user_id", "id")),
        ("الدولة", display_value(data, "region", "country")),
        ("اللغة", display_value(data, "language", "lang")),
        ("المتابعين", display_value(data, "followers", "follower_count")),
        ("يتابع", display_value(data, "following", "following_count")),
        ("اللايكات", display_value(data, "likes", "heart_count", "total_favorited")),
        ("الفيديوهات", display_value(data, "video", "videos", "video_count")),
        ("الحساب", account_privacy(data)),
        ("النبذه", display_value(data, "signature", "bio")),
    ]
    fields.extend(additional_fields(data))
    fields.append(("رابط الحساب", f"https://www.tiktok.com/@{username}"))
    return "\n".join(f"{label}: {value}" for label, value in fields)


async def edit_message(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        return
    except TelegramBadRequest as exc:
        error = str(exc).casefold()
        if "message is not modified" in error:
            return
        if "there is no text in the message to edit" in error or "message can't be edited" in error:
            try:
                await message.edit_caption(caption=text, reply_markup=markup)
                return
            except TelegramBadRequest as caption_exc:
                if "message is not modified" in str(caption_exc).casefold():
                    return
        log.info("Could not edit Telegram message; sending replacement: %s", exc)
    await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


async def edit_media_or_text(message: Message, username: str, data: dict, caption: str,
                             markup: InlineKeyboardMarkup) -> None:
    image_url = data.get("profile")
    if isinstance(image_url, str) and image_url.startswith("https://"):
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(image_url) as response:
                    response.raise_for_status()
                    payload = await response.read()
            if len(payload) <= 8 * 1024 * 1024:
                media = InputMediaPhoto(media=BufferedInputFile(payload, filename="tiktok_profile.jpg"),
                                        caption=caption, parse_mode=ParseMode.HTML)
                try:
                    await message.edit_media(media=media, reply_markup=markup)
                    return
                except TelegramBadRequest as exc:
                    if "message is not modified" in str(exc).casefold():
                        return
                    log.info("Could not replace message with profile photo for @%s", username)
        except Exception:
            log.info("Profile image unavailable for @%s", username)
    await edit_message(message, caption, markup)


async def record_search(user_id: int, username: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO searches(user_id,username,created_at) VALUES(?,?,?)",
                         (user_id, normalized_key(username), datetime.now(timezone.utc).isoformat()))
        await db.execute("UPDATE users SET searches=searches+1 WHERE user_id=?", (user_id,))
        await db.commit()


async def is_favorite(user_id: int, username: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM favorites WHERE user_id=? AND lower(username)=lower(?) LIMIT 1",
                                  (user_id, username))
        return await cursor.fetchone() is not None


async def save_profile_name(user_id: int, username: str, data: dict) -> bool:
    display_name = raw_value(data, "fullname", "nickname")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM favorites WHERE user_id=? AND lower(username)=lower(?) LIMIT 1",
                                  (user_id, username))
        exists = await cursor.fetchone() is not None
        if exists and display_name:
            await db.execute("UPDATE favorites SET display_name=? WHERE user_id=? AND lower(username)=lower(?)",
                             (str(display_name)[:80], user_id, username))
        await db.commit()
    return exists


async def send_document(chat_id: int, data: dict, username: str) -> None:
    content = make_report(data, username).encode("utf-8")
    await bot.send_document(chat_id, BufferedInputFile(content, filename="BX@BXCODE.txt"),
                            caption="📄 معلومات الحساب بملف TXT")


async def send_profile(chat_id: int, user_id: int, username: str, *,
                       target_message: Message | None = None, count_search: bool = True) -> None:
    now = time.monotonic()
    if user_id in active_lookups:
        if target_message:
            await edit_message(target_message, "⏳ بعدني أجيب معلومات الحساب، انتظر شوي حب.", pending_keyboard())
        else:
            await bot.send_message(chat_id, "⏳ بعدني أجيب معلومات الحساب، انتظر شوي حب.")
        return
    if count_search and now - last_lookup.get(user_id, 0) < LOOKUP_COOLDOWN:
        if target_message:
            await edit_message(target_message, "⏱️ انتظر شوي وبعدين جرّب تبحث مرة ثانية.", home_keyboard())
        else:
            await bot.send_message(chat_id, "⏱️ انتظر شوي وبعدين جرّب تبحث مرة ثانية.")
        return

    active_lookups.add(user_id)
    if count_search:
        last_lookup[user_id] = now
    progress: Message | None = None
    try:
        if target_message is None:
            progress = await bot.send_message(chat_id, "⏳ دا أجيب معلومات الحساب حب...")
        else:
            await edit_message(target_message, "⏳ دا أجيب معلومات الحساب حب...", None)
        data = await fetch_profile(username)
        if count_search:
            await record_search(user_id, username)
        favorite = await save_profile_name(user_id, username, data)
        result_text = format_profile(data, username)
        markup = profile_keyboard(username, favorite)
        if target_message is not None:
            await edit_media_or_text(target_message, username, data, result_text, markup)
        else:
            sent_photo = False
            image_url = data.get("profile")
            if isinstance(image_url, str) and image_url.startswith("https://"):
                try:
                    timeout = aiohttp.ClientTimeout(total=15)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.get(image_url) as response:
                            response.raise_for_status()
                            payload = await response.read()
                    if len(payload) <= 8 * 1024 * 1024:
                        await bot.send_photo(chat_id, BufferedInputFile(payload, filename="tiktok_profile.jpg"),
                                             caption=result_text, reply_markup=markup)
                        sent_photo = True
                except Exception:
                    log.info("Profile image unavailable for @%s", username)
            if not sent_photo:
                await bot.send_message(chat_id, result_text, reply_markup=markup,
                                       disable_web_page_preview=True)
        if progress:
            try:
                await progress.delete()
            except TelegramBadRequest:
                pass
        try:
            await send_document(chat_id, data, username)
        except Exception:
            log.exception("Could not send TXT report for @%s", username)
    except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
        await _show_lookup_error(chat_id, target_message, progress, "⌛ الخدمة طولت بالرد، جرّب بعدين حب.")
    except aiohttp.ClientResponseError as exc:
        message = "❌ ما لگيت الحساب أو خدمة المعلومات متوقفة هسه." if exc.status == 404 else "❌ صار خلل بخدمة المعلومات، جرّب بعدين حب."
        await _show_lookup_error(chat_id, target_message, progress, message)
    except aiohttp.ClientError:
        log.exception("Network error while looking up @%s", username)
        await _show_lookup_error(chat_id, target_message, progress, "❌ ما گدرت أجيب المعلومات هسه، جرّب بعدين حب.")
    except Exception:
        log.exception("Profile lookup failed for @%s", username)
        await _show_lookup_error(chat_id, target_message, progress, "❌ ما گدرت أجيب المعلومات هسه، جرّب بعدين حب.")
    finally:
        active_lookups.discard(user_id)


async def _show_lookup_error(chat_id: int, target: Message | None, progress: Message | None, text: str) -> None:
    destination = target or progress
    if destination:
        await edit_message(destination, text, home_keyboard())
    else:
        await bot.send_message(chat_id, text, reply_markup=home_keyboard())


async def show_stats(message: Message, user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT searches FROM users WHERE user_id=?", (user_id,))
        user_row = await cursor.fetchone()
        cursor = await db.execute("SELECT COUNT(*) FROM favorites WHERE user_id=?", (user_id,))
        favorites_count = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT username FROM searches WHERE user_id=? ORDER BY id DESC LIMIT 10", (user_id,))
        recent = await cursor.fetchall()
    search_count = user_row[0] if user_row else 0
    lines = [
        "هاك احصائياتك حب",
        f'المفضلات مالاتك : "{favorites_count}"',
        f'بحوثاتك كلهن : "{search_count}"',
        "اخر 10 حسابات بحثت عليهم🫆:",
    ]
    if recent:
        quoted = [f"{index}. @{html.escape(row[0])}" for index, row in enumerate(recent, 1)]
        lines.append("<blockquote>🫆 آخر الحسابات اللي بحثت عنها\n" + "\n".join(quoted) + "</blockquote>")
    else:
        lines.append("بعدك ماباحث عن اي حساب حب، دوس بحث وجرب ❤️")
    if favorites_count == 0:
        lines.extend(["", "مفضلاتك فارغة حب، ضيفلك حساب يعجبك ⭐"])
    await edit_message(message, "\n".join(lines), back_keyboard())


FAVORITES_PER_PAGE = 12


async def show_favorites(message: Message, user_id: int, page: int = 0, *, deleting: bool = False) -> None:
    page = max(0, page)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT username,display_name FROM favorites WHERE user_id=? ORDER BY added_at DESC, username LIMIT ? OFFSET ?",
                                  (user_id, FAVORITES_PER_PAGE, page * FAVORITES_PER_PAGE))
        rows = await cursor.fetchall()
        cursor = await db.execute("SELECT COUNT(*) FROM favorites WHERE user_id=?", (user_id,))
        total = (await cursor.fetchone())[0]
    if deleting:
        title = "🗑 اختار المفضلة اللي تريد تحذفها حب:"
    else:
        title = "⭐ مفضلاتك حب:"
    lines = [title]
    keyboard_rows = []
    if not rows:
        lines.append("ما عندك حسابات مفضلة حب، ضيفلك واحد ⭐")
    else:
        for username, display_name in rows:
            shown = f"@{username}"
            if display_name:
                shown += f" — {display_name[:80]}"
            lines.append("• " + html.escape(shown))
            label = ("🗑 @" if deleting else "🔎 @") + username
            callback = f"fdelask:{username}:{page}" if deleting else f"fopen:{username}"
            keyboard_rows.append([button(label[:60], callback)])
    pages = max(1, (total + FAVORITES_PER_PAGE - 1) // FAVORITES_PER_PAGE)
    if pages > 1:
        page_row = []
        if page > 0:
            page_row.append(button("⬅️ السابق", f"{'favdelpage' if deleting else 'favpage'}:{page - 1}"))
        page_row.append(button(f"{page + 1}/{pages}", "noop"))
        if page + 1 < pages:
            page_row.append(button("التالي ➡️", f"{'favdelpage' if deleting else 'favpage'}:{page + 1}"))
        keyboard_rows.append(page_row)
    if deleting:
        keyboard_rows.append([button("⬅️ رجوع للمفضلة", "favorites")])
    else:
        keyboard_rows.extend([
            [button("➕ إضافة مفضلة", "favadd", style="success"), button("🗑 حذف مفضلة", "favdel", style="danger")],
            [button("🏠 الرئيسية", "home")],
        ])
    await edit_message(message, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard_rows))


async def _set_pending(message: Message, user_id: int, mode: str, text: str) -> None:
    pending_actions[user_id] = (message.chat.id, message, mode)
    await edit_message(message, text, pending_keyboard())


async def _handle_pending(message: Message, mode: str, target: Message) -> None:
    user_id = message.from_user.id
    try:
        username = normalize_username(message.text or "")
    except ValueError as exc:
        await edit_message(target, f"❌ {html.escape(str(exc))}\n\nجرّب مرة ثانية حب.", pending_keyboard())
        return
    await touch_user(message.from_user)
    if not await allowed(message):
        return
    if mode == "search":
        pending_actions.pop(user_id, None)
        await send_profile(target.chat.id, user_id, username, target_message=target, count_search=True)
        return
    try:
        data = await fetch_profile(username)
    except (asyncio.TimeoutError, aiohttp.ClientError, ValueError):
        log.exception("Could not verify favorite @%s", username)
        await edit_message(target, "❌ ما گدرت أتأكد من الحساب هسه. جرّب بعدين حب.", pending_keyboard())
        return
    name = str(raw_value(data, "fullname", "nickname") or "")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM favorites WHERE user_id=? AND lower(username)=lower(?) LIMIT 1",
                                  (user_id, username))
        exists = await cursor.fetchone() is not None
        if not exists:
            await db.execute("INSERT INTO favorites(user_id,username,added_at,display_name) VALUES(?,?,?,?)",
                             (user_id, normalized_key(username), datetime.now(timezone.utc).isoformat(), name[:80]))
            await db.commit()
    pending_actions.pop(user_id, None)
    text = "هالحساب موجود بالمفضلة من قبل حب ⭐" if exists else "تمت الإضافة للمفضلة حب ⭐"
    await edit_message(target, text, back_keyboard("favorites", "⭐ للمفضلة"))


@router.message(CommandStart())
async def start(message: Message) -> None:
    await touch_user(message.from_user)
    if not await allowed(message):
        return
    await message.answer("هلا بيك حب، اختار شتريد من القائمة 👇", reply_markup=home_keyboard())


@router.message(Command("help"))
async def help_cmd(message: Message) -> None:
    await touch_user(message.from_user)
    if not await allowed(message):
        return
    await message.answer(
        "هلا بيك حياتي مايرادلها شي دوس بحث\n\n"
        "ودز يوزر الحساب وحيطلعلك معلوماته\n\n"
        "وانشاء الله قريبا ح نضيف بلبوت قائمه المتابعين واليتابعهم تابعونا @bxcode",
        reply_markup=home_keyboard(),
    )


@router.message(Command("privacy"))
async def privacy(message: Message) -> None:
    await message.answer("🔐 نخزن سجل بحثك ومفضلاتك حتى تظل موجودة بعد ما يطفي البوت. تگدر تمسح بياناتك بأمر /delete_me.")


@router.message(Command("delete_me"))
async def delete_me(message: Message) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM favorites WHERE user_id=?", (message.from_user.id,))
        await db.execute("DELETE FROM searches WHERE user_id=?", (message.from_user.id,))
        await db.execute("DELETE FROM users WHERE user_id=?", (message.from_user.id,))
        await db.commit()
    pending_actions.pop(message.from_user.id, None)
    await message.answer("انمسحت بياناتك حب. أرسل /start حتى ترجع تستخدم البوت.")


@router.message(F.web_app_data)
async def miniapp_data(message: Message) -> None:
    try:
        data = json.loads(message.web_app_data.data)
        username = normalize_username(data.get("username", ""))
    except Exception:
        await message.answer("❌ ما گدرت أقرا اليوزر من Mini App.")
        return
    await touch_user(message.from_user)
    if await allowed(message):
        await send_profile(message.chat.id, message.from_user.id, username)


def verify_webapp_init_data(init_data: str, *, now: int | None = None) -> dict | None:
    """Verify Telegram Mini App initData using the Bot API HMAC procedure."""
    if not init_data or len(init_data) > 8192:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        values = dict(pairs)
        if len(values) != len(pairs):
            return None
        received_hash = values.pop("hash", "")
        auth_date = int(values.get("auth_date", "0"))
        current = int(time.time()) if now is None else now
        if not received_hash or auth_date > current + 60 or current - auth_date > WEBAPP_AUTH_MAX_AGE:
            return None
        data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None
        user = json.loads(values.get("user", "{}"))
        if not isinstance(user, dict) or not isinstance(user.get("id"), int) or isinstance(user.get("id"), bool):
            return None
        return user
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


async def miniapp_identity(request: web.Request) -> tuple[dict | None, web.Response | None]:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("tma "):
        return None, web.json_response({"error": "افتح Mini App من داخل تيليجرام."}, status=401)
    user = verify_webapp_init_data(authorization[4:].strip())
    if not user:
        return None, web.json_response({"error": "انتهت جلسة تيليجرام أو بياناتها غير صالحة. سكّر التطبيق وافتحه من البوت."}, status=401)
    status = await is_subscribed(user["id"])
    if status is None:
        return None, web.json_response({"error": "ما گدرت أتحقق من اشتراكك هسه. جرّب بعد شوي."}, status=503)
    if not status:
        return None, web.json_response({"error": "اشترك بقناة BXCODE وبعدين ارجع افتح التطبيق.", "subscribe_url": CHANNEL_URL}, status=403)
    await touch_user(SimpleNamespace(
        id=user["id"], username=user.get("username"), first_name=user.get("first_name") or "",
    ))
    return user, None


def web_profile_payload(data: dict, username: str, is_favorite: bool) -> dict:
    def scalar(*keys: str, fallback: str = ""):
        value = raw_value(data, *keys)
        if value is None:
            return fallback
        if isinstance(value, (str, int, float, bool)):
            return value if not isinstance(value, str) else value[:700]
        return str(value)[:700]

    avatar = raw_value(data, "profile", "avatar", "avatar_url")
    if not isinstance(avatar, str) or not avatar.startswith("https://"):
        avatar = ""
    else:
        parsed = urlsplit(avatar)
        host = (parsed.hostname or "").casefold()
        if not host.endswith((".tiktokcdn.com", ".tiktokcdn-us.com", ".ibytedtos.com")):
            avatar = ""
    return {
        "username": str(raw_value(data, "username") or username)[:30],
        "fullname": str(raw_value(data, "fullname", "nickname") or username)[:100],
        "userid": str(raw_value(data, "userid", "user_id", "id") or "")[:50],
        "region": str(raw_value(data, "region", "country") or "")[:50],
        "language": str(raw_value(data, "language", "lang") or "")[:50],
        "followers": scalar("followers", "follower_count", fallback="0"),
        "following": scalar("following", "following_count", fallback="0"),
        "likes": scalar("likes", "heart_count", "total_favorited", fallback="0"),
        "videos": scalar("video", "videos", "video_count", fallback="0"),
        "privacy": account_privacy(data),
        "bio": str(raw_value(data, "signature", "bio") or "")[:700],
        "verified": verification_label(data) or "غير متوفر",
        "avatar": avatar,
        "profile_url": f"https://www.tiktok.com/@{username}",
        "is_favorite": is_favorite,
    }


async def webapp_index(request: web.Request) -> web.StreamResponse:
    index_path = Path(__file__).resolve().parent / "miniapp" / "index.html"
    if not index_path.is_file():
        return web.Response(text="Mini App file is missing.", status=503)
    return web.FileResponse(index_path, headers={"Cache-Control": "no-cache"})


async def webapp_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "bx-tiktok-miniapp"})


async def webapp_lookup(request: web.Request) -> web.Response:
    user, error = await miniapp_identity(request)
    if error:
        return error
    try:
        payload = await request.json()
        username = normalize_username(payload.get("username", ""))
    except (ValueError, TypeError, json.JSONDecodeError):
        return web.json_response({"error": "دز يوزر تيك توك صحيح، مثل tiktok."}, status=400)
    user_id = user["id"]
    now = time.monotonic()
    if user_id in active_lookups:
        return web.json_response({"error": "بعدني أجيب نتيجة البحث السابق. انتظر شوي."}, status=409)
    if now - last_lookup.get(user_id, 0) < LOOKUP_COOLDOWN:
        return web.json_response({"error": "انتظر ثواني وبعدين ابحث مرة ثانية."}, status=429)
    active_lookups.add(user_id)
    try:
        data = await fetch_profile(username)
        await record_search(user_id, username)
        favorite = await save_profile_name(user_id, username, data)
        return web.json_response({"profile": web_profile_payload(data, username, favorite)})
    except aiohttp.ClientResponseError as exc:
        status = 404 if exc.status == 404 else 502
        message = "ما لگيت الحساب." if status == 404 else "خدمة معلومات الحساب ما جاوبت هسه. جرّب بعدين."
        return web.json_response({"error": message}, status=status)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        log.exception("Mini App lookup failed for @%s", username)
        return web.json_response({"error": "ما گدرت أجيب معلومات الحساب هسه. جرّب بعدين."}, status=502)
    except Exception:
        log.exception("Unexpected Mini App lookup error")
        return web.json_response({"error": "صار خلل مؤقت بالبحث. جرّب بعدين."}, status=500)
    finally:
        last_lookup[user_id] = time.monotonic()
        active_lookups.discard(user_id)


async def webapp_favorites(request: web.Request) -> web.Response:
    user, error = await miniapp_identity(request)
    if error:
        return error
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT username,display_name FROM favorites WHERE user_id=? ORDER BY added_at DESC, username LIMIT 100",
            (user["id"],),
        )
        rows = await cursor.fetchall()
    return web.json_response({"favorites": [{"username": name, "display_name": display or ""} for name, display in rows]})


async def webapp_add_favorite(request: web.Request) -> web.Response:
    user, error = await miniapp_identity(request)
    if error:
        return error
    try:
        payload = await request.json()
        username = normalize_username(payload.get("username", ""))
    except (ValueError, TypeError, json.JSONDecodeError):
        return web.json_response({"error": "دز يوزر صحيح حتى نضيفه للمفضلة."}, status=400)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO favorites(user_id,username,added_at,display_name) VALUES(?,?,?,?)",
            (user["id"], normalized_key(username), datetime.now(timezone.utc).isoformat(), ""),
        )
        await db.commit()
    return web.json_response({"ok": True, "username": username, "is_favorite": True})


async def webapp_remove_favorite(request: web.Request) -> web.Response:
    user, error = await miniapp_identity(request)
    if error:
        return error
    try:
        username = normalize_username(request.match_info["username"])
    except ValueError:
        return web.json_response({"error": "اسم الحساب مو صحيح."}, status=400)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM favorites WHERE user_id=? AND lower(username)=lower(?)", (user["id"], username))
        await db.commit()
    return web.json_response({"ok": True, "username": username, "is_favorite": False})


async def webapp_stats(request: web.Request) -> web.Response:
    user, error = await miniapp_identity(request)
    if error:
        return error
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT searches FROM users WHERE user_id=?", (user["id"],))
        user_row = await cursor.fetchone()
        cursor = await db.execute("SELECT COUNT(*) FROM favorites WHERE user_id=?", (user["id"],))
        favorites_count = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT username FROM searches WHERE user_id=? ORDER BY id DESC LIMIT 10", (user["id"],))
        recent = [row[0] for row in await cursor.fetchall()]
    return web.json_response({
        "searches": user_row[0] if user_row else 0,
        "favorites": favorites_count,
        "recent": recent,
    })


def create_web_app() -> web.Application:
    app = web.Application(client_max_size=16 * 1024)
    app.router.add_get("/", webapp_index)
    app.router.add_get("/healthz", webapp_health)
    app.router.add_post("/api/lookup", webapp_lookup)
    app.router.add_get("/api/favorites", webapp_favorites)
    app.router.add_post("/api/favorites", webapp_add_favorite)
    app.router.add_delete("/api/favorites/{username}", webapp_remove_favorite)
    app.router.add_get("/api/stats", webapp_stats)
    return app


@router.message(F.text)
async def text_message(message: Message) -> None:
    if not message.text or message.text.startswith("/"):
        return
    user_id = message.from_user.id
    pending = pending_actions.get(user_id)
    if pending:
        chat_id, target, mode = pending
        if message.chat.id == chat_id:
            await _handle_pending(message, mode, target)
            return
    if not USERNAME_RE.fullmatch(message.text.strip().removeprefix("@").strip()):
        await message.answer("دز يوزر تيك توك فقط، مثال: <code>tiktok</code>، أو دوس زر 🔎 بحث.")
        return
    try:
        username = normalize_username(message.text)
    except ValueError:
        await message.answer("دز يوزر تيك توك صحيح حب.")
        return
    await touch_user(message.from_user)
    if await allowed(message):
        await send_profile(message.chat.id, user_id, username)


@router.callback_query()
async def callbacks(call: CallbackQuery) -> None:
    action = call.data or ""
    user = call.from_user
    message = call.message
    if message is None or not isinstance(message, Message):
        await call.answer("ما گدرت أتعامل ويا هالرسالة.", show_alert=True)
        return
    try:
        if action == "check_sub":
            status = await is_subscribed(user.id)
            await call.answer("تم الاشتراك حب ✅" if status else "اشترك بالقناة وبعدين تحقق حب.", show_alert=status is not True)
            if status:
                await edit_message(message, "هلا بيك حب، اختار شتريد من القائمة 👇", home_keyboard())
            return
        protected = {
            "search", "help", "home", "favorites", "mystats", "favadd", "favdel", "cancel",
            "favpage", "favdelpage", "fdelask", "fdelok", "fdelno", "fopen", "refresh", "report", "fav",
        }
        if any(action == item or action.startswith(item + ":") for item in protected):
            status = await is_subscribed(user.id)
            if status is not True:
                await call.answer("الاشتراك بقناة @bxcode مطلوب أول حب", show_alert=True)
                await edit_message(message, "🔒 اشترك بالقناة وبعدين ارجع:", subscription_keyboard())
                return
        if action == "noop":
            await call.answer()
            return
        if not action.startswith("fav:"):
            await call.answer()
        if action == "home":
            pending_actions.pop(user.id, None)
            await edit_message(message, "هلا بيك حب، اختار شتريد من القائمة 👇", home_keyboard())
        elif action == "search":
            await _set_pending(message, user.id, "search", "🔎 دز يوزر تيك توك حب، ويا أو بدون @.")
        elif action == "help":
            await edit_message(
                message,
                "هلا بيك حياتي مايرادلها شي دوس بحث\n\n"
                "ودز يوزر الحساب وحيطلعلك معلوماته\n\n"
                "وانشاء الله قريبا ح نضيف بلبوت قائمه المتابعين واليتابعهم تابعونا @bxcode",
                back_keyboard(),
            )
        elif action == "mystats":
            await show_stats(message, user.id)
        elif action == "favorites":
            pending_actions.pop(user.id, None)
            await show_favorites(message, user.id)
        elif action == "favadd":
            await _set_pending(message, user.id, "favadd", "➕ دز يوزر الحساب اللي تريد تضيفه للمفضلة حب.")
        elif action == "favdel":
            await show_favorites(message, user.id, deleting=True)
        elif action.startswith("favpage:"):
            await show_favorites(message, user.id, int(action.split(":", 1)[1]))
        elif action.startswith("favdelpage:"):
            await show_favorites(message, user.id, int(action.split(":", 1)[1]), deleting=True)
        elif action.startswith("fopen:"):
            username = normalize_username(action.split(":", 1)[1])
            await send_profile(message.chat.id, user.id, username, target_message=message, count_search=False)
        elif action.startswith("fdelask:"):
            _, username, page_text = action.split(":", 2)
            username = normalize_username(username)
            if not await is_favorite(user.id, username):
                await call.answer("هالحساب مو بقائمة مفضلاتك حب.", show_alert=True)
                return
            await edit_message(
                message,
                f"متأكد تريد تحذف @{html.escape(username)} من المفضلة حب؟",
                InlineKeyboardMarkup(inline_keyboard=[
                    [button("إي احذفها", f"fdelok:{username}:{int(page_text)}", style="danger"),
                     button("لا رجّعني", f"fdelno:{int(page_text)}")],
                ]),
            )
        elif action.startswith("fdelok:"):
            _, username, page_text = action.split(":", 2)
            username = normalize_username(username)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM favorites WHERE user_id=? AND lower(username)=lower(?)", (user.id, username))
                await db.commit()
            await show_favorites(message, user.id, int(page_text))
        elif action.startswith("fdelno:"):
            await show_favorites(message, user.id, int(action.split(":", 1)[1]), deleting=True)
        elif action.startswith("fav:"):
            username = normalize_username(action.split(":", 1)[1])
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute("SELECT 1 FROM favorites WHERE user_id=? AND lower(username)=lower(?) LIMIT 1",
                                          (user.id, username))
                exists = await cursor.fetchone() is not None
                if exists:
                    await db.execute("DELETE FROM favorites WHERE user_id=? AND lower(username)=lower(?)", (user.id, username))
                    text = f"انحذف @{html.escape(username)} من المفضلة حب."
                else:
                    await db.execute("INSERT OR IGNORE INTO favorites(user_id,username,added_at,display_name) VALUES(?,?,?,?)",
                                     (user.id, normalized_key(username), datetime.now(timezone.utc).isoformat(), ""))
                    text = "تمت الإضافة للمفضلة حب ⭐"
                await db.commit()
            await call.answer(text, show_alert=False)
            try:
                await message.edit_reply_markup(reply_markup=profile_keyboard(username, not exists))
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).casefold():
                    log.info("Could not update favorite button: %s", exc)
        elif action.startswith("refresh:"):
            username = normalize_username(action.split(":", 1)[1])
            await send_profile(message.chat.id, user.id, username, target_message=message, count_search=False)
        elif action.startswith("report:"):
            username = normalize_username(action.split(":", 1)[1])
            data = await fetch_profile(username)
            await send_document(message.chat.id, data, username)
        else:
            await call.answer("هالزر ما عاد شغال، دوس رجوع للقائمة.", show_alert=True)
    except Exception:
        log.exception("Callback failed for user %s, action=%s", user.id, action)
        try:
            await edit_message(message, "❌ صار خطأ هسه، جرّب مرة ثانية حب.", home_keyboard())
        except Exception:
            pass


async def main() -> None:
    global bot
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    if MINI_APP_URL.startswith("https://"):
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text="فتح Mini App", web_app=WebAppInfo(url=MINI_APP_URL)))
        except Exception:
            log.exception("Could not set bot menu button; configure it manually in BotFather if needed")
    app_runner = web.AppRunner(create_web_app())
    try:
        await app_runner.setup()
        port = int(os.getenv("PORT", "8000"))
        site = web.TCPSite(app_runner, host="0.0.0.0", port=port)
        await site.start()
        log.info("Starting %s; required channel=%s; mini app configured=%s; web port=%s",
                 BOT_NAME, CHANNEL, bool(MINI_APP_URL), port)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await app_runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
