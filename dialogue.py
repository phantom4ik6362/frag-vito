"""Choose a short reply and the next state of one Avito chat."""

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent
INTRO_QUESTIONS = {
    'street_overview': ('installation_city', 'Подскажите, в каком городе планируете установку?'),
    'street_quote': ('installation_city', 'Подскажите, в каком городе планируете установку?'),
    'turnkey_overview': ('location_status', 'Подскажите, локацию уже выбрали или ещё ищете?'),
}
REQUIRED = set(INTRO_QUESTIONS) | {
    'availability_yes', 'clarify_delivery_city', 'delivery_quote_requested',
    'contact_received', 'info_received',
}
NEW_ACTIONS = set(INTRO_QUESTIONS) | {
    'availability_yes', 'clarify_delivery_city', 'delivery_quote_requested', 'handoff',
}
CLOSED = 'closed'
WAITING_QUESTIONS = {
    state: question for state, question in INTRO_QUESTIONS.values()
} | {'delivery_destination': 'В какой город или населённый пункт нужна доставка?'}

PHONE = re.compile(r'(?<![\w+])(?:\+7|7|8)?[\s().-]*9(?:[\s().-]*[0-9]){9}(?![\w+])')
HANDLE = re.compile(r'(?<![\w@])@[A-Za-z][A-Za-z0-9_]{4,31}(?![\w.])')
CONTACT_CUE = re.compile(
    r'\b(?:перезвон\w*|позвон\w*|наберите|свяж\w*|тел(?:ефон\w*)?|мой номер|'
    r'мой контакт|пишите|напишите|ватсап\w*|вацап\w*|whatsapp|telegram|телеграм\w*|'
    r'тг|max|мах|макс)\b'
)
NO_CONTACT = re.compile(
    r'\b(?:не\s+(?:надо\s+)?(?:звон\w*|перезван\w*|перезвон\w*|пишите|позвон\w*|связыва\w*)'
    r'|(?:не мой|чужой) номер)\b'
)
MONEY = re.compile(r'₽|\b(?:руб\w*|цен[аыуе]|стоимост\w*|бюджет\w*|артикул\w*|объявление\s*№)\b')
GREETING = re.compile(r'(?:здравствуй(?:те)?|привет(?:ствую)?|добр(?:ый (?:день|вечер)|ое утро))[\s!.,🙂👋]*')
DELIVERY = re.compile(r'\b(?:достав\w*|(?:при|от|до)вез\w*|везти|привоз\w*|отправляете)\b')
UNRESOLVED_DESTINATION = re.compile(
    r'\b(?:в (?:другой|другом|другую) (?:город\w*|насел[её]нн\w* пункт\w*)'
    r'|(?:город|адрес|место) (?:пока )?(?:не знаю|не определ[её]н|уточняю)'
    r'|пока уточняю)\b'
)
EXPLICIT_DESTINATION = re.compile(
    r'(?i:\b(?:везти|доставить|отправить)\s+(?:нужно\s+|надо\s+)?(?:в|во|до)\s+)'
    r'(?!(?i:друг(?:ой|ом|ую))\b)[А-ЯЁ][а-яё]+'
)
AMBIGUOUS_FOLLOWUP = re.compile(
    r'(?:да|нет|ага|угу|ок(?:ей)?|спасибо|понятно|актуально|здравствуйте|привет)[\s!.,?]*'
)


LOCATION_REPLY = re.compile(
    r'(?:да[,\s]+)?(?:(?:место|локаци[яю])\s+(?:уже\s+)?'
    r'(?:нашли|наш[её]л|выбрали|выбрал[аи]?|готов[оа]|ещ[её]\s+не\s+нашли)'
    r'|(?:пока|ещ[её])\s+(?:ищу|ищем|в поиске)|в поиске)[\s!.,]*'
)


@dataclass(frozen=True)
class Reply:
    id: str
    text: str = ''
    next_state: str = CLOSED


def first_greeting(messages):
    return bool(messages and all(GREETING.fullmatch(text.strip().casefold()) for text in messages))


def listing_intro(title):
    title = title.casefold().replace('ё', 'е')
    if re.search(r'\b(?:уличн\w*\s+кофейн\w*|киоск\w*|павильон\w*)\b', title):
        return 'street_overview'
    if re.search(r'\bкофейн\w*\b', title) and re.search(r'\bпод\s+ключ\b', title):
        return 'turnkey_overview'
    return None


def has_contact(messages):
    # Do not join two separate digit fragments into a phone number.
    if any(NO_CONTACT.search(text.casefold()) for text in messages):
        return False
    for text in messages:
        lowered = text.casefold()
        for pattern in (PHONE, HANDLE):
            for match in pattern.finditer(text):
                rest = (text[:match.start()] + ' ' + text[match.end():]).strip(' \t\n.,!:-()')
                if not rest:
                    return True
                if not MONEY.search(rest.casefold()) and CONTACT_CUE.search(lowered):
                    return True
                # A callback/contact can be supplied alongside a quote request.
                if re.search(r'\b(?:перезвон\w*|позвон\w*|мой (?:номер|телефон)|свяж\w*)\b', lowered):
                    if not MONEY.search(text[max(0, match.start() - 16):match.start()].casefold()):
                        return True
    return False


