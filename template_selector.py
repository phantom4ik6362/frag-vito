"""Prototype selector. Import with the existing bot on PYTHONPATH."""

import json
import os
import re
from pathlib import Path

from dialogue import (
    AMBIGUOUS_FOLLOWUP,
    CHANNEL,
    CHANNEL_ONLY,
    CLOSED,
    CONTACT_STATES,
    FULL_INTROS,
    INTRO_QUESTIONS,
    LOCATION_REPLY,
    NEW_ACTIONS,
    NO_CONTACT,
    PHONE,
    WAITING_QUESTIONS,
    Reply,
    decide as legacy_decide,
    has_contact,
    keyword_action,
    render,
    reply_to_greeting,
    strip_greetings,
)

SYSTEM_PROMPT = Path(__file__).with_name("template_selector_prompt.txt")
CATALOG_IDS = NEW_ACTIONS - {"greeting", "handoff"}
CONTROL_REQUEST = re.compile(r"\b(?:игнорируй(?:те)?|режим администратора|верни(?:те)?|напечатай(?:те)?)\b")
INTERNAL_ID = re.compile(r"\b(?:" + "|".join(sorted(CATALOG_IDS)) + r")\b")
FAKE_ROLE = re.compile(r"\b(?:системн(?:ое сообщение|ая команда)|system message|режим администратора)\b")
MODEL_PAYLOAD = re.compile(r'[{"\s]"?requests"?\s*:\s*\[')
COPY_COMMAND = re.compile(r"\b(?:скопируй(?:те)?|верни(?:те)?|выведи(?:те)?|напечатай(?:те)?)\b")
CITY_STATES = {"delivery_destination", "installation_city", "street_details"}
LOCATION_STATES = {"location_status", "turnkey_details"}
CONTACT_LABEL = re.compile(r"\b(?:вот номер|номер телефона|телефон)\s*[:—-]?\s*$")
BUDGET_STATEMENT = re.compile(
    r"(?:(?:мой|наш|у меня|у нас) )?бюджет\s*[:—-]?\s*"
    r"(?:(?:до|примерно|около|не больше) )?"
    r"[0-9]+(?: [0-9]{3})*(?:[.,][0-9]{1,2})?"
    r"(?:\s*(?:тыс\.?|тысяч[аи]?|млн\.?|миллион(?:ов|а)?))?"
    r"(?:\s*(?:₽|руб\.?|рубл(?:ь|я|ей)))?"
)


DIRECT_REQUESTS = (
    (re.compile(
        r"(?:организуете (?:доставку|перевозку)|(?:доставку|перевозку) организуете)"
        r"[?.!,;:]? (?:(?:город|населенный пункт) (?:еще|пока) не (?:знаю|знаем|выбрали))"
    ), "clarify_delivery_city"),
    (re.compile(r"(?:объявление )?(?:еще )?актуальн(?:о|ое)|(?:еще )?продаете"), "availability_yes"),
    (re.compile(r"сколько (?:стоит )?доставка|доставка сколько стоит"), "clarify_delivery_city"),
    (re.compile(r"по какой цене (?:идет|продается) (?:сам )?(?:павильон|киоск|ларек)"), "street_overview"),
)
REGION_ONLY = re.compile(r"(?:республик\w*\s+[\w -]+|[\w -]+\s+(?:област\w*|кра[йяе]|округ\w*))")
UNDECIDED = re.compile(r"\b(?:еще|пока) не (?:решили|решил[аи]?|выбрали|выбрал[аи]?|определились)\b|\bне уверен[аы]?\b")
OUTDOOR = re.compile(r"\b(?:уличн\w*|павильон\w*|киоск\w*|лар[её]к\w*)\b")
OUTDOOR_KIT = re.compile(
    r"\b(?:павильон\w*|киоск\w*|ларек|ларьк\w*) "
    r"(?:с (?:аппарат\w*|кофемашин\w*|оборудовани\w*) под ключ|"
    r"под ключ с (?:аппарат\w*|кофемашин\w*|оборудовани\w*))\b"
)
DESTINATION = re.compile(r"(?i:\b(?:везти|доставить|везите)\s+в\s+)([А-ЯЁ][а-яё]+(?:[- ][А-ЯЁ][а-яё]+)*)[.?!]?$")
DELIVERY_ORIGIN = re.compile(
    r"(?:(?:(?:я|мы|сам|сами) )+(?:(?:живу|живем|нахожусь|находимся) )?(?:в|из)|"
    r"(?:живу|живем|нахожусь|находимся) (?:в|из)|из) "
    r"[а-я]+(?:[- ][а-я]+){0,2}"
)
DELIVERY_TO = re.compile(
    r"(?i:(?:(?:павильон|киоск|лар[её]к|корпус) )?до )"
    r"([А-ЯЁ][а-яё]+(?:[- ][А-ЯЁ][а-яё]+){0,2}) "
    r"(?i:привез[её]те|довез[её]те|доставите)[?!.]?"
)
NON_PLACE = re.compile(r"завтра|вечера|утра|обеда|понедельника|вторника|среды|четверга|пятницы|субботы|воскресенья")


