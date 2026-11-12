"""Interactively test template selection and validate a mock Avito POST."""

import asyncio
import json
import logging
import os
from urllib.parse import parse_qs

import httpx

from app import AvitoClient, extract_event, templates
from dialogue import decide_safely

TEST_ACCOUNT = "123456"
TEST_CHAT = "dry-run-chat"
TEST_TOKEN = "dry-run-token"
TEST_CONFIG = {
    "AVITO_USER_ID": TEST_ACCOUNT,
    "AVITO_CLIENT_ID": "dry-run-client",
    "AVITO_CLIENT_SECRET": "dry-run-secret",
}


def mock_avito(request: httpx.Request) -> httpx.Response:
    if request.method != "POST" or request.url.host != "api.avito.ru":
        return httpx.Response(400, text="Ожидался POST к api.avito.ru")

    if request.url.path == "/token":
        form = parse_qs(request.content.decode())
        if form != {
            "grant_type": ["client_credentials"],
            "client_id": [TEST_CONFIG["AVITO_CLIENT_ID"]],
            "client_secret": [TEST_CONFIG["AVITO_CLIENT_SECRET"]],
        }:
            return httpx.Response(400, text="Неверная форма запроса токена")
        return httpx.Response(200, json={"access_token": TEST_TOKEN, "expires_in": 3600})

    print(f"\n{request.method} {request.url}")
    expected_path = f"/messenger/v1/accounts/{TEST_ACCOUNT}/chats/{TEST_CHAT}/messages"
    if request.url.path != expected_path or request.url.query:
        return httpx.Response(400, text="Неверный URL отправки сообщения")
    if request.headers.get("authorization") != f"Bearer {TEST_TOKEN}":
        return httpx.Response(401, text="Отсутствует тестовый Bearer-токен")
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0] != "application/json":
        return httpx.Response(400, text="Ожидался Content-Type: application/json")

    print(f"Content-Type: {content_type}")
    print("Authorization: Bearer <тестовый токен>")
    try:
        body = json.loads(request.content)
        message = body["message"]["text"]
    except (ValueError, KeyError, TypeError):
        return httpx.Response(400, text="Ожидалось поле message.text")
    print(json.dumps(body, ensure_ascii=False, indent=2))
    if body.get("type") != "text" or not isinstance(message, str):
        return httpx.Response(400, text="Ожидались type=text и строка message.text")
    if not message.strip() or len(message) > 1000:
        return httpx.Response(400, text="Текст должен содержать от 1 до 1000 символов")
    return httpx.Response(200, json={"id": "dry-run-message", "type": "text"})


async def preview(question, llm, avito, history):
    event = {"payload": {"type": "message", "value": {
        "id": "dry-run-incoming",
        "chat_id": TEST_CHAT,
        "user_id": TEST_ACCOUNT,
        "author_id": "654321",
        "type": "text",
        "content": {"text": question},
    }}}
    incoming = extract_event(event, TEST_ACCOUNT)
    if incoming is None:
        print("Введите непустое сообщение до 10 000 символов.")
        return

    _, chat_id, text = incoming
    options = templates()
    reply = await decide_safely(llm, [text], history, options, logging.getLogger(__name__))
    print(f"\nРешение: {reply.id}")
    if not reply.text:
        history.append({'text': text, 'action': reply.id})
        print("Автоответ пропущен.\n")
        return
    print(f"Ответ: {reply.text}")
    response = await avito.send(chat_id, reply.text)
    if response.is_success:
        history.append({'text': text, 'action': reply.id})
    if response.is_success:
        print("Локальная проверка POST: OK (имитация HTTP 200).\n")
    else:
        print(f"Локальная проверка POST: HTTP {response.status_code}. {response.text}\n")


async def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not os.getenv("LLM_API_KEY", "").strip():
        raise SystemExit("Укажите LLM_API_KEY и настройки модели в .env.")
    print("Тестовый CLI. Модель вызывается через API, Avito имитируется локально.")
    print("Введите сообщение покупателя. Для выхода введите /exit.")
    async with (
        httpx.AsyncClient(timeout=20) as llm,
        httpx.AsyncClient(transport=httpx.MockTransport(mock_avito)) as avito_http,
    ):
        avito = AvitoClient(avito_http, TEST_CONFIG)
        history = []
        while True:
            try:
                question = input("Покупатель> ").strip()
            except EOFError:
                break
            if question == "/exit":
                break
            if question:
                try:
                    await preview(question, llm, avito, history)
                except (OSError, ValueError) as exc:
                    print(f"Ошибка файлов шаблонов или промпта: {exc}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
