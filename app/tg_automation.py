"""Controls for the persisted daily application workflow."""

import json
from datetime import datetime, timezone

from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from .automation import AutomationStore
from .assessment import DIRECTIONS, ROLES
from .portfolio import LETTER_LIMIT

MODES = {'auto':'Автоматический','semi':'Полуручной','paused':'Пауза'}
STATES = {'QUEUED':'Ожидает обработки','WAITING_AI':'Ожидает обновления лимита OpenAI','PREPARING':'Подготовка','REVIEW':'Нужна проверка',
          'READY':'Готово к отправке','BLOCKED':'Отправка остановлена','SENDING':'Отправляется',
          'SENT':'Отправлено','UNKNOWN':'Исход неизвестен','UNKNOWN_DISMISSED':'Снято с проверки · исход неизвестен','REJECTED':'Отклонено',
          'UNSUITABLE':'Не подошло','FAILED':'Ошибка подготовки','STALE':'Пакет устарел'}

CATEGORIES = {
    'review':('Проверить и отправить',('REVIEW','READY')),
    'waiting':('Ожидают обработки',('QUEUED','WAITING_AI')),
    'processing':('В работе',('PREPARING','SENDING')),
    'attention':('Требуют внимания',('BLOCKED','FAILED','STALE','UNKNOWN')),
    'sent':('Отправлены',('SENT',)),
    'closed':('Сняты с очереди',('REJECTED','UNSUITABLE','UNKNOWN_DISMISSED')),
    'all':('Все задания',None),
}

REJECTABLE = ('QUEUED','WAITING_AI','REVIEW','READY','BLOCKED','FAILED','STALE')


def buttons(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label,callback_data=data) for label,data in row] for row in rows])


