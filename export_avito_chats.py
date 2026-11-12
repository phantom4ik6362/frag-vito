"""Export Avito chat history for analysis using the project's .env."""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app import AvitoClient

PAGE_SIZE = 100
MAX_OFFSET = 1000


def redact(text):
    text = re.sub(r'https?://[^\s]+|www\.[^\s]+', '[ссылка]', text, flags=re.I)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[\w-]+', '[email]', text)
    text = re.sub(r'(?<!\w)@[\w.]+', '[ник]', text)

    def mask_phone(match):
        number = match.group()
        return '[телефон]' if 10 <= sum(c.isdigit() for c in number) <= 15 else number

    return re.sub(r'(?<!\w)\+?\d(?:[ ().-]{0,3}\d){8,13}\d(?!\w)', mask_phone, text)


async def pages(api, path, field, limit=PAGE_SIZE, *, item_ids=None):
    seen = set()
    for offset in range(0, MAX_OFFSET + 1, limit):
        params = {'limit': limit, 'offset': offset}
        if item_ids:
            params['item_ids'] = item_ids
        for attempt in range(4):
            response = await api.request('GET', path, params=params)
            if response.status_code not in (429, 500, 502, 503, 504) or attempt == 3:
                break
            retry_after = response.headers.get('Retry-After', '')
            delay = int(retry_after) if retry_after.isdigit() else 2 ** attempt
            if delay > 60:
                raise RuntimeError('Авито попросил подождать более минуты. Повтори выгрузку позже.')
            await asyncio.sleep(delay)
        response.raise_for_status()
        payload = response.json()
        rows = payload.get(field) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError(f'Авито вернул неожиданный формат списка {field}.')
        for row in rows:
            row_id = row.get('id')
            if row_id is not None and row_id in seen:
                continue
            if row_id is not None:
                seen.add(row_id)
            yield row
        if len(rows) < limit:
            return
    print(f'Внимание: достигнут предел пагинации для {field}; выгрузка может быть неполной.', file=sys.stderr)


def normalize_message(message, account, tz, since, until):
    created = message.get('created')
    if not isinstance(created, (int, float)):
        raise ValueError('В сообщении отсутствует числовой timestamp created.')
    timestamp = datetime.fromtimestamp(created, tz=timezone.utc).astimezone(tz)
    if since and timestamp.date() < since or until and timestamp.date() > until:
        return None
    kind = message.get('type')
    direction = message.get('direction')
    if kind == 'system':
        direction = 'system'
    elif direction not in ('in', 'out'):
        author = message.get('author_id')
        direction = 'unknown' if author is None else ('out' if str(author) == account else 'in')
    content = message.get('content')
    text = content.get('text') if isinstance(content, dict) else None
    return {
        'created_at': timestamp.isoformat(),
        'unix_time': created,
        'direction': direction,
        'type': kind,
        'text': redact(text) if isinstance(text, str) else None,
    }


