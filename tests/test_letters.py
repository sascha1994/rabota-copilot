import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram.types import Message

from app.assessment import AssessmentError, AssessmentService
from app.letters import LetterStore
import test_resumes


class LetterTests(unittest.TestCase):
    setUp = test_resumes.ResumeTests.setUp
    tearDown = test_resumes.ResumeTests.tearDown

    def resume(self):
        item = self.store.create('graphic', 'Навыки: Figma', vacancy_id='1')
        return self.store.approve(item['id'], 42)

    def test_letter_requires_approved_vacancy_resume(self):
        item = self.store.base('graphic')
        letters = LetterStore(self.path)
        try:
            with self.assertRaisesRegex(ValueError, 'утвердите'):
                letters.create(item['id'], 'Письмо')
            self.store.approve(item['id'], 42)
            with self.assertRaisesRegex(ValueError, 'под вакансию'):
                letters.create(item['id'], 'Письмо')
        finally:
            letters.close()

    def test_edits_preserve_approved_letter_and_block_old_buttons(self):
        resume = self.resume()
        letters = LetterStore(self.path)
        try:
            first = letters.create(resume['id'], 'Первое письмо')
            letters.approve(first['id'], 42)
            second = letters.create(resume['id'], 'Правка', parent_id=first['id'])
            self.assertIsNone(second['approved_at'])
            self.assertEqual(letters.get(first['id'])['text'], 'Первое письмо')
            self.assertIsNotNone(letters.get(first['id'])['approved_at'])
            with self.assertRaisesRegex(ValueError, 'новая версия письма'):
                letters.approve(first['id'], 42)
            self.store.edit(resume['id'], 'Новое резюме')
            with self.assertRaisesRegex(ValueError, 'новая версия резюме'):
                letters.approve(second['id'], 42)
        finally:
            letters.close()

    def test_profile_change_blocks_letter_creation(self):
        resume = self.resume()
        self.store.profile.update('figma', 2, 'reject', 42)
        letters = LetterStore(self.path)
        try:
            with self.assertRaisesRegex(ValueError, 'Профиль изменился'):
                letters.create(resume['id'], 'Письмо')
        finally:
            letters.close()

    def test_letter_schema_cache_quota_and_wrong_vacancy(self):
        resume = self.resume()
        self.store.profile.storage.save({'id':'1','name':'Дизайнер'},'test',None,'2026-11-15')
        calls = []
        def opener(request, **kwargs):
            calls.append(request)
            body = {'text':'Здравствуйте! Работаю в Figma.', 'fact_ids':['figma'], 'questions':[]}
            return io.BytesIO(json.dumps({'status':'completed','usage':{'total_tokens':100},
                'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(body)}]}]}).encode())
        service = AssessmentService(self.path, {'OPENAI_API_KEY':'test','AI_DAILY_CALL_LIMIT':'1'},
                                    opener=opener, details=lambda vid:'Нужен дизайнер Figma')
        result, cached = service.evaluate('1', letter_resume_id=resume['id'])
        self.assertEqual(result.fact_ids, ['figma'])
        self.assertFalse(cached)
        self.assertTrue(service.evaluate('1', letter_resume_id=resume['id'])[1])
        with self.assertRaisesRegex(AssessmentError, 'другой вакансии'):
            service.evaluate('2', letter_resume_id=resume['id'])
        with self.assertRaisesRegex(AssessmentError, 'лимит'):
            service.evaluate('1')
        self.assertEqual(len(calls), 1)


class LetterAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_stranger_cannot_generate_letter(self):
        from app.tg_bot import BotController
        controller = BotController(Mock(), 42)
        callback = SimpleNamespace(message=Message(message_id=1,date=0,chat={'id':99,'type':'private'}),
            from_user=SimpleNamespace(id=99), data='letter:generate:1', answer=AsyncMock())
        with patch.object(controller.resumes, 'generate_letter', new=AsyncMock()) as generate:
            await controller.handle_callback(callback)
            generate.assert_not_called()
