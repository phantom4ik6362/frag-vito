# Эксплуатация FragVito на VPS

| Параметр | Значение |
|---|---|
| Публичный адрес | `https://bot.interviewintel.space` |
| Проверка доступности | `https://bot.interviewintel.space/health` |
| DNS | A-запись `bot` в зоне `interviewintel.space` у Porkbun ведёт на IPv4 VPS |
| HTTPS | Caddy проксирует запросы на `127.0.0.1:8000` |
| Репозиторий на VPS | `/opt/fragvito` |
| Настройки | `/etc/fragvito/bot.env` |
| SQLite | `/app/data/queue.sqlite3` в томе `fragvito-data` |

Команды Docker выполняются на VPS по SSH. Команды `scp` выполняются на Mac.

## Поднять контейнер

На чистом VPS с Git и Docker:

```bash
git clone https://github.com/phantom4ik6362/frag-vito.git /opt/fragvito
cd /opt/fragvito
docker build -t fragvito:local .
```

Восстановите `/etc/fragvito/bot.env` из защищённой копии или заполните по `.env.example`. Установите права `600`. Для команд ниже оставьте `PORT` и `DB_PATH` незаданными. Значения в `bot.env` вводите без обрамляющих кавычек.

Если переносите историю, восстановите SQLite по инструкции ниже **до первого запуска**.

```bash
docker run -d --name fragvito \
  --restart unless-stopped \
  --env-file /etc/fragvito/bot.env \
  -e DRY_RUN=false \
  -v fragvito-data:/app/data \
  -p 127.0.0.1:8000:8000 \
  fragvito:local

docker exec fragvito python manage.py check
curl -fsS --retry 5 --retry-all-errors --retry-delay 1 \
  https://bot.interviewintel.space/health
```

При замене VPS обновите A-запись и настройте Caddy для того же домена. Если поменялся домен или `WEBHOOK_SECRET`, после проверки HTTPS зарегистрируйте вебхук:

```bash
docker exec fragvito python manage.py subscribe https://bot.interviewintel.space
```

## Обновить код после слияния в main

Порядок сборки, замены контейнера и первой проверки описан в [UPDATE_REPLY_LOGIC.md](UPDATE_REPLY_LOGIC.md).

## Выключить или перезапустить контейнер

| Действие | Команда |
|---|---|
| Остановить | `docker stop fragvito` |
| Запустить существующий контейнер | `docker start fragvito` |
| Перезапустить | `docker restart fragvito` |
| Посмотреть логи | `docker logs --tail 80 fragvito` |

После изменения `bot.env` выполните `docker stop fragvito && docker rm fragvito` и повторите `docker run` из раздела запуска. Docker перечитает настройки, а база сохранится в томе `fragvito-data`.

## Сделать резервную копию SQLite

Бот может продолжать работать. Команда создаёт согласованную копию SQLite и публикует файл только после проверки целостности.

```bash
(
  set -e
  umask 077
  install -d -m 700 /root/fragvito-backups
  backup="/root/fragvito-backups/fragvito-$(date -u +%F-%H%M%S).sqlite3"
  temp=$(mktemp "${backup}.XXXXXX")
  trap 'rm -f "$temp"' EXIT

  docker exec -i fragvito python - > "$temp" <<'PY'
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from app import DB

with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "backup.sqlite3"
    with closing(sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(path)) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise SystemExit("Проверка целостности не прошла")
    sys.stdout.buffer.write(path.read_bytes())
PY

  mv "$temp" "$backup"
  printf 'Проверенная копия: %s\n' "$backup"
)
```

На Mac подставьте IP сервера и имя копии из вывода команды:

```bash
scp 'root@IP_VPS:/root/fragvito-backups/ИМЯ_КОПИИ.sqlite3' ~/Downloads/
```

Храните копии вне VPS и публичного Git-репозитория. В базе есть сообщения покупателей.

## Восстановить базу из чистого клона

Соберите `fragvito:local` по инструкции выше. Затем загрузите копию с Mac на VPS:

```bash
scp ~/Downloads/ИМЯ_КОПИИ.sqlite3 root@IP_VPS:/root/fragvito-restore.sqlite3
```

На VPS выполните блок **до запуска бота**. Он рассчитан на пустой том и откажется перезаписывать существующие данные.

```bash
docker volume create fragvito-data

docker run --rm -i -v fragvito-data:/app/data fragvito:local \
  python -c '
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

directory = Path("/app/data")
if any(directory.iterdir()):
    raise SystemExit("Том должен быть пустым")

with tempfile.NamedTemporaryFile(dir=directory) as temporary:
    shutil.copyfileobj(sys.stdin.buffer, temporary)
    temporary.flush()
    path = Path(temporary.name)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as backup:
        if backup.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise SystemExit("Проверка целостности не прошла")
        tables = {row[0] for row in backup.execute(
            "SELECT name FROM sqlite_master WHERE type = ?", ("table",)
        )}
        if not {"jobs", "telegram_jobs"} <= tables:
            raise SystemExit("Файл не похож на базу FragVito")
    (directory / "queue.sqlite3").hardlink_to(path)
print("SQLite восстановлена")
' < /root/fragvito-restore.sqlite3
```

Запустите контейнер и проверьте историю:

```bash
docker exec fragvito python manage.py jobs
docker exec fragvito python manage.py telegram-jobs
```

После запуска бот может продолжить обработку незавершённых заданий из копии. Держите только один боевой экземпляр бота.

## Возобновить автоответы в одном чате

```bash
docker exec fragvito python manage.py resume-chat 'CHAT_ID'
```

Команда снимает ручной режим и сбрасывает состояние диалога. История, очередь Telegram и защита от повторной доставки сохраняются. Если в чате есть задачи в работе, команда откажется его возобновлять.

## Сбросить данные одного диалога

Возьмите `chat_id` из уведомления Telegram или вывода `docker exec fragvito python manage.py jobs`. Дождитесь окончания обработки сообщений в этом чате и подставьте его ID:

```bash
docker exec -i fragvito python - 'CHAT_ID' <<'PY'
import sys
from contextlib import closing
from app import db

chat_id = sys.argv[1]
with closing(db()) as conn, conn:
    conn.execute("BEGIN IMMEDIATE")
    for table in ("jobs", "telegram_jobs", "seller_events", "bot_messages", "manual_chats", "chat_dialogue"):
        conn.execute(f"DELETE FROM {table} WHERE chat_id = ?", (chat_id,))
print(f"Локальное состояние диалога удалено: {chat_id}")
PY
```

Команда удаляет локальное состояние **одного диалога**, включая историю обработки, дедупликацию и ручной режим. Переписка в Avito, отправленные уведомления Telegram и ранее сделанные бэкапы сохраняются. Если у пользователя несколько диалогов, у каждого свой `chat_id`.