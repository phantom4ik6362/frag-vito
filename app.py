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

from dialogue import CLOSED, INTRO_QUESTIONS, REQUIRED, decide_safely, first_greeting
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
        c.execute("""CREATE TABLE IF NOT EXISTS chat_dialogue (
            chat_id TEXT NOT NULL, preview INTEGER NOT NULL, state TEXT NOT NULL,
            PRIMARY KEY (chat_id, preview))""")
        if recover:
            # Old jobs do not reliably identify which question was last sent.
            migrated = c.execute(
                "INSERT OR IGNORE INTO runtime_state VALUES ('reply_logic_v2', 1)"
            ).rowcount
            if migrated:
                c.execute("""
                    INSERT OR IGNORE INTO chat_dialogue
                    SELECT DISTINCT chat_id, status='dry_run', ? FROM jobs
                """, (CLOSED,))
            c.execute("""
                INSERT INTO chat_dialogue (chat_id,preview,state)
                SELECT DISTINCT chat_id,0,? FROM jobs WHERE status='sending'
                ON CONFLICT(chat_id,preview) DO UPDATE SET state=excluded.state
            """, (CLOSED,))
            # A send may have succeeded before a restart: never resend automatically.
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
        if path.stem in REQUIRED and (not text or len(text) > 1000):
            raise ValueError(f'{path.name}: текст должен содержать от 1 до 1000 символов')
        suffix = INTRO_QUESTIONS.get(path.stem, ('', ''))[1]
        if path.stem in REQUIRED and len('Здравствуйте!\n\n' + text + '\n\n' + suffix) > 1000:
            raise ValueError(f'{path.name}: автоответ превышает 1000 символов с учётом приветствия и вопроса')
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
    if inserted and (status == 'media' or action == 'recovered_handoff'):
        save_chat_state(conn, chat, dry_run(), CLOSED)
    if inserted and telegram_notify.settings() and not dry_run():
        conn.execute(
            'INSERT INTO telegram_jobs (id,chat_id,text,status,due) VALUES (?,?,?,?,?)',
            (ident, chat, message or attachment, 'pending', now),
        )
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

    async def listing_title(self, chat):
        path = f"/messenger/v2/accounts/{self.cfg['AVITO_USER_ID']}/chats/{quote(chat, safe='')}"
        try:
            response = await self.request('GET', path, timeout=5)
            response.raise_for_status()
            details = response.json()
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log.warning('Не удалось получить объявление для чата %r (%s)', chat, type(exc).__name__)
            return ''
        context = details.get('context') if isinstance(details, dict) else None
        value = context.get('value') if isinstance(context, dict) else None
        title = value.get('title') if isinstance(value, dict) else None
        return title[:500] if isinstance(title, str) else ''

    async def send(self, chat, text):
        path = f"/messenger/v1/accounts/{self.cfg['AVITO_USER_ID']}/chats/{quote(chat, safe='')}/messages"
        return await self.request('POST', path, json={'type': 'text', 'message': {'text': text}})


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


def chat_state(conn, chat, preview):
    row = conn.execute(
        'SELECT state FROM chat_dialogue WHERE chat_id=? AND preview=?', (chat, preview),
    ).fetchone()
    return row['state'] if row is not None else None


def save_chat_state(conn, chat, preview, state):
    conn.execute("""
        INSERT INTO chat_dialogue (chat_id,preview,state) VALUES (?,?,?)
        ON CONFLICT(chat_id,preview) DO UPDATE SET state=excluded.state
        WHERE chat_dialogue.state!=?
    """, (chat, preview, state, CLOSED))


def record_batch(conn, batch, status, action):
    conn.execute('UPDATE jobs SET status=?, template_id=?, text=? WHERE id=?', (
        status, action, '\n'.join(row['text'] for row in batch)[-10000:], batch[-1]['id'],
    ))
    conn.executemany("UPDATE jobs SET status='merged' WHERE id=?", [(row['id'],) for row in batch[:-1]])