# An unsupported document may be requested alongside a supported price quote.
DOCUMENT_REQUEST = re.compile(
    r"(?:^|[.!?;]\s+)(?:пожалуйста[ ,]+)?(?:пришлите|скиньте|отправьте|дайте) "
    r"(?:пожалуйста[ ,]+)?(?:прайс(?:-лист)?|цен[ауы]|стоимость|расчет)\s+и\s+"
    r"(?:план|чертеж|схему|договор|сертификат)\b"
)
UNSUPPORTED_REQUEST = re.compile(
    r"\b(?:вы покупаете (?:б[ /]у|подержанн\w+)|"
    r"(?:вы )?(?:выкупаете|купите|выкупите) (?:наш\w*|мой|мои|мою|мо[её])|"
    r"(?:приложите|пришлите|скиньте|отправьте|покажите) (?:мне |пожалуйста )?"
    r"(?:чертеж\w*|паспорт изделия|сертификат\w*|договор\w*)|"
    r"сфотографируйте|сфоткайте|сделайте (?:(?:новое|отдельное|детальное) )?(?:фото|снимок)|"
    r"(?:пришлите|скиньте|отправьте|покажите) (?:мне |пожалуйста )?(?:фото|снимок) "
    r"(?:электрощит\w*|щитк\w*|щит\w*|разводк\w*|серийн\w+ номер\w*|изнутри|крупным планом)|"
    r"(?:точн\w+ (?:день|дату|срок)\b.{0,35}\b(?:привез\w*|достав\w*)|"
    r"(?:ширин\w+|высот\w+|глубин\w+) (?:окна|проема|прилавка))|"
    r"(?:это|за эту цену)\s+только\s+(?:будк\w*|бутк\w*|ларек|киоск|павильон)"
    r"\s*[?.!,]*\s+или(?: же)?\s+(?:все |входит )?оборудован\w*)\b"
)
NO_CHOSEN_CITY = re.compile(
    r"\b(?:(?:точный|конечный) )?город(?: установки| доставки| назначения)? "
    r"(?:еще |пока )?не (?:выбрали|выбрал[аи]?|определили|знаем|знаю)\b"
)
EXAMPLE_CITY = re.compile(r"\b(?:назва(?:л[аи]?|ли)|привел[аи]?) (?:просто )?для примера\b")
LOCATION_QUESTION = re.compile(r"\?|\bгде (?:можно|разрешено)\b")
LOCATION_STATUS = re.compile(
    r"(?:(?:мы )?(?:место|точку|локацию|площадку|помещение|адрес) "
    r"(?:(?:пока|еще|уже) )?(?:подбираем|ищ(?:у|ем)|наш(?:ли|ел|ла)|"
    r"выбра(?:ли|л|ла)|(?:не (?:нашли|выбрали|определили)))|"
    r"(?:место|точка|локация|площадка|помещение) (?:(?:уже|пока) )?"
    r"(?:есть|готов[оа]?|выбран[оа]?|в поиске)|"
    r"(?:(?:еще|пока|уже) )?(?:ищ(?:у|ем)|подбираем|нашли|выбрали) "
    r"(?:место|точку|локацию|площадку|помещение))"
)
CITY_PREFIX = (
    r"(?:(?<!\w)(?:(?:в )?город(?:е)?|г\.?|(?:планиру(?:ю|ем)|"
    r"ставить будем) в|в|для установки в)(?:\s*[:—-]\s*|\s+))?"
)
CITY_QUALIFIER = re.compile(r"(?:для установки|планиру(?:ю|ем)(?: поставить| установку)?|ставить будем)")
UNNAMED_CITY = re.compile(
    r"(?:(?:в )?(?:друг[а-я]+|соседн[а-я]+|люб[а-я]+|наш[а-я]*) "
    r"(?:город[а-я]*|район[а-я]*|населенн[а-я]+ пункт[а-я]*)|город|село|район)"
)
MESSENGER = rf"(?:{CHANNEL}|телеграмм?[ае]?|телег[ауи]|ватсап[ае]?|вацап[ае]?|макс[ае]?|мах[ае]?)"
CHANNEL_PREFERENCE = re.compile(
    rf"(?:(?:(?:можно|лучше|удобнее) )?(?:(?:в|на|через) )?"
    rf"(?:любой (?:мессенджер|канал)|любое приложение|без разницы)"
    rf"(?: кроме {MESSENGER})?|все кроме {MESSENGER}|(?:(?:только|лучше) )?не (?:в |через )?{MESSENGER}|"
    r"(?:давайте|лучше|удобнее) (?:здесь|в авито|в чате))"
)
MATERIAL = r"(?:готовые варианты|варианты(?: с фото)?|с фото|фото)"
SEND = r"(?:пришлите|скиньте|отправьте)"
NEW_REQUEST = re.compile(r"\b(?:пришлите|скиньте|отправьте|покажите|сообщите|назовите)\s+\w")
MATERIAL_FOLLOWUP = re.compile(
    rf"(?:(?:если удобно|пожалуйста|можно) )*(?:подробнее )?"
    rf"(?:{SEND} {MATERIAL}|{MATERIAL} {SEND})"
    rf"(?: (?:в|на|через) {MESSENGER})?(?: пожалуйста)?"
)


