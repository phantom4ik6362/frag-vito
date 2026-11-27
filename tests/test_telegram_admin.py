import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import app
import telegram_admin as admin


class TelegramAdminTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        override = patch.object(app, "DB", Path(temp.name) / "queue.sqlite3")
        override.start()
        self.addCleanup(override.stop)
        env = patch.dict(os.environ, {"DRY_RUN": "false"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        app.init_db(recover=False)
        self.cfg = admin.Settings("2:admin")
        self.calls = []

    def message(self, ident, text, user=123):
        return {"update_id": ident, "message": {
            "chat": {"id": user, "type": "private"},
            "from": {"id": user, "is_bot": False}, "text": text,
        }}

    async def test_start_and_all_five_buttons_are_open_to_every_user(self):
        def transport(request):
            self.calls.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"ok": True, "result": {}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            for i, text in enumerate(("/start", *admin.LABELS)):
                await admin.handle_update(client, app.db, self.cfg, self.message(i, text, user=i+100))
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(self.calls[0][0], "/bot2:admin/sendMessage")
        self.assertEqual(len(self.calls[0][1]["reply_markup"]["keyboard"]), 3)
        self.assertTrue(all("parse_mode" not in payload for _, payload in self.calls))
        self.assertIn("Новых чатов — 0", self.calls[-1][1]["text"])

    async def test_offset_is_persistent_and_duplicate_updates_are_skipped(self):
        update = self.message(11, "Сегодня")
        def transport(request):
            payload = json.loads(request.content)
            self.calls.append((request.url.path, payload))
            result = [update] if request.url.path.endswith("getUpdates") else {}
            return httpx.Response(200, json={"ok": True, "result": result})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await admin.poll_once(client, app.db, self.cfg)
            app.init_db(recover=False)
            await admin.poll_once(client, app.db, self.cfg)
        sends = [payload for path, payload in self.calls if path.endswith("sendMessage")]
        polls = [payload for path, payload in self.calls if path.endswith("getUpdates")]
        self.assertEqual(len(sends), 1)
        self.assertEqual([p["offset"] for p in polls], [0, 12])

    async def test_rate_limit_does_not_confirm_unanswered_update(self):
        def transport(request):
            if request.url.path.endswith("getUpdates"):
                return httpx.Response(200, json={"ok": True, "result": [self.message(5, "Вчера")]})
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 9}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            with self.assertRaises(admin.TelegramError) as caught:
                await admin.poll_once(client, app.db, self.cfg)
        self.assertEqual(caught.exception.retry_after, 9)
        self.assertEqual(admin.next_update(app.db, self.cfg.bot_id), 0)
        self.assertNotIn("2:admin", str(caught.exception))

    async def test_blocked_user_does_not_stop_processing_next_user(self):
        def transport(request):
            if request.url.path.endswith("getUpdates"):
                return httpx.Response(200, json={"ok": True, "result": [
                    self.message(5, "Сегодня", 100), self.message(6, "Сегодня", 101),
                ]})
            payload = json.loads(request.content)
            self.calls.append(payload["chat_id"])
            code = 403 if payload["chat_id"] == 100 else 200
            return httpx.Response(code, json={"ok": code == 200, "result": {}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await admin.poll_once(client, app.db, self.cfg)
        self.assertEqual(self.calls, [100, 101])
        self.assertEqual(admin.next_update(app.db, self.cfg.bot_id), 7)

    def test_optional_configuration_and_separate_bot_identity(self):
        self.assertIsNone(admin.settings())
        with patch.dict(os.environ, {"TELEGRAM_ADMIN_BOT_TOKEN": "invalid"}):
            self.assertRaises(ValueError, admin.settings)
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "2:old", "TELEGRAM_ADMIN_BOT_TOKEN": "2:new"}):
            self.assertRaises(ValueError, admin.settings)
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "1:alerts", "TELEGRAM_ADMIN_BOT_TOKEN": "2:admin"}):
            self.assertEqual(admin.settings(), self.cfg)