def render(action, options, already_answered=False, welcome=False):
    text = options[action]
    next_state = CLOSED
    if action in INTRO_QUESTIONS and not already_answered:
        next_state, question = INTRO_QUESTIONS[action]
        text += '\n\n' + question
    elif action == 'clarify_delivery_city':
        next_state = 'delivery_destination'
    if welcome:
        text = 'Здравствуйте!\n\n' + text
    return Reply(action, text, next_state)


async def decide(client, messages, state, options, model=None, listing_title=''):
    if state == CLOSED:
        return Reply('closed')
    if not messages:
        return Reply('handoff')
    if has_contact(messages):
        return render('contact_received', options)
    if first_greeting(messages):
        intro = listing_intro(listing_title) if state is None else None
        return render(intro, options, welcome=True) if intro else Reply('handoff')

    combined = '\n'.join(messages)
    normalized = ' '.join(combined.casefold().split())
    if all(re.fullmatch(r'[0-9\s()+.-]+', text) for text in messages):
        return Reply('handoff')
    if state is not None and ('?' in combined or AMBIGUOUS_FOLLOWUP.fullmatch(normalized)):
        return Reply('handoff')
    if state == 'location_status' and LOCATION_REPLY.fullmatch(normalized):
        return render('info_received', options)
    unresolved = list(UNRESOLVED_DESTINATION.finditer(combined.casefold()))
    unresolved = bool(unresolved and not EXPLICIT_DESTINATION.search(combined, unresolved[-1].end()))

    payload = {'messages': messages, 'waiting_for': WAITING_QUESTIONS.get(state)}
    response = await client.post(
        os.getenv('LLM_BASE_URL', 'https://api.openai.com/v1').rstrip('/') + '/chat/completions',
        headers={'Authorization': 'Bearer ' + os.environ['LLM_API_KEY']},
        json={
            'model': model or os.getenv('LLM_MODEL', 'gpt-4o-mini'),
            'temperature': 0,
            'messages': [
                {'role': 'system', 'content': (BASE / 'prompt.txt').read_text(encoding='utf-8')},
                {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
            ],
        },
    )
    response.raise_for_status()
    content = response.json()['choices'][0]['message']['content']
    if not isinstance(content, str):
        raise ValueError('decision_content_type')
    content = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.I | re.S)
    try:
        result = json.loads(fenced.group(1) if fenced else content)
    except json.JSONDecodeError:
        raise ValueError('decision_json_invalid') from None
    if not isinstance(result, dict):
        raise ValueError('decision_not_object')

    selected = result.get('id')
    if not isinstance(selected, str) or selected not in NEW_ACTIONS | {'answer'}:
        raise ValueError('decision_unknown_action')
    if state is not None:
        # No follow-up can select a new sales template while a question is outstanding.
        if selected == 'answer' and not (state == 'delivery_destination' and unresolved):
            return render('info_received', options)
        return Reply('handoff')
    if selected in {'answer', 'handoff'}:
        return Reply('handoff')
    if selected in {'clarify_delivery_city', 'delivery_quote_requested'}:
        if not DELIVERY.search(combined.casefold()):
            return Reply('handoff')
        if selected == 'delivery_quote_requested' and unresolved:
            selected = 'clarify_delivery_city'
    already_answered = result.get('already_answered')
    if selected in INTRO_QUESTIONS and type(already_answered) is not bool:
        raise ValueError('decision_already_answered_missing_or_invalid')
    return render(selected, options, already_answered is True, welcome=True)


async def decide_safely(client, messages, state, options, log, listing_title=''):
    fallback = os.getenv('LLM_FALLBACK_MODEL', '').strip()
    for attempt in range(2):
        retry_after = ''
        try:
            return await decide(client, messages, state, options,
                                model=fallback if attempt and fallback else None,
                                listing_title=listing_title)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            log.warning('API модели вернул HTTP %s (попытка %s)', status, attempt + 1)
            if status != 429 and status < 500:
                break
            retry_after = exc.response.headers.get('Retry-After', '')
            if not fallback and retry_after.isdigit() and int(retry_after) > 3:
                break
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            log.warning('Сбой соединения с моделью (%s)', type(exc).__name__)
        except ValueError as exc:
            reason = str(exc)
            if reason not in {
                'decision_content_type', 'decision_json_invalid', 'decision_not_object',
                'decision_already_answered_missing_or_invalid', 'decision_unknown_action',
            }:
                reason = 'invalid_provider_response'
            log.warning('Ошибка формата модели: %s (попытка %s)', reason, attempt + 1)
        except (httpx.HTTPError, KeyError, TypeError, IndexError) as exc:
            log.warning('Ошибка классификации (%s)', type(exc).__name__)
            if not fallback:
                break
        if not attempt:
            await asyncio.sleep(1 if fallback else max(1, int(retry_after) if retry_after.isdigit() else 1))
    return Reply('model_error')