def preflight(messages, state, options, *, operator=False):
    """Apply the current stop and contact rules before invoking the model."""
    if operator:
        return Reply("operator")
    if state == CLOSED:
        return Reply("closed")
    if not messages:
        return Reply("handoff")
    content = strip_greetings(messages)
    normalized = " ".join(" ".join(content).casefold().split())
    if ((CONTROL_REQUEST.search(normalized) and INTERNAL_ID.search(normalized))
            or (MODEL_PAYLOAD.search(normalized)
                and (FAKE_ROLE.search(normalized) or COPY_COMMAND.search(normalized)))):
        return Reply("handoff")
    without_contact = any(NO_CONTACT.search(message.casefold()) for message in content)
    explicit_phone = (
        state in CONTACT_STATES and not without_contact
        and any(CONTACT_LABEL.search(message[:match.start()].casefold())
                and not message[match.end():].strip(" .,!;")
                for message in content for match in PHONE.finditer(message))
    )
    if has_contact(content, expecting_contact=state in CONTACT_STATES) or explicit_phone:
        return render("contact_received", options)
    if not content:
        return reply_to_greeting(state, options)
    text = normalized.replace("ё", "е")
    if BUDGET_STATEMENT.fullmatch(text.rstrip(" .,!;")):
        return Reply("handoff")
    if DOCUMENT_REQUEST.search(text) or UNSUPPORTED_REQUEST.search(text):
        return Reply("handoff")
    action = keyword_action(content)
    if action is None and len(content) > 1:
        unique = {message.casefold().strip(" .,!?:;") for message in content}
        if len(unique) == 1:
            action = keyword_action(content[:1])
    if not action and len(content) == 1 and state in {None, "greeted"}:
        direct_text = normalized.replace("ё", "е").strip(" .,!?:;")
        action = next((ident for pattern, ident in DIRECT_REQUESTS if pattern.fullmatch(direct_text)), None)
    if action is not None:
        if state == "delivery_destination":
            return Reply("handoff")
        if action in FULL_INTROS and state == FULL_INTROS[action][0]:
            return Reply("already_replied", next_state=state)
        return render(action, options, welcome=state is None)
    if state in CONTACT_STATES and channel_preference(normalized):
        return Reply("waiting_contact", next_state="contact_channel")
    if state == "contact_channel" and without_contact:
        return Reply("handoff")
    if all(re.fullmatch(r"[0-9\s()+.-]+", message) for message in content):
        return Reply("handoff")
    if AMBIGUOUS_FOLLOWUP.fullmatch(normalized):
        return Reply("handoff")
    if state in LOCATION_STATES and LOCATION_REPLY.fullmatch(normalized):
        next_state = "contact_channel" if state == "turnkey_details" else CLOSED
        return Reply("info_received", options["info_received"], next_state)
    return None


