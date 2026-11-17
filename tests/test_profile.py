import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram.types import Message

from app.candidate_profile import ProfileStore, StaleFact
from app.tg_bot import BotController
from test_bot import message


def draft():
    return {'facts':[
        {'id':'figma','category':'Навыки','value':'Figma','sources':[{'document':'cv.pdf','page':1,'quote':'Figma'}]},
        {'id':'city','category':'Условия','value':'Город A или B','question':'Какой город актуален?',
         'sources':[{'document':'cv.pdf','page':1,'quote':'Город A'}]},
    ]}


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.path=Path(self.folder.name)/'db.sqlite3'
        self.store=ProfileStore(self.path)

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def test_import_has_no_confirmed_facts_and_is_idempotent(self):
        self.assertEqual(self.store.import_draft(draft()),2)
        self.assertEqual(self.store.confirmed(),[])
        self.store.update('figma',1,'confirm',42)
        self.assertEqual(self.store.import_draft(draft()),0)
        self.assertEqual(len(self.store.confirmed()),1)
        self.assertEqual(self.store.get('figma')['revision'],2)

    def test_conflict_requires_edit_then_separate_confirmation(self):
        self.store.import_draft(draft())
        with self.assertRaises(ValueError):
            self.store.update('city',1,'confirm',42)
        self.store.update('city',1,'edit',42,'Город B')
        self.assertEqual(self.store.get('city')['status'],'UNCONFIRMED')
        self.assertEqual(self.store.get('city')['question'],'')
        self.store.update('city',2,'confirm',42)
        self.assertEqual(self.store.confirmed()[0]['value'],'Город B')
        self.assertEqual(len(self.store.get('city')['sources']),2)

    def test_reject_edit_audit_and_stale_revision(self):
        self.store.import_draft(draft())
        self.store.update('figma',1,'confirm',42)
        with self.assertRaises(StaleFact):
            self.store.update('figma',1,'reject',42)
        self.store.update('figma',2,'reject',42)
        self.assertEqual(self.store.confirmed(),[])
        self.store.update('figma',3,'edit',42,'Figma: компоненты')
        self.assertEqual(self.store.get('figma')['status'],'UNCONFIRMED')
        count=self.store.db.execute("SELECT COUNT(*) FROM candidate_fact_events WHERE fact_id='figma'").fetchone()[0]
        self.assertEqual(count,4)
        self.store.close()
        self.store=ProfileStore(self.path)
        self.assertEqual(self.store.get('figma')['revision'],4)

    def test_invalid_import_is_atomic(self):
        data=draft();data['facts'][1]['sources']=[]
        with self.assertRaises(ValueError):
            self.store.import_draft(data)
        self.assertEqual(self.store.all(),[])

    def test_vacancy_clear_does_not_touch_profile(self):
        self.store.import_draft(draft())
        self.store.storage.clear_results('any')
        self.assertEqual(len(self.store.all()),2)


class ProfileBotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.path=Path(self.folder.name)/'db.sqlite3'
        store=ProfileStore(self.path)
        store.import_draft(draft());store.close()
        self.controller=BotController(Mock(db_path=str(self.path)),42)

    def tearDown(self):
        self.folder.cleanup()

    def callback(self, data, uid=42):
        return SimpleNamespace(data=data,from_user=SimpleNamespace(id=uid),answer=AsyncMock(),
            message=Message(message_id=1,date=0,chat={'id':42,'type':'private'}))

    async def test_owner_review_and_stale_button(self):
        with patch.object(Message,'answer',new=AsyncMock()):
            callback=self.callback('profile:confirm:figma:1')
            await self.controller.handle_callback(callback)
            await self.controller.handle_callback(callback)
            self.assertTrue(callback.answer.call_args.kwargs['show_alert'])
        store=ProfileStore(self.path)
        try:
            self.assertEqual(len(store.confirmed()),1)
            self.assertEqual(store.get('figma')['revision'],2)
        finally:
            store.close()

    async def test_stranger_cannot_confirm(self):
        await self.controller.handle_callback(self.callback('profile:confirm:figma:1',99))
        store=ProfileStore(self.path)
        try:
            self.assertEqual(store.confirmed(),[])
        finally:
            store.close()

    async def test_correction_then_cancel(self):
        with patch.object(Message,'answer',new=AsyncMock()):
            await self.controller.handle_callback(self.callback('profile:edit:city:1'))
        await self.controller.handle(message('Город B'))
        store=ProfileStore(self.path)
        try:
            self.assertEqual(store.get('city')['value'],'Город B')
            self.assertEqual(store.get('city')['status'],'UNCONFIRMED')
        finally:
            store.close()
        with patch.object(Message,'answer',new=AsyncMock()):
            await self.controller.handle_callback(self.callback('profile:edit:city:2'))
        await self.controller.handle(message('/cancel'))
        self.assertIsNone(self.controller.profile.pending)
