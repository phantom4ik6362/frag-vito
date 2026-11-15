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
        self.model_messages = []
        self.listing_calls = 0
        self.sent = []
        self.message_number = 0

    def model(self, request):
        self.model_calls += 1
        payload = json.loads(request.content)
        self.model_messages.append(json.loads(payload['messages'][-1]['content'])['messages'])
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
            async def listing_title(api, _):
                self.listing_calls += 1
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
        self.assertEqual(state, 'turnkey_details')
        self.assertFalse(manual, 'Own outgoing webhook must not close the chat')
        self.assertEqual(self.sent[-1].count('?'), 2)
        self.assertIn('локацию', self.sent[-1])
        row, state, _ = await self.turn('Да место уже готово')
        self.assertEqual((row['template_id'], state), ('info_received', 'contact_channel'))
        self.assertEqual(self.sent[-1], 'Спасибо за информацию!')
        app.init_db()  # Restart before the buyer sends the phone.
        row, state, _ = await self.turn(f'{PHONE} Макс')
        self.assertEqual((row['template_id'], state), ('contact_received', CLOSED))
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.sent[-1], 'Спасибо за контакт! Свяжемся с вами.')
        reports = [telegram_notify.claim_report(app.db, float('inf'), 0) for _ in range(3)]
        self.assertIn(PHONE, reports[-1][1])
        self.assertIn('Поблагодарил за контакт', reports[-1][1])

    async def test_placement_question_does_not_start_delivery(self):
        await self.turn('Расчёт')
        row, state, _ = await self.turn('Казань, ЖК Светлая долина или ЖК Весна, где можно?', model_result={'answered': False})
        self.assertEqual((row['template_id'], state), ('handoff', CLOSED))
        await self.turn(PHONE)
        await self.turn('Выше написала')
        self.assertEqual(len(self.sent), 1)

    async def test_contact_and_location_in_same_batch(self):
        await self.turn('Расчёт')
        row, state, _ = await self.turn('Город Казань, локацию не нашли ещё', PHONE)
        self.assertEqual((row['template_id'], state), ('contact_received', CLOSED))
        self.assertEqual(self.sent[-1], 'Спасибо за контакт! Свяжемся с вами.')
        self.assertEqual(self.model_calls, 0)

    async def test_messenger_then_nickname_is_acknowledged(self):
        for index, nickname in enumerate(('Добрый день! @coffee_owner', 'никнейм: CoffeeOwner', 'В Telegram CoffeeOwner')):
            with self.subTest(nickname=nickname):
                chat = f'u2i-nickname-{index}'
                await self.turn('Улица', chat=chat)
                row, state, _ = await self.turn('Лучше в телеграм', chat=chat)
                self.assertEqual((row['template_id'], state), ('waiting_contact', 'contact_channel'))
                row, state, _ = await self.turn(nickname, chat=chat)
                self.assertEqual((row['template_id'], state), ('contact_received', CLOSED))
                self.assertEqual(self.sent[-1], 'Спасибо за контакт! Свяжемся с вами.')
        self.assertEqual(self.model_calls, 0)

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

    async def test_greeting_uses_the_full_listing_template(self):
        row, state, _ = await self.turn('Здравствуйте!', title='Уличная кофейня самообслуживания')
        self.assertEqual((row['template_id'], state), ('street_overview', 'street_details'))
        self.assertEqual(self.sent[-1].count('?'), 2)
        self.assertIn('230 000 ₽', self.sent[-1])
        row, state, _ = await self.turn('Здравствуйте!', title='Оборудование', chat='other')
        self.assertEqual((row['template_id'], state), ('handoff', CLOSED))
        self.assertEqual(self.model_calls, 0)

    async def test_colloquial_greeting_gets_listing_overview(self):
        for index, message in enumerate(('Драстье!', 'дравствуйте!', 'Доброго дня 👋')):
            with self.subTest(message=message):
                row, state, _ = await self.turn(
                    message, title='Уличная кофейня самообслуживания', chat=f'u2i-hello-{index}',
                )
                self.assertEqual((row['template_id'], state), ('street_overview', 'street_details'))
        self.assertEqual(self.model_calls, 0)
        self.assertEqual(self.listing_calls, 3)

    async def test_greeting_classified_by_model_gets_listing_overview(self):
        row, state, _ = await self.turn(
            'Моё почтение!', model_result={'id': 'greeting', 'already_answered': False},
            title='Уличная кофейня самообслуживания',
        )
        self.assertEqual((row['template_id'], state), ('street_overview', 'street_details'))
        self.assertEqual(self.model_calls, 1)
        self.assertEqual(self.listing_calls, 1)

    async def test_colloquial_greeting_with_question_keeps_question_intent(self):
        for index, messages in enumerate((('Драстье, скока доставка?',), ('Драстье!', 'Скока доставка?'))):
            with self.subTest(messages=messages):
                row, state, _ = await self.turn(
                    *messages, model_result={'id': 'clarify_delivery_city'}, chat=f'u2i-delivery-{index}',
                )
                self.assertEqual((row['template_id'], state), ('clarify_delivery_city', 'delivery_destination'))
        self.assertEqual(self.model_messages, [['скока доставка?'], ['Скока доставка?']])
        self.assertEqual(self.listing_calls, 0)

    async def test_core_keywords_work_with_or_without_greeting(self):
        for index, (messages, expected) in enumerate((
            (('Улица',), 'street_overview'),
            (('Расчёт',), 'turnkey_overview'),
            (('Добрый день!', 'улица'), 'street_overview'),
            (('добрый день!', 'РАСЧЕТ!'), 'turnkey_overview'),
            (('Здравствуйте, можно расчёт, пожалуйста?',), 'turnkey_overview'),
            (('Улица, добрый день!',), 'street_overview'),
            (('Добрый день, расскажите про уличный киоск',), 'street_overview'),
            (('Привет!', 'улица', 'расчет'), 'street_quote'),
        )):
            with self.subTest(messages=messages):
                row, _, _ = await self.turn(*messages, chat=f'u2i-command-{index}')
                self.assertEqual((row['template_id'], row['status']), (expected, 'sent'))
                if expected in {'street_overview', 'turnkey_overview'}:
                    self.assertEqual(self.sent[-1], app.templates()[expected])
                    self.assertEqual(self.sent[-1].count('Добрый день!)'), 1)
                    self.assertTrue(self.sent[-1].endswith('Отправьте, пожалуйста, ваш номер телефона или никнейм.'))
        self.assertEqual(self.model_calls, 0)
        self.assertEqual(self.listing_calls, 0)

    async def test_keyword_works_after_bot_has_answered_greeting(self):
        await self.turn('Добрый день!', title='Кофейня под ключ')
        row, state, _ = await self.turn('улица')
        self.assertEqual((row['template_id'], state), ('street_overview', 'street_details'))
        self.assertFalse(self.sent[-1].startswith(('Здравствуйте', 'Добрый день')))
        row, state, _ = await self.turn('расчет')
        self.assertEqual((row['template_id'], state), ('turnkey_overview', 'turnkey_details'))
        self.assertEqual(self.model_calls, 0)

    async def test_greeting_does_not_override_other_meanings_of_keywords(self):
        for index, (text, expected) in enumerate((
            ('Добрый день! Улица Ленина, дом 5', 'handoff'),
            ('Здравствуйте, расчёт доставки в Казань', 'delivery_quote_requested'),
            ('Привет! Расчёт не нужен', 'handoff'),
            ('Здравствуйте! Ещё актуально?', 'availability_yes'),
        )):
            with self.subTest(text=text):
                row, _, _ = await self.turn(
                    text, model_result={'id': expected}, chat=f'u2i-context-{index}',
                )
                self.assertEqual(row['template_id'], expected)
        self.assertEqual(self.model_calls, 4)
        self.assertEqual(self.listing_calls, 0)

    async def test_installation_city_after_listing_greeting(self):
        await self.turn('Здравствуйте!', title='Уличная кофейня самообслуживания')
        row, state, _ = await self.turn('Добрый день!', 'Казань', model_result={'answered': True})
        self.assertEqual((row['template_id'], state), ('info_received', 'contact_channel'))
        self.assertEqual(self.sent[-1], 'Спасибо за информацию!')

    async def test_provided_city_and_explicit_delivery(self):
        row, state, _ = await self.turn(
            'Хочу уличный киоск, установка в Казани',
            model_result={'id': 'street_overview', 'already_answered': True},
        )
        self.assertEqual((row['template_id'], state), ('street_overview', 'contact_channel'))
        self.assertNotIn('в каком городе', self.sent[-1])
        self.assertIn('WhatsApp, Telegram или Max?', self.sent[-1])
        row, state, _ = await self.turn('В Телеграм')
        self.assertEqual((row['template_id'], row['status'], state), ('waiting_contact', 'skipped', 'contact_channel'))
        row, state, _ = await self.turn('coffee_owner')
        self.assertEqual((row['template_id'], state), ('contact_received', CLOSED))
        await self.turn('Доставите в Казань?', model_result={'id': 'delivery_quote_requested'}, chat='delivery')
        self.assertIn('Уточним стоимость доставки', self.sent[-1])
        row, state, _ = await self.turn(
            'Казань, локацию не нашли ещё', model_result={'id': 'handoff'}, chat='bad-route',
        )
        self.assertEqual((row['template_id'], state), ('handoff', CLOSED))

    async def test_new_topic_during_delivery_wait_cannot_reopen(self):
        await self.turn('Сколько стоит доставка?', model_result={'id': 'clarify_delivery_city'})
        row, state, _ = await self.turn(
            'Сколько стоит кофемашина?', model_result={'answered': False},
        )
        self.assertEqual((row['template_id'], state), ('handoff', CLOSED))
        await self.turn('Казань')
        self.assertEqual(len(self.sent), 1)

    async def test_delivery_city_completes_the_chat(self):
        await self.turn('Сколько стоит доставка?', model_result={'id': 'clarify_delivery_city'})
        row, state, _ = await self.turn('Павловск, Алтайский край', model_result={'answered': True})
        self.assertEqual((row['template_id'], state), ('info_received', CLOSED))
        self.assertEqual(self.sent[-1], 'Спасибо за информацию!')

    async def test_delivery_can_be_classified_without_exact_keywords(self):
        for index, (text, action) in enumerate((
            ('дост.', 'clarify_delivery_city'),
            ('дост. в мсква', 'delivery_quote_requested'),
            ('Возите в азань?', 'delivery_quote_requested'),
        )):
            with self.subTest(text=text):
                row, _, _ = await self.turn(text, model_result={'id': action}, chat=f'u2i-short-{index}')
                self.assertEqual((row['template_id'], row['status']), (action, 'sent'))

    async def test_followup_accepts_misspelled_city_without_inventing_a_name(self):
        for index, text in enumerate(('азань', 'мсква?')):
            with self.subTest(text=text):
                chat = f'u2i-city-{index}'
                await self.turn('дост.', model_result={'id': 'clarify_delivery_city'}, chat=chat)
                row, state, _ = await self.turn(text, model_result={'answered': True}, chat=chat)
                self.assertEqual((row['template_id'], state), ('info_received', CLOSED))
                self.assertEqual(self.sent[-1], 'Спасибо за информацию!')
                with app.db() as connection:
                    stored = connection.execute(
                        'SELECT text FROM jobs WHERE chat_id=? AND template_id=?', (chat, 'info_received'),
                    ).fetchone()
                self.assertEqual(stored['text'], text)

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
        self.assertEqual(state, 'turnkey_details')
        with app.db() as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM jobs WHERE id='old'").fetchone())

    async def test_llm_failure_is_logged_in_job_and_chat_closes(self):
        row, state, _ = await self.turn('Почём кофейня под ключ?', model_result=401)
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
