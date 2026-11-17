import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram.types import Message
from app.collector import search_key
from app.config import SearchConfig
from app.storage import Storage
from app.tg_bot import BotController
from test_bot import message
from test_search import vacancy


class ClearStorageTests(unittest.TestCase):
    def test_scoped_clear_preserves_shared_rows_and_resets_cursors(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder)/'db.sqlite3')
            try:
                storage.save(vacancy('1'),'first',None,'2026-11-14')
                storage.save(vacancy('2'),'first',None,'2026-11-14')
                storage.save(vacancy('1'),'second',None,'2026-11-14')
                for key in ('first','web:first','second'):
                    storage.advance(key,'2026-11-14')
                self.assertEqual(storage.clear_results('first'),2)
                self.assertEqual(storage.results('first'),[])
                self.assertEqual(len(storage.results('second')),1)
                self.assertIsNone(storage.cursor('first'))
                self.assertIsNone(storage.cursor('web:first'))
                self.assertEqual(storage.cursor('second'),'2026-11-14')
                self.assertEqual(storage.clear_results('first'),0)
                self.assertTrue(storage.save(vacancy('2'),'first',None,'2026-11-14'))
            finally:
                storage.close()


class ClearBotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.config = SearchConfig()
        self.path = Path(self.folder.name)/'search.json'
        self.config.save(self.path)
        self.service = Mock(config_path=str(self.path))
        self.service.read.return_value=(self.config,[],3,None)
        self.service.clear.return_value=3
        self.controller=BotController(self.service,42)

    def tearDown(self):
        self.folder.cleanup()

    def callback(self, action='yes', uid=42):
        token=self.controller.clear_confirmation[0]
        return SimpleNamespace(data=f'clear:{token}:{action}',from_user=SimpleNamespace(id=uid),
            message=Message(message_id=1,date=0,chat={'id':42,'type':'private'}),answer=AsyncMock())

    async def test_confirmation_required_and_cannot_be_reused(self):
        await self.controller.handle(message('Очистить вакансии'))
        self.service.clear.assert_not_called()
        callback=self.callback()
        with patch.object(Message,'answer',new=AsyncMock()):
            await self.controller.handle_callback(callback)
            await self.controller.handle_callback(callback)
        self.service.clear.assert_called_once_with(search_key(self.config))

    async def test_cancel_and_changed_filters_do_not_delete(self):
        await self.controller.ask_clear(message('/clear'))
        await self.controller.handle_callback(self.callback('no'))
        self.service.clear.assert_not_called()
        await self.controller.ask_clear(message('/clear'))
        SearchConfig(text='Java').save(self.path)
        await self.controller.handle_callback(self.callback())
        self.service.clear.assert_not_called()

    async def test_stranger_cannot_confirm(self):
        await self.controller.ask_clear(message('/clear'))
        await self.controller.handle_callback(self.callback(uid=99))
        self.service.clear.assert_not_called()

    async def test_search_in_progress_blocks_confirmation(self):
        await self.controller.ask_clear(message('/clear'))
        task=asyncio.create_task(asyncio.sleep(10))
        self.controller.search_task=task
        try:
            await self.controller.handle_callback(self.callback())
            self.service.clear.assert_not_called()
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
