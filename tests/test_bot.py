import os
import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

os.environ.setdefault("BOT_TOKEN", "123456:test-token")

import aiosqlite
import bot
from aiohttp.test_utils import TestClient, TestServer


class FakeMessage:
    def __init__(self):
        self.text = None
        self.reply_markup = None

    async def edit_text(self, text, **kwargs):
        self.text = text
        self.reply_markup = kwargs.get("reply_markup")


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        bot.DB_PATH = str(Path(self.temp.name) / "test.sqlite3")

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_legacy_database_migration_and_user_isolation(self):
        async with aiosqlite.connect(bot.DB_PATH) as db:
            await db.execute("""CREATE TABLE users (
                user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT NOT NULL DEFAULT '',
                joined_at TEXT NOT NULL, last_seen TEXT NOT NULL, searches INTEGER NOT NULL DEFAULT 0)""")
            await db.execute("""CREATE TABLE favorites (
                user_id INTEGER NOT NULL, username TEXT NOT NULL, added_at TEXT NOT NULL,
                PRIMARY KEY(user_id, username))""")
            await db.execute("""CREATE TABLE searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                username TEXT NOT NULL, created_at TEXT NOT NULL)""")
            await db.commit()

        await bot.init_db()
        user1 = SimpleNamespace(id=101, username="user_one", first_name="One")
        user2 = SimpleNamespace(id=202, username="user_two", first_name="Two")
        await bot.touch_user(user1)
        await bot.touch_user(user2)
        await bot.record_search(101, "FirstAccount")
        await bot.record_search(101, "second_account")

        async with aiosqlite.connect(bot.DB_PATH) as db:
            columns = await (await db.execute("PRAGMA table_info(favorites)")).fetchall()
            rows = await (await db.execute("SELECT user_id, searches FROM users ORDER BY user_id")).fetchall()
            history1 = await (await db.execute(
                "SELECT username FROM searches WHERE user_id=? ORDER BY id DESC", (101,)
            )).fetchall()
            history2 = await (await db.execute(
                "SELECT username FROM searches WHERE user_id=?", (202,)
            )).fetchall()
        self.assertIn("display_name", {column[1] for column in columns})
        self.assertEqual(rows, [(101, 2), (202, 0)])
        self.assertEqual(history1, [("second_account",), ("firstaccount",)])
        self.assertEqual(history2, [])

    async def test_profile_format_and_plain_text_report(self):
        profile = {
            "fullname": "اسم <تجريبي>", "username": "sample", "userid": "987",
            "region": "IQ", "language": "ar", "followers": 123, "following": 45,
            "likes": 678, "video": 9, "private": False, "signature": "",
            "verified": True, "friends": 12, "created_time": "2020-01-01",
            "bio_link": "https://example.test",
        }
        rendered = bot.format_profile(profile, "sample")
        expected_order = [
            "<blockquote><b>BX TikTok</b></blockquote>", "الاسم:", "اليوزر:", "الايدي:",
            "الدولة:", "اللغة:", "المتابعين:", "يتابع:", "اللايكات:", "الفيديوهات:",
            "الحساب:", "النبذه:", "معلومات إضافية",
        ]
        positions = [rendered.index(value) for value in expected_order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("&lt;تجريبي&gt;", rendered)
        self.assertIn("الحساب:</b> عام", rendered)
        self.assertIn("النبذه:</b> ماكو", rendered)
        self.assertNotIn("المعلومات عامة وتعرض", rendered)

        report = bot.make_report(profile, "sample")
        self.assertTrue(report.startswith("الاسم:"))
        self.assertIn("المتابعين: 123", report)
        self.assertIn("التوثيق: موثق", report)
        self.assertNotIn("BX TIKTOK INFO", report)
        self.assertNotIn("المعلومات عامة وتعرض", report)

    async def test_keyboard_order_and_danger_style(self):
        rows = bot.home_keyboard().inline_keyboard
        self.assertEqual([button.text for button in rows[0]], ["🔎 بحث", "⭐ المفضلة"])
        self.assertEqual([button.text for button in rows[1]], ["🔴 إحصائياتي", "📖 شرح البوت"])
        self.assertEqual(rows[1][0].style, "danger")
        self.assertEqual(rows[-1][0].text, "📢 قناة BXCODE")
        self.assertEqual(bot.API_URL, "https://tiktokinfov10.itfreeforever.workers.dev")
        self.assertEqual(bot.CHANNEL, "@bxcode")

    async def test_stats_show_only_owners_last_ten_searches(self):
        await bot.init_db()
        user1 = SimpleNamespace(id=101, username="one", first_name="One")
        user2 = SimpleNamespace(id=202, username="two", first_name="Two")
        await bot.touch_user(user1)
        await bot.touch_user(user2)
        for index in range(12):
            await bot.record_search(101, f"account{index}")
        message = FakeMessage()
        await bot.show_stats(message, 101)
        self.assertIn('المفضلات مالاتك : "0"', message.text)
        self.assertIn('بحوثاتك كلهن : "12"', message.text)
        self.assertIn("1. @account11", message.text)
        self.assertIn("10. @account2", message.text)
        self.assertNotIn("@account1</", message.text)

        empty_message = FakeMessage()
        await bot.show_stats(empty_message, 202)
        self.assertIn("بعدك ماباحث عن اي حساب حب", empty_message.text)
        self.assertIn("مفضلاتك فارغة حب", empty_message.text)

    async def test_favorites_list_is_user_scoped_and_has_actions(self):
        await bot.init_db()
        async with aiosqlite.connect(bot.DB_PATH) as db:
            await db.executemany(
                "INSERT INTO favorites(user_id,username,added_at,display_name) VALUES(?,?,?,?)",
                [
                    (101, "first_user", "2026-01-01", "First Name"),
                    (202, "other_user", "2026-01-02", "Other Name"),
                ],
            )
            await db.commit()
        message = FakeMessage()
        await bot.show_favorites(message, 101)
        self.assertIn("@first_user — First Name", message.text)
        self.assertNotIn("other_user", message.text)
        callbacks = [button.callback_data for row in message.reply_markup.inline_keyboard for button in row]
        self.assertIn("fopen:first_user", callbacks)
        self.assertIn("favadd", callbacks)
        self.assertIn("favdel", callbacks)
        self.assertIn("home", callbacks)

    async def test_username_validation(self):
        self.assertEqual(bot.normalize_username("@abc_123"), "abc_123")
        for invalid in ("https://tiktok.com/@abc", "@@abc", "a", "bad name"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    bot.normalize_username(invalid)


def signed_init_data(user: dict, auth_date: int) -> str:
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAExampleQuery",
        "user": json.dumps(user, separators=(",", ":"), ensure_ascii=False),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot.BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class MiniAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        bot.DB_PATH = str(Path(self.temp.name) / "miniapp.sqlite3")
        await bot.init_db()
        bot.last_lookup.clear()
        bot.active_lookups.clear()
        self.auth_patch = patch.object(bot, "verify_webapp_init_data", return_value={
            "id": 303, "username": "mini_user", "first_name": "Mini",
        })
        self.subscription_patch = patch.object(bot, "is_subscribed", new_callable=AsyncMock, return_value=True)
        self.auth_patch.start()
        self.subscription_patch.start()
        self.client = TestClient(TestServer(bot.create_web_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.auth_patch.stop()
        self.subscription_patch.stop()
        self.temp.cleanup()

    def headers(self):
        return {"Authorization": "tma test-init-data"}

    async def test_signed_telegram_init_data_and_expiration(self):
        now = int(time.time())
        data = signed_init_data({"id": 303, "first_name": "Mini"}, now)
        self.auth_patch.stop()
        try:
            self.assertEqual(bot.verify_webapp_init_data(data, now=now)["id"], 303)
            self.assertIsNone(bot.verify_webapp_init_data(data + "x", now=now))
            self.assertIsNone(bot.verify_webapp_init_data(data, now=now + bot.WEBAPP_AUTH_MAX_AGE + 5))
        finally:
            self.auth_patch.start()

    async def test_web_app_serves_custom_dashboard_and_health(self):
        page = await self.client.get("/")
        self.assertEqual(page.status, 200)
        self.assertIn("BX SIGNAL", await page.text())
        health = await self.client.get("/healthz")
        self.assertEqual(await health.json(), {"status": "ok", "service": "bx-tiktok-miniapp"})

    async def test_api_requires_telegram_init_data(self):
        response = await self.client.get("/api/stats")
        self.assertEqual(response.status, 401)

    async def test_lookup_stats_and_favorites_are_user_scoped(self):
        profile_data = {
            "username": "sample", "fullname": "Sample User", "userid": "987",
            "followers": 123, "following": 45, "likes": 678, "video": 9,
            "private": False, "signature": "Bio", "verified": True,
            "email": "must-not-be-returned", "phone": "must-not-be-returned",
        }
        with patch.object(bot, "fetch_profile", new_callable=AsyncMock, return_value=profile_data):
            response = await self.client.post("/api/lookup", json={"username": "sample"}, headers=self.headers())
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["profile"]["username"], "sample")
        self.assertTrue(body["profile"]["verified"] == "موثق")
        self.assertNotIn("email", body["profile"])
        self.assertNotIn("phone", body["profile"])

        response = await self.client.post("/api/favorites", json={"username": "sample"}, headers=self.headers())
        self.assertEqual(response.status, 200)
        favorites = await self.client.get("/api/favorites", headers=self.headers())
        self.assertEqual((await favorites.json())["favorites"], [{"username": "sample", "display_name": ""}])

        stats = await self.client.get("/api/stats", headers=self.headers())
        self.assertEqual(await stats.json(), {"searches": 1, "favorites": 1, "recent": ["sample"]})

        removed = await self.client.delete("/api/favorites/sample", headers=self.headers())
        self.assertTrue((await removed.json())["ok"])
        stats = await self.client.get("/api/stats", headers=self.headers())
        self.assertEqual((await stats.json())["favorites"], 0)


if __name__ == "__main__":
    unittest.main()