REQUEST_TO_TEMPLATE = {
    "pavilion": "street_overview",
    "outdoor_bundle": "street_quote",
    "coffee_bundle": "turnkey_overview",
    "availability": "availability_yes",
}
REQUEST_TYPES = {*REQUEST_TO_TEMPLATE, "delivery", "other"}


def request_payload(messages, state, options, history):
    catalog = {}
    for ident in sorted(CATALOG_IDS):
        question = INTRO_QUESTIONS.get(ident, (None, ""))[1]
        catalog[ident] = options[ident]
        if ident not in FULL_INTROS and question:
            catalog[ident] += "\n\n" + question
    return {
        "history": [{"role": role, "text": text[-3000:]} for role, text in history[-8:]],
        "messages": [message[:3000] for message in messages],
        "state": state,
        "pending_question": WAITING_QUESTIONS.get(state),
        "catalog": catalog,
    }


def validate_decision(facts):
    if not isinstance(facts, dict) or facts.keys() != {"requests", "city", "location"}:
        raise ValueError("selector_schema")
    requests = facts["requests"]
    if (
        not isinstance(requests, list) or any(
            not isinstance(item, str) or item not in REQUEST_TYPES for item in requests
        ) or any(type(facts[key]) is not str for key in ("city", "location"))
    ):
        raise ValueError("selector_schema")


def verified_quote(quote, messages):
    quote = " ".join(quote.casefold().split())
    if not quote or not any(character.isalpha() for character in quote):
        return ""
    pattern = re.compile(r"(?<!\w)" + re.escape(quote) + r"(?!\w)")
    return quote if any(pattern.search(" ".join(text.casefold().split())) for text in messages) else ""


def normalized_fact(text):
    return " ".join(text.casefold().replace("ё", "е").split())


def matches_destination(proposed, quoted, *, after_do=False):
    """Accept only a case variant of a place quoted in a delivery construction."""
    proposed, quoted = normalized_fact(proposed), normalized_fact(quoted)
    if proposed == quoted:
        return True
    if not proposed or not quoted or len(proposed) < 4:
        return False
    if after_do:
        if proposed.endswith("а"):
            return quoted in (proposed[:-1] + "ы", proposed[:-1] + "и")
        if proposed.endswith("я"):
            return quoted == proposed[:-1] + "и"
        if proposed.endswith(("ь", "й")):
            return quoted == proposed[:-1] + "я"
        return quoted == proposed + "а"
    return proposed.endswith(("а", "я")) and quoted == (
        proposed[:-1] + {"а": "у", "я": "ю"}[proposed[-1]]
    )


def statements(messages):
    for message in messages:
        for sentence in re.split(r"(?<=[.!?])\s+|[,;\n]", message):
            statement = normalized_fact(sentence).strip(" .!:—")
            if statement and not LOCATION_QUESTION.search(statement) and not statement.startswith("если "):
                yield statement


def channel_preference(text):
    text = normalized_fact(text).strip(" .,!;?")
    return bool(CHANNEL_ONLY.fullmatch(text) or CHANNEL_PREFERENCE.fullmatch(text.replace(",", "")))


def remaining_clauses(messages, city, location):
    text = "\n".join(normalized_fact(message) for message in messages)
    for quote, prefix in ((location, ""), (city, CITY_PREFIX)):
        if quote:
            pattern = prefix + r"(?<!\w)" + re.escape(normalized_fact(quote)) + r"(?!\w)"
            text = re.sub(pattern, "\n", text, count=1)
    return [
        clause
        for part in re.split(r"(?<=[.!?])\s+|[;\n]", text)
        if (clause := part.strip(" .,!;:—–")) and clause != "?"
    ]


def is_followup_detail(clause, state, history):
    if state in CITY_STATES and CITY_QUALIFIER.fullmatch(clause):
        return True
    if state not in CONTACT_STATES:
        return False
    if channel_preference(clause):
        return True
    if not MATERIAL_FOLLOWUP.fullmatch(clause.replace(",", "")):
        return False
    offer = next((normalized_fact(text) for role, text in reversed(history) if role == "seller"), "")
    material = "фото" if "фото" in clause else "вариант"
    return material in offer and bool(re.search(r"\b(?:скинуть|отправить|прислать|показать|подберем)\b", offer))


