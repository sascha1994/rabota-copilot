"""Daily preparation queue. External submission requires a verified adapter."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from .assessment import AssessmentService, AssessmentError, DIRECTIONS, LocalQuotaExceeded
from .collector import search_key
from .config import SearchConfig
from .portfolio import LETTER_LIMIT, with_portfolio
from .resumes import snapshot
from .storage import Storage

MINSK = timezone(timedelta(hours=3))
DEFAULTS = {'mode':'auto', 'time':'09:00', 'threshold':75, 'daily_limit':10,
            'insights_time':'23:00','ai_daily_calls':None,'ai_daily_tokens':None,
            'browser_hold':False,'browser_idle_minutes':45}
TERMINAL = ('SENT','REJECTED','UNSUITABLE','UNKNOWN','UNKNOWN_DISMISSED','STALE')


class SubmissionUnavailable(ValueError):
    pass


class UnconfiguredSender:
    available = False

    def send(self, package):
        raise SubmissionUnavailable('Отправка на rabota.by не подключена. Пакет сохранён.')


class AutomationStore:
    def __init__(self, path):
        self.path = path
        self.storage = Storage(path)
        self.db = self.storage.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS automation_settings (
                id INTEGER PRIMARY KEY CHECK(id=1), json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS automation_runs (
                day TEXT PRIMARY KEY, status TEXT NOT NULL, started_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS application_jobs (
                id INTEGER PRIMARY KEY, vacancy_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL, config_key TEXT NOT NULL, profile_hash TEXT NOT NULL,
                raw_json TEXT NOT NULL, evaluation_json TEXT, package_revision INTEGER,
                error TEXT, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS application_packages (
                job_id INTEGER NOT NULL, revision INTEGER NOT NULL,
                resume_text TEXT NOT NULL, letter_text TEXT NOT NULL,
                description_id INTEGER NOT NULL, metadata_json TEXT NOT NULL,
                PRIMARY KEY(job_id,revision));
            CREATE TABLE IF NOT EXISTS application_receipts (
                job_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, receipt_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS application_events (
                id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, state TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS automation_ai_pause (
                id INTEGER PRIMARY KEY CHECK(id=1), retry_at TEXT NOT NULL);
        ''')

    def close(self):
        self.storage.close()

    def defer_ai(self, ident, retry_at):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('INSERT OR REPLACE INTO automation_ai_pause VALUES (1,?)',(retry_at,))
            # A quota deferral is not a completed preparation attempt for the daily budget.
            self.db.execute("UPDATE application_events SET state='PREPARING_QUOTA' WHERE id="
                "(SELECT max(id) FROM application_events WHERE job_id=? AND state='PREPARING')",(ident,))
            self.db.execute("UPDATE application_jobs SET state='WAITING_AI',error=NULL,updated_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(),ident))
            self.db.execute("INSERT INTO application_events(job_id,state,created_at) VALUES (?,'WAITING_AI',?)",
                (ident,datetime.now(timezone.utc).isoformat()))

    def ai_ready(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            pause = self.db.execute('SELECT retry_at FROM automation_ai_pause WHERE id=1').fetchone()
            if pause and datetime.fromisoformat(pause[0])>datetime.now(timezone.utc):
                return False
            self.db.execute("UPDATE application_jobs SET state='QUEUED',error=NULL WHERE state='WAITING_AI'")
            self.db.execute('DELETE FROM automation_ai_pause')
        return True

    def settings(self):
        row = self.db.execute('SELECT json FROM automation_settings WHERE id=1').fetchone()
        return {**DEFAULTS, **(json.loads(row[0]) if row else {})}

    def configure(self, field, value):
        if field=='browser_hold' and type(value) is not bool:
            raise ValueError('Некорректное состояние проверки браузера.')
        if field=='mode' and value not in ('auto','semi','paused'):
            raise ValueError('Неизвестный режим.')
        if field in ('time','insights_time'):
            try:
                hour, minute = map(int,value.split(':'))
                if not 0<=hour<=23 or not 0<=minute<=59:
                    raise ValueError()
                value = f'{hour:02}:{minute:02}'
            except (ValueError,AttributeError):
                raise ValueError('Введите время ЧЧ:ММ по Минску, например 09:00.') from None
        if field in ('threshold','daily_limit','ai_daily_calls','ai_daily_tokens','browser_idle_minutes'):
            try:
                value = int(value)
            except (ValueError,TypeError):
                raise ValueError('Введите целое число.') from None
            low, high = {'threshold':(0,100),'daily_limit':(1,500),
                         'ai_daily_calls':(1,1000),'ai_daily_tokens':(10000,10000000),'browser_idle_minutes':(1,240)}[field]
            if not low<=value<=high:
                raise ValueError(f'Допустимо от {low} до {high}.')
        if field not in DEFAULTS:
            raise ValueError('Неизвестная настройка.')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            settings = self.settings()
            settings[field] = value
            self.db.execute('INSERT OR REPLACE INTO automation_settings VALUES (1,?)', (json.dumps(settings),))
        return settings

    def due(self, now):
        settings = self.settings()
        local = now.astimezone(MINSK)
        return (settings['mode']!='paused' and local.strftime('%H:%M')>=settings['time']
                and not self.db.execute('SELECT 1 FROM automation_runs WHERE day=?',(local.date().isoformat(),)).fetchone())

    def claim_day(self, now):
        with self.db:
            cursor = self.db.execute('INSERT OR IGNORE INTO automation_runs VALUES (?,?,?)',
                (now.astimezone(MINSK).date().isoformat(),'RUNNING',now.isoformat()))
        return cursor.rowcount==1

    def queue_status(self, ident=None, now=None):
        local = (now or datetime.now(timezone.utc)).astimezone(MINSK)
        midnight = local.replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).isoformat()
        prepared = self.db.execute("SELECT count(*) FROM application_events WHERE state='PREPARING' AND created_at>=?", (midnight,)).fetchone()[0]
        waiting = self.db.execute("SELECT count(*) FROM application_jobs WHERE state='QUEUED'").fetchone()[0]
        position = self.db.execute("SELECT count(*) FROM application_jobs WHERE state='QUEUED' AND id<=?", (ident,)).fetchone()[0] if ident else None
        settings = self.settings()
        return {'prepared':prepared,'waiting':waiting,'position':position,
                'remaining':max(0,settings['daily_limit']-prepared),
                'reset':(local+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0).strftime('%d.%m %H:%M'),
                **settings}

    def daily_vacancy_counts(self, now=None):
        local = (now or datetime.now(timezone.utc)).astimezone(MINSK)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        bounds = tuple(day.astimezone(timezone.utc).isoformat()
                       for day in (start, start + timedelta(days=1)))
        found = self.db.execute("""
            SELECT count(*) FROM vacancies v
            WHERE v.first_seen >= ? AND v.first_seen < ?
              AND EXISTS (SELECT 1 FROM search_results s
                          WHERE s.vacancy_id = v.id AND s.rejection IS NULL)
        """, bounds).fetchone()[0]
        processed = self.db.execute("""
            SELECT count(DISTINCT e.job_id) FROM application_events e
            JOIN application_jobs j ON j.id = e.job_id
            WHERE e.state = 'PREPARING' AND e.created_at >= ? AND e.created_at < ?
              AND (j.state != 'PREPARING' OR e.id < (
                  SELECT max(active.id) FROM application_events active
                  WHERE active.job_id = e.job_id AND active.state = 'PREPARING'
              ))
        """, bounds).fetchone()[0]
        return found, processed

    def finish_day(self, now, state):
        with self.db:
            self.db.execute('UPDATE automation_runs SET status=? WHERE day=?',
                            (state,now.astimezone(MINSK).date().isoformat()))

    def recover(self):
        # A send may already have reached the site. Never retry it automatically.
        for row in self.db.execute("SELECT id,state FROM application_jobs WHERE state IN ('SENDING','PREPARING')").fetchall():
            self.transition(row['id'], 'UNKNOWN' if row['state']=='SENDING' else 'FAILED',
                            error='Процесс прерван. Требуется проверка перед повтором.')
        with self.db:
            self.db.execute("UPDATE automation_runs SET status='INTERRUPTED' WHERE status='RUNNING'")

    def enqueue(self, item, config_key, profile_hash):
        with self.db:
            return self.insert_job(item, config_key, profile_hash)

    def insert_job(self, item, config_key, profile_hash):
        cursor = self.db.execute("""
            INSERT INTO application_jobs
                (vacancy_id,state,config_key,profile_hash,raw_json,updated_at)
            SELECT ?, 'QUEUED', ?, ?, ?, ?
            WHERE NOT EXISTS (SELECT 1 FROM vacancy_tracking
                              WHERE vacancy_id=? AND state IN ('skipped','applied'))
            ON CONFLICT(vacancy_id) DO NOTHING
        """, (str(item['id']), config_key, profile_hash, json.dumps(item, ensure_ascii=False),
              datetime.now(timezone.utc).isoformat(), str(item['id'])))
        return cursor.rowcount

    def get(self, ident):
        row = self.db.execute('SELECT * FROM application_jobs WHERE id=?',(ident,)).fetchone()
        if row is None:
            raise ValueError('Задание не найдено.')
        return dict(row)

    def jobs(self, states=None, limit=20, offset=0, newest_first=False):
        if states:
            query = 'SELECT * FROM application_jobs WHERE state IN ('+','.join('?' for _ in states)+') ORDER BY id '+('DESC' if newest_first else 'ASC')+' LIMIT ? OFFSET ?'
            return [dict(r) for r in self.db.execute(query,(*states,limit,offset))]
        return [dict(r) for r in self.db.execute('SELECT * FROM application_jobs ORDER BY id DESC LIMIT ? OFFSET ?', (limit,offset))]

    def transition(self, ident, state, *, expected=None, expected_revision=None, error=None):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            item = self.get(ident)
            if expected and item['state'] not in expected:
                raise ValueError('Состояние задания изменилось. Откройте очередь заново.')
            if expected_revision is not None and (item['package_revision'] or 0)!=expected_revision:
                raise ValueError('Версия пакета изменилась. Откройте очередь заново.')
            stamp = datetime.now(timezone.utc).isoformat()
            self.db.execute('UPDATE application_jobs SET state=?,error=?,updated_at=? WHERE id=?', (state,error,stamp,ident))
            self.db.execute('INSERT INTO application_events(job_id,state,created_at) VALUES (?,?,?)',(ident,state,stamp))

    def package(self, ident, revision=None):
        job = self.get(ident)
        row = self.db.execute('SELECT * FROM application_packages WHERE job_id=? AND revision=?',
                              (ident,revision or job['package_revision'])).fetchone()
        if row is None:
            raise ValueError('Пакет ещё не подготовлен.')
        return dict(row)

    def reopen_after_verification(self, ident, revision):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            job = self.get(ident)
            if job['state'] not in ('UNKNOWN', 'UNKNOWN_DISMISSED') or job['package_revision'] != revision:
                raise ValueError('Состояние или версия изменились. Откройте карточку заново.')
            package = self.package(ident, revision)
            stamp = datetime.now(timezone.utc).isoformat()
            self.db.execute('INSERT INTO application_packages VALUES (?,?,?,?,?,?)',
                            (ident, revision + 1, package['resume_text'], package['letter_text'],
                             package['description_id'], package['metadata_json']))
            self.db.execute("UPDATE application_jobs SET state='REVIEW',package_revision=?,error=NULL,updated_at=? WHERE id=?",
                            (revision + 1, stamp, ident))
            self.db.execute("INSERT INTO application_events(job_id,state,created_at) VALUES (?,'REVIEW',?)",
                            (ident, stamp))

    def save_package(self, ident, resume, letter, description_id, metadata, *, expected_revision=None):
        if len(resume)>18000 or not 1<=len(letter.strip())<=LETTER_LIMIT:
            raise ValueError('Письмо должно содержать от 1 до 1800 символов.')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            job = self.get(ident)
            if job['state'] not in ('PREPARING','REVIEW','READY','BLOCKED'):
                raise ValueError('Это задание уже нельзя редактировать.')
            if expected_revision is not None and expected_revision!=job['package_revision']:
                raise ValueError('Пакет изменён. Откройте очередь заново.')
            revision = (job['package_revision'] or 0)+1
            self.db.execute('INSERT INTO application_packages VALUES (?,?,?,?,?,?)',
                (ident,revision,resume,letter,description_id,json.dumps(metadata,ensure_ascii=False)))
            self.db.execute("UPDATE application_jobs SET state='REVIEW',package_revision=?,error=NULL WHERE id=?",(revision,ident))
        return self.get(ident)


class AutomationEngine:
    def __init__(self, service, *, sender=None, ai_factory=AssessmentService, keep_browser_open=False):
        self.service = service
        from .browser_sender import SeriesBrowserSender, make_sender
        env = service.env if isinstance(service.env,dict) else {}
        self.sender = sender if sender is not None else make_sender(env,keep_open=keep_browser_open)
        self.browser_series = self.sender if isinstance(self.sender,SeriesBrowserSender) else None
        self.ai_factory = ai_factory

    def outgoing_letter(self, store, job, package):
        metadata = json.loads(package['metadata_json'])
        vacancy = json.loads(job['raw_json'])
        description = store.storage.description(package['description_id'])['text']
        return with_portfolio(package['letter_text'],metadata.get('direction'),
                              vacancy.get('name') or '',description,self.service.env)

    async def daily(self, notify):
        now = datetime.now(timezone.utc)
        store = AutomationStore(self.service.db_path)
        try:
            if not store.due(now) or not store.claim_day(now):
                return
            try:
                config = SearchConfig.load(self.service.config_path)
                stats = await asyncio.to_thread(self.service.collect, config)
                store.finish_day(now,'PARTIAL' if stats.partial else 'DONE')
                await notify(f'Ежедневный поиск завершён. Добавлено в очередь: {stats.queued}.' + (' Выдача частичная: достигнут лимит страниц.' if stats.partial else ''))
            except Exception:
                store.finish_day(now,'FAILED')
                await notify('Ежедневный поиск не завершён. Проверьте /status, фильтры и доступ к источнику. Сегодня автоматического повтора поиска не будет.')
        finally:
            store.close()

    def current(self, job):
        return (search_key(SearchConfig.load(self.service.config_path))==job['config_key']
                and snapshot(self.service.db_path)[1]==job['profile_hash'])

    async def prepare_one(self, notify):
        store = AutomationStore(self.service.db_path)
        ident = None
        try:
            settings = store.settings()
            if settings['mode']=='paused':
                return
            if not store.ai_ready():
                return
            day_start = datetime.now(timezone.utc).astimezone(MINSK).replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).isoformat()
            prepared = store.db.execute("SELECT count(*) FROM application_events WHERE state='PREPARING' AND created_at>=?",(day_start,)).fetchone()[0]
            if prepared>=settings['daily_limit']:
                return
            current_key = search_key(SearchConfig.load(self.service.config_path))
            facts, current_digest = snapshot(self.service.db_path)
            if not facts:
                return
            # Batch-retire unprepared queued items from superseded search filters instead of dripping one per cycle.
            stale_ids = [
                r[0] for r in store.db.execute(
                    "SELECT id FROM application_jobs WHERE state='QUEUED' AND package_revision IS NULL AND config_key!=?",
                    (current_key,),
                ).fetchall()
            ]
            for stale_id in stale_ids:
                store.transition(
                    stale_id,
                    'STALE',
                    expected=('QUEUED',),
                    error='Фильтры поиска изменились до начала подготовки. Нажмите «Подготовить заново», если вакансия актуальна.',
                )
            jobs = store.jobs(('QUEUED',),1)
            if not jobs:
                return
            job = jobs[0]; ident = job['id']
            # Unprepared queued jobs from the active filter adopt the latest confirmed profile automatically.
            if job['package_revision'] is None and job['config_key']==current_key and job['profile_hash']!=current_digest:
                with store.db:
                    store.db.execute(
                        "UPDATE application_jobs SET profile_hash=? WHERE id=? AND state='QUEUED'",
                        (current_digest, ident),
                    )
                job = store.get(ident)
            if not self.current(job):
                store.transition(
                    ident,
                    'STALE',
                    expected=('QUEUED',),
                    error='Фильтры поиска или профиль изменились. Нажмите «Подготовить заново».',
                )
                return
            mark = store.db.execute('SELECT state FROM vacancy_tracking WHERE vacancy_id=?',(job['vacancy_id'],)).fetchone()
            if mark and mark[0] in ('skipped','applied'):
                store.transition(ident,'REJECTED',expected=('QUEUED',))
                return
            store.transition(ident,'PREPARING',expected=('QUEUED',))
            ai = self.ai_factory(self.service.db_path,self.service.env)
            evaluation, _ = await asyncio.to_thread(ai.evaluate,job['vacancy_id'])
            with store.db:
                store.db.execute('UPDATE application_jobs SET evaluation_json=? WHERE id=?',(evaluation.model_dump_json(),ident))
            if evaluation.resume=='none' or evaluation.score<settings['threshold']:
                store.transition(ident,'UNSUITABLE',expected=('PREPARING',))
                return
            evaluated_description = ai.description_id
            result, _ = await asyncio.to_thread(ai.evaluate,job['vacancy_id'],application_direction=evaluation.resume)
            if not self.current(job):
                store.transition(ident,'STALE',expected=('PREPARING',))
                return
            metadata = {'direction':evaluation.resume,'questions':evaluation.questions+result.questions,
                        'gaps':evaluation.gaps,'model':self.service.env.get('OPENAI_MODEL','gpt-5.4-mini')}
            if evaluated_description!=ai.description_id:
                metadata['questions'].append('Описание изменилось после оценки. Требуется повторная проверка соответствия.')
            store.save_package(ident,'',result.text,ai.description_id,metadata)
            # Unknown requirements and explicit gaps always require review.
            letter_fits = len(self.outgoing_letter(store,job,store.package(ident)))<=LETTER_LIMIT
            current_settings = store.settings()
            if (settings['mode']=='auto' and current_settings['mode']=='auto' and not current_settings['browser_hold']
                    and not metadata['questions'] and not metadata['gaps'] and letter_fits):
                store.transition(ident,'READY',expected=('REVIEW',))
                await self.send(ident,store.get(ident)['package_revision'],automatic=True)
            await notify(f'Пакет #{ident} подготовлен. Откройте /queue для просмотра и действий.')
            return ident
        except LocalQuotaExceeded as exc:
            store.defer_ai(ident,exc.retry_at)
            reset = datetime.fromisoformat(exc.retry_at).astimezone(MINSK).strftime('%d.%m %H:%M')
            await notify(f'Достигнут локальный лимит OpenAI. Задание #{ident} сохранено; '
                         f'очередь продолжится автоматически {reset} по Минску, если бот запущен. '
                         'Запрос по этой операции не отправлялся.')
        except Exception as exc:
            if ident is not None and store.get(ident)['state']=='PREPARING':
                store.transition(ident,'FAILED',error=str(exc) if isinstance(exc,(AssessmentError,ValueError)) else 'Подготовка прервана. Проверьте профиль, доступ OpenAI и дневные лимиты.')
            await notify('Подготовка пакета не завершена. Подробности доступны в /queue. Повтор автоматически не выполняется.')
        finally:
            store.close()

    async def force_prepare(self, ident, direction=None):
        """Prepare a cover letter package bypassing score threshold (owner explicit override)."""
        store = AutomationStore(self.service.db_path)
        previous_state = None
        previous_error = None
        try:
            job = store.get(ident)
            if job['state'] not in ('UNSUITABLE', 'REJECTED', 'FAILED', 'STALE'):
                raise ValueError('Задание уже подготовлено или находится в обработке.')
            previous_state, previous_error = job['state'], job['error']
            evaluation_data = json.loads(job['evaluation_json'] or '{}')
            chosen_dir = direction or evaluation_data.get('resume')
            if chosen_dir not in DIRECTIONS:
                raise ValueError('Выберите направление резюме для принудительного отклика.')
            mark = store.db.execute('SELECT state FROM vacancy_tracking WHERE vacancy_id=?', (job['vacancy_id'],)).fetchone()
            if mark and mark[0] in ('skipped', 'applied'):
                raise ValueError('Вакансия пропущена или уже отмечена как отклик.')
            facts, digest = snapshot(self.service.db_path)
            if not facts:
                raise ValueError('Сначала подтвердите факты профиля в /profile.')
            current_key = search_key(SearchConfig.load(self.service.config_path))
            with store.db:
                store.db.execute('BEGIN IMMEDIATE')
                latest = store.get(ident)
                if latest['state'] != previous_state:
                    raise ValueError('Состояние задания изменилось. Откройте карточку заново.')
                stamp = datetime.now(timezone.utc).isoformat()
                store.db.execute(
                    "UPDATE application_jobs SET state='PREPARING',config_key=?,profile_hash=?,error=NULL,updated_at=? WHERE id=?",
                    (current_key, digest, stamp, ident),
                )
                store.db.execute("INSERT INTO application_events(job_id,state,created_at) VALUES (?,'PREPARING',?)", (ident, stamp))
            ai = self.ai_factory(self.service.db_path, self.service.env)
            result, _ = await asyncio.to_thread(ai.evaluate, job['vacancy_id'], application_direction=chosen_dir)
            metadata = {
                'direction': chosen_dir,
                'questions': list(evaluation_data.get('questions', [])) + list(result.questions),
                'gaps': list(evaluation_data.get('gaps', [])),
                'forced': True,
                'model': self.service.env.get('OPENAI_MODEL', 'gpt-5.4-mini'),
            }
            updated = store.save_package(ident, '', result.text, ai.description_id, metadata)
            return updated['package_revision']
        except Exception as exc:
            if previous_state is not None:
                try:
                    if store.get(ident)['state'] == 'PREPARING':
                        err = str(exc) if isinstance(exc, (AssessmentError, ValueError)) else previous_error
                        store.transition(ident, previous_state, expected=('PREPARING',), error=err)
                except Exception:
                    pass
            raise
        finally:
            store.close()

    async def send(self, ident, revision, *, automatic=False):
        store = AutomationStore(self.service.db_path)
        try:
            job = store.get(ident)
            package = store.package(ident)
            if job['package_revision']!=revision or job['state'] not in ('REVIEW','READY','BLOCKED'):
                raise ValueError('Пакет или состояние изменились. Откройте очередь заново.')
            settings = store.settings()
            if settings['browser_hold']:
                raise ValueError('Отправка ждёт проверки браузера. Откройте «Режим и расписание».')
            if settings['mode']=='paused' or (automatic and settings['mode']!='auto'):
                raise ValueError('Отправка остановлена текущим режимом.')
            evaluation = json.loads(job['evaluation_json'] or '{}')
            metadata = json.loads(package['metadata_json'])
            if metadata.get('direction') not in DIRECTIONS:
                message = 'Пакет создан для прежнего AI-направления. Подготовьте его заново, чтобы выбрать одно из четырёх резюме.'
                store.transition(ident,'STALE',expected=('REVIEW','READY','BLOCKED'),error=message)
                raise ValueError(message)
            letter = self.outgoing_letter(store,job,package)
            if len(letter)>LETTER_LIMIT:
                raise ValueError(f'Письмо с портфолио занимает {len(letter)} символов. Сократите текст до {LETTER_LIMIT} символов вместе со ссылками.')
            if automatic and (evaluation.get('score',0)<settings['threshold'] or metadata.get('questions') or metadata.get('gaps')):
                store.transition(ident,'REVIEW')
                raise ValueError('Пакет требует проверки по текущим критериям.')
            if not self.current(job):
                message = 'Пакет устарел: изменились фильтры или профиль. Подготовьте новый пакет.'
                store.transition(ident,'STALE',expected=('REVIEW','READY','BLOCKED'),error=message)
                raise ValueError(message)
            mark = store.db.execute('SELECT state FROM vacancy_tracking WHERE vacancy_id=?',(job['vacancy_id'],)).fetchone()
            if mark and mark[0] in ('skipped','applied'):
                raise ValueError('Вакансия пропущена или уже отмечена как отклик.')
            if not self.sender.available:
                store.transition(ident,'BLOCKED',error='Отправка на rabota.by не подключена. Пакет сохранён.')
                return 'Отправка на rabota.by не подключена. Пакет сохранён.'
            # Compare the vacancy requirements before opening the response form.
            ai = self.ai_factory(self.service.db_path,self.service.env)
            current_text = await asyncio.to_thread(ai.details,job['vacancy_id'])
            saved = store.storage.description(package['description_id'])
            if current_text.split()!=saved['text'].split():
                message = 'Текст описания вакансии изменился. Подготовьте новый пакет.'
                store.transition(ident,'STALE',expected=('REVIEW','READY','BLOCKED'),error=message)
                raise ValueError(message)
            # Recheck after the network wait, then atomically reserve the send.
            if not self.current(job) or store.settings()['mode']=='paused' or (automatic and store.settings()['mode']!='auto'):
                raise ValueError('Настройки изменились. Отправка остановлена.')
            with store.db:
                store.db.execute('BEGIN IMMEDIATE')
                latest = store.get(ident)
                if latest['package_revision']!=revision or latest['state'] not in ('REVIEW','READY','BLOCKED'):
                    raise ValueError('Пакет изменился.')
                mark = store.db.execute('SELECT state FROM vacancy_tracking WHERE vacancy_id=?',(job['vacancy_id'],)).fetchone()
                if mark and mark[0] in ('skipped','applied'):
                    raise ValueError('Вакансия исключена или отклик уже отмечен.')
                current_settings = store.settings()
                if current_settings['browser_hold']:
                    raise ValueError('Отправка ждёт проверки браузера.')
                if current_settings['mode']=='paused' or (automatic and current_settings['mode']!='auto'):
                    raise ValueError('Отправка приостановлена.')
                stamp = datetime.now(timezone.utc).isoformat()
                midnight = datetime.now(timezone.utc).astimezone(MINSK).replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).isoformat()
                count = store.db.execute("SELECT count(*) FROM application_events WHERE state='SENDING' AND created_at>=?",(midnight,)).fetchone()[0]
                if count>=current_settings['daily_limit']:
                    raise ValueError('Дневной лимит отправки исчерпан.')
                store.db.execute("UPDATE application_jobs SET state='SENDING',updated_at=? WHERE id=?",(stamp,ident))
                store.db.execute("INSERT INTO application_events(job_id,state,created_at) VALUES (?,'SENDING',?)",(ident,stamp))
            try:
                def before_submit():
                    from .browser_sender import SubmissionBlocked
                    check = AutomationStore(self.service.db_path)
                    try:
                        latest = check.get(ident)
                        modes = check.settings()
                        if latest['state']!='SENDING' or latest['package_revision']!=revision or not self.current(latest):
                            raise SubmissionBlocked('Задание изменилось. Отправка остановлена до нажатия кнопки.')
                        if modes['browser_hold'] or modes['mode']=='paused' or (automatic and modes['mode']!='auto'):
                            raise SubmissionBlocked('Режим изменился. Отправка остановлена до нажатия кнопки.')
                        mark = check.db.execute('SELECT state FROM vacancy_tracking WHERE vacancy_id=?',(latest['vacancy_id'],)).fetchone()
                        if mark and mark[0] in ('skipped','applied'):
                            raise SubmissionBlocked('Вакансия исключена или отклик отмечен вручную.')
                    finally:
                        check.close()
                outgoing = dict(package,letter_text=letter,vacancy_id=job['vacancy_id'],_before_submit=before_submit)
                if self.browser_series:
                    receipt = await self.browser_series.send_in_series(outgoing,settings['browser_idle_minutes'])
                else:
                    receipt = await asyncio.to_thread(self.sender.send,outgoing)
                if not receipt:
                    raise RuntimeError('No verified receipt')
            except Exception as exc:
                from .browser_sender import BrowserVerificationRequired, SubmissionBlocked
                if isinstance(exc,BrowserVerificationRequired):
                    store.configure('browser_hold',True)
                    state = 'UNKNOWN' if exc.outcome_unknown else 'BLOCKED'
                    message = str(exc)
                    if exc.outcome_unknown:
                        message += ' После нажатия результат неизвестен. Проверьте отклики на сайте. Повтор этой отправки заблокирован.'
                    store.transition(ident,state,expected=('SENDING',),error=message)
                    return message
                if isinstance(exc,SubmissionBlocked):
                    store.transition(ident,'BLOCKED',error=str(exc))
                    return str(exc)
                store.transition(ident,'UNKNOWN',error='Исход отправки неизвестен. Проверьте отклики на сайте. Автоповтора нет.')
                return 'Исход отправки неизвестен. Проверьте отклики на сайте; повтор заблокирован.'
            with store.db:
                store.db.execute('INSERT OR REPLACE INTO application_receipts VALUES (?,?,?)',(ident,revision,json.dumps(receipt,ensure_ascii=False)))
                store.db.execute("UPDATE application_jobs SET state='SENT',error=NULL WHERE id=?",(ident,))
                store.db.execute("INSERT INTO application_events(job_id,state,created_at) VALUES (?,'SENT',?)",(ident,datetime.now(timezone.utc).isoformat()))
            return 'Отклик подтверждён площадкой.'
        finally:
            store.close()
