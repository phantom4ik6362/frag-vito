import asyncio
import logging
import os
import re
import sqlite3
import time

import httpx

log = logging.getLogger('avito-bot.telegram')
UNFINISHED = ('pending', 'processing', 'sending')
GREETINGS = re.compile(
    r'(?:(?:здравствуйте|здравствуй|привет|добрый день|добрый вечер|доброе утро|доброго времени суток)[ !.,?]*)+',
    re.IGNORECASE,
)
SENT_ACTIONS = {
    'street_quote': 'Покупатель просит расчёт для уличного киоска. Нужно подготовить предложение.',
    'needs_human': 'Я подтвердил получение вопроса. Нужен ваш ответ в Авито.',
    'turnkey_overview': 'Рассказал покупателю, как подготовим расчёт по кофейне.',
    'street_overview': 'Рассказал покупателю про уличный киоск и сообщил цену павильона.',
    'availability_yes': 'Подтвердил, что объявление актуально, и уточнил, какая комплектация интересует покупателя.',
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


def format_report(rows, continuation):
    title = 'Покупатель снова написал в Авито.' if continuation else 'У вас новый диалог в Авито.'
    messages = [row['text'].strip() for row in rows]
    actions = {row['template_id'] for row in rows if row['reply_status'] == 'sent'}
    if 'delivery_quote_requested' in actions:
        actions.discard('clarify_delivery_city')
    details = [line for action, line in SENT_ACTIONS.items() if action in actions]
    if actions - SENT_ACTIONS.keys() - {'clarify_intent'}:
        details.append('Отправил покупателю ответ по его вопросу.')
    if 'clarify_intent' in actions:
        if all(GREETINGS.fullmatch(message) for message in messages):
            details.append('Покупатель поздоровался. Я спросил, чем помочь, и пока ответа нет.')
        elif not details:
            details.append('Я уточнил, что интересует покупателя. Посмотрите, пожалуйста, переписку.')
    if any(row['reply_status'] in {'failed', 'unknown'} for row in rows):
        details.append('Проверьте переписку: отправка автоответа не подтверждена.')
    elif any(row['template_id'] == 'model_error' for row in rows):
        details.append('Я не смог обработать сообщение. Посмотрите, пожалуйста, переписку.')
    elif any(row['template_id'] == 'recovered_handoff' for row in rows):
        details.append('При сверке с Авито нашли входящее сообщение. Проверьте, нужен ли ответ покупателю.')
    elif any(row['template_id'] == 'operator' for row in rows):
        details.append('Диалог ведёт продавец. Автоответ не отправлен.')
    elif any(row['template_id'] in {'stop', 'handoff'} for row in rows):
        details.append('Дальше нужен ваш ответ в Авито.')
    elif not details:
        if all(row['reply_status'] == 'media' for row in rows):
            details.append('Покупатель прислал вложение. Откройте переписку, чтобы его посмотреть.')
        elif all(GREETINGS.fullmatch(message) for message in messages):
            details.append('Покупатель поздоровался. Вопроса пока нет.')
        else:
            details.append('Я оставил сообщение без автоответа. Посмотрите, пожалуйста, переписку.')
    elif actions & {'street_overview', 'turnkey_overview', 'availability_yes'}:
        details.append('Посмотрите переписку, чтобы продолжить общение.')

    if not all(GREETINGS.fullmatch(message) for message in messages):
        quotes = list(dict.fromkeys(message for message in messages if message and not GREETINGS.fullmatch(message)))
        if quotes:
            recent = quotes[-3:]
            details.append('Последние сообщения покупателя:\n' + '\n'.join(
                '«' + message[:320] + ('…' if len(message) > 320 else '') + '»'
                for message in recent
            ))
    return title + '\n\n' + '\n\n'.join(details)


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

        rows = connection.execute("""
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
        """, (now, *UNFINISHED)).fetchall()
        if not rows:
            return None
        chat_id = rows[0]['chat_id']
        # Earlier inputs in a processed batch already have individual receipt notices.
        connection.execute("""
            UPDATE telegram_jobs SET status='merged'
            WHERE chat_id=? AND status='pending' AND id IN (
                SELECT id FROM jobs WHERE chat_id=? AND status='merged'
            )
        """, (chat_id, chat_id))
        continuation = connection.execute("""
            SELECT 1 FROM telegram_jobs
            WHERE chat_id=? AND status IN ('sent', 'unknown')
            AND id NOT LIKE 'received:%' AND id NOT LIKE 'operator:%' LIMIT 1
        """, (chat_id,)).fetchone() is not None
        report = format_report(rows, continuation) + '\n\nЧат: ' + chat_id
        report_id = rows[-1]['id']
        connection.executemany(
            "UPDATE telegram_jobs SET status='merged' WHERE id=?",
            [(row['id'],) for row in rows[:-1]],
        )
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
