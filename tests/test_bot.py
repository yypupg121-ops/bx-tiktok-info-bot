import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "123456:test-token")

import aiosqlite
import bot


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
            "الحساب:", "النبذه:", "معلومات إضافية", "🔗 الربط -",
        ]
        positions = [rendered.index(value) for value in expected_order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("&lt;تجريبي&gt;", rendered)
        self.assertIn("الحساب:</b> عام", rendered)
        self.assertIn("النبذه:</b> ماكو", rendered)
        self.assertIn("مصدر الفحص ما يرجّع معلومات موثوقة", rendered)
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
        self.assertEqual([button.text for button in rows[2]], ["🔎 كشف الرابط", "⬇️ تحميل مقاطع"])
        self.assertEqual(rows[2][1].callback_data, "download")
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
        self.assertIn('⭐ المفضلات: "0"', message.text)
        self.assertIn('🔎 عدد البحوث: "12"', message.text)
        self.assertIn("1. @account11", message.text)
        self.assertIn("10. @account2", message.text)
        self.assertNotIn("@account1</", message.text)

        empty_message = FakeMessage()
        await bot.show_stats(empty_message, 202)
        self.assertIn("لحد هسه ما بحثت عن حساب", empty_message.text)
        self.assertIn("قائمة المفضلة فارغة", empty_message.text)

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

    async def test_tiktok_url_validation_accepts_public_and_short_links(self):
        self.assertEqual(
            bot.normalize_tiktok_url("www.tiktok.com/@sample/video/123"),
            "https://www.tiktok.com/@sample/video/123",
        )
        self.assertEqual(
            bot.normalize_tiktok_url("https://vm.tiktok.com/ZMabc/"),
            "https://vm.tiktok.com/ZMabc/",
        )
        for invalid in (
            "https://tiktok.com.evil.example/@sample/video/123",
            "https://example.com/?next=tiktok.com",
            "http://www.tiktok.com/@sample/video/123",
            "https://user@www.tiktok.com/@sample/video/123",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    bot.normalize_tiktok_url(invalid)

    async def test_clipx_quality_mapping_uses_documented_fields(self):
        video = {
            "hd_mp4": "https://cdn.example/hd.mp4",
            "standard_mp4": "https://cdn.example/sd.mp4",
            "wmplay": "https://cdn.example/wm.mp4",
        }
        self.assertEqual(bot.select_video_url({"video": video}, "best"), video["hd_mp4"])
        self.assertEqual(bot.select_video_url({"video": video}, "hd"), video["hd_mp4"])
        self.assertEqual(bot.select_video_url({"video": video}, "sd"), video["standard_mp4"])
        with self.assertRaises(ValueError):
            bot.select_video_url({"video": {"standard_mp4": video["standard_mp4"]}}, "hd")
        buttons = [button for row in bot.download_quality_keyboard().inline_keyboard for button in row]
        self.assertEqual([button.text for button in buttons], [
            "🎬 أفضل جودة", "📺 HD", "📱 جودة عادية", "❌ إلغاء",
        ])
        self.assertEqual([button.callback_data for button in buttons], [
            "dl:best", "dl:hd", "dl:sd", "download_cancel",
        ])

    async def test_clipx_request_uses_selected_quality_parameter(self):
        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def raise_for_status(self):
                return None

            async def json(self, content_type=None):
                return {"success": True, "data": {"video": {"hd_mp4": "https://cdn.example/hd.mp4"}}}

        class FakeSession:
            def __init__(self):
                self.request = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, url, *, params):
                self.request = (url, params)
                return FakeResponse()

        session = FakeSession()
        with patch.object(bot.aiohttp, "ClientSession", return_value=session):
            video_url, _ = await bot.fetch_clipx_video("https://vm.tiktok.com/ZMabc/", "hd")
        self.assertEqual(video_url, "https://cdn.example/hd.mp4")
        self.assertEqual(session.request, (
            "https://clipx.zamdev.workers.dev/",
            {"url": "https://vm.tiktok.com/ZMabc/", "quality": "hd"},
        ))

    async def test_clipx_empty_result_and_rate_limit_are_reported(self):
        class FakeResponse:
            def __init__(self, status, payload):
                self.status = status
                self.payload = payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def raise_for_status(self):
                return None

            async def json(self, content_type=None):
                return self.payload

        class FakeSession:
            def __init__(self, response):
                self.response = response

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, url, *, params):
                return self.response

        with patch.object(bot.aiohttp, "ClientSession", return_value=FakeSession(FakeResponse(200, {"success": False}))):
            with self.assertRaises(ValueError):
                await bot.fetch_clipx_video("https://vm.tiktok.com/ZMabc/", "best")
        with patch.object(bot.aiohttp, "ClientSession", return_value=FakeSession(FakeResponse(429, {}))):
            with self.assertRaisesRegex(RuntimeError, "حد الطلبات"):
                await bot.fetch_clipx_video("https://vm.tiktok.com/ZMabc/", "best")

    async def test_video_send_failure_returns_direct_download_link(self):
        class FakeBot:
            async def send_video(self, *args, **kwargs):
                raise RuntimeError("Telegram rejected remote file")

        message = FakeMessage()
        direct_url = "https://cdn.example/video.mp4"
        with patch.object(bot, "fetch_clipx_video", return_value=(direct_url, {"title": "Clip"})), \
             patch.object(bot, "bot", FakeBot(), create=True):
            await bot.deliver_clip(123, message, "https://vm.tiktok.com/ZMabc/", "best")
        self.assertIn("رابط التنزيل المباشر", message.text)
        self.assertEqual(message.reply_markup.inline_keyboard[0][0].url, direct_url)


if __name__ == "__main__":
    unittest.main()
