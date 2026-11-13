import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import app
import reconcile
import telegram_notify
from dialogue import Reply


class ReleaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        database = patch.object(app, 'DB', Path(folder.name) / 'queue.sqlite3')
        database.start()
        self.addCleanup(database.stop)
        env = patch.dict(os.environ, {
            'DRY_RUN': 'false', 'TELEGRAM_BOT_TOKEN': '1:test', 'TELEGRAM_CHAT_ID': '123',
            'REPLY_DELAY_SECONDS': '0', 'AVITO_USER_ID': '123', 'AVITO_CLIENT_ID': 'test',
            'AVITO_CLIENT_SECRET': 'test', 'WEBHOOK_SECRET': 'x' * 32, 'LLM_API_KEY': 'test',
        })
        env.start()
        self.addCleanup(env.stop)
        app.init_db()

    async def post(self, ident, author=123, chat='chat'):
        event = {'payload': {'type': 'message', 'value': {
            'id': ident, 'chat_id': chat, 'user_id': 123, 'author_id': author,
            'type': 'text', 'content': {'text': 'Здравствуйте'},
        }}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url='http://test') as client:
            self.assertEqual((await client.post('/webhook/' + 'x' * 32, json=event)).status_code, 200)

    async def test_one_report_per_batch_without_waiting_for_next_input(self):
        await self.post('buyer', 456)
        await self.post('buyer', 456)
        await self.post('second', 456)
        self.assertIsNone(telegram_notify.claim_report(app.db, time.time(), 0))
        batch = app.claim_batch()
        self.assertEqual(len(batch), 2)
        await self.post('newer', 456)
        app.record_batch(batch, 'skipped', 'model_error')
        item = telegram_notify.claim_report(app.db, time.time(), 0)
        self.assertEqual(item[0], 'second')
        self.assertIn('Сообщения\nЗдравствуйте\nЗдравствуйте', item[1])
        self.assertIn('Действие\nНе смог обработать', item[1])
        self.assertIsNone(telegram_notify.claim_report(app.db, time.time(), 0))

    async def test_operator_during_classification(self):
        await self.post('buyer', 456)
        async def classify(*args):
            await self.post('operator')
            return Reply('clarify_intent', 'Здравствуйте')
        with patch.object(app, 'decide_safely', classify):
            await app.process_batch(None, None, app.claim_batch())
        app.init_db()
        await self.post('operator')
        with app.db() as conn:
            self.assertTrue(app.is_manual(conn, 'chat'))
            self.assertEqual(conn.execute("SELECT template_id FROM jobs WHERE id='buyer'").fetchone()[0], 'operator')
            self.assertEqual(conn.execute("SELECT count(*) FROM telegram_jobs WHERE id LIKE 'operator:%'").fetchone()[0], 1)
        await self.post('buyer2', 456)
        await app.process_batch(None, None, app.claim_batch())

    async def test_bot_echo_before_and_after_api_response(self):
        await self.post('buyer', 456)
        async def classify(*args):
            return Reply('clarify_intent', 'Здравствуйте')
        for echo_first in (True, False):
            with self.subTest(echo_first=echo_first):
                outgoing = f'out-{echo_first}'
                async def send(chat, message):
                    if echo_first:
                        await self.post(outgoing)
                    return httpx.Response(200, json={'id': outgoing})
                api = type('Api', (), {'send': staticmethod(send)})()
                with patch.object(app, 'decide_safely', classify):
                    await app.process_batch(None, api, app.claim_batch())
                await self.post(outgoing)
                with app.db() as conn:
                    self.assertFalse(app.is_manual(conn, 'chat'))
                if echo_first:
                    await self.post('buyer2', 456)

    async def test_reconcile_older_inbound_behind_outbound(self):
        timestamp = time.time() - 100
        class Api:
            async def request(self, method, path, params):
                last = {'id': 'last', 'created': timestamp, 'direction': 'out'}
                if path.endswith('/messages/'):
                    inbound = {'id': 'missing', 'created': timestamp-1, 'direction': 'in',
                               'type': 'text', 'content': {'text': 'Сколько доставка?'}}
                    return httpx.Response(200, json={'messages': [last, inbound]})
                return httpx.Response(200, json={'chats': [{'id': 'chat', 'last_message': last}]})
        for _ in range(2):
            await reconcile.scan(Api(), app.db, app.enqueue, '123', timestamp-10, app.log)
        self.assertEqual(app.claim_batch(), [])
        report = telegram_notify.claim_report(app.db, time.time(), 90)
        self.assertEqual(report[0], 'missing')
        self.assertIn('Сообщение найдено при сверке', report[1])

    async def test_result_waits_for_receipt_retry_and_failed_health(self):
        await self.post('buyer', 456)
        app.record_batch(app.claim_batch(), 'skipped', 'model_error')
        now = time.time()
        with app.db() as conn:
            app.queue_notice(conn, 'received:buyer', 'chat', 'Старое уведомление')
        now = time.time()
        self.assertEqual(telegram_notify.claim_report(app.db, now, 0)[0], 'received:buyer')
        with app.db() as conn:
            conn.execute("UPDATE telegram_jobs SET status='report_retry', due=? WHERE id='received:buyer'", (now+30,))
        self.assertIsNone(telegram_notify.claim_report(app.db, now+1, 0))
        with app.db() as conn:
            conn.execute("UPDATE telegram_jobs SET status='failed' WHERE id='received:buyer'")
        with self.assertRaises(app.HTTPException) as failure:
            await app.health()
        self.assertEqual(failure.exception.status_code, 503)
        self.assertEqual(telegram_notify.claim_report(app.db, now+1, 0)[0], 'buyer')
        with app.db() as conn:
            conn.execute("UPDATE telegram_jobs SET status='acknowledged' WHERE id='received:buyer'")
        self.assertEqual(await app.health(), {'status': 'ok'})

    async def test_unmatched_outgoing_requests_review(self):
        await self.post('unmatched')
        notice = telegram_notify.claim_report(app.db, time.time(), 0)
        self.assertIn('не смог подтвердить', notice[1])
        with app.db() as conn:
            self.assertTrue(app.is_manual(conn, 'chat'))