def apply_decision(facts, state, options, messages, *, history=()):
    validate_decision(facts)
    if state == CLOSED:
        return Reply("closed")
    requests = set(facts["requests"])
    city = verified_quote(facts["city"], messages)
    literal_location = verified_quote(facts["location"], messages)
    if LOCATION_QUESTION.search(literal_location):
        return Reply("handoff")
    current_statements = list(statements(messages))
    confirmed_location = next((text for text in current_statements if LOCATION_STATUS.fullmatch(text)), "")
    location = confirmed_location
    if not location and literal_location and not AMBIGUOUS_FOLLOWUP.fullmatch(literal_location):
        phrase = normalized_fact(literal_location).strip(" .,!;:")
        location = verified_quote(phrase, current_statements)
    city = city.strip(" .,!?:;")
    if (REGION_ONLY.fullmatch(city) or UNNAMED_CITY.fullmatch(normalized_fact(city))
            or re.fullmatch(MESSENGER, city)):
        city = ""
    text = " ".join(messages).casefold().replace("ё", "е")
    if city and state in {None, "greeted"} and not remaining_clauses(messages, city, ""):
        return Reply("handoff")
    if state == "street_details" and confirmed_location and NO_CHOSEN_CITY.search(text):
        remainder = remaining_clauses(messages, "", confirmed_location)
        if all(NO_CHOSEN_CITY.fullmatch(clause) or is_followup_detail(clause, state, history)
               for clause in remainder):
            return Reply("handoff")
    delivery_turn = state == "delivery_destination" or requests == {"delivery"}
    if delivery_turn and not city:
        if state == "delivery_destination" and requests <= {"delivery"}:
            destination = DESTINATION.search(" ".join(messages))
            if destination and matches_destination(facts["city"], destination[1]):
                city = destination[1].casefold()
        elif requests == {"delivery"} and len(messages) == 1:
            destination = DELIVERY_TO.fullmatch(messages[0])
            if (destination and not NON_PLACE.fullmatch(destination[1].casefold())
                    and (not facts["city"] or matches_destination(facts["city"], destination[1], after_do=True))):
                city = destination[1].casefold()
    if ((re.search(r"\bили\b", text) and UNDECIDED.search(text))
            or (delivery_turn and (NO_CHOSEN_CITY.search(text) or EXAMPLE_CITY.search(text)))):
        city = ""
    if location and city.startswith(location.strip(" .,!?:;")):
        if confirmed_location:
            city = ""
        else:
            location = ""

    if (city and state in CITY_STATES) or (location and state in LOCATION_STATES):
        remainder = remaining_clauses(messages, city, location)
        if city and state == "delivery_destination" and requests == {"delivery"}:
            # A destination statement can be mislabeled as a second delivery request.
            route = re.compile(r"(?i:\b(?:везти|доставить|везите)\s+в\s+)"
                               + re.escape(city) + r"[.!]?$")
            combined = " ".join(messages).casefold()
            if match := route.search(combined):
                source = combined[:match.start()].strip(" .,!;:—")
                if not source or DELIVERY_ORIGIN.fullmatch(normalized_fact(source)):
                    requests.clear()
        ancillary_only = all(is_followup_detail(clause, state, history) for clause in remainder)
        if ancillary_only:
            # A quoted fact with only channel preferences or offered materials
            # may have picked up the old intent or a spurious 'other'.
            if city and state in CITY_STATES and (
                requests <= {"pavilion", "outdoor_bundle", "delivery"}
                or (state in CONTACT_STATES and bool(remainder)
                    and requests <= {"pavilion", "outdoor_bundle", "other"})
            ):
                requests.clear()
            if location and state in LOCATION_STATES and (
                requests == {"coffee_bundle"}
                or (requests == {"other"} and bool(confirmed_location))
            ):
                requests.clear()
        elif not requests and any(
            (LOCATION_QUESTION.search(clause) or NEW_REQUEST.search(clause))
            and not is_followup_detail(clause, state, history)
            for clause in remainder
        ):
            return Reply("handoff")

    if not requests:
        next_state = "contact_channel" if state in CONTACT_STATES else CLOSED
        if city and state in CITY_STATES:
            return Reply("city_received", options["city_received"], next_state)
        if location and state in {"location_status", "turnkey_details"}:
            return Reply("info_received", options["info_received"], next_state)
        return Reply("handoff")
    if len(requests) != 1 or "other" in requests or state == "delivery_destination":
        return Reply("handoff")

    kind = next(iter(requests))
    if kind == "pavilion" and OUTDOOR_KIT.search(text) and not re.search(r"\b(?:или|без)\b", text):
        kind = "outdoor_bundle"
    if kind == "outdoor_bundle" and location and state not in {"installation_city", "street_details"}:
        buyer_context = [message for role, message in history if role == "buyer"]
        if not any(OUTDOOR.search(message.casefold()) for message in [*buyer_context, *messages]):
            kind = "coffee_bundle"
    if kind == "delivery":
        ident = "delivery_quote_requested" if city else "clarify_delivery_city"
    else:
        ident = REQUEST_TO_TEMPLATE[kind]
    if ident in FULL_INTROS and state == FULL_INTROS[ident][0]:
        return Reply("already_replied", next_state=state)
    answered = (
        (kind in {"pavilion", "outdoor_bundle"} and bool(city))
        or (kind == "coffee_bundle" and bool(location))
    )
    return render(ident, options, already_answered=answered, welcome=state is None)


