import asyncio
import logging
import os
import re
import sqlite3
import time

import httpx

log = logging.getLogger('avito-bot.telegram')
UNFINISHED = ('pending', 'processing', 'sending')
SENT_ACTIONS = {
    'city_received': 'Покупатель указал город. Подготовьте расчёт и свяжитесь с ним.',
    'contact_received': 'Поблагодарил за контакт. Продолжите общение с покупателем.',
    'info_received': 'Подтвердил получение информации. Продолжите переписку при необходимости.',
    'street_quote': 'Покупатель просит расчёт для уличного киоска. Нужно подготовить предложение.',
    'needs_human': 'Я подтвердил получение вопроса. Нужен ваш ответ в Авито.',
    'turnkey_overview': 'Рассказал покупателю, как подготовим расчёт по кофейне.',
    'street_overview': 'Рассказал покупателю про уличный киоск.',
    'availability_yes': 'Подтвердил актуальность объявления.',
    'clarify_delivery_city': 'Покупатель спросил про доставку. Я уточнил, в какой город она нужна.',
    'delivery_quote_requested': 'Покупатель указал город доставки. Нужно подготовить расчёт.',
}


def settings():
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if not token and not chat_id:
        return None
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
        raise ValueError('Укажите корректный TELEGRAM_BOT_TOKEN')
    if not re.fullmatch(r'-?[0-9]+|@[A-Za-z0-9_]+', chat_id):
        raise ValueError('Укажите TELEGRAM_CHAT_ID')
    report_idle()
    return token, chat_id


async def send(client, config, text):
    token, chat_id = config
    return await client.post(
        f'https://api.telegram.org/bot{token}/sendMessage',
        json={'chat_id': chat_id, 'text': text, 'link_preview_options': {'is_disabled': True}},
    )


async def deliver(client, config, item):
    header = f"Новое сообщение в Авито\nЧат: {item['chat_id']}\n\n"
    body = item['text']
    text = header + body[:3400] + ('…' if len(body) > 3400 else '')
    return await send_report(client, config, text)


async def send_report(client, config, text):
    try:
        response = await send(client, config, text)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return 'pending', 10
    except httpx.HTTPError:
        return 'unknown', 0

    try:
        result = response.json()
    except ValueError:
        return 'unknown', 0
    if response.is_success and isinstance(result, dict) and result.get('ok') is True:
        return 'sent', 0
    if response.status_code == 429 and isinstance(result, dict):
        parameters = result.get('parameters')
        retry = parameters.get('retry_after') if isinstance(parameters, dict) else None
        return 'pending', max(1, retry) if isinstance(retry, int) else 30
    return ('unknown' if response.status_code >= 500 else 'failed'), 0


def format_report(row):
    status, action = row['reply_status'], row['template_id']
    if status == 'failed':
        outcome = 'Авито отклонило ответ. Нужен продавец.'
    elif status == 'unknown':
        outcome = 'Отправка ответа не подтверждена. Проверьте чат в Авито.'
    elif status == 'media':
        outcome = 'Вложение передано на просмотр продавцу.'
    elif status == 'sent':
        outcome = SENT_ACTIONS.get(action, 'Автоответ отправлен.')
        if action == 'clarify_intent':
            outcome = 'Уточнил, что интересует покупателя.'
    else:
        outcome = {
            'model_error': 'Не смог обработать сообщение. Нужен продавец.',
            'recovered_handoff': 'Сообщение найдено при сверке с Авито. Проверьте, нужен ли ответ.',
            'operator': 'Автоответы остановлены после исходящего сообщения или вручную.',
            'closed': 'Бот завершил свою часть диалога. Новое сообщение для продавца.',
            'stop': 'Нужен ответ продавца.',
            'handoff': 'Нужен ответ продавца.',
            'repeated_greeting': 'Повторное приветствие. Автоответ пропущен.',
            'already_replied': 'Бот уже отправил этот ответ. Повтор пропущен.',
            'waiting_contact': 'Ожидаю номер телефона или никнейм покупателя.',
        }.get(action, 'Без автоответа.')
    message = row['text'].strip() or '[Нет текста]'
    excerpt = message[:1200] + ('…' if len(message) > 1200 else '')
    return f"Авито · {row['chat_id']}\n\nСообщения\n{excerpt}\n\nДействие\n{outcome}"


