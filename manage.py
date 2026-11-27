import argparse
import asyncio
import json
import os
from urllib.parse import urlparse
import httpx
import chat_statistics
import telegram_admin
from app import AvitoClient, config, db, dry_run, init_db, templates
from telegram_notify import settings as telegram_settings

async def main():
    parser = argparse.ArgumentParser(description='Управление ботом')
    sub = parser.add_subparsers(dest='command', required=True)
    reg = sub.add_parser('subscribe', help='Подключить HTTPS-вебхук')
    reg.add_argument('base_url', help='Например https://bot.example.com')
    sub.add_parser('account', help='Узнать ID аккаунта Avito')
    sub.add_parser('jobs', help='Статусы последних 50 сообщений')
    sub.add_parser('telegram-jobs', help='Статусы последних 50 уведомлений')
    sub.add_parser('check', help='Проверить настройки и шаблоны')
    stats = sub.add_parser('stats', help='Статистика новых чатов по московскому времени')
    stats.add_argument('--period', choices=tuple(chat_statistics.PERIODS), default='all')
    for command in ('stats-exclude', 'stats-include'):
        sub.add_parser(command, help='Изменить исключения статистики').add_argument('identifiers', nargs='+')
    sub.add_parser('stats-exclusions', help='Показать исключённые чаты Авито')
    for command in ('pause-chat', 'resume-chat', 'ack-telegram'):
        sub.add_parser(command).add_argument('identifier')
    args = parser.parse_args()
    if args.command in ('stats-exclude', 'stats-include', 'stats-exclusions'):
        init_db(recover=False)
        with db() as c:
            if args.command == 'stats-exclusions':
                rows = c.execute('SELECT chat_id FROM statistics_excluded_chats ORDER BY chat_id').fetchall()
                print('\n'.join(row['chat_id'] for row in rows) or 'Исключений нет.')
            else:
                try:
                    chat_statistics.set_excluded(
                        c, args.identifiers, excluded=args.command == 'stats-exclude',
                    )
                except ValueError as exc:
                    raise SystemExit(str(exc)) from None
                print('Исключения статистики обновлены.')
        return
    if args.command == 'stats':
        init_db(recover=False)
        print(chat_statistics.format_report(chat_statistics.collect(db, args.period)))
        return
    if args.command in ('pause-chat', 'resume-chat', 'ack-telegram'):
        init_db(recover=False)
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            if args.command == 'pause-chat':
                c.execute('INSERT OR IGNORE INTO manual_chats VALUES (?)', (args.identifier,))
            elif args.command == 'resume-chat':
                if c.execute(
                    "SELECT 1 FROM jobs WHERE chat_id=? AND status IN ('pending','processing','sending')",
                    (args.identifier,),
                ).fetchone():
                    raise SystemExit('В чате есть задачи в работе. Повторите после их завершения.')
                c.execute('DELETE FROM manual_chats WHERE chat_id=?', (args.identifier,))
                c.execute('DELETE FROM chat_dialogue WHERE chat_id=?', (args.identifier,))
                c.execute('DELETE FROM selector_turns WHERE chat_id=?', (args.identifier,))
            else:
                result = c.execute(
                    "UPDATE telegram_jobs SET status='acknowledged' WHERE id=? AND status IN ('failed','unknown')",
                    (args.identifier,),
                )
                if not result.rowcount:
                    raise SystemExit('Нет сбоя Telegram с таким ID.')
        print('Готово.')
        return
    if args.command == 'telegram-jobs':
        init_db(recover=False)
        with db() as c:
            for row in c.execute('SELECT id, chat_id, status, attempts FROM telegram_jobs ORDER BY rowid DESC LIMIT 50'):
                print(dict(row))
        return
    if args.command == 'jobs':
        init_db(recover=False)
        with db() as c:
            for row in c.execute('SELECT id, chat_id, status, template_id, error FROM jobs ORDER BY created DESC LIMIT 50'):
                print(dict(row))
        return
    if args.command == 'account':
        # account lookup is useful before the user ID is known
        if not os.getenv('AVITO_USER_ID'):
            os.environ['AVITO_USER_ID'] = '1'
    cfg = config()
    if args.command == 'check':
        telegram = telegram_settings()
        admin = telegram_admin.settings()
        if not dry_run() and not telegram:
            raise SystemExit('Для боевого режима нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID.')
        print('Настройки и шаблоны прочитаны. ID:', ', '.join(templates()))
        print('DRY_RUN:', dry_run())
        print('Telegram:', 'настроен' if telegram else 'отключён')
        print('Меню статистики:', 'настроено, доступ открыт всем' if admin else 'отключено')
        return
    async with httpx.AsyncClient(timeout=20) as client:
        avito = AvitoClient(client, cfg)
        if args.command == 'account':
            r = await avito.request('GET', '/core/v1/accounts/self')
        else:
            p = urlparse(args.base_url)
            if p.scheme != 'https' or not p.netloc or p.path not in ('', '/') or p.query or p.fragment or p.username:
                parser.error('Нужен базовый HTTPS URL без пути, логина, параметров и якоря')
            url = args.base_url.rstrip('/') + '/webhook/' + cfg['WEBHOOK_SECRET']
            r = await avito.request('POST', '/messenger/v3/webhook', json={'url': url})
        if not r.is_success:
            print('Avito HTTP', r.status_code, '(проверьте доступы и настройки API)')
            raise SystemExit(1)
        if args.command == 'account':
            print(json.dumps(r.json(), ensure_ascii=False, indent=2))
        else:
            print('Запрос на подписку выполнен. Проверьте доставку тестовым сообщением.')

if __name__ == '__main__':
    asyncio.run(main())
