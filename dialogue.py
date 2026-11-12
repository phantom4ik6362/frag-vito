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
DECISIONS = REQUIRED | {'delivery', 'ignore', 'stop'}
CITY_QUESTION = 'Подскажите , в каком городе планируете установку ?'
LOCATION_QUESTION = 'Чтобы посчитать максимально точно, подскажите , пожалуйста, локацию уже присмотрели или еще в поиске?'
CHANNEL_QUESTIONS = {
    'turnkey_overview': 'И куда удобнее отправить  WhatsApp, Telegram или Max?',
    'street_overview': 'И куда удобнее отправить подробную информацию и варианты WhatsApp, Telegram или Max?',
    'delivery_quote_requested': 'Подскажите, пожалуйста, в какой мессенджер удобнее отправить расчёт: WhatsApp, Telegram или Max?',
}


@dataclass(frozen=True)
class Reply:
    id: str
    text: str = ''


async def decide(client, messages, history, options):
    previous = [turn['action'] for turn in history]
    if 'stop' in previous:
        return Reply('stop')

    last = next((action for action in reversed(previous) if action not in {'ignore', 'model_error'}), None)
    waiting_city = last == 'clarify_delivery_city'
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
            'model': os.getenv('LLM_MODEL', 'gpt-4o-mini'),
            'temperature': 0,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
            ],
        },
    )
    response.raise_for_status()
    result = json.loads(response.json()['choices'][0]['message']['content'])
    if not isinstance(result, dict) or not isinstance(result.get('id'), str):
        raise ValueError('Invalid model decision')
    selected = result['id']
    if selected not in DECISIONS and selected not in options:
        raise ValueError('Unknown model decision')
    city = result.get('city') is True
    location = result.get('location_status') is True
    messenger = result.get('messenger') is True

    if waiting_city:
        selected = 'delivery_quote_requested' if city else 'ignore'
    elif selected in {'delivery', 'clarify_delivery_city', 'delivery_quote_requested'}:
        selected = 'delivery_quote_requested' if city else 'clarify_delivery_city'
    if selected in {'stop', 'ignore'}:
        return Reply(selected)
    if selected in previous:
        return Reply('ignore')

    answer = options[selected]
    if city and selected == 'street_overview':
        answer = answer.replace(CITY_QUESTION, '').rstrip()
    if location and selected == 'turnkey_overview':
        answer = answer.replace(LOCATION_QUESTION, '').rstrip()
    if messenger and selected in CHANNEL_QUESTIONS:
        answer = answer.replace(CHANNEL_QUESTIONS[selected], '').rstrip()
    return Reply(selected, answer)


async def decide_safely(client, messages, history, options, log):
    try:
        return await decide(client, messages, history, options)
    except httpx.HTTPStatusError as exc:
        log.warning(
            'API модели вернул HTTP %s. Автоответ пропущен.',
            exc.response.status_code,
        )
        return Reply('model_error')
    except (httpx.HTTPError, KeyError, ValueError, TypeError, IndexError) as exc:
        log.warning(
            'Ошибка классификации (%s). Автоответ пропущен.',
            type(exc).__name__,
        )
        return Reply('model_error')