def claim_report(db, now, idle):
    with db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute(
            "SELECT 1 FROM runtime_state WHERE key='telegram_retry_after' AND value>?", (now,)
        ).fetchone():
            return None
        notice = connection.execute("""
            SELECT * FROM telegram_jobs WHERE status='notice' AND due<=?
            ORDER BY due, rowid LIMIT 1
        """, (now,)).fetchone()
        if notice:
            connection.execute("UPDATE telegram_jobs SET status='sending' WHERE id=?", (notice['id'],))
            return notice['id'], notice['text'], notice['attempts']

        retry = connection.execute("""
            SELECT * FROM telegram_jobs WHERE status='report_retry' AND due<=?
            ORDER BY due LIMIT 1
        """, (now,)).fetchone()
        if retry:
            connection.execute("UPDATE telegram_jobs SET status='sending' WHERE id=?", (retry['id'],))
            return retry['id'], retry['text'], retry['attempts']

        row = connection.execute("""
            SELECT t.id, t.chat_id, CASE WHEN j.status='media' THEN t.text ELSE j.text END AS text, j.status AS reply_status, j.template_id
            FROM telegram_jobs AS t JOIN jobs AS j ON j.id=t.id
            WHERE t.status='pending' AND t.due<=?
            AND NOT EXISTS (
                SELECT 1 FROM telegram_jobs AS receipt
                WHERE receipt.id='received:' || t.id
                AND receipt.status IN ('notice','sending','report_retry')
            )
            AND j.status NOT IN (?, ?, ?, 'merged')
            ORDER BY j.created, j.rowid LIMIT 1
        """, (now, *UNFINISHED)).fetchone()
        if row is None:
            return None
        chat_id = row['chat_id']
        # Earlier inputs are included in the final job text.
        connection.execute("""
            UPDATE telegram_jobs SET status='merged'
            WHERE chat_id=? AND status='pending' AND id IN (
                SELECT id FROM jobs WHERE chat_id=? AND status='merged'
            )
        """, (chat_id, chat_id))
        report = format_report(row)
        report_id = row['id']
        connection.execute(
            "UPDATE telegram_jobs SET status='sending', text=?, attempts=0 WHERE id=?",
            (report, report_id),
        )
        return report_id, report, 0


def report_idle():
    value = os.getenv('TELEGRAM_REPORT_IDLE_SECONDS', '90').strip() or '90'
    if not value.isascii() or not value.isdigit() or int(value) < 1:
        raise ValueError('TELEGRAM_REPORT_IDLE_SECONDS должен быть целым числом от 1')
    return int(value)


async def worker(db, config):
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                item = claim_report(db, time.time(), 0)
            except sqlite3.Error:
                log.exception('Не удалось прочитать очередь Telegram')
                await asyncio.sleep(3)
                continue
            if item is None:
                await asyncio.sleep(0.5)
                continue
            report_id, text, attempts = item
            status, delay = await send_report(client, config, text)
            attempts += 1
            retry_requested = status == 'pending'
            if retry_requested:
                status = 'failed' if attempts >= 5 else 'report_retry'
            with db() as connection:
                if retry_requested:
                    connection.execute(
                        "INSERT OR REPLACE INTO runtime_state VALUES ('telegram_retry_after',?)",
                        (time.time() + delay,),
                    )
                connection.execute(
                    'UPDATE telegram_jobs SET status=?, due=?, attempts=? WHERE id=?',
                    (status, time.time() + delay, attempts, report_id),
                )
            if status == 'sent':
                log.info('Уведомление Telegram отправлено: %r', report_id)
            else:
                log.warning('Отчёт %s: %s (попытка %s)', report_id, status, attempts)
