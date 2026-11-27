"""Chat-based statistics from local inbound jobs, independent of Telegram delivery."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from dialogue import HANDLE, MONEY, NO_CONTACT, PHONE, has_contact

MOSCOW = timezone(timedelta(hours=3), name="МСК")
PERIODS = {
    "today": "Сегодня",
    "yesterday": "Вчера",
    "7d": "Последние 7 дней",
    "30d": "Последние 30 дней",
    "all": "За всё время",
}


def contact_supplied(text):
    """Recognize an inbound contact, including a phone followed by a name."""
    if not text or NO_CONTACT.search(text.casefold()):
        return False
    if has_contact(text.splitlines()):
        return True
    if MONEY.search(text.casefold()):
        return False
    return any(PHONE.search(line) or HANDLE.search(line) for line in text.splitlines())


def record_incoming(conn, ident, chat, text, created, status):
    conn.execute(
        """INSERT OR IGNORE INTO statistics_events
           (job_id, chat_id, received_at, reply_status, contact_received)
           VALUES (?, ?, ?, ?, ?)""",
        (ident, chat, created, status, int(contact_supplied(text))),
    )


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS statistics_events (
        job_id TEXT PRIMARY KEY,
        chat_id TEXT NOT NULL,
        received_at REAL NOT NULL,
        reply_status TEXT NOT NULL,
        contact_received INTEGER NOT NULL DEFAULT 0 CHECK (contact_received IN (0,1))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS statistics_chat_date ON statistics_events(chat_id, received_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS statistics_date ON statistics_events(received_at)")
    conn.execute("""CREATE TABLE IF NOT EXISTS admin_telegram_state (
        bot_id TEXT PRIMARY KEY,
        next_update INTEGER NOT NULL
    )""")
    if conn.execute(
        "SELECT 1 FROM runtime_state WHERE key='statistics_backfilled_v1'"
    ).fetchone():
        return

    # Older jobs had no explicit preview flag. Exclude known dry-run jobs
    # and batches that only have preview results in their chat.
    rows = conn.execute("""
        SELECT j.id, j.chat_id, j.text, j.created, j.status, j.template_id
        FROM jobs AS j
        WHERE j.status!='dry_run'
          AND NOT EXISTS (
              SELECT 1 FROM selector_turns AS t WHERE t.job_id=j.id AND t.preview=1
          )
          AND (j.status!='merged' OR EXISTS (
              SELECT 1 FROM jobs AS live WHERE live.chat_id=j.chat_id
                AND live.status NOT IN ('merged', 'dry_run')
          ))
    """).fetchall()
    for row in rows:
        conn.execute(
            """INSERT OR IGNORE INTO statistics_events
               (job_id, chat_id, received_at, reply_status, contact_received)
               VALUES (?, ?, ?, ?, ?)""",
            (row["id"], row["chat_id"], row["created"], row["status"],
             int(row["template_id"] == "contact_received" or contact_supplied(row["text"]))),
        )
    conn.execute("INSERT OR IGNORE INTO runtime_state VALUES ('statistics_backfilled_v1', 1)")


def refresh(conn, chat=None):
    # Keep outcome snapshots after explicit clearing of the operational queue.
    conn.execute("""
        UPDATE statistics_events
        SET reply_status=(SELECT j.status FROM jobs AS j WHERE j.id=job_id),
            contact_received=MAX(contact_received, EXISTS (
                SELECT 1 FROM jobs AS j
                WHERE j.id=job_id AND j.template_id='contact_received'
            ))
        WHERE EXISTS (SELECT 1 FROM jobs AS j WHERE j.id=job_id)
          AND (? IS NULL OR chat_id=?)
    """, (chat, chat))


@dataclass(frozen=True)
class Report:
    period: str
    start: Optional[datetime]
    end: datetime
    as_of: datetime
    total: int
    answered: int
    handed_off: int
    contacts: int
    pending: int
    earliest: Optional[datetime]


def window(period, now):
    if period not in PERIODS:
        raise ValueError("Неизвестный период статистики")
    if now.tzinfo is None:
        raise ValueError("Для статистики требуется время с часовым поясом")
    local = now.astimezone(MOSCOW)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "all":
        return None, local
    if period == "yesterday":
        return midnight - timedelta(days=1), midnight
    days = {"today": 1, "7d": 7, "30d": 30}[period]
    return midnight - timedelta(days=days - 1), local


def collect(db, period, *, now=None):
    now = datetime.now(MOSCOW) if now is None else now
    start, end = window(period, now)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        refresh(conn)
        rows = conn.execute("""
            SELECT chat_id, MIN(received_at) AS first_received,
                   MAX(reply_status='sent') AS answered,
                   MAX(contact_received) AS contact_received,
                   MAX(reply_status NOT IN ('pending','processing','sending','merged','dry_run','sent')) AS handed_off
            FROM statistics_events
            WHERE received_at<=? AND reply_status!='dry_run'
            GROUP BY chat_id
        """, (now.timestamp(),)).fetchall()

    earliest = min((row["first_received"] for row in rows), default=None)
    selected = [
        row for row in rows
        if (start is None or row["first_received"] >= start.timestamp())
        and row["first_received"] < end.timestamp()
    ]
    answered = sum(bool(row["answered"]) for row in selected)
    handed_off = sum(not row["answered"] and bool(row["handed_off"]) for row in selected)
    return Report(
        period, start, end, now.astimezone(MOSCOW), len(selected), answered, handed_off,
        sum(bool(row["contact_received"]) for row in selected),
        len(selected) - answered - handed_off,
        datetime.fromtimestamp(earliest, MOSCOW) if earliest is not None else None,
    )


def format_report(report):
    if report.start is None:
        period = "За всё время"
    else:
        last_day = report.end - timedelta(microseconds=1) if report.period == "yesterday" else report.end
        first = report.start.strftime("%d.%m.%Y")
        last = last_day.strftime("%d.%m.%Y")
        period = first if first == last else f"{first} — {last}"
    lines = [
        f"Статистика Авито. {PERIODS[report.period]}",
        f"Период — {period}, МСК",
        "",
        f"Новых чатов — {report.total}",
        f"Ответил бот — {report.answered}",
        f"Передано администратору без ответа бота — {report.handed_off}",
        f"Получен контакт — {report.contacts}",
    ]
    if report.pending:
        lines.append(f"Ожидают обработки — {report.pending}")
    if report.earliest is not None:
        lines.extend(("", f"Данные доступны с {report.earliest.strftime('%d.%m.%Y')}."))
    else:
        lines.extend(("", "Сохранённых обращений пока нет."))
    lines.append(f"Состояние на {report.as_of.strftime('%d.%m.%Y %H:%M')} МСК.")
    return "\n".join(lines)