class AutomationConsole:
    def __init__(self, path, engine):
        self.path, self.engine, self.pending = path, engine, None

    async def settings(self, message):
        from .ai_limits import effective_limits
        call_limit,token_limit = effective_limits(self.path,self.engine.service.env)
        store = AutomationStore(self.path)
        try:
            values = store.settings()
            progress = store.queue_status()
            usage = (0,0)
            if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='ai_call_usage'").fetchone():
                usage = store.db.execute('SELECT count(*),coalesce(sum(tokens),0) FROM ai_call_usage WHERE day=?',
                    (datetime.now(timezone.utc).date().isoformat(),)).fetchone()
        finally:
            store.close()
        series = self.engine.browser_series
        browser_timeout = f"Закрытие Chrome после простоя: {values['browser_idle_minutes']} мин.\n" if series else ''
        await message.answer(f"Ежедневная обработка\nРежим: {MODES[values['mode']]}\n"
            f"Время: {values['time']} · Минск\nПорог: {values['threshold']}/100\n"
            f"Лимит подготовки и отправки: {values['daily_limit']} в день\n"
            f"Обработано сегодня: {progress['prepared']}/{values['daily_limit']} · ожидают: {progress['waiting']}\n"
            f"OpenAI: {usage[0]}/{call_limit} запросов; "
            f"{usage[1]}/{token_limit} токенов (включая резервы). Сброс в 03:00 по Минску.\n"
            f"Вечерняя сводка: {values['insights_time']} · Минск\n"
            f"{browser_timeout}"
            f"Отправка: {'пауза до проверки браузера' if values['browser_hold'] else 'подключена' if self.engine.sender.available else 'не подключена; пакеты сохраняются в очереди'}\n\n"
            + ('Пройдите ручную проверку в открытом окне Chrome бота и оставьте его открытым. '
               'Если окна уже нет, запустите на компьютере проекта:\n'
               '.\\.venv\\Scripts\\python.exe -m app browser-login\n'
               'Откройте нужную вакансию, пройдите проверку и закройте окно, запущенное этой командой. '
               'Затем нажмите «Возобновить отправку».\n\n' if values['browser_hold'] else '')
            +
            'Режимы применяются к следующим действиям. Переключение в авто не отправляет ранее подготовленные пакеты. '
            'Вопросы и расхождения направляются на проверку. Очередь: /queue',
            reply_markup=buttons([
                [(MODES[m],f'flow:mode:{m}') for m in ('auto','semi')],
                [('Пауза','flow:mode:paused')],
                [('Время запуска','flow:setting:time'),('Порог','flow:setting:threshold')],
                [('Дневной лимит','flow:setting:daily_limit')],
                [('Запросы OpenAI','flow:setting:ai_daily_calls'),('Токены OpenAI','flow:setting:ai_daily_tokens')],
                [('Время вечерней сводки','flow:setting:insights_time')],
                *([[('Простой браузера','flow:setting:browser_idle_minutes')]] if series else []),
                *([[('Возобновить отправку','flow:browserresume:0')]] if values['browser_hold'] else []),
                [('Очередь','flow:categories:0')]]))

    async def queue(self, message, page=0, category=None):
        if category is not None and category not in CATEGORIES:
            raise ValueError('Неизвестная категория очереди.')
        store = AutomationStore(self.path)
        try:
            counts = dict(store.db.execute('SELECT state,count(*) FROM application_jobs GROUP BY state'))
            if category is None:
                rows = [[(f'{label} · {sum(counts.values()) if states is None else sum(counts.get(s,0) for s in states)}',
                          f'flow:category:{key}:0')] for key,(label,states) in CATEGORIES.items()]
                await message.answer('Очередь откликов · выберите категорию\n'
                    '«Требуют внимания»: ошибки, остановленные и устаревшие пакеты, неизвестный результат отправки.\n'
                    '«Сняты с очереди»: отклонённые, не прошедшие отбор и убранные из внимания.',reply_markup=buttons(rows))
                return
            label,states = CATEGORIES[category]
            count = sum(counts.values()) if states is None else sum(counts.get(s,0) for s in states)
            pages = max(1,(count+19)//20)
            page = min(max(0,page),pages-1)
            jobs = store.jobs(states,limit=20,offset=page*20,newest_first=True)
        finally:
            store.close()
        if not jobs:
            await message.answer(f'{label}: заданий пока нет.',reply_markup=buttons([[('Категории','flow:categories:0')]]))
            return
        rows = [[
            (f"#{j['id']} · {STATES[j['state']]} · {json.loads(j['raw_json']).get('name','Вакансия')[:35]}",
             f"flow:view:{j['id']}")] for j in jobs]
        navigation = []
        if page>0:
            navigation.append(('← Новее',f'flow:category:{category}:{page-1}'))
        if page+1<pages:
            navigation.append(('Раньше →',f'flow:category:{category}:{page+1}'))
        if navigation:
            rows.append(navigation)
        if pages>1:
            rows.append([('Самые новые',f'flow:category:{category}:0'),('Самые первые',f'flow:category:{category}:{pages-1}')])
        rows.append([('Категории','flow:categories:0')])
        await message.answer(f'{label} · страница {page+1} из {pages} · заданий: {count}',reply_markup=buttons(rows))

    async def show(self, message, ident):
        from .tg_bot import vacancy_card
        store = AutomationStore(self.path)
        try:
            job = store.get(ident)
            package = store.package(ident) if job['package_revision'] else None
            receipt_row = store.db.execute('SELECT receipt_json FROM application_receipts WHERE job_id=?',(ident,)).fetchone()
            receipt = json.loads(receipt_row[0]) if receipt_row else None
            letter = None
            if package:
                if job['state']=='SENT':
                    letter = receipt.get('letter_text',package['letter_text']) if isinstance(receipt,dict) else package['letter_text']
                else:
                    letter = self.engine.outgoing_letter(store,job,package)
            progress = store.queue_status(ident)
            ai_pause = store.db.execute('SELECT retry_at FROM automation_ai_pause WHERE id=1').fetchone()
        finally:
            store.close()
        text = f"Задание #{ident}: {STATES[job['state']]}\n\n" + vacancy_card(json.loads(job['raw_json']))
        if job['evaluation_json']:
            evaluation = json.loads(job['evaluation_json'])
            text += f"\n\nСоответствие: {evaluation['score']}/100\n{evaluation['summary']}"
        if job['error']:
            text += '\n\n'+job['error']
        if job['state']=='SENT' and receipt:
            text += (f"\n\nДоставка подтверждена площадкой · версия {receipt['revision']}."
                     f"\nПодтверждение (UTC): {receipt.get('confirmed_at','—')}")
        if job['state'] in ('UNKNOWN','UNKNOWN_DISMISSED'):
            text += '\nПовторная отправка заблокирована. Проверьте раздел «Отклики» на rabota.by.'
        if job['state']=='WAITING_AI':
            from .automation import MINSK
            reset = datetime.fromisoformat(ai_pause[0]).astimezone(MINSK).strftime('%d.%m %H:%M') if ai_pause else 'при следующей обработке'
            text += f'\n\nЛокальный лимит OpenAI исчерпан. Продолжение автоматически: {reset} по Минску, если бот запущен. Повторять вручную не нужно.'
        if job['state']=='QUEUED':
            text += (f"\n\nПакет ещё не подготовлен. Позиция в очереди: {progress['position']} из {progress['waiting']}."
                     f"\nОбработано сегодня: {progress['prepared']}/{progress['daily_limit']} вакансий. "
                     'В лимит входят и вакансии, не прошедшие отбор.')
            if progress['mode']=='paused':
                text += '\nОбработка на паузе. Возобновите её в настройках.'
            elif progress['remaining']==0:
                text += (f"\nДневной лимит исчерпан. Он обновится {progress['reset']} по Минску. "
                         'После этого очередь продолжится, если бот запущен. Лимит можно изменить в настройках.')
            else:
                text += '\nОбработка начнётся по очереди, когда завершится текущая операция. Результат придёт отдельным сообщением.'
        await message.answer(text[:4000],parse_mode=None,link_preview_options=LinkPreviewOptions(is_disabled=True))
        actions = []
        if package:
            metadata = json.loads(package['metadata_json'])
            key = metadata.get('direction')
            label = ROLES.get(key,'—')
            await message.answer(f'Резюме: {label}\n\nСопроводительное письмо\n\n{letter}',
                                 parse_mode=None,link_preview_options=LinkPreviewOptions(is_disabled=True))
            questions = metadata.get('questions',[])+metadata.get('gaps',[])
            if questions:
                await message.answer('Проверить перед отправкой:\n'+'\n'.join(questions)[:3700],parse_mode=None)
            prefix = f"{ident}:{package['revision']}"
            if job['state'] in ('REVIEW','READY','BLOCKED'):
                if key in DIRECTIONS:
                    if progress['browser_hold']:
                        await message.answer('Отправка на паузе до проверки браузера. Инструкции доступны в «Настройки».')
                    elif len(letter)<=LETTER_LIMIT:
                        actions.append([('Отправить',f'flow:send:{prefix}')])
                    else:
                        await message.answer(f'Сократите письмо: вместе со ссылками допускается до {LETTER_LIMIT} символов.')
                    actions.append([('Изменить письмо',f'flow:editletter:{prefix}')])
                else:
                    await message.answer('Этот пакет подготовлен для прежнего AI-направления. Нажмите «Подготовить заново», чтобы выбрать актуальное резюме.')
            actions.append([('Выгрузить письмо TXT',f'flow:export:{prefix}')])
        if job['state'] in ('FAILED','STALE','REVIEW','READY','BLOCKED','UNSUITABLE'):
            actions.append([('Подготовить заново',f'flow:retry:{ident}')])
        revision = job['package_revision'] or 0
        if job['state'] in REJECTABLE:
            actions.append([('Отклонить',f'flow:reject:{ident}:{revision}')])
        elif job['state']=='UNKNOWN':
            actions.append([('Убрать из внимания',f'flow:dismiss:{ident}:{revision}')])
        actions.append([('Обновить статус',f'flow:view:{ident}')])
        category=next((key for key,(_,states) in CATEGORIES.items() if states and job['state'] in states),'all')
        actions.append([('В категорию',f'flow:category:{category}:0')])
        actions.append([('Категории очереди','flow:categories:0'),('Настройки','flow:settings:0')])
        await message.answer('Действия с текущей версией пакета:' if package else 'Действия с заданием:',reply_markup=buttons(actions))

    async def callback(self, callback):
        self.pending = None
        parts = callback.data.split(':')
        if len(parts)<3:
            raise ValueError('Некорректная кнопка.')
        action, value = parts[1:3]
        if action in ('categories','category'):
            await callback.answer()
            await self.queue(callback.message,int(parts[3]) if action=='category' and len(parts)==4 else 0,
                             value if action=='category' else None)
            return
        store = AutomationStore(self.path)
        try:
            if action=='browserresume':
                await callback.answer()
                try:
                    if self.engine.browser_series:
                        await self.engine.browser_series.resume_in_series(store.settings()['browser_idle_minutes'])
                except ValueError as exc:
                    await callback.message.answer(str(exc),parse_mode=None)
                    return
                store.configure('browser_hold',False)
                await self.settings(callback.message)
                return
            if action=='mode':
                store.configure('mode',value)
                await callback.answer('Режим сохранён')
                await self.settings(callback.message)
                return
            if action=='setting':
                if value not in ('time','threshold','daily_limit','insights_time','ai_daily_calls','ai_daily_tokens','browser_idle_minutes'):
                    raise ValueError('Неизвестная настройка.')
                self.pending = ('setting',value)
                await callback.answer()
                await callback.message.answer({'time':'Введите время ЧЧ:ММ по Минску.',
                    'insights_time':'Введите время вечерней сводки ЧЧ:ММ по Минску.',
                    'ai_daily_calls':'Введите лимит запросов OpenAI в сутки: 1–100. Повышение может увеличить расход API.',
                    'ai_daily_tokens':'Введите лимит токенов OpenAI в сутки: 10000–2000000. Повышение может увеличить расход API.',
                    'browser_idle_minutes':'Введите, через сколько минут простоя закрывать Chrome: 1–240.',
                    'threshold':'Введите порог соответствия от 0 до 100.',
                    'daily_limit':'Введите дневной лимит от 1 до 50.'}[value]+' Отмена: /cancel.')
                return
            if action in ('settings','list'):
                await callback.answer()
                await (self.settings(callback.message) if action=='settings' else self.queue(callback.message,int(value),'all'))
                return
            ident = int(value)
            job = store.get(ident)
            if action=='view':
                await callback.answer()
                await self.show(callback.message,ident)
                return
            if action=='retry':
                from .config import SearchConfig
                from .collector import search_key
                from .resumes import snapshot
                digest = snapshot(self.path)[1]
                # Only an explicit action can resume failed or stale preparation.
                with store.db:
                    store.db.execute('BEGIN IMMEDIATE')
                    job = store.get(ident)
                    if job['state'] not in ('FAILED','STALE','REVIEW','READY','BLOCKED','UNSUITABLE'):
                        raise ValueError('Повтор этого задания запрещён.')
                    store.db.execute("UPDATE application_jobs SET state='QUEUED',config_key=?,profile_hash=?,error=NULL WHERE id=?",
                        (search_key(SearchConfig.load(self.engine.service.config_path)),digest,ident))
                await callback.answer('Повторная подготовка поставлена в очередь')
                return
            if len(parts)!=4 or int(parts[3])!=(job['package_revision'] or 0):
                raise ValueError('Версия пакета изменилась. Откройте очередь заново.')
            if action=='reject':
                store.transition(ident,'REJECTED',expected=REJECTABLE,expected_revision=int(parts[3]))
            elif action=='dismiss':
                store.transition(ident,'UNKNOWN_DISMISSED',expected=('UNKNOWN',),
                                 expected_revision=int(parts[3]),error=job['error'])
            elif action=='editletter':
                if job['state'] not in ('REVIEW','READY','BLOCKED'):
                    raise ValueError('Это задание нельзя изменять.')
                self.pending = (action,ident,int(parts[3]))
            elif action=='export':
                package = store.package(ident,int(parts[3]))
                if job['state']=='SENT':
                    row = store.db.execute('SELECT receipt_json FROM application_receipts WHERE job_id=?',(ident,)).fetchone()
                    receipt = json.loads(row[0]) if row else None
                    letter = receipt.get('letter_text',package['letter_text']) if isinstance(receipt,dict) else package['letter_text']
                else:
                    letter = self.engine.outgoing_letter(store,job,package)
                direction = json.loads(package['metadata_json']).get('direction')
                text = f"Задание #{ident} · версия {package['revision']} · {STATES[job['state']]}\nРезюме: {ROLES.get(direction,'—')}\n\n{letter}"
                await callback.answer()
                await callback.message.answer_document(BufferedInputFile(text.encode('utf-8-sig'),filename=f'package_{ident}_v{package["revision"]}.txt'))
                return
            else:
                raise ValueError('Неизвестное действие.')
        finally:
            store.close()
        if action in ('reject','dismiss') and job['state'] in CATEGORIES['attention'][1]:
            await callback.answer('Вакансия отклонена' if action=='reject' else 'Задание убрано из внимания')
            await self.queue(callback.message,category='attention')
            return
        await callback.answer()
        if action=='editletter':
            await callback.message.answer('Пришлите новый текст письма одним сообщением. Бот добавит подходящие ссылки на портфолио. /cancel — отмена.')
        else:
            await self.show(callback.message,ident)

    async def save_text(self, message):
        if not message.text:
            await message.answer('Отправьте значение текстом или /cancel.')
            return
        store = AutomationStore(self.path)
        try:
            pending = self.pending
            if pending[0]=='setting':
                settings = store.configure(pending[1],message.text)
                if pending[1]=='browser_idle_minutes' and self.engine.browser_series:
                    await self.engine.browser_series.set_idle_minutes(settings['browser_idle_minutes'])
                if pending[1] in ('ai_daily_calls','ai_daily_tokens'):
                    with store.db:
                        store.db.execute('DELETE FROM automation_ai_pause')
                        store.db.execute("UPDATE application_jobs SET state='QUEUED',error=NULL WHERE state='WAITING_AI'")
            else:
                _, ident, revision = pending
                package = store.package(ident,revision)
                letter = self.engine.outgoing_letter(store,store.get(ident),dict(package,letter_text=message.text))
                if len(letter)>LETTER_LIMIT:
                    raise ValueError(f'Сократите письмо: вместе со ссылками допускается до {LETTER_LIMIT} символов.')
                store.save_package(ident,'',message.text,package['description_id'],
                    dict(json.loads(package['metadata_json']),user_edit=True),expected_revision=revision)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        finally:
            store.close()
        self.pending = None
        await message.answer('Изменение сохранено.')
        await (self.settings(message) if pending[0]=='setting' else self.show(message,pending[1]))
