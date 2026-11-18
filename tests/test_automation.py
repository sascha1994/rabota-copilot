import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.letters import GeneratedLetter
from app.assessment import Evaluation, LocalQuotaExceeded
from app.automation import AutomationEngine, AutomationStore
from app.candidate_profile import ProfileStore
from app.collector import search_key
from app.config import SearchConfig
from app.resumes import snapshot
from app.tg_automation import AutomationConsole


class AutomationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'db.sqlite3'
        self.config_path = Path(self.temp.name)/'search.json'
        self.config = SearchConfig()
        self.config.save(self.config_path)
        self.store = AutomationStore(self.path)
        profile = ProfileStore(self.path)
        profile.import_draft({'facts':[{'id':'skill','category':'Навыки','value':'Figma',
            'sources':[{'document':'cv','page':1,'quote':'Figma'}]}]})
        profile.update('skill',1,'confirm',42)
        profile.close()
        self.raw = {'id':'1','name':'Дизайнер','alternate_url':'https://rabota.by/vacancy/1'}
        self.store.storage.save(self.raw,search_key(self.config),None,datetime.now(timezone.utc).isoformat())
        self.desc = self.store.storage.save_description('1','Нужен дизайнер Figma')
        self.store.enqueue(self.raw,search_key(self.config),snapshot(self.path)[1])
        self.service = SimpleNamespace(db_path=self.path,config_path=self.config_path,env={})
        self.score = 90
        self.questions = []
        owner = self
        class FakeAI:
            description_id = owner.desc
            details = staticmethod(lambda vid:'Нужен дизайнер Figma')
            def evaluate(self, vid, **kwargs):
                if kwargs.get('application_direction'):
                    return GeneratedLetter(text='Здравствуйте! Работаю в Figma.',fact_ids=['skill'],questions=[]),False
                return Evaluation(score=owner.score,resume='graphic',summary='Есть соответствие',
                    matches=[],gaps=[],questions=owner.questions),False
        self.engine = AutomationEngine(self.service,ai_factory=lambda *a:FakeAI())
        self.notify = AsyncMock()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_auto_prepares_and_blocks_unconfigured_submission(self):
        ident = await self.engine.prepare_one(self.notify)
        self.assertEqual(ident,1)
        self.assertEqual(self.store.get(1)['state'],'BLOCKED')
        self.assertIn('не подключена',self.store.get(1)['error'])
        self.assertEqual(self.store.package(1)['resume_text'],'')
        self.assertIn('Figma',self.store.package(1)['letter_text'])

    async def test_local_quota_waits_without_draining_queue_and_resumes(self):
        self.store.configure('mode','semi')
        original_factory = self.engine.ai_factory
        failing_ai = Mock()
        failing_ai.evaluate.side_effect = LocalQuotaExceeded()
        self.engine.ai_factory = Mock(return_value=failing_ai)
        await self.engine.prepare_one(self.notify)
        self.assertEqual(self.store.get(1)['state'],'WAITING_AI')
        await self.engine.prepare_one(self.notify)
        failing_ai.evaluate.assert_called_once()
        msg = SimpleNamespace(answer=AsyncMock())
        await AutomationConsole(self.path,self.engine).show(msg,1)
        self.assertIn('Продолжение автоматически',msg.answer.call_args_list[0].args[0])
        with self.store.db:
            self.store.db.execute("UPDATE automation_ai_pause SET retry_at='2000-01-01T00:00:00+00:00'")
        self.engine.ai_factory = original_factory
        await self.engine.prepare_one(self.notify)
        self.assertEqual(self.store.get(1)['state'],'REVIEW')

    async def test_semiauto_and_questions_require_review(self):
        self.store.configure('mode','semi')
        await self.engine.prepare_one(self.notify)
        self.assertEqual(self.store.get(1)['state'],'REVIEW')
        self.store.configure('mode','auto')
        self.assertEqual(self.store.get(1)['state'],'REVIEW')

    async def test_unknown_requirement_in_auto_is_not_sent(self):
        self.questions = ['Подтвердите обязательный опыт.']
        await self.engine.prepare_one(self.notify)
        self.assertEqual(self.store.get(1)['state'],'REVIEW')

    async def test_below_threshold_rejected_without_document_generation(self):
        self.score = 50
        await self.engine.prepare_one(self.notify)
        self.assertEqual(self.store.get(1)['state'],'UNSUITABLE')
        self.assertIsNone(self.store.get(1)['package_revision'])

    async def test_edit_is_immutable_and_invalidates_old_send_button(self):
        self.store.configure('mode','semi')
        await self.engine.prepare_one(self.notify)
        first = self.store.package(1)
        self.store.save_package(1,'','Исправленное письмо',self.desc,{},expected_revision=1)
        self.assertEqual(self.store.package(1,1)['letter_text'],first['letter_text'])
        with self.assertRaisesRegex(ValueError,'изменились'):
            await self.engine.send(1,1)

    async def test_confirmed_send_cannot_repeat(self):
        self.store.configure('mode','semi')
        await self.engine.prepare_one(self.notify)
        sender = SimpleNamespace(available=True,send=Mock(return_value='receipt-123'))
        self.engine.sender = sender
        await self.engine.send(1,1)
        self.assertEqual(self.store.get(1)['state'],'SENT')
        with self.assertRaises(ValueError):
            await self.engine.send(1,1)
        sender.send.assert_called_once()

    async def test_portfolio_in_preview_matches_outgoing_letter_without_resume_draft(self):
        self.store.configure('mode','semi')
        self.service.env.update(PORTFOLIO_GRAPHIC='https://example.com/graphic',
                                PORTFOLIO_AI='https://example.com/ai')
        await self.engine.prepare_one(self.notify)
        first = self.store.package(1)
        self.store.save_package(1,'Старый черновик',first['letter_text'],self.desc,
                                {'direction':'graphic'},expected_revision=1)
        msg = SimpleNamespace(answer=AsyncMock())
        await AutomationConsole(self.path,self.engine).show(msg,1)
        messages = [call.args[0] for call in msg.answer.call_args_list]
        preview = next(t for t in messages if 'Сопроводительное письмо' in t)
        self.assertFalse(any('Старый черновик' in t for t in messages))
        sender = SimpleNamespace(available=True,send=Mock(return_value={'revision':2}))
        self.engine.sender = sender
        await self.engine.send(1,2)
        letter = sender.send.call_args.args[0]['letter_text']
        self.assertIn(letter,preview)
        self.assertIn('https://example.com/graphic',letter)
        self.assertNotIn('https://example.com/ai',letter)

    async def test_ambiguous_send_never_retries(self):
        self.store.configure('mode','semi')
        await self.engine.prepare_one(self.notify)
        sender = SimpleNamespace(available=True,send=Mock(side_effect=TimeoutError))
        self.engine.sender = sender
        await self.engine.send(1,1)
        self.assertEqual(self.store.get(1)['state'],'UNKNOWN')
        with self.assertRaises(ValueError):
            await self.engine.send(1,1)
        sender.send.assert_called_once()

    async def test_browser_verification_after_click_pauses_sending_and_keeps_unknown(self):
        from app.browser_sender import BrowserVerificationRequired
        self.store.configure('mode','semi')
        await self.engine.prepare_one(self.notify)
        sender = SimpleNamespace(available=True,send=Mock(side_effect=BrowserVerificationRequired(outcome_unknown=True)))
        self.engine.sender = sender
        await self.engine.send(1,1)
        self.assertTrue(self.store.settings()['browser_hold'])
        self.assertEqual(self.store.get(1)['state'],'UNKNOWN')
        msg = SimpleNamespace(answer=AsyncMock())
        console = AutomationConsole(self.path,self.engine)
        await console.settings(msg)
        markup = msg.answer.call_args.kwargs['reply_markup']
        self.assertIn('flow:browserresume:0',[b.callback_data for row in markup.inline_keyboard for b in row])
        await console.callback(SimpleNamespace(data='flow:browserresume:0',answer=AsyncMock(),message=msg))
        self.assertFalse(self.store.settings()['browser_hold'])
        with self.assertRaises(ValueError):
            await self.engine.send(1,1)
        sender.send.assert_called_once()

    async def test_pause_and_profile_change_stop_submission(self):
        self.store.configure('mode','semi')
        await self.engine.prepare_one(self.notify)
        self.store.configure('mode','paused')
        with self.assertRaisesRegex(ValueError,'остановлена'):
            await self.engine.send(1,1)
        self.store.configure('mode','semi')
        profile = ProfileStore(self.path)
        profile.update('skill',2,'reject',42)
        profile.close()
        with self.assertRaisesRegex(ValueError,'устарел'):
            await self.engine.send(1,1)
        self.assertEqual(self.store.get(1)['state'],'STALE')

    async def test_daily_search_runs_once_and_deduplicates(self):
        self.store.configure('time','00:00')
        from app.tg_bot import SearchService

        service = SearchService(self.config_path, self.path, '')
        self.engine.service = service
        config = SearchConfig(excluded_title_words=('продажи',))
        config.save(self.config_path)
        skipped = {'id': '2', 'name': 'Дизайнер'}
        self.store.storage.save(skipped, search_key(config), None, datetime.now(timezone.utc).isoformat())
        self.store.storage.mark('2', 'skipped')
        self.store.transition(1, 'REJECTED')
        items = [self.raw, skipped, {'id': '3', 'name': 'Дизайнер'}, {'id': '4', 'name': 'Продажи'}]
        with patch('app.web_search.WebSearchClient.search', return_value={'items': items, 'has_next': False}) as fetch:
            self.assertEqual(service.collect(config).queued, 1)
            service.last_web_search = None
            items.append({'id': '5', 'name': 'Дизайнер'})
            await self.engine.daily(self.notify)
            await self.engine.daily(self.notify)
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual({j['vacancy_id']: j['state'] for j in self.store.jobs()},
                         {'1': 'REJECTED', '3': 'QUEUED', '5': 'QUEUED'})
        self.assertIn('Добавлено в очередь: 1', self.notify.await_args.args[0])

    async def test_settings_buttons_and_input_persist(self):
        console = AutomationConsole(self.path,self.engine)
        message = SimpleNamespace(text='10:30',answer=AsyncMock())
        console.pending = ('setting','time')
        await console.save_text(message)
        self.assertEqual(self.store.settings()['time'],'10:30')
        self.store.configure('daily_limit',200)
        self.assertEqual(self.store.settings()['daily_limit'],200)
        for field,value in [('time','25:60'),('threshold',101),('daily_limit',0),('daily_limit',501)]:
            with self.assertRaises(ValueError):
                self.store.configure(field,value)

    async def test_api_budget_edit_is_shared_and_resumes_waiting_jobs(self):
        from app.ai_limits import effective_limits
        console=AutomationConsole(self.path,self.engine)
        self.store.defer_ai(1,LocalQuotaExceeded().retry_at)
        console.pending=('setting','ai_daily_calls')
        await console.save_text(SimpleNamespace(text='250',answer=AsyncMock()))
        self.assertEqual(effective_limits(self.path,{'AI_DAILY_CALL_LIMIT':'20'}),(250,200000))
        self.assertEqual(self.store.get(1)['state'],'QUEUED')
        for field,value in [('ai_daily_calls',0),('ai_daily_calls',1001),('ai_daily_tokens',9999),('insights_time','24:30')]:
            with self.assertRaises(ValueError): self.store.configure(field,value)

    async def test_waiting_card_explains_daily_limit_and_has_refresh(self):
        self.store.configure('daily_limit',1)
        self.store.transition(1,'PREPARING')
        self.store.enqueue({'id':'2','name':'Вторая вакансия'},search_key(self.config),snapshot(self.path)[1])
        console = AutomationConsole(self.path,self.engine)
        message = SimpleNamespace(answer=AsyncMock())
        await console.show(message,2)
        text = message.answer.call_args_list[0].args[0]
        self.assertIn('Дневной лимит исчерпан',text)
        self.assertIn('Позиция в очереди: 1 из 1',text)
        self.assertIn('Обработано сегодня: 1/1',text)
        markup = message.answer.call_args_list[-1].kwargs['reply_markup']
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn('flow:view:2',callbacks)
        self.assertFalse(any(data.startswith('flow:send:') for data in callbacks))

    async def test_queue_pagination_reaches_first_job(self):
        for ident in range(2,44):
            self.store.enqueue({'id':str(ident),'name':'Vacancy'},search_key(self.config),'profile')
        console = AutomationConsole(self.path,self.engine)
        msg = SimpleNamespace(answer=AsyncMock())
        await console.queue(msg,category='all')
        markup = msg.answer.call_args.kwargs['reply_markup']
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn('flow:view:43',data)
        self.assertNotIn('flow:view:1',data)
        self.assertIn('flow:category:all:1',data)
        self.assertIn('flow:category:all:2',data)
        callback = SimpleNamespace(data='flow:category:all:2',message=msg,answer=AsyncMock())
        await console.callback(callback)
        markup = msg.answer.call_args.kwargs['reply_markup']
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn('flow:view:1',data)
        self.assertIn('flow:category:all:1',data)
        self.assertNotIn('flow:category:all:3',data)
        self.assertIn('страница 3 из 3',msg.answer.call_args.args[0])
        await console.queue(msg,999,'all')
        self.assertIn('страница 3 из 3',msg.answer.call_args.args[0])

    async def test_categories_cover_every_state_and_filter_jobs(self):
        from app.tg_automation import CATEGORIES,STATES
        states=[state for _,group in CATEGORIES.values() if group for state in group]
        self.assertEqual(set(states),set(STATES))
        self.assertEqual(len(states),len(set(states)))
        for ident,state in enumerate(STATES,2):
            self.store.enqueue({'id':str(ident),'name':state},'test','profile')
            self.store.transition(ident,state)
        console=AutomationConsole(self.path,self.engine)
        msg=SimpleNamespace(answer=AsyncMock())
        await console.queue(msg)
        menu=msg.answer.call_args.kwargs['reply_markup']
        self.assertEqual(len(menu.inline_keyboard),len(CATEGORIES))
        sent=next(j['id'] for j in self.store.jobs(('SENT',)))
        callback=SimpleNamespace(data='flow:category:sent:0',message=msg,answer=AsyncMock())
        await console.callback(callback)
        markup=msg.answer.call_args.kwargs['reply_markup']
        data=[b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual([d for d in data if d.startswith('flow:view:')],[f'flow:view:{sent}'])
        self.assertIn('flow:categories:0',data)
        await console.queue(msg,category='waiting')
        data=[b.callback_data for row in msg.answer.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn('flow:view:1',data)
        self.assertNotIn(f'flow:view:{sent}',data)

    async def test_empty_category_and_legacy_page_button(self):
        console=AutomationConsole(self.path,self.engine)
        msg=SimpleNamespace(answer=AsyncMock())
        await console.queue(msg,category='sent')
        self.assertIn('заданий пока нет',msg.answer.call_args.args[0])
        await console.callback(SimpleNamespace(data='flow:list:0',message=msg,answer=AsyncMock()))
        self.assertIn('Все задания',msg.answer.call_args.args[0])

    async def test_attention_cards_can_be_removed_without_losing_unknown_outcome(self):
        console=AutomationConsole(self.path,self.engine)
        msg=SimpleNamespace(answer=AsyncMock())
        for ident,state in enumerate(('FAILED','STALE','BLOCKED','UNKNOWN'),1):
            with self.subTest(state=state):
                if ident!=1:
                    self.store.enqueue({'id':str(ident),'name':state},'test','profile')
                self.store.transition(ident,state,error='Проверьте статус')
                await console.show(msg,ident)
                markup=msg.answer.call_args.kwargs['reply_markup']
                action='dismiss' if state=='UNKNOWN' else 'reject'
                data=f'flow:{action}:{ident}:0'
                self.assertIn(data,[button.callback_data for row in markup.inline_keyboard for button in row])
                await console.callback(SimpleNamespace(data=data,message=msg,answer=AsyncMock()))
                self.assertIn('Требуют внимания',msg.answer.call_args.args[0])
                updated=self.store.get(ident)
                self.assertEqual(updated['state'],'UNKNOWN_DISMISSED' if state=='UNKNOWN' else 'REJECTED')
                if state=='UNKNOWN':
                    self.assertEqual(updated['error'],'Проверьте статус')
        await console.queue(msg,category='closed')
        markup=msg.answer.call_args.kwargs['reply_markup']
        self.assertIn('flow:view:4',[button.callback_data for row in markup.inline_keyboard for button in row])

    async def test_queue_budget_resets_at_minsk_midnight(self):
        self.store.transition(1,'PREPARING')
        with self.store.db:
            self.store.db.execute("UPDATE application_events SET created_at='2026-11-15T20:59:00+00:00'")
        before = self.store.queue_status(now=datetime(2026,11,15,20,59,tzinfo=timezone.utc))
        after = self.store.queue_status(now=datetime(2026,11,15,21,0,tzinfo=timezone.utc))
        self.assertEqual(before['prepared'],1)
        self.assertEqual(after['prepared'],0)

    async def test_daily_counts_deduplicate_and_use_minsk_midnight(self):
        yesterday = '2026-11-15T20:59:00+00:00'
        today = '2026-11-15T21:00:00+00:00'
        now = datetime.fromisoformat(today)
        key = search_key(self.config)
        with self.store.db:
            self.store.db.execute('UPDATE vacancies SET first_seen=?, last_seen=?', (yesterday, yesterday))
        self.store.storage.save(self.raw, key, None, today)
        self.store.storage.save({'id': '2'}, key, None, today)
        self.store.storage.save({'id': '2'}, 'another-filter', None, today)
        self.store.storage.save({'id': '3'}, key, 'filtered', today)

        self.store.transition(1, 'PREPARING')
        with self.store.db:
            self.store.db.execute('UPDATE application_events SET created_at=?', (today,))
        self.assertEqual(self.store.daily_vacancy_counts(now), (1, 0))

        self.store.transition(1, 'UNSUITABLE')
        self.store.transition(1, 'PREPARING')
        self.store.transition(1, 'UNSUITABLE')
        with self.store.db:
            self.store.db.execute('UPDATE application_events SET created_at=?', (today,))
        self.assertEqual(self.store.daily_vacancy_counts(now), (1, 1))
        self.assertEqual(self.store.daily_vacancy_counts(datetime.fromisoformat(yesterday)), (1, 0))

    async def test_sent_card_has_confirmation_and_no_send_button(self):
        self.store.configure('mode','semi')
        await self.engine.prepare_one(self.notify)
        self.engine.sender = SimpleNamespace(available=True,send=Mock(return_value={
            'revision':1,'confirmed_at':'2026-11-15T20:00:00+00:00'}))
        await self.engine.send(1,1)
        msg = SimpleNamespace(answer=AsyncMock())
        await AutomationConsole(self.path,self.engine).show(msg,1)
        self.assertIn('Доставка подтверждена',msg.answer.call_args_list[0].args[0])
        markup = msg.answer.call_args_list[-1].kwargs['reply_markup']
        self.assertFalse(any(b.callback_data.startswith('flow:send:') for row in markup.inline_keyboard for b in row))

    async def test_recovery_and_clear_preserve_work(self):
        self.store.transition(1,'SENDING')
        self.store.recover()
        self.assertEqual(self.store.get(1)['state'],'UNKNOWN')
        self.store.storage.clear_results(search_key(self.config))
        self.assertIsNotNone(self.store.db.execute('SELECT id FROM vacancies WHERE id=?',('1',)).fetchone())
