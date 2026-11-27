# Меню статистики в отдельном Telegram-боте

Новый бот запускается в контейнере FRAG-VITO и получает команды через
long polling. Для него используется отдельный токен.
Доступ открыт всем, кто откроет этого бота.

После /start появляются кнопки «Сегодня», «Вчера», «Последние 7 дней»,
«Последние 30 дней», «За всё время». Команды /menu и /stats возвращают меню.
Отчёты отправляются обычным текстом без смайликов.

## Подсчёт

Каждый chat_id Авито считается один раз. В отчёт попадают чаты по дате
первого входящего сообщения в сохранённой истории. Новое сообщение
в старом чате не создаёт новый чат.

Дни считаются по Москве. Последние 7 дней включают сегодня и шесть предыдущих,
последние 30 дней включают сегодня и двадцать девять предыдущих.
«Вчера» охватывает предыдущий календарный день.

- «Новых чатов» — число впервые поступивших чатов выбранного периода.
- «Ответил бот» — бот хотя бы раз успешно отправил автоответ в Авито.
  Приветствие с уточнением также считается. Последующее подключение
  продавца сохраняет чат в этом показателе.
- «Передано администратору без ответа бота» — подтверждённых отправок бота
  нет, и обращение завершило автоматическую обработку. Сюда входят ручной
  режим, неподдерживаемые вопросы, вложения и ошибки.
- «Получен контакт» — число чатов с распознаваемым телефоном или никнеймом.
  Учитываются контакт в первом сообщении, ответ на запрос бота и переписка
  с продавцом. Повторы в одном чате не увеличивают число.
- «Ожидают обработки» появляется, если часть чатов ещё в очереди.

Контакты могут пересекаться с обработанными ботом и переданными продавцу.
Сумма «Ответил бот», «Передано администратору без ответа бота» и
«Ожидают обработки» равна общему числу новых чатов.

Итоги чатов выбранного периода показываются на момент запроса отчёта.
В отчёте за вчера могут быть учтены ответы бота и контакты, полученные
сегодня в тех же чатах.

## История

При первом запуске создаются таблицы statistics_events и admin_telegram_state.
Статистика восстанавливается из jobs существующей SQLite-базы, затем
пополняется при получении новых сообщений. Дубли вебхуков и повторный
запуск не удваивают историю. resume-chat сохраняет статистику.

«За всё время» означает всю сохранившуюся историю этой базы. Время начала
чата определяется первым входящим, известным боту. Обращения, которые бот
никогда не получал или которые удалили до обновления, автоматически
восстановить нельзя. Экспорт Telegram/Avito для имеющихся jobs не нужен.

DRY_RUN после обновления исключается. В старой истории исключаются
известные dry-run записи. Для старых смешанных пачек режим каждого
сообщения не всегда восстановим. Тесты через боевой аккаунт Авито входят
в статистику наравне с другими обращениями.

Телефоны и никнеймы распознаются по поддерживаемым форматам. Само слово
«Telegram» или «WhatsApp» без контакта не засчитывается. Признак контакта
не подтверждает существование аккаунта или доступность номера.

## Образец сообщения для согласования

Числа приведены для примера.

```text
Статистика Авито. Последние 7 дней
Период — 22.11.2026 — 28.11.2026, МСК

Новых чатов — 40
Ответил бот — 27
Передано администратору без ответа бота — 13
Получен контакт — 18

Данные доступны с 14.11.2026.
Состояние на 28.11.2026 12:30 МСК.
```

## Mac

Открой терминал в существующем локальном репозитории frag-vito.
Скачай патч в Downloads.

```bash
(
  set -e
  git fetch origin main
  git switch -c feature/telegram-statistics origin/main
  git apply --check --index ~/Downloads/fragvito-admin-stats.patch
  git apply --index ~/Downloads/fragvito-admin-stats.patch
  git diff --cached --check
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/python -m unittest discover -s tests
  git commit -m "Add Telegram statistics menu with chat-based reports"
  git push -u origin feature/telegram-statistics
)
```

Открой PR из feature/telegram-statistics в main. После слияния обновляй VPS.

## VPS

Создай отдельного бота через BotFather. Токен нужен только на VPS.
Добавь его в файл настроек.

```bash
sudoedit /etc/fragvito/bot.env
```

```dotenv
TELEGRAM_ADMIN_BOT_TOKEN=ТОКЕН_НОВОГО_БОТА
```

После слияния PR проверь действующий том и порт.

```bash
cd /opt/fragvito
docker inspect --format '{{range .Mounts}}{{println .Name .Destination}}{{end}}' fragvito
docker port fragvito
git status --short
```

Дальнейшие команды рассчитаны на том fragvito-data в /app/data
и локальный адрес 127.0.0.1:8000.

```bash
bash <<'BASH'
set -Eeuo pipefail
umask 077
cd /opt/fragvito
git switch main
git pull --ff-only origin main

test "$(docker inspect -f '{{range .Mounts}}{{println .Name .Destination}}{{end}}' fragvito)" = 'fragvito-data /app/data'
test "$(docker port fragvito)" = '8000/tcp -> 127.0.0.1:8000'

image="fragvito:$(git rev-parse --short HEAD)"
git archive HEAD | docker build -t "$image" -
docker run --rm --env-file /etc/fragvito/bot.env \
  -e DRY_RUN=false -e REPLY_SELECTOR=v48 "$image" python manage.py check

stamp="$(date -u +%Y%m%d-%H%M%S)"
previous="fragvito-before-stats-$stamp"
install -d -m 700 /root/fragvito-backups
backup="/root/fragvito-backups/fragvito-$stamp.sqlite3"

docker exec fragvito python -c '
import sqlite3, sys, tempfile
from contextlib import closing
from pathlib import Path
from app import DB

with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "backup.sqlite3"
    with closing(sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(path)) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise SystemExit("Ошибка проверки резервной копии")
    sys.stdout.buffer.write(path.read_bytes())
' > "$backup.tmp"
mv "$backup.tmp" "$backup"
echo "Резервная копия SQLite: $backup"

docker stop fragvito
if ! docker rename fragvito "$previous"; then
  docker start fragvito
  exit 1
fi
if docker run -d --name fragvito --restart unless-stopped \
    --env-file /etc/fragvito/bot.env -e DRY_RUN=false -e REPLY_SELECTOR=v48 \
    -v fragvito-data:/app/data -p 127.0.0.1:8000:8000 "$image" &&
   curl -fsS --max-time 5 --retry 8 --retry-all-errors --retry-delay 1 \
     https://bot.interviewintel.space/health
then
  printf '\nRELEASE: OK | %s | rollback: %s\n' "$image" "$previous"
else
  if docker container inspect fragvito >/dev/null 2>&1; then
    docker rm -f fragvito
  fi
  docker rename "$previous" fragvito
  docker start fragvito
  exit 1
fi

docker exec fragvito python manage.py stats --period all
BASH
```

## Проверка

1. Открой нового бота и отправь /start.
2. Нажми каждую из пяти кнопок.
3. Сравни отчёт «За всё время» с командой ниже.
4. Отправь /start с другого аккаунта. Доступ также должен работать.

```bash
docker exec fragvito python manage.py stats --period all
docker exec fragvito python manage.py stats --period today
docker logs --since 10m fragvito 2>&1 | grep -E 'Меню статистики|telegram-admin'
```

Ошибка Telegram 409 означает активный webhook либо другой процесс
getUpdates у этого токена. Новый бот использует long polling. Для нового
токена webhook настраивать не требуется.