async def choose(client, prompt, payload, model, *, correction=""):
    instructions = prompt.read_text(encoding="utf-8")
    if correction:
        instructions += "\n" + correction
    response = await client.post(
        os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/chat/completions",
        headers={"Authorization": "Bearer " + os.environ["LLM_API_KEY"]},
        json={
            "model": model or os.getenv("LLM_MODEL", "gpt-4o-mini"),
            "temperature": 0,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        },
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise ValueError("selector_content_not_text")
    content = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.I | re.S)
    try:
        choice = json.loads(fenced.group(1) if fenced else content)
    except json.JSONDecodeError:
        raise ValueError("selector_invalid_json") from None
    validate_decision(choice)
    return choice


async def decide(client, messages, state, options, *, history=(), operator=False, model=None):
    early = preflight(messages, state, options, operator=operator)
    if early is not None:
        return early
    if state == "contact_channel":
        return await legacy_decide(client, messages, state, options, model=model)
    # Keep line breaks useful to the model when a single Avito message
    # contains a city, a channel and a reply to our offer on separate lines.
    content = strip_greetings([line for message in messages for line in message.splitlines()])
    payload = request_payload(content, state, options, history)
    facts = await choose(client, SYSTEM_PROMPT, payload, model)
    current_messages = payload["messages"]
    reply = apply_decision(facts, state, options, current_messages, history=history)
    if reply.id == "handoff" and not LOCATION_QUESTION.search(
        verified_quote(facts["location"], current_messages)
    ):
        previously_other = facts["requests"] == ["other"]
        may_be_followup = previously_other and (
            bool(facts["city"]) or any(is_followup_detail(text, state, history) for text in current_messages)
        )
        if state == "street_details" and (not facts["requests"] or may_be_followup) and not verified_quote(
            facts["city"], current_messages
        ):
            correction = (
                "Повторно проверь текущий ход. Если покупатель сообщает, где планирует "
                "установку, точно скопируй город из messages. Согласие получить уже "
                "предложенные продавцом варианты или фото и выбор мессенджера не "
                "считаются отдельным запросом. Новые самостоятельные запросы перечисли."
            )
        elif (not facts["requests"] and state not in CITY_STATES | {"location_status"}
                and not (verified_quote(facts["city"], current_messages)
                         and not remaining_clauses(current_messages, facts["city"], ""))):
            correction = (
                "Первая попытка не нашла самостоятельного запроса. Прочитай заново "
                "только текущие реплики. Определи, о чём просят покупатели, даже "
                "если пишут с опечаткой или без вопросительного знака. Указанный бюджет "
                "и своё оборудование сами по себе не образуют запрос. Если покупатель "
                "только сообщил вводные и не ответил на уточнение, верни requests=[\"other\"]. "
                "Заполни requests "
                "для вопросов, просьб и уточнений предмета покупки. «Доставите?» — "
                "delivery, «интересует один ларёк без аппарата» — pavilion, "
                "неподдерживаемый вопрос — other. Сохрани город и локацию только "
                "если они подтверждены в текущих репликах."
            )
        else:
            correction = ""
        if correction:
            facts = await choose(client, SYSTEM_PROMPT, payload, model, correction=correction)
            checked = apply_decision(facts, state, options, current_messages, history=history)
            if not previously_other:
                return checked
            corrected_city = verified_quote(facts["city"], current_messages).strip(" .,!?:;")
            if checked.id == "city_received" and all(
                is_followup_detail(clause, state, history)
                for clause in remaining_clauses(current_messages, corrected_city, "")
            ):
                return checked
    return reply
