"""Exercise the opt-in selector through the actual Avito queue worker."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

import app
import chat_statistics


class ReleaseSelectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for override in (
            patch.object(app, 'DB', Path(directory.name) / 'queue.sqlite3'),
            patch.dict(os.environ, {
                'REPLY_SELECTOR': 'v48', 'DRY_RUN': 'false', 'LLM_API_KEY': 'test',
                'REPLY_DELAY_SECONDS': '0',
            }, clear=True),
        ):
            override.start()
            self.addCleanup(override.stop)
        app.init_db()
        self.index = 0
        self.requests = []
        self.api = AsyncMock()
        self.api.send.side_effect = lambda chat, text: httpx.Response(200, json={'id': f'out-{self.index}'})

    async def turn(self, messages, facts=None, *, chat='release'):
        with app.db() as conn:
            for text in messages:
                self.index += 1
                app.enqueue(conn, str(self.index), chat, text, 'pending')

        def model(request):
            payload = json.loads(request.content)
            self.requests.append(json.loads(payload['messages'][-1]['content']))
            if isinstance(facts, int):
                return httpx.Response(facts)
            self.assertIsNotNone(facts, 'Unexpected model request')
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(facts)}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as client:
            await app.process_batch(client, self.api, app.claim_batch())
        with app.db() as conn:
            row = conn.execute('SELECT template_id, status FROM jobs WHERE id=?', (str(self.index),)).fetchone()
            return row['template_id'], row['status'], app.chat_state(conn, chat, app.dry_run())

    async def test_context_survives_restart_and_contact_ends_automation(self):
        self.assertEqual(await self.turn(['Улица']), ('street_overview', 'sent', 'street_details'))
        app.init_db()
        facts = {'requests': ['other'], 'city': 'Самаре', 'location': ''}
        self.assertEqual(
            await self.turn(['В Самаре', 'Пришлите варианты с фото', 'Лучше в Макс'], facts),
            ('city_received', 'sent', 'contact_channel'),
        )
        self.assertEqual(self.requests[-1]['history'][-1]['role'], 'seller')
        self.assertIn('фото', self.requests[-1]['history'][-1]['text'])
        self.assertEqual(await self.turn(['+7 (900) 000-00-11']), ('contact_received', 'sent', 'closed'))
        self.assertEqual(await self.turn(['Ещё вопрос']), ('closed', 'skipped', 'closed'))
        self.assertEqual(self.api.send.await_count, 3)

    async def test_statistics_follow_real_queue_and_manual_chat_contacts(self):
        await self.turn(['Улица'])
        await self.turn(['+7 (900) 000-00-01'])
        with app.db() as conn:
            conn.execute("INSERT INTO manual_chats (chat_id) VALUES ('human')")
        self.assertEqual(
            await self.turn(['+7 (900) 000-00-02 Антон'], chat='human'),
            ('operator', 'skipped', None),
        )
        report = chat_statistics.collect(app.db, 'all')
        self.assertEqual(
            (report.total, report.answered, report.handed_off, report.contacts, report.pending),
            (2, 1, 1, 2, 0),
        )

    async def test_custom_photo_request_hands_off_without_sending_again(self):
        self.assertEqual(await self.turn(['Улица']), ('street_overview', 'sent', 'street_details'))
        self.assertEqual(
            await self.turn(['Ставить будем в Калуге', 'Сфотографируйте разводку внутри щита крупным планом']),
            ('handoff', 'skipped', 'closed'),
        )
        self.assertEqual(await self.turn(['Так можно?']), ('closed', 'skipped', 'closed'))
        self.api.send.assert_awaited_once()
        self.assertEqual(self.requests, [])

    async def test_standalone_budget_hands_off_without_calling_model(self):
        self.assertEqual(
            await self.turn(['Мой бюджет 9000000001 рублей']),
            ('handoff', 'skipped', 'closed'),
        )
        self.api.send.assert_not_awaited()
        self.assertEqual(self.requests, [])

    async def test_context_or_purchase_intent_in_the_current_turn(self):
        self.assertEqual(
            await self.turn(
                ['Оборудование у нас своё.'],
                {'requests': ['other'], 'city': '', 'location': ''},
                chat='context-only',
            ),
            ('handoff', 'skipped', 'closed'),
        )
        self.api.send.assert_not_awaited()
        self.assertEqual(
            await self.turn(
                ['Нужен только корпус, оборудование своё.'],
                {'requests': ['pavilion'], 'city': '', 'location': ''},
                chat='purchase-intent',
            ),
            ('street_overview', 'sent', 'street_details'),
        )
        self.api.send.assert_awaited_once()

    async def test_manual_chat_stays_silent(self):
        with app.db() as conn:
            conn.execute("INSERT INTO manual_chats (chat_id) VALUES ('release')")
        action, status, _ = await self.turn(['Сколько доставка?'])
        self.assertEqual((action, status), ('operator', 'skipped'))
        self.api.send.assert_not_awaited()
        self.assertEqual(self.requests, [])

    async def test_invalid_credentials_close_without_sending(self):
        self.assertEqual(await self.turn(['Довезёте до Костромы?'], 401), ('model_error', 'skipped', 'closed'))
        self.api.send.assert_not_awaited()

    async def test_preview_does_not_send_or_share_live_context(self):
        with patch.dict(os.environ, {'DRY_RUN': 'true'}):
            self.assertEqual(await self.turn(['Улица']), ('street_overview', 'dry_run', 'street_details'))
        self.api.send.assert_not_awaited()
        self.assertEqual(await self.turn(['Сколько доставка?']), ('clarify_delivery_city', 'sent', 'delivery_destination'))
        with app.db() as conn:
            preview = app.selector_history(conn, 'release', True)
            live = app.selector_history(conn, 'release', False)
        self.assertIn('павильон', preview[-1][1])
        self.assertIn('доставка', live[-1][1].lower())
