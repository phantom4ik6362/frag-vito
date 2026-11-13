import asyncio
import re
import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent
REQUIRED = {
    'turnkey_overview', 'street_overview', 'clarify_intent', 'availability_yes',
    'clarify_delivery_city', 'delivery_quote_requested',
}
DECISIONS = REQUIRED | {'delivery', 'ignore', 'stop', 'needs_human', 'street_quote'}
CITY_QUESTION = 'Подскажите , в каком городе планируете установку ?'
LOCATION_QUESTION = 'Чтобы посчитать максимально точно, подскажите , пожалуйста, локацию уже присмотрели или еще в поиске?'
CHANNEL_QUESTIONS = {
    'turnkey_overview': 'И куда удобнее отправить  WhatsApp, Telegram или Max?',
    'street_overview': 'И куда удобнее отправить подробную информацию и варианты WhatsApp, Telegram или Max?',
    'delivery_quote_requested': 'Подскажите, пожалуйста, в какой мессенджер удобнее отправить расчёт: WhatsApp, Telegram или Max?',
}


# Explicitly unresolved destinations override a model's city flag.
UNRESOLVED_DESTINATION = re.compile(
    r'\b(?:в (?:другой|другом|другую) (?:город\w*|насел[её]нн\w* пункт\w*)'
    r'|(?:город|адрес|место) (?:пока )?(?:не знаю|не определ[её]н|уточняю)'
    r'|пока уточняю)\b',
    re.IGNORECASE,
)
EXPLICIT_DESTINATION = re.compile(
    r'(?i:\b(?:везти|доставить|отправить)\s+(?:нужно\s+|надо\s+)?(?:в|во|до)\s+)'
    r'(?!(?i:друг(?:ой|ом|ую))\b)[А-ЯЁ][а-яё]+'
)


@dataclass(frozen=True)
class Reply:
    id: str
    text: str = ''


async def decide(client, messages, history, options, model=None):
    previous = [turn['action'] for turn in history]
    if 'stop' in previous:
        return Reply('stop')

    last = next((action for action in reversed(previous) if action not in {'ignore', 'model_error'}), None)
    waiting_city = last == 'clarify_delivery_city'
    if waiting_city:
        combined = '\n'.join(messages)
        uncertainties = list(UNRESOLVED_DESTINATION.finditer(combined))
        if uncertainties and not EXPLICIT_DESTINATION.search(combined, uncertainties[-1].end()):
            return Reply('ignore')
    system = (BASE / 'prompt.txt').read_text(encoding='utf-8')
    payload = {
        'messages': messages,
        'history': history,
        'waiting_city': waiting_city,
        'additional_templates': {
            template_id: text for template_id, text in options.items()
            if template_id not in REQUIRED and template_id not in DECISIONS
        },
    }
    response = await client.post(
        os.getenv('LLM_BASE_URL', 'https://api.openai.com/v1').rstrip('/') + '/chat/completions',
        headers={'Authorization': 'Bearer ' + os.environ['LLM_API_KEY']},
        json={
            'model': model or os.getenv('LLM_MODEL', 'gpt-4o-mini'),
            'temperature': 0,
            'messages': [
                {'role': 'system', 'content': system},
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
    if waiting_city:
        if type(result.get('city')) is not bool:
            raise ValueError('decision_city_missing_or_invalid')
        selected = 'delivery'
    else:
        selected = result.get('id')
        if not isinstance(selected, str) or (selected not in DECISIONS and selected not in options):
            raise ValueError('decision_unknown_action')
    city = result.get('city') is True
    location = result.get('location_status') is True
    messenger = result.get('messenger') is True

    quote_request = any(re.fullmatch(r'расч[её]т[.!? ]*', msg.strip(), re.I) for msg in messages)
    if not waiting_city and selected != 'stop' and quote_request and 'street_overview' in previous:
        selected = 'street_quote'
    if waiting_city:
        selected = 'delivery_quote_requested' if city else 'ignore'
    elif selected in {'delivery', 'clarify_delivery_city', 'delivery_quote_requested'}:
        selected = 'delivery_quote_requested' if city else 'clarify_delivery_city'
    if selected in {'stop', 'ignore'}:
        return Reply(selected)
    if selected in previous:
        if selected == 'needs_human':
            return Reply('handoff')
        if selected in {'clarify_intent', 'clarify_delivery_city'}:
            return Reply('ignore')
        normalize = lambda text: ' '.join(re.findall(r'\w+', text.casefold().replace('ё', 'е')))
        seen = {normalize(turn.get('text', '')) for turn in history if turn['action'] == selected}
        if all(normalize(message) in seen for message in messages):
            return Reply('ignore')
        return Reply('handoff')

    if selected == 'needs_human' and selected not in options:
        return Reply('handoff')
    answer = options[selected]
    if selected == 'street_quote':
        if city:
            answer = answer.replace('Подскажите, пожалуйста, в каком городе планируете установку?', '').strip()
        if messenger:
            answer = answer.replace('Куда удобнее отправить расчёт: WhatsApp, Telegram или Max?', '').strip()
        answer = ' '.join(answer.split())
    if city and selected == 'street_overview':
        answer = answer.replace(CITY_QUESTION, '').rstrip()
    if location and selected == 'turnkey_overview':
        answer = answer.replace(LOCATION_QUESTION, '').rstrip()
    if messenger and selected in CHANNEL_QUESTIONS:
        answer = answer.replace(CHANNEL_QUESTIONS[selected], '').rstrip()
    return Reply(selected, answer)


async def decide_safely(client, messages, history, options, log):
    fallback = os.getenv('LLM_FALLBACK_MODEL', '').strip()
    for attempt in range(2):
        retry_after = ''
        try:
            if attempt and fallback:
                return await decide(client, messages, history, options, fallback)
            return await decide(client, messages, history, options)
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
                'decision_city_missing_or_invalid', 'decision_unknown_action',
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
