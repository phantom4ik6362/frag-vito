import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import app
import telegram_notify
from dialogue import decide

class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = app.DB
        app.DB = Path(self.tmp.name) / 'queue.db'
        app.init_db()
        self.event = {'payload': {'type': 'message', 'value': {'id': 'm1', 'chat_id': 'c1', 'user_id': 123, 'author_id': 456, 'type': 'text', 'content': {'text': 'Есть доставка?'}}}}

    def tearDown(self):
        app.DB = self.old_db
        self.tmp.cleanup()

    def test_filtering(self):
        self.assertEqual(app.extract_event(self.event, '123'), ('m1', 'c1', 'Есть доставка?'))
        self.assertIsNone(app.extract_event(self.event, '999'))
        self.event['payload']['value']['author_id'] = 123
        self.assertIsNone(app.extract_event(self.event, '123'))
        self.event['payload']['value']['author_id'] = 456
        self.event['payload']['value']['type'] = 'image'
        self.assertIsNone(app.extract_event(self.event, '123'))

    def test_dedup_and_secret(self):
        async def run():
            settings = {'AVITO_CLIENT_ID': 'id', 'AVITO_CLIENT_SECRET': 'secret', 'AVITO_USER_ID': '123', 'WEBHOOK_SECRET': 'x' * 32, 'LLM_API_KEY': 'key'}
            with patch.dict(os.environ, settings, clear=True):
                transport = httpx.ASGITransport(app=app.app)
                async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                    self.assertEqual((await client.post('/webhook/bad', json=self.event)).status_code, 404)
                    for _ in range(2):
                        self.assertEqual((await client.post('/webhook/' + 'x'*32, json=self.event)).status_code, 200)
                with app.db() as c:
                    rows = c.execute('SELECT * FROM jobs').fetchall()
                self.assertEqual(len(rows), 1)
                self.assertGreaterEqual(rows[0]['due'] - rows[0]['created'], 5)
        asyncio.run(run())

    def test_dialogue(self):
        async def run():
            cases = [
                ('clarify_intent', {}, [], 'clarify_intent'),
                ('clarify_intent', {}, ['clarify_intent'], 'ignore'),
                ('delivery', {}, ['clarify_intent', 'ignore'], 'clarify_delivery_city'),
                ('turnkey_overview', {}, ['clarify_delivery_city'], 'ignore'),
                ('delivery', {'city': True, 'messenger': True}, ['clarify_delivery_city', 'model_error'], 'delivery_quote_requested'),
                ('street_overview', {'city': True}, [], 'street_overview'),
                ('turnkey_overview', {'location_status': True, 'messenger': True}, [], 'turnkey_overview'),
                ('stop', {}, ['availability_yes'], 'stop'),
                ('availability_yes', {}, ['stop'], 'stop'),
            ]
            for label, fields, actions, expected in cases:
                def handler(request):
                    return httpx.Response(200, json={'choices': [{'message': {
                        'content': json.dumps({'id': label, **fields}),
                    }}]})
                with patch.dict(os.environ, {'LLM_API_KEY': 'test'}):
                    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                        history = [{'text': '', 'action': action} for action in actions]
                        reply = await decide(client, ['пример'], history, app.templates())
                        self.assertEqual(reply.id, expected)
                        if fields.get('messenger'):
                            self.assertNotIn('куда удобнее', reply.text)
                            self.assertNotIn('в какой мессенджер', reply.text)
                        if expected == 'street_overview':
                            self.assertIn('230 000 ₽', reply.text)
                            self.assertNotIn('в каком городе', reply.text)
                        if fields.get('location_status'):
                            self.assertNotIn('локацию уже', reply.text)
        asyncio.run(run())

    def test_batch_waits_for_latest_message(self):
        with app.db() as c:
            for ident, created, due in [('a', 1, 2), ('b', 2, 6)]:
                c.execute("INSERT INTO jobs (id,chat_id,text,status,created,due) VALUES (?,'chat','text','pending',?,?)", (ident, created, due))
        with patch.object(app.time, 'time', return_value=3):
            self.assertEqual(app.claim_batch(), [])
        with patch.object(app.time, 'time', return_value=6):
            batch = app.claim_batch()
        self.assertEqual([row['id'] for row in batch], ['a', 'b'])
        app.record_batch(batch, 'sent', 'clarify_delivery_city')
        self.assertEqual(app.history_for('chat', False), [{'text': 'text\ntext', 'action': 'clarify_delivery_city'}])
        self.assertEqual(app.history_for('chat', True), [])

    def test_webhook_queues_telegram_once_and_dry_run_does_not_queue(self):
        async def run():
            settings = {
                'AVITO_CLIENT_ID': 'id', 'AVITO_CLIENT_SECRET': 'secret', 'AVITO_USER_ID': '123',
                'WEBHOOK_SECRET': 'x' * 32, 'LLM_API_KEY': 'key', 'TELEGRAM_BOT_TOKEN': '1:test',
                'TELEGRAM_CHAT_ID': '-100123', 'DRY_RUN': 'false',
            }
            with patch.dict(os.environ, settings, clear=True):
                transport = httpx.ASGITransport(app=app.app)
                async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                    url = '/webhook/' + 'x' * 32
                    for _ in range(2):
                        self.assertEqual((await client.post(url, json=self.event)).status_code, 200)
                    self.event['payload']['value'].update(id='image', type='image')
                    self.assertEqual((await client.post(url, json=self.event)).status_code, 200)
                    self.event['payload']['value'].update(id='dry', type='text')
                    with patch.dict(os.environ, {'DRY_RUN': 'true'}):
                        self.assertEqual((await client.post(url, json=self.event)).status_code, 200)
            with app.db() as c:
                rows = c.execute('SELECT id, text FROM telegram_jobs ORDER BY id').fetchall()
            self.assertEqual([row['id'] for row in rows], ['image', 'm1'])
            self.assertIn('Есть доставка?', rows[-1]['text'])
        asyncio.run(run())

    def test_telegram_post_and_rate_limit(self):
        async def run():
            def handler(request):
                self.assertEqual(request.url.path, '/bot1:test/sendMessage')
                data = json.loads(request.content)
                self.assertEqual(data['chat_id'], '-100123')
                self.assertIn('Чат: c1', data['text'])
                self.assertIn('Есть доставка?', data['text'])
                return httpx.Response(429, json={'ok': False, 'parameters': {'retry_after': 4}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                outcome = await telegram_notify.deliver(client, ('1:test', '-100123'), {'chat_id': 'c1', 'text': 'Есть доставка?'})
            self.assertEqual(outcome, ('pending', 4))
        asyncio.run(run())

    def test_restart_sending_is_unknown(self):
        with app.db() as c:
            c.execute("INSERT INTO jobs (id,chat_id,text,status,created,due) VALUES ('id','chat','text','sending',0,0)")
        app.init_db()
        with app.db() as c:
            self.assertEqual(c.execute('SELECT status FROM jobs').fetchone()['status'], 'unknown')

if __name__ == '__main__':
    unittest.main()
