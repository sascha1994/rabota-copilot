import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram.types import Message

from app.storage import Storage


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Storage(Path(self.folder.name)/'db.sqlite3')
        self.store.save({'id':'1','name':'Дизайнер'},'search',None,'2026-11-15')

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def test_marks_survive_clear_and_search_refresh(self):
        self.store.mark('1','applied')
        self.store.mark('1','applied')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM vacancy_tracking_events').fetchone()[0],1)
        self.store.clear_results('search')
        self.assertEqual(self.store.tracked()[0]['_tracking_state'],'applied')
        self.store.save({'id':'1','name':'Новое название'},'search',None,'2026-11-16')
        self.assertEqual(self.store.results('search')[0]['_tracking_state'],'applied')
        self.assertEqual(self.store.tracked()[0]['name'],'Новое название')
        self.store.mark('1','new')
        self.assertEqual(self.store.tracked(),[])

    def test_description_changes_and_reversion_are_versioned(self):
        a = self.store.save_description('1','Первоначальный текст')
        self.assertEqual(a,self.store.save_description('1','Первоначальный текст'))
        b = self.store.save_description('1','Изменённый текст')
        c = self.store.save_description('1','Первоначальный текст')
        self.assertTrue(a<b<c)
        self.assertEqual(len(self.store.descriptions('1')),3)
        self.store.clear_results('search')
        self.assertEqual(self.store.description(a)['text'],'Первоначальный текст')
        self.assertEqual(self.store.description(b)['text'],'Изменённый текст')

    def test_invalid_mark_and_unknown_vacancy_do_not_create_rows(self):
        for vid,state in [('1','invalid'),('9','interested')]:
            with self.assertRaises(ValueError):
                self.store.mark(vid,state)
        self.assertEqual(self.store.tracked(),[])


class TrackingAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_foreign_user_cannot_mark_or_export_description(self):
        from app.tg_bot import BotController
        controller = BotController(Mock(),42)
        callback = SimpleNamespace(message=Message(message_id=1,date=0,chat={'id':99,'type':'private'}),
            from_user=SimpleNamespace(id=99),data='mark:applied:1',answer=AsyncMock())
        with patch.object(controller.vacancy_console,'callback',new=AsyncMock()) as action:
            for data in ('mark:applied:1','history:1','description:1'):
                callback.data=data
                await controller.handle_callback(callback)
            action.assert_not_called()
