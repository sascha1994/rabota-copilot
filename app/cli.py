import argparse
import json
import sqlite3
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .collector import search_key
from .config import SearchConfig
from .env import load_env
from .hh_client import ApiError
from .storage import Storage
from .sources import make_collector, source_name


def main(argv=None):
    # Windows consoles may use a legacy code page; API titles can contain emoji.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="Поиск вакансий rabota.by без отправки откликов")
    try:
        env = load_env()
    except (ValueError, OSError) as exc:
        print(f"Ошибка загрузки .env: {exc}", file=sys.stderr)
        return 1
    parser.add_argument("--config", default=env.get("SEARCH_CONFIG", "search.example.json"))
    parser.add_argument("--db", default=env.get("SQLITE_PATH", "data/vacancies.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bot", help="Запустить Telegram-бота")
    send_job = commands.add_parser('send-job',help='Отправить выбранную версию готового пакета через настроенный адаптер')
    send_job.add_argument('--job',type=int,required=True)
    send_job.add_argument('--revision',type=int,required=True)
    browser_check = commands.add_parser('browser-check',help='Проверить форму отклика без отправки и изменения резюме')
    browser_check.add_argument('--job',type=int,required=True)
    browser_check.add_argument('--status-only',action='store_true',help='Прочитать состояние вакансии без открытия формы отклика')
    browser_login = commands.add_parser('browser-login',help='Войти в rabota.by в отдельном браузерном профиле')
    browser_login.add_argument('--profile-dir',default=env.get('BROWSER_PROFILE_DIR','data/browser-profile'))
    browser_login.add_argument('--channel',default=env.get('BROWSER_CHANNEL','chrome'))
    backup = commands.add_parser('backup', help='Создать согласованную копию SQLite')
    backup.add_argument('--output-dir', default=env.get('BACKUP_DIR','data/backups'))
    check = commands.add_parser('backup-verify', help='Проверить резервную копию')
    check.add_argument('--input', required=True)
    recovery = commands.add_parser('restore', help='Восстановить в новый файл базы')
    recovery.add_argument('--input', required=True)
    recovery.add_argument('--output', required=True)
    profile_import = commands.add_parser("profile-import", help="Импортировать черновик профиля без подтверждения фактов")
    profile_import.add_argument("--input", default="data/candidate_profile.draft.json")
    collect = commands.add_parser("collect", help="Собрать вакансии из выбранного источника")
    collect.add_argument("--days", type=int, default=1, help="Глубина первого сбора, 1–30 дней")
    collect.add_argument("--full", action="store_true", help="Повторно проверить выбранный период")
    collect.add_argument("--watch", type=int, metavar="SECONDS", help="Повторять сбор, минимум 60 секунд")
    commands.add_parser("list", help="Показать сохранённые вакансии текущего поиска").add_argument("--limit", type=int, default=20)
    export = commands.add_parser("export", help="Экспорт в JSON")
    export.add_argument("--output", default="data/vacancies.json")
    export.add_argument("--limit", type=int, default=10000)
    args = parser.parse_args(argv)
    if args.command == "collect" and (not 1 <= args.days <= 30 or (args.watch is not None and args.watch < 60)):
        parser.error("--days должен быть от 1 до 30; --watch — не менее 60 секунд")
    if args.command in ("list", "export") and args.limit < 1:
        parser.error("--limit должен быть положительным")
    storage = None
    try:
        if args.command == 'send-job':
            import asyncio
            from .automation import AutomationEngine
            from .tg_bot import SearchService
            service = SearchService(args.config,args.db,env.get('HH_USER_AGENT',''),env=env)
            print(asyncio.run(AutomationEngine(service).send(args.job,args.revision)))
            return 0
        if args.command == 'browser-check':
            from .browser_sender import BrowserSender
            from .automation import AutomationStore
            store = AutomationStore(args.db)
            try:
                job = store.get(args.job)
                package = dict(store.package(args.job),vacancy_id=job['vacancy_id'])
            finally:
                store.close()
            print(json.dumps(BrowserSender(env).check(package,status_only=args.status_only),ensure_ascii=False))
            return 0
        if args.command == 'browser-login':
            import asyncio
            from .browser_session import login
            asyncio.run(login(args.profile_dir,args.channel))
            return 0
        if args.command in ('backup','backup-verify','restore'):
            from .backup import create_backup, verify, restore
            if args.command == 'backup':
                output = create_backup(args.db,args.output_dir)
                print(f'Проверенная копия создана: {output}')
            elif args.command == 'backup-verify':
                verify(args.input)
                print('Целостность и базовая структура копии проверены.')
            else:
                output = restore(args.input,args.output)
                print(f'База восстановлена в новый файл: {output}')
                print('Остановите бота перед переключением SQLITE_PATH на восстановленный файл.')
            return 0
        if args.command == "profile-import":
            from .candidate_profile import ProfileStore
            profile = ProfileStore(args.db)
            try:
                count = profile.import_draft(json.loads(Path(args.input).read_text(encoding='utf-8')))
                print(f"Добавлено неподтверждённых пунктов: {count}")
            finally:
                profile.close()
            return 0
        if args.command == "bot":
            import asyncio
            try:
                from .tg_bot import run_bot
            except ModuleNotFoundError:
                raise ValueError("Установите зависимости: python -m pip install -e .") from None
            from .process_lock import ProcessLock
            with ProcessLock(args.db):
                asyncio.run(run_bot(env, args.config, args.db))
            return 0
        config = SearchConfig.load(args.config)
        if args.command == "collect" and args.watch and source_name(env) == "web":
            raise ValueError("Публичный поиск запускается вручную: режим --watch доступен только источнику api")
        storage = Storage(args.db)
        if args.command == "collect":
            collector = make_collector(env, storage, config)
            while True:
                try:
                    stats = collector.collect(days=args.days, full=args.full)
                    print(json.dumps(asdict(stats), ensure_ascii=False), flush=True)
                except ApiError as exc:
                    if not args.watch:
                        raise
                    print(f"Сбор не завершён: {exc}", file=sys.stderr, flush=True)
                if not args.watch:
                    break
                time.sleep(args.watch)
        else:
            rows = storage.results(search_key(config), args.limit)
            if args.command == "export":
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.output).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"Сохранено вакансий: {len(rows)} → {args.output}")
            else:
                for item in rows:
                    salary = item.get("salary")
                    pay = f"{salary.get('from') or '—'}–{salary.get('to') or '—'} {salary.get('currency', '')}" if salary else "не указана"
                    print(f"[{item['id']}] {item.get('name', '')} | {(item.get('employer') or {}).get('name', '—')} | {pay}\n{item.get('alternate_url', '')}\n")
                print(f"Показано: {len(rows)}")
        return 0
    except (ValueError, OSError, sqlite3.Error, ApiError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nСбор остановлен. Сохранённые вакансии остаются в БД.")
        return 130
    finally:
        if storage:
            storage.close()
