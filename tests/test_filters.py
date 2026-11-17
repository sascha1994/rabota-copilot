import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram.types import Message

from app.config import SearchConfig
from app.filter_editor import edit_filter
from app.tg_bot import BotController
from test_bot import message


class FilterValidationTests(unittest.TestCase):
    def test_supported_values_and_normalization(self):
        config = SearchConfig()
        for field, text, expected in [
            ('text', '  Java backend ', 'Java backend'),
            ('areas', 'Минск', ('1002',)),
            ('areas', '   ', ()),
            ('areas', 'нет', ()),
            ('areas', '16,1002,16', ('16','1002')),
            ('experience', '1–3', 'between1And3'),
            ('experience', 'любой', None),
            ('salary', '2 500', 2500),
            ('salary', 'нет', None),
            ('currency', 'byn', 'BYR'),
            ('currency', 'rub', 'RUR'),
            ('excluded_title_words', 'senior, lead', ('senior','lead')),
            ('excluded_employer_ids', 'нет', ()),
            ('work_formats', 'удалённо, гибрид', ('REMOTE','HYBRID')),
            ('work_formats', 'любой', ()),
        ]:
            self.assertEqual(getattr(edit_filter(config,field,text),field), expected)

    def test_invalid_values(self):
        for field, text in [('text',' '), ('areas','-1'), ('areas','Минск, foo'),
                            ('salary','-500'), ('salary','1.5'), ('currency','XYZ'),
                            ('experience','expert'), ('excluded_employer_ids','abc'),
                            ('excluded_title_words','one,,two'), ('work_formats','по настроению')]:
            with self.assertRaises(ValueError, msg=field):
                edit_filter(SearchConfig(),field,text)

    def test_atomic_persistence_and_failed_replace(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'search.json'
            SearchConfig().save(path)
            with patch('app.config.os.replace', side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    SearchConfig(text='Java').save(path)
            self.assertEqual(SearchConfig.load(path).text,'Python')
            self.assertEqual(list(Path(folder).glob('*.tmp')),[])
            SearchConfig(text='Java',areas=('1002',)).save(path)
            self.assertEqual(SearchConfig.load(path).text,'Java')


class FilterBotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)/'search.json'
        SearchConfig().save(self.path)
        self.service = Mock(config_path=str(self.path), env={'VACANCY_SOURCE':'web'})
        self.controller = BotController(self.service,42)

    def tearDown(self):
        self.folder.cleanup()

    async def test_owner_can_save_and_reopen_filters(self):
        self.controller.pending_field = 'text'
        await self.controller.handle(message('Java backend'))
        self.assertEqual(SearchConfig.load(self.path).text,'Java backend')
        self.assertIsNone(self.controller.pending_field)
        reopened = BotController(self.service,42)
        msg=message('/settings')
        await reopened.handle(msg)
        self.assertIn('Java backend',msg.answer.call_args.args[0])
        self.assertIsNotNone(msg.answer.call_args.kwargs['reply_markup'])

    async def test_clear_region_button_persists_empty_selection(self):
        msg=Message(message_id=1,date=0,chat={'id':42,'type':'private'})
        callback=SimpleNamespace(message=msg,from_user=SimpleNamespace(id=42),data='filter:areas',answer=AsyncMock())
        with patch.object(Message,'answer',new=AsyncMock()):
            await self.controller.handle_callback(callback)
            callback.data='filter:clear_region'
            await self.controller.handle_callback(callback)
        self.assertEqual(SearchConfig.load(self.path).areas,())
        self.assertNotIn('area', SearchConfig.load(self.path).params())

    async def test_invalid_input_and_cancel_leave_file_unchanged(self):
        before=self.path.read_bytes()
        self.controller.pending_field='salary'
        await self.controller.handle(message('-500'))
        self.assertEqual(self.controller.pending_field,'salary')
        self.assertEqual(self.path.read_bytes(),before)
        await self.controller.handle(message('/cancel'))
        self.assertIsNone(self.controller.pending_field)
        self.assertEqual(self.path.read_bytes(),before)

    async def test_stranger_cannot_change_pending_filter(self):
        self.controller.pending_field='text'
        await self.controller.handle(message('malicious change',uid=99))
        self.assertEqual(SearchConfig.load(self.path).text,'Python')
        self.assertEqual(self.controller.pending_field,'text')

    async def test_callback_checks_owner_and_selects_field(self):
        msg=Message(message_id=1,date=0,chat={'id':42,'type':'private'})
        callback=SimpleNamespace(message=msg,from_user=SimpleNamespace(id=99),data='filter:text',answer=AsyncMock())
        with patch.object(Message,'answer',new=AsyncMock()) as answer:
            await self.controller.handle_callback(callback)
            self.assertIsNone(self.controller.pending_field)
            answer.assert_not_called()
            callback.from_user.id=42
            await self.controller.handle_callback(callback)
            self.assertEqual(self.controller.pending_field,'text')
            self.assertIn('Введите запрос',answer.call_args.args[0])

    async def test_search_captures_config_before_later_edits(self):
        async def fake_search(msg,config):
            return config
        with patch.object(self.controller,'search',side_effect=fake_search) as search:
            await self.controller.handle(message('/search'))
            SearchConfig(text='Java').save(self.path)
            result=await self.controller.search_task
            self.assertEqual(result.text,'Python')
            self.assertEqual(search.call_args.args[1].text,'Python')

    async def test_work_format_checkbox_draft_and_apply(self):
        msg=Message(message_id=1,date=0,chat={'id':42,'type':'private'})
        callback=SimpleNamespace(message=msg,from_user=SimpleNamespace(id=42),data='filter:work_formats',answer=AsyncMock())
        with patch.object(Message,'answer',new=AsyncMock()), patch.object(Message,'edit_reply_markup',new=AsyncMock()):
            await self.controller.handle_callback(callback)
            for code in ('REMOTE','HYBRID'):
                callback.data='format:toggle:'+code
                await self.controller.handle_callback(callback)
            self.assertEqual(SearchConfig.load(self.path).work_formats,())
            callback.data='format:apply'
            await self.controller.handle_callback(callback)
            self.assertEqual(SearchConfig.load(self.path).work_formats,('REMOTE','HYBRID'))
            self.assertIsNone(self.controller.pending_field)

    async def test_stale_and_unauthorized_format_callbacks_do_not_save(self):
        msg=Message(message_id=1,date=0,chat={'id':42,'type':'private'})
        callback=SimpleNamespace(message=msg,from_user=SimpleNamespace(id=42),data='format:apply',answer=AsyncMock())
        self.controller.work_format_draft=('REMOTE',)
        await self.controller.handle_callback(callback)
        self.assertEqual(SearchConfig.load(self.path).work_formats,())
        self.controller.pending_field='work_formats'
        callback.from_user.id=99
        await self.controller.handle_callback(callback)
        self.assertEqual(SearchConfig.load(self.path).work_formats,())
