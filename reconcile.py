"""Recover recent missing inbound messages for seller review."""

import asyncio
import time
from urllib.parse import quote


async def scan(api, db, enqueue, account, since, log, checked=None):
    checked = {} if checked is None else checked
    cutoff = time.time() - 60
    response = await api.request(
        'GET', f'/messenger/v2/accounts/{account}/chats', params={'limit': 100},
    )
    if not response.is_success:
        log.warning('Сверка Авито: HTTP %s', response.status_code)
        return
    for chat in response.json()['chats']:
        last = chat.get('last_message') or {}
        chat_id = chat.get('id')
        if not chat_id or not isinstance(last.get('created'), (float, int)) or last['created'] < since:
            continue
        marker = (last.get('id'), last['created'])
        if marker[1] > cutoff or checked.get(chat_id) == marker:
            continue
        path = f'/messenger/v3/accounts/{account}/chats/{quote(str(chat_id), safe="")}/messages/'
        history = await api.request('GET', path, params={'limit': 100})
        if not history.is_success:
            log.warning('Сверка истории Авито: HTTP %s', history.status_code)
            continue
        for message in reversed(history.json()['messages']):
            created, ident = message.get('created'), message.get('id')
            if not ident or not isinstance(created, (float, int)) or created < since:
                continue
            if message.get('direction') != 'in' or message.get('type') == 'system':
                continue
            content = message.get('content') or {}
            raw = content.get('text')
            body = raw[:10000] if isinstance(raw, str) and raw.strip() else '[Вложение]'
            with db() as conn:
                if enqueue(conn, str(ident), str(chat_id), body, 'skipped', 'recovered_handoff'):
                    log.warning('Сверка Авито: найдено обращение для продавца, чат %r', chat_id)
        checked[chat_id] = marker


async def worker(api, db, enqueue, account, interval, log):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO runtime_state (key,value) VALUES ('reconcile_since',?)",
            (time.time(),),
        )
        since = conn.execute("SELECT value FROM runtime_state WHERE key='reconcile_since'").fetchone()[0]
    checked = {}
    while True:
        await asyncio.sleep(interval)
        try:
            await scan(api, db, enqueue, account, since, log, checked)
        except Exception as exc:
            log.warning('Ошибка сверки Авито (%s)', type(exc).__name__)
