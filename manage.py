import argparse
import asyncio
import json
import os
from urllib.parse import urlparse
import httpx
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
    args = parser.parse_args()
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
        print('Настройки и шаблоны прочитаны. ID:', ', '.join(templates()))
        print('DRY_RUN:', dry_run())
        print('Telegram:', 'настроен' if telegram else 'отключён')
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