async def export(args, output):
    cfg = {key: os.getenv(key, '').strip() for key in (
        'AVITO_CLIENT_ID', 'AVITO_CLIENT_SECRET', 'AVITO_USER_ID',
    )}
    for key, value in cfg.items():
        if not value:
            raise ValueError(f'Заполни {key} в .env.')
    account = cfg['AVITO_USER_ID']
    if not re.fullmatch(r'[0-9]+', account):
        raise ValueError('AVITO_USER_ID должен быть числом.')
    tz = ZoneInfo(args.tz)
    total_chats = total_messages = scanned = 0
    listing_ids = {}
    skipped_402 = 0
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        api = AvitoClient(client, cfg)
        chat_path = f'/messenger/v2/accounts/{account}/chats'
        chat_limit = PAGE_SIZE if args.all else 1
        async for chat in pages(api, chat_path, 'chats', chat_limit, item_ids=args.item_ids):
            scanned += 1
            chat_id = quote(str(chat['id']), safe='')
            path = f'/messenger/v3/accounts/{account}/chats/{chat_id}/messages/'
            messages = []
            try:
                async for row in pages(api, path, 'messages'):
                    message = normalize_message(row, account, tz, args.since, args.until)
                    if message is not None:
                        messages.append(message)
            except httpx.HTTPStatusError as exc:
                is_chat_history = exc.request.url.raw_path.split(b'?', 1)[0] == path.encode()
                if not args.all or exc.response.status_code != 402 or not is_chat_history:
                    raise
                skipped_402 += 1
                print(f'Чат chat-{scanned} пропущен: история вернула HTTP 402.', file=sys.stderr)
                continue

            if messages:
                total_chats += 1
                total_messages += len(messages)
                context = chat.get('context') or {}
                listing = (context.get('value') or {}).get('id')
                listing_key = None
                if listing is not None:
                    listing_key = listing_ids.setdefault(str(listing), f'listing-{len(listing_ids) + 1}')
                # Avito returns newest first. Reverse before sorting to preserve
                # the API's relative order for messages sharing a timestamp.
                messages.reverse()
                messages.sort(key=lambda message: message['unix_time'])
                output.write(json.dumps({
                    'chat_id': f'chat-{scanned}',
                    'listing_id': listing_key,
                    'messages': messages,
                }, ensure_ascii=False) + '\n')
                output.flush()

            if not args.all:
                break
            if scanned % 25 == 0:
                print(f'Проверено чатов: {scanned}. Выгружено: {total_chats}.', file=sys.stderr)

    print(f'Проверено чатов: {scanned}. Выгружено чатов: {total_chats}, сообщений: {total_messages}.')
    if skipped_402:
        print(f'Выгрузка неполная: пропущено чатов с HTTP 402: {skipped_402}.', file=sys.stderr)
    return total_chats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--item-ids', help='Числовые ID объявлений через запятую.')
    parser.add_argument('--all', action='store_true', help='Обойти все доступные страницы чатов.')
    parser.add_argument('--since', type=date.fromisoformat, help='Первая дата включительно, YYYY-MM-DD.')
    parser.add_argument('--until', type=date.fromisoformat, help='Последняя дата включительно, YYYY-MM-DD.')
    parser.add_argument('--tz', default='UTC', help='Часовой пояс дат, например Europe/Moscow.')
    parser.add_argument('--out', type=Path, required=True, help='Новый файл JSONL.')
    args = parser.parse_args()
    if args.item_ids is not None:
        ids = [item.strip() for item in args.item_ids.split(',')]
        if any(not re.fullmatch(r'[1-9][0-9]*', item) for item in ids):
            parser.error('--item-ids: укажи числовые ID объявлений через запятую.')
        args.item_ids = ','.join(dict.fromkeys(ids))
    if args.since and args.until and args.since > args.until:
        parser.error('--since должен быть не позже --until.')
    logging.getLogger('httpx').setLevel(logging.WARNING)

    try:
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        parser.exit(1, f'Не удалось создать файл: {exc}\n')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            count = asyncio.run(export(args, output))
    except httpx.HTTPStatusError as exc:
        path = exc.request.url.path.rstrip('/')
        stage = 'история чата' if path.endswith('/messages') else ('список чатов' if path.endswith('/chats') else 'авторизация')
        parser.exit(1, f'Авито вернул HTTP {exc.response.status_code}, этап: {stage}. Файл может быть неполным: {args.out}\n')
    except (httpx.RequestError, OSError, ValueError, KeyError, RuntimeError, ZoneInfoNotFoundError, KeyboardInterrupt) as exc:
        detail = type(exc).__name__ if isinstance(exc, httpx.RequestError) else str(exc)
        parser.exit(1, f'Выгрузка прервана ({detail}). Файл может быть неполным: {args.out}\n')
    if count == 0:
        print('Нет сообщений в выбранном диапазоне. Для проверки одного чата убери фильтр дат.')
    print(f'Файл: {args.out}')


if __name__ == '__main__':
    main()