async def process_batch(client, api, batch):
    claimed_at = time.time()
    job = batch[-1]
    chat = job['chat_id']
    preview = dry_run()
    skipped = 'dry_run' if preview else 'skipped'
    with db() as c:
        state = chat_state(c, chat, preview)
        if is_manual(c, chat):
            record_batch(c, batch, skipped, 'operator')
            return
        if state == CLOSED:
            record_batch(c, batch, skipped, 'closed')
            return

    messages = [row['text'][:3000] for row in batch]
    # Preview never calls Avito for listing metadata.
    listing_title = ''
    if state is None and not preview and chat.startswith('u2i-') and first_greeting(messages):
        listing_title = await api.listing_title(chat)
    reply = await decide_safely(client, messages, state, templates(), log, listing_title=listing_title)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if is_manual(c, chat):
            record_batch(c, batch, skipped, 'operator')
            return
        if chat_state(c, chat, preview) == CLOSED:
            record_batch(c, batch, skipped, 'closed')
            return
        # Reconsider with recent follow-ups that arrived while classifying.
        if len(batch) < 20 and claimed_at - batch[0]['created'] < 60 and c.execute(
            "SELECT 1 FROM jobs WHERE chat_id=? AND status='pending' AND created>=? LIMIT 1",
            (chat, claimed_at),
        ).fetchone():
            c.executemany("UPDATE jobs SET status='pending' WHERE id=?", [(row['id'],) for row in batch])
            return
        if preview or not reply.text:
            save_chat_state(c, chat, preview, reply.next_state)
            record_batch(c, batch, skipped, reply.id)
            log.info('Диалог %s: %s%s', chat, reply.id, ' (DRY_RUN)' if preview else '')
            return
        record_batch(c, batch, 'sending', reply.id)

    response = None
    try:
        response = await api.send(chat, reply.text)
    except Exception as exc:
        log.warning('Неясный результат отправки в Авито (%s)', type(exc).__name__)
    if response is None:
        status, error = 'unknown', 'network error during send'
    elif response.is_success:
        status, error = 'sent', None
    else:
        status = 'unknown' if response.status_code >= 500 else 'failed'
        error = f'Avito HTTP {response.status_code}'
    outgoing = None
    if status == 'sent':
        try:
            payload = response.json()
            outgoing = payload.get('id') if isinstance(payload, dict) else None
        except ValueError:
            pass
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if type(outgoing) in (str, int) and outgoing:
            c.execute('INSERT OR IGNORE INTO bot_messages VALUES (?,?)', (str(outgoing), chat))
            next_state = reply.next_state
        else:
            next_state = CLOSED
            if status == 'sent':
                log.warning('Авито подтвердило ответ без ID; автоответы прекращены в чате %r', chat)
        save_chat_state(c, chat, False, next_state)
        c.execute('UPDATE jobs SET status=?, error=? WHERE id=?', (status, error, job['id']))
        if status == 'sent' and next_state == CLOSED and reply.next_state != CLOSED:
            queue_notice(c, 'unconfirmed:' + job['id'], chat,
                         f'Авито приняло автоответ без ID сообщения. Автоответы остановлены. Проверьте чат: {chat}')
        # Save our own message ID before deciding whether a racing outgoing event was manual.
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
                with db() as c:
                    c.execute('BEGIN IMMEDIATE')
                    for job in batch:
                        c.execute("""
                            UPDATE jobs SET status=CASE WHEN status='sending' THEN 'unknown' ELSE 'failed' END,
                            error='processing error; see logs'
                            WHERE id=? AND status IN ('pending','processing','sending')
                        """, (job['id'],))
                    save_chat_state(c, batch[-1]['chat_id'], dry_run(), CLOSED)
                    resolve_seller_events(c)
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
    if not dry_run() and not telegram:
        raise RuntimeError('Для боевого режима нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID')
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
