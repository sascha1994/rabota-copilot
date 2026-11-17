import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.automation import AutomationStore
from app.candidate_profile import ProfileStore
from app.daily_insights import DailyInsights


class InsightTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'test.sqlite3'
        self.insights=DailyInsights(self.path)
        self.store=AutomationStore(self.path)
        self.now=datetime(2026,11,15,20,30,tzinfo=timezone.utc)
        for ident,stamp in [('1','2026-11-14T22:00:00+00:00'),('2','2026-11-15T12:00:00+00:00'),('3','2026-11-14T20:59:00+00:00')]:
            self.store.storage.save({'id':ident,'name':'Дизайнер','key_skills':[]},'test',None,stamp)
            self.store.storage.save_description(ident,'Figma figma FIGMA; Photoshop; удалённая работа; портфолио')
        # Snapshot timestamps must represent what was known before the report.
        with self.store.db:
            self.store.db.execute("UPDATE vacancy_descriptions SET first_seen='2026-11-15T10:00:00+00:00'")

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    async def test_frequency_is_per_unique_vacancy_and_minsk_day(self):
        report=self.insights.build(self.now)
        self.assertEqual(report['total'],2)
        figma=next(i for i in report['items'] if i['label']=='Figma')
        self.assertEqual(len(figma['sources']),2)
        self.assertEqual({s['id'] for s in figma['sources']},{'1','2'})
        profile=ProfileStore(self.path)
        self.assertEqual(profile.confirmed(),[])
        profile.close()

    async def test_schedule_once_and_explicit_skill_confirmation_is_idempotent(self):
        from unittest.mock import patch
        msg=SimpleNamespace(answer=AsyncMock())
        # The scheduler passes its cutoff into report construction.
        with patch.object(self.insights,'report',return_value=self.insights.build(self.now)):
            await self.insights.due(msg,self.now)
            await self.insights.due(msg,self.now)
        msg.answer.assert_awaited_once()
        report=self.insights.report(self.now)
        index=next(n for n,i in enumerate(report['items']) if i['label']=='Figma')
        callback=SimpleNamespace(data=f"insight:add:{report['day']}:{index}",message=msg,answer=AsyncMock())
        await self.insights.add(callback,42)
        await self.insights.add(callback,42)
        profile=ProfileStore(self.path)
        self.assertEqual(len(profile.confirmed()),1)
        self.assertEqual(profile.confirmed()[0]['value'],'Figma')
        profile.close()

    async def test_before_report_time_does_not_send(self):
        msg=SimpleNamespace(answer=AsyncMock())
        await self.insights.due(msg,self.now.replace(hour=19))
        msg.answer.assert_not_awaited()
