import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

from dialogue import REQUIRED, decide_safely
import telegram_notify
import reconcile

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'), format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('avito-bot')
# HTTPX request logs include the Telegram token in the URL.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
MESSAGES = BASE / 'messages'
DB = Path(os.getenv('DB_PATH', str(BASE / 'data' / 'queue.sqlite3')))
AVITO = 'https://api.avito.ru'


def config():
    result = {k: os.getenv(k, '').strip() for k in ('AVITO_CLIENT_ID', 'AVITO_CLIENT_SECRET', 'AVITO_USER_ID', 'WEBHOOK_SECRET', 'LLM_API_KEY')}
    for k, v in result.items():
        if not v:
            raise RuntimeError(f'Укажите {k} в .env')
    if not result['AVITO_USER_ID'].isdigit():
        raise RuntimeError('AVITO_USER_ID должен быть числовым')
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,}', result['WEBHOOK_SECRET']):
        raise RuntimeError('WEBHOOK_SECRET: минимум 32 символа A-Z, a-z, 0-9, _ или -')
    return result


def dry_run():
    return os.getenv('DRY_RUN', 'true').strip().lower() == 'true'


def db():
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(recover=True):
    DB.parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute("CREATE TABLE IF NOT EXISTS runtime_state (key TEXT PRIMARY KEY, value REAL)")
        c.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, text TEXT NOT NULL,
            status TEXT NOT NULL, created REAL NOT NULL, due REAL NOT NULL,
            template_id TEXT, error TEXT)''')
        c.execute("CREATE INDEX IF NOT EXISTS jobs_chat_created ON jobs(chat_id, created)")
        c.execute("""CREATE TABLE IF NOT EXISTS telegram_jobs (
            id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, text TEXT NOT NULL,
            status TEXT NOT NULL, due REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS seller_events (
            id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, handled INTEGER NOT NULL DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS bot_messages (
            id TEXT PRIMARY KEY, chat_id TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS manual_chats (
            chat_id TEXT PRIMARY KEY)""")
        c.execute("CREATE INDEX IF NOT EXISTS seller_unhandled ON seller_events(handled, chat_id)")
        c.execute("CREATE INDEX IF NOT EXISTS jobs_status_due ON jobs(status, due)")
        c.execute("CREATE INDEX IF NOT EXISTS telegram_ready ON telegram_jobs(status, due)")
        # A send may have succeeded before a restart: never resend automatically.
        if recover:
            c.execute("UPDATE jobs SET status='unknown', error='restart during send' WHERE status='sending'")
            c.execute("UPDATE jobs SET status='pending' WHERE status='processing'")
            c.execute("UPDATE telegram_jobs SET status='unknown' WHERE status='sending'")
            resolve_seller_events(c)


def templates():
    found = {}
    for path in sorted(MESSAGES.glob('*.txt')):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', path.stem):
            raise ValueError(f'Недопустимый ID файла: {path.name}')
        text = path.read_text(encoding='utf-8').strip()
        if not text or len(text) > 1000:
            raise ValueError(f'{path.name}: текст должен содержать от 1 до 1000 символов')
        found[path.stem] = text
    missing = REQUIRED - found.keys()
    if missing:
        raise ValueError('Отсутствуют шаблоны: ' + ', '.join(sorted(missing)))
    return found


def parse_event(data, own_id, include_media=False):
    if not isinstance(data, dict) or not isinstance(data.get('payload'), dict):
        return None, 'payload'
    p = data['payload']
    if p.get('type') != 'message' or not isinstance(p.get('value'), dict):
        return None, 'event_type'
    v = p['value']
    # Require all identifiers; never trust a user ID from an unexpected account.
    if any(v.get(k) is None or str(v.get(k)).strip() == '' for k in ('id', 'chat_id', 'user_id', 'author_id')):
        return None, 'missing_id'
    if str(v['user_id']) != own_id:
        return None, 'wrong_account'
    kind = v.get('type')
    if len(str(v['id'])) > 200 or len(str(v['chat_id'])) > 200:
        return None, 'id_length'
    if str(v['author_id']) == own_id:
        if isinstance(kind, str) and kind != 'system':
            return None, 'seller'
        return None, 'message_type'
    if kind != 'text':
        if include_media and isinstance(kind, str) and kind != 'system':
            return (str(v['id']), str(v['chat_id']), None), None
        return None, 'message_type'
    content = v.get('content')
    if not isinstance(content, dict) or not isinstance(content.get('text'), str) or not content['text'].strip():
        return None, 'empty_text'
    if len(content['text']) > 10000:
        return None, 'text_length'
    return (str(v['id']), str(v['chat_id']), content['text'].strip()), None


def extract_event(data, own_id, include_media=False):
    event, _ = parse_event(data, own_id, include_media)
    return event


def queue_notice(conn, ident, chat, text):
    if telegram_notify.settings() and not dry_run():
        conn.execute(
            "INSERT OR IGNORE INTO telegram_jobs (id,chat_id,text,status,due) VALUES (?,?,?,'notice',?)",
            (ident, chat, text, time.time()),
        )


def resolve_seller_events(conn):
    events = conn.execute("""
        SELECT e.* FROM seller_events AS e WHERE handled=0
        AND NOT EXISTS (SELECT 1 FROM jobs WHERE chat_id=e.chat_id AND status='sending')
    """).fetchall()
    for event in events:
        ident, chat = event['id'], event['chat_id']
        known = conn.execute('SELECT 1 FROM bot_messages WHERE id=? AND chat_id=?', (ident, chat)).fetchone()
        if not known:
            changed = conn.execute('INSERT OR IGNORE INTO manual_chats VALUES (?)', (chat,)).rowcount
            if changed:
                queue_notice(conn, 'operator:' + ident, chat,
                             f'В чате Авито появилось исходящее сообщение. Бот не смог подтвердить, что это его сообщение. Автоответы приостановлены. Проверьте переписку.\nЧат: {chat}')
        conn.execute('UPDATE seller_events SET handled=1 WHERE id=?', (ident,))


def is_manual(conn, chat):
    return conn.execute('SELECT 1 FROM manual_chats WHERE chat_id=?', (chat,)).fetchone() is not None


def enqueue(conn, ident, chat, message, status, action=None, attachment='[Вложение]'):
    now = time.time()
    delay = max(0, float(os.getenv('REPLY_DELAY_SECONDS', '5')))
    inserted = conn.execute(
        'INSERT OR IGNORE INTO jobs (id,chat_id,text,status,created,due,template_id) VALUES (?,?,?,?,?,?,?)',
        (ident, chat, message or '', status, now, now + delay, action),
    ).rowcount
    if inserted and telegram_notify.settings() and not dry_run():
        conn.execute(
            'INSERT INTO telegram_jobs (id,chat_id,text,status,due) VALUES (?,?,?,?,?)',
            (ident, chat, message or attachment, 'pending', now),
        )
        if status in {'pending', 'media'}:
            excerpt = (message or attachment)[:1000]
            queue_notice(conn, 'received:' + ident, chat, f'Новое сообщение в Авито\nЧат: {chat}\n\n{excerpt}')
    return bool(inserted)


class AvitoClient:
    def __init__(self, client, cfg):
        self.client, self.cfg = client, cfg
        self.token, self.expiry = '', 0

    async def get_token(self):
        if time.time() < self.expiry:
            return self.token
        r = await self.client.post(AVITO + '/token', data={'grant_type': 'client_credentials', 'client_id': self.cfg['AVITO_CLIENT_ID'], 'client_secret': self.cfg['AVITO_CLIENT_SECRET']})
        r.raise_for_status()
        value = r.json()
        self.token = value['access_token']
        self.expiry = time.time() + max(0, int(value.get('expires_in', 3600)) - 60)
        return self.token

    async def request(self, method, path, **kwargs):
        token = await self.get_token()
        r = await self.client.request(method, AVITO + path, headers={'Authorization': 'Bearer ' + token}, **kwargs)
        if r.status_code == 401:
            self.expiry = 0
            token = await self.get_token()
            r = await self.client.request(method, AVITO + path, headers={'Authorization': 'Bearer ' + token}, **kwargs)
        return r

    async def send(self, chat, text):
        path = f"/messenger/v1/accounts/{self.cfg['AVITO_USER_ID']}/chats/{quote(chat, safe='')}/messages"
        return await self.request('POST', path, json={'type': 'text', 'message': {'text': text}})


def update(job, status, error=None, template_id=None):
    with db() as c:
        c.execute('UPDATE jobs SET status=?, error=?, template_id=COALESCE(?,template_id) WHERE id=?', (status, error, template_id, job))


def claim_batch():
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        first = c.execute("""
            SELECT * FROM jobs AS j WHERE status='pending' AND due<=?
            AND (j.created<=? OR NOT EXISTS (
                SELECT 1 FROM jobs AS newer WHERE newer.chat_id=j.chat_id
                AND newer.status='pending' AND newer.due>?
            )) ORDER BY created, rowid LIMIT 1
        """, (time.time(), time.time() - 60, time.time())).fetchone()
        if first is None:
            return []
        batch = c.execute("""
            SELECT * FROM jobs WHERE chat_id=? AND status='pending'
            ORDER BY created, rowid LIMIT 20
        """, (first['chat_id'],)).fetchall()
        c.executemany("UPDATE jobs SET status='processing' WHERE id=?", [(row['id'],) for row in batch])
        return batch


def history_for(chat, preview):
    statuses = ('dry_run',) if preview else ('sent', 'skipped', 'unknown')
    placeholders = ','.join('?' for _ in statuses)
    with db() as c:
        rows = c.execute(f"""
            SELECT text, template_id FROM jobs WHERE chat_id=?
            AND status IN ({placeholders}) AND template_id IS NOT NULL AND template_id!='recovered_handoff'
            ORDER BY created DESC, rowid DESC LIMIT 16
        """, (chat, *statuses)).fetchall()
        sticky = c.execute(f"""
            SELECT 1 FROM jobs WHERE chat_id=?
            AND status IN ({placeholders}) AND template_id='stop' LIMIT 1
        """, (chat, *statuses)).fetchone()
    result = [{'text': row['text'][-2000:], 'action': row['template_id']} for row in reversed(rows)]
    if sticky:
        result.append({'text': '', 'action': 'stop'})
    return result


def record_batch(batch, status, action):
    with db() as c:
        c.execute('UPDATE jobs SET status=?, template_id=?, text=? WHERE id=?', (
            status, action, '\n'.join(row['text'] for row in batch)[-10000:], batch[-1]['id'],
        ))
        c.executemany("UPDATE jobs SET status='merged' WHERE id=?", [(row['id'],) for row in batch[:-1]])


async def process_batch(client, api, batch):
    claimed_at = time.time()
    job = batch[-1]
    preview = dry_run()
    with db() as c:
        manual = is_manual(c, job['chat_id'])
    history = history_for(job['chat_id'], preview)
    if manual:
        record_batch(batch, 'dry_run' if preview else 'skipped', 'operator')
        return
    reply = await decide_safely(client, [row['text'][:3000] for row in batch], history, templates(), log)
    # If a buyer wrote again while classification was running, reconsider the batch.
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if claimed_at - batch[0]['created'] < 60 and c.execute(
            "SELECT 1 FROM jobs WHERE chat_id=? AND status='pending' AND created>? LIMIT 1",
            (job['chat_id'], claimed_at),
        ).fetchone():
            c.executemany("UPDATE jobs SET status='pending' WHERE id=?", [(row['id'],) for row in batch])
            return
    with db() as c:
        manual = is_manual(c, job['chat_id'])
    if manual:
        record_batch(batch, 'dry_run' if preview else 'skipped', 'operator')
        return
    if preview or not reply.text:
        record_batch(batch, 'dry_run' if preview else 'skipped', reply.id)
        log.info('Диалог %s: %s%s', job['chat_id'], reply.id, ' (DRY_RUN)' if preview else '')
        return

    record_batch(batch, 'sending', reply.id)
    try:
        response = await api.send(job['chat_id'], reply.text)
    except Exception:
        update(job['id'], 'unknown', 'network error during send')
        with db() as c:
            resolve_seller_events(c)
        log.exception('Неясный результат отправки, нужна ручная проверка')
        return
    if response.is_success:
        try:
            payload = response.json()
            outgoing = payload.get('id') if isinstance(payload, dict) else None
        except ValueError:
            outgoing = None
        with db() as c:
            if isinstance(outgoing, (str, int)) and outgoing:
                c.execute('INSERT OR IGNORE INTO bot_messages VALUES (?,?)', (str(outgoing), job['chat_id']))
            else:
                log.warning('Авито подтвердило ответ без ID; исходящее событие может включить ручной режим')
            c.execute("UPDATE jobs SET status='sent' WHERE id=?", (job['id'],))
            resolve_seller_events(c)
    else:
        status = 'unknown' if response.status_code >= 500 else 'failed'
        update(job['id'], status, f'Avito HTTP {response.status_code}')
        with db() as c:
            resolve_seller_events(c)


async def worker(cfg):
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        api = AvitoClient(client, cfg)
        while True:
            try:
                batch = claim_batch()
            except sqlite3.Error:
                log.exception('Не удалось прочитать очередь Авито')
                await asyncio.sleep(3)
                continue
            if not batch:
                await asyncio.sleep(0.5)
                continue
            try:
                await process_batch(client, api, batch)
            except Exception:
                for job in batch:
                    update(job['id'], 'failed', 'processing error; see logs')
                log.exception('Не удалось обработать сообщения')


async def reconcile_worker(cfg, interval):
    async with httpx.AsyncClient(timeout=15) as client:
        await reconcile.worker(AvitoClient(client, cfg), db, enqueue, cfg['AVITO_USER_ID'], interval, log)


def watch(task):
    if not task.cancelled():
        error = task.exception()
        log.error('Фоновая задача %s завершилась (%s)', task.get_name(), type(error).__name__)


@asynccontextmanager
async def lifespan(app):
    cfg = config()
    templates()
    telegram = telegram_notify.settings()
    init_db()
    interval = int(os.getenv('AVITO_RECONCILE_SECONDS', '0'))
    tasks = [asyncio.create_task(worker(cfg), name='avito')]
    if telegram and not dry_run():
        tasks.append(asyncio.create_task(telegram_notify.worker(db, telegram), name='telegram'))
    if interval > 0 and not dry_run():
        tasks.append(asyncio.create_task(reconcile_worker(cfg, max(60, interval)), name='reconcile'))
    app.state.workers = tasks
    for task in tasks:
        task.add_done_callback(watch)
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

@app.get('/health')
async def health():
    if any(task.done() for task in getattr(app.state, 'workers', ())):
        raise HTTPException(status_code=503, detail='background worker stopped')
    try:
        with db() as conn:
            if telegram_notify.settings() and not dry_run() and conn.execute(
                "SELECT 1 FROM telegram_jobs WHERE status IN ('failed','unknown') LIMIT 1"
            ).fetchone():
                raise HTTPException(status_code=503, detail='Telegram delivery needs review')
            if conn.execute("SELECT 1 FROM telegram_jobs WHERE status IN ('notice','report_retry') AND due<? LIMIT 1",
                            (time.time() - 180,)).fetchone():
                raise HTTPException(status_code=503, detail='Telegram queue overdue')
            if conn.execute("SELECT 1 FROM jobs WHERE status IN ('pending','processing','sending') AND due<? LIMIT 1",
                            (time.time() - 180,)).fetchone():
                raise HTTPException(status_code=503, detail='Avito queue overdue')
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail='database unavailable') from None
    return {'status': 'ok'}

@app.post('/webhook/{secret}')
async def webhook(secret: str, request: Request):
    cfg = config()
    if not hmac.compare_digest(secret, cfg['WEBHOOK_SECRET']):
        log.warning('Вебхук Авито: неверный секрет')
        raise HTTPException(status_code=404)
    body = await request.body()
    if len(body) > 65536:
        raise HTTPException(status_code=413)
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400)
    event, reason = parse_event(data, cfg['AVITO_USER_ID'], include_media=True)
    if reason == 'seller':
        value = data['payload']['value']
        with db() as c:
            c.execute('INSERT OR IGNORE INTO seller_events (id,chat_id) VALUES (?,?)',
                      (str(value['id']), str(value['chat_id'])))
            resolve_seller_events(c)
        log.info('Вебхук Авито: исходящее событие chat=%r', value['chat_id'])
        return {'ok': True}
    if event is None:
        log.info('Вебхук Авито: filtered reason=%s', reason)
        return {'ok': True}
    ident, chat, message = event
    media_type = str(data['payload']['value'].get('type', ''))[:80]
    with db() as conn:
        inserted = enqueue(
            conn, ident, chat, message, 'pending' if message else 'media',
            attachment=f'[Вложение: {media_type}]',
        )
    log.info('Вебхук Авито: %s chat=%r', 'queued' if inserted else 'duplicate', chat)
    return {'ok': True}
