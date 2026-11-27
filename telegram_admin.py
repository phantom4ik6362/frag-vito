"""A separate, public Telegram bot for aggregate Avito statistics."""

import asyncio
from dataclasses import dataclass, field
import logging
import os
import re
import sqlite3

import httpx

import chat_statistics

log = logging.getLogger("avito-bot.admin")
LABELS = {label: key for key, label in chat_statistics.PERIODS.items()}
KEYBOARD = {
    "keyboard": [
        [{"text": "Сегодня"}, {"text": "Вчера"}],
        [{"text": "Последние 7 дней"}, {"text": "Последние 30 дней"}],
        [{"text": "За всё время"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)

    @property
    def bot_id(self):
        return self.token.split(":", 1)[0]


def settings():
    token = os.getenv("TELEGRAM_ADMIN_BOT_TOKEN", "").strip()
    if not token:
        return None
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ValueError("Укажите корректный TELEGRAM_ADMIN_BOT_TOKEN")
    notifications = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if notifications and token.split(":", 1)[0] == notifications.split(":", 1)[0]:
        raise ValueError("Для меню статистики нужен отдельный Telegram-бот")
    return Settings(token)


class TelegramError(Exception):
    def __init__(self, code, retry_after=5):
        super().__init__(f"Telegram HTTP {code}")
        self.code = code
        self.retry_after = retry_after


async def request(client, cfg, method, payload):
    response = await client.post(f"https://api.telegram.org/bot{cfg.token}/{method}", json=payload)
    try:
        data = response.json()
    except ValueError:
        raise TelegramError(response.status_code) from None
    if not response.is_success or not isinstance(data, dict) or data.get("ok") is not True:
        parameters = data.get("parameters") if isinstance(data, dict) else None
        retry = parameters.get("retry_after", 5) if isinstance(parameters, dict) else 5
        retry = retry if type(retry) is int and retry > 0 else 5
        raise TelegramError(response.status_code, min(retry, 60))
    return data.get("result")


async def handle_update(client, db, cfg, update):
    message = update.get("message")
    if not isinstance(message, dict):
        return
    chat = message.get("chat")
    sender = message.get("from")
    if (not isinstance(chat, dict) or type(chat.get("id")) is not int
            or not isinstance(sender, dict) or sender.get("is_bot") is True):
        return
    text = message.get("text")
    if not isinstance(text, str):
        return
    text = text.strip()
    period = LABELS.get(text)
    command = text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""
    if period is None and command not in {"/start", "/menu", "/stats", "/help"} and chat.get("type") != "private":
        return
    if period is None:
        answer = "Статистика Авито. Выберите период."
    else:
        report = await asyncio.to_thread(chat_statistics.collect, db, period)
        answer = chat_statistics.format_report(report)
    await request(client, cfg, "sendMessage", {
        "chat_id": chat["id"],
        "text": answer,
        "reply_markup": KEYBOARD,
        "link_preview_options": {"is_disabled": True},
    })


def next_update(db, bot_id):
    with db() as conn:
        row = conn.execute(
            "SELECT next_update FROM admin_telegram_state WHERE bot_id=?", (bot_id,),
        ).fetchone()
    return row["next_update"] if row else 0


async def poll_once(client, db, cfg):
    offset = next_update(db, cfg.bot_id)
    updates = await request(client, cfg, "getUpdates", {
        "offset": offset, "timeout": 25, "limit": 100, "allowed_updates": ["message"],
    })
    if not isinstance(updates, list):
        raise ValueError("Telegram вернул неверный список обновлений")
    for update in updates:
        if not isinstance(update, dict) or type(update.get("update_id")) is not int:
            continue
        update_id = update["update_id"]
        if update_id < offset:
            continue
        try:
            await handle_update(client, db, cfg, update)
        except TelegramError as exc:
            # A blocked bot cannot deliver to this user, but must serve the rest.
            if exc.code not in {400, 403}:
                raise
            log.warning("Не удалось отправить статистику получателю (HTTP %s)", exc.code)
        offset = update_id + 1
        with db() as conn:
            conn.execute("""
                INSERT INTO admin_telegram_state VALUES (?,?)
                ON CONFLICT(bot_id) DO UPDATE SET next_update=MAX(next_update,excluded.next_update)
            """, (cfg.bot_id, offset))
    return len(updates)


async def worker(db, cfg):
    # Exception messages from HTTPX contain URLs with tokens. Log only safe codes.
    async with httpx.AsyncClient(timeout=35, follow_redirects=False) as client:
        while True:
            try:
                count = await poll_once(client, db, cfg)
                if not count:
                    await asyncio.sleep(0.5)
            except TelegramError as exc:
                if exc.code == 409:
                    log.warning("Меню статистики: проверьте webhook и второй экземпляр polling")
                else:
                    log.warning("Меню статистики: Telegram HTTP %s", exc.code)
                await asyncio.sleep(exc.retry_after if exc.code == 429 else 15)
            except (httpx.HTTPError, sqlite3.Error, ValueError, TypeError):
                log.warning("Меню статистики: временная ошибка соединения, данных или базы")
                await asyncio.sleep(5)
