import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import app
import telegram_notify
from dialogue import CLOSED

# Synthetic contact data and chat IDs. No buyer database is needed for these tests.
PHONE = '+7 (900) 000-00-01'
CHAT = 'u2i-example'
SETTINGS = {
    'LLM_API_KEY': 'test', 'DRY_RUN': 'false', 'REPLY_DELAY_SECONDS': '0',
    'TELEGRAM_BOT_TOKEN': '1:test', 'TELEGRAM_CHAT_ID': '-100123',
}


class ReplyLogicTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db_patch = patch.object(app, 'DB', Path(temp.name) / 'queue.sqlite3')
        db_patch.start()
        self.addCleanup(db_patch.stop)
        env_patch = patch.dict(os.environ, SETTINGS, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        app.init_db()
        self.model_answer = None
        self.model_calls = 0
        self.sent = []
        self.message_number = 0

    def model(self, request):
        self.model_calls += 1
        self.assertIsNotNone(self.model_answer, 'Unexpected model call')
        if isinstance(self.model_answer, int):
            return httpx.Response(self.model_answer)
        return httpx.Response(200, json={'choices': [{'message': {
            'content': json.dumps(self.model_answer),
        }}]})

    async def turn(self, *messages, model_result=None, status=200, title='', race='own', chat=CHAT):
        self.model_answer = model_result
        with app.db() as c:
            for message in messages:
                self.message_number += 1
                app.enqueue(c, str(self.message_number), chat, message, 'pending')

        async def send(chat_id, body):
            self.sent.append(body)
            outgoing = 'outgoing-' + str(self.message_number)
            if race:
                with app.db() as c:
                    ident = outgoing if race == 'own' else 'manual-' + outgoing
                    c.execute('INSERT INTO seller_events (id,chat_id) VALUES (?,?)', (ident, chat_id))
                    app.resolve_seller_events(c)
            return httpx.Response(status, json={'id': outgoing})

        class Avito:
            async def listing_title(self, _):
                return title

        api = Avito()
        api.send = send
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.model)) as llm:
            await app.process_batch(llm, api, app.claim_batch())

        with app.db() as c:
            return (
                c.execute('SELECT template_id,status FROM jobs WHERE id=?', (str(self.message_number),)).fetchone(),
                app.chat_state(c, chat, False),
                app.is_manual(c, chat),
            )

    async def test_price_ready_location_then_late_contact(self):
        _, state, manual = await self.turn(
            'Здравствуйте, а какая цена под ключ как на фото?',
            model_result={'id': 'turnkey_overview', 'already_answered': False},
        )
        self.assertEqual(state, 'location_status')
        self.assertFalse(manual, 'Own outgoing webhook must not close the chat')
        self.assertEqual(self.sent[-1].count('?'), 1)
        self.assertIn('локацию', self.sent[-1])
        row, state, _ = await self.turn('Да место уже готово')
        self.assertEqual((row['template_id'], state), ('info_received', CLOSED))
        self.assertEqual(self.sent[-1], 'Спасибо за информацию!')
        app.init_db()  # Restart before the buyer sends the phone.
        row, state, _ = await self.turn(f'{PHONE} Макс')
        self.assertEqual((row['template_id'], state), ('closed', CLOSED))
        self.assertEqual(len(self.sent), 2)
        reports = [telegram_notify.claim_report(app.db, float('inf'), 0) for _ in range(3)]
        self.assertIn(PHONE, reports[-1][1])
        self.assertIn('для продавца', reports[-1][1])

    async def test_placement_question_does_not_start_delivery(self):
        await self.turn('Расчёт', model_result={'id': 'turnkey_overview', 'already_answered': False})
        row, state, _ = await self.turn('Казань, ЖК Светлая долина или ЖК Весна, где можно?')
        self.assertEqual((row['template_id'], state), ('handoff', CLOSED))
        await self.turn(PHONE)
        await self.turn('Выше написала')
        self.assertEqual(len(self.sent), 1)

    async def test_contact_and_location_in_same_batch(self):
        await self.turn('Расчёт', model_result={'id': 'turnkey_overview', 'already_answered': False})
        row, state, _ = await self.turn('Город Казань, локацию не нашли ещё', PHONE)
        self.assertEqual((row['template_id'], state), ('contact_received', CLOSED))
        self.assertEqual(self.sent[-1], 'Спасибо за контакт! Свяжемся с вами.')
        self.assertEqual(self.model_calls, 1)

    async def test_callback_with_contact_and_mixed_delivery(self):
        row, state, _ = await self.turn(f'Интересна франшиза. Перезвоните, пожалуйста: {PHONE}')
        self.assertEqual((row['template_id'], state), ('contact_received', CLOSED))
        row, state, _ = await self.turn(
            'Возможна ли доставка в Алтайский край? И можно рассчитать стоимость вместе с оборудованием?',
            model_result={'id': 'handoff'}, chat='mixed',
        )
        self.assertEqual((row['template_id'], state), ('handoff', CLOSED))
        await self.turn(f'В мах {PHONE}', chat='mixed')
        await self.turn('Алтайский край, Павловский район, с. Павловск', chat='mixed')
        self.assertEqual(len(self.sent), 1)

    async def test_greeting_uses_listing_and_cannot_quote_unverified_price(self):
        row, state, _ = await self.turn('Здравствуйте!', title='Уличная кофейня самообслуживания')
        self.assertEqual((row['template_id'], state), ('street_overview', 'installation_city'))
        self.assertEqual(self.sent[-1].count('?'), 1)
        self.assertNotIn('230 000', self.sent[-1])
        row, state, _ = await self.turn('Здравствуйте!', title='Оборудование', chat='other')
        self.assertEqual((row['template_id'], state), ('handoff', CLOSED))
        self.assertEqual(self.model_calls, 0)

    async def test_provided_city_and_explicit_delivery(self):
        row, state, _ = await self.turn(
            'Хочу уличный киоск, установка в Казани',
            model_result={'id': 'street_overview', 'already_answered': True},
        )
        self.assertEqual((row['template_id'], state), ('street_overview', CLOSED))
        self.assertNotIn('?', self.sent[-1])
        await self.turn('Доставите в Казань?', model_result={'id': 'delivery_quote_requested'}, chat='delivery')
        self.assertIn('Уточним стоимость доставки', self.sent[-1])
        row, state, _ = await self.turn(
            'Казань, локацию не нашли ещё', model_result={'id': 'delivery_quote_requested'}, chat='bad-route',
        )
        self.assertEqual((row['template_id'], state), ('handoff', CLOSED))

    async def test_new_topic_during_delivery_wait_cannot_reopen(self):
        await self.turn('Сколько стоит доставка?', model_result={'id': 'clarify_delivery_city'})
        row, state, _ = await self.turn(
            'Расскажите про уличный киоск', model_result={'id': 'street_overview', 'already_answered': False},
        )
        self.assertEqual((row['template_id'], state), ('handoff', CLOSED))
        await self.turn('Казань')
        self.assertEqual(len(self.sent), 1)

    async def test_delivery_city_completes_the_chat(self):
        await self.turn('Сколько стоит доставка?', model_result={'id': 'clarify_delivery_city'})
        row, state, _ = await self.turn('Павловск, Алтайский край', model_result={'id': 'answer'})
        self.assertEqual((row['template_id'], state), ('info_received', CLOSED))
        self.assertEqual(self.sent[-1], 'Спасибо за информацию!')

    async def test_phone_fragments_and_price_question(self):
        row, state, _ = await self.turn('918', '9800001')
        self.assertEqual((row['template_id'], state), ('handoff', CLOSED))
        row, state, _ = await self.turn(
            'Это цена с кофейным аппаратом?', 'Или только павильон',
            model_result={'id': 'handoff'}, chat='price',
        )
        self.assertEqual((row['template_id'], state), ('handoff', CLOSED))
        self.assertEqual(self.sent, [])

    async def test_operator_and_send_error_are_terminal(self):
        _, _, manual = await self.turn(
            'Доставка?', model_result={'id': 'clarify_delivery_city'}, race='seller',
        )
        self.assertTrue(manual)
        row, _, _ = await self.turn('Казань')
        self.assertEqual(row['template_id'], 'operator')
        row, state, _ = await self.turn(
            'Доставка?', model_result={'id': 'clarify_delivery_city'}, chat='broken',
            status=502, race=None,
        )
        self.assertEqual((row['status'], state), ('unknown', CLOSED))
        await self.turn('Казань', chat='broken')
        self.assertEqual(len(self.sent), 2)

    async def test_legacy_migration_and_addressed_manual_reset(self):
        with app.db() as c:
            c.execute("DELETE FROM runtime_state WHERE key='reply_logic_v2'")
            c.execute("""INSERT INTO jobs (id,chat_id,text,status,created,due,template_id)
                         VALUES ('old','legacy','расчёт','sent',0,0,'turnkey_overview')""")
        app.init_db()
        row, state, _ = await self.turn('Расчёт', chat='legacy')
        self.assertEqual((row['template_id'], state), ('closed', CLOSED))
        from manage import main
        with patch('sys.argv', ['manage.py', 'resume-chat', 'legacy']):
            await main()
        app.init_db()
        _, state, _ = await self.turn(
            'Расчёт', chat='legacy', model_result={'id': 'turnkey_overview', 'already_answered': False},
        )
        self.assertEqual(state, 'location_status')
        with app.db() as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM jobs WHERE id='old'").fetchone())

    async def test_llm_failure_is_logged_in_job_and_chat_closes(self):
        row, state, _ = await self.turn('Расчёт', model_result=401)
        self.assertEqual((row['template_id'], state), ('model_error', CLOSED))
        await self.turn(PHONE)
        self.assertEqual(self.sent, [])

    async def test_listing_lookup_shape(self):
        def backend(request):
            if request.url.path == '/token':
                return httpx.Response(200, json={'access_token': 'test'})
            self.assertEqual(request.url.path, f'/messenger/v2/accounts/123/chats/{CHAT}')
            return httpx.Response(200, json={'context': {'type': 'item', 'value': {'title': 'Киоск'}}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as client:
            api = app.AvitoClient(client, {'AVITO_USER_ID': '123', 'AVITO_CLIENT_ID': 'x', 'AVITO_CLIENT_SECRET': 'x'})
            self.assertEqual(await api.listing_title(CHAT), 'Киоск')
