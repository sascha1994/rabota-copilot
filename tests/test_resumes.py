import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram.types import Message

from app.assessment import AssessmentError, AssessmentService
from app.resumes import ResumeStore
from app.storage import Storage


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'resume.sqlite3'
        self.store = ResumeStore(self.path)
        self.store.profile.import_draft({'facts': [
            {'id':'figma', 'category':'Навыки', 'value':'Figma',
             'sources':[{'document':'cv','page':1,'quote':'Figma'}]}]})
        self.store.profile.update('figma', 1, 'confirm', 42)

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def test_edit_preserves_approved_version_and_requires_new_approval(self):
        first = self.store.base('web_ui')
        self.store.approve(first['id'], 42)
        second = self.store.edit(first['id'], 'Новая формулировка')
        self.assertIsNone(second['approved_at'])
        self.assertEqual(second['parent_id'], first['id'])
        self.assertEqual(self.store.get(first['id'])['text'], first['text'])
        self.assertIsNotNone(self.store.get(first['id'])['approved_at'])
        with self.assertRaisesRegex(ValueError, 'новая версия'):
            self.store.approve(first['id'], 42)

    def test_profile_change_blocks_approval_and_edit(self):
        item = self.store.base('graphic')
        self.store.profile.update('figma', 2, 'edit', 42, 'Figma и прототипирование')
        with self.assertRaisesRegex(ValueError, 'Профиль изменился'):
            self.store.approve(item['id'], 42)
        with self.assertRaises(ValueError):
            self.store.edit(item['id'], 'Исправленный текст')

    def test_generation_cache_and_fact_validation_share_quota(self):
        storage = Storage(self.path)
        storage.save({'id':'1','name':'Дизайнер'}, 'test', None, '2026-11-15')
        storage.close()
        calls = []
        def opener(request, **kwargs):
            calls.append(request)
            text = {'blocks':[{'heading':'Навыки', 'text':'Figma', 'fact_ids':['figma']}], 'questions':[]}
            return io.BytesIO(json.dumps({'status':'completed','usage':{'total_tokens':100},
                'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(text)}]}]}).encode())
        service = AssessmentService(self.path, {'OPENAI_API_KEY':'test', 'AI_DAILY_CALL_LIMIT':'1'},
                                    opener=opener, details=lambda vid:'Работа в Figma')
        result, cached = service.evaluate('1', draft_direction='web_ui')
        self.assertFalse(cached)
        self.assertEqual(result.blocks[0].fact_ids, ['figma'])
        self.assertTrue(service.evaluate('1', draft_direction='web_ui')[1])
        with self.assertRaisesRegex(AssessmentError, 'лимит'):
            service.evaluate('1')
        self.assertEqual(len(calls), 1)

    def test_unconfirmed_facts_are_not_used(self):
        self.store.profile.update('figma', 2, 'reject', 42)
        with self.assertRaisesRegex(ValueError, 'подтвердите'):
            self.store.base('ai')

    def test_unknown_fact_never_becomes_a_valid_generated_resume(self):
        storage = Storage(self.path)
        storage.save({'id':'1','name':'Дизайнер'}, 'test', None, '2026-11-15')
        storage.close()
        response = {'status':'completed','output':[{'type':'message','content':[
            {'type':'output_text','text':json.dumps({'blocks':[
                {'heading':'Опыт','text':'Несуществующий опыт','fact_ids':['invented']}], 'questions':[]})}]}]}
        service = AssessmentService(self.path, {'OPENAI_API_KEY':'test'},
            opener=lambda *a, **kw:io.BytesIO(json.dumps(response).encode()), details=lambda vid:'Описание')
        with self.assertRaisesRegex(AssessmentError, 'проверку'):
            service.evaluate('1', draft_direction='ai')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM assessments').fetchone()[0], 0)


class ResumeAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_foreign_user_cannot_generate_or_open_resumes(self):
        from app.tg_bot import BotController
        controller = BotController(Mock(), 42)
        callback = SimpleNamespace(message=Message(message_id=1,date=0,chat={'id':99,'type':'private'}),
            from_user=SimpleNamespace(id=99), data='draft:ai:1', answer=AsyncMock())
        with patch.object(controller.resumes, 'generate', new=AsyncMock()) as generate:
            await controller.handle_callback(callback)
            generate.assert_not_called()
        callback.data = 'resume:open:ai'
        with patch.object(controller.resumes, 'callback', new=AsyncMock()) as action:
            await controller.handle_callback(callback)
            action.assert_not_called()
