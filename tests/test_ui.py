import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import Message

from app.assessment import Evaluation, Match, render_evaluation
from app.automation import AutomationEngine, AutomationStore
from app.candidate_profile import ProfileStore
from app.collector import search_key
from app.config import SearchConfig
from app.letters import GeneratedLetter
from app.resumes import snapshot
from app.tg_automation import AutomationConsole
from app.tg_bot import ALIASES, KEYBOARD, BotController, vacancy_card
from app.tg_ui import bar, esc, expandable, fmt_dt, fmt_salary, render
from app.tg_vacancies import vacancy_actions


class UiFormattingTests(unittest.TestCase):
    def test_html_escape_truncates_before_escaping_entities(self):
        raw = "<b>" + ("&" * 100) + "</b>"
        escaped = esc(raw, 20)
        self.assertNotIn("<b>", escaped)
        self.assertFalse(escaped.endswith("&am"))
        self.assertIn("&lt;b&gt;", escaped)

    def test_progress_bar_and_salary_and_dates(self):
        self.assertEqual(bar(0, 10), "▱" * 10)
        self.assertEqual(bar(5, 10), "▰" * 5 + "▱" * 5)
        self.assertEqual(bar(25, 10), "▰" * 10)
        self.assertEqual(fmt_salary({"from": 3000, "to": 4500, "currency": "BYR", "gross": False}), "3 000 – 4 500 BYN (на руки)")
        self.assertEqual(fmt_salary(None), "не указана")
        self.assertIn("ноя", fmt_dt("2026-11-17T09:30:00+03:00"))

    def test_vacancy_card_escapes_html_and_keeps_valid_link(self):
        card = vacancy_card({
            "name": "UX/UI <script>alert(1)</script> & AI",
            "employer": {"name": "ООО «Рога & Копыта»"},
            "area": {"name": "Минск"},
            "salary": {"from": 3200, "currency": "BYR"},
            "published_at": "2026-11-17T10:15:00+0300",
            "alternate_url": "https://rabota.by/vacancy/777",
            "_tracking_state": "interested",
        })
        self.assertNotIn("<script>", card)
        self.assertIn("&lt;script&gt;", card)
        self.assertIn("3 200 BYN", card)
        self.assertIn('href="https://rabota.by/vacancy/777"', card)
        self.assertIn("⭐ Интересно", card)

    def test_evaluation_html_contains_badges_and_escapes_untrusted_text(self):
        ev = Evaluation(
            score=84,
            resume="web_ui",
            summary="Хорошее совпадение <img src=x>",
            matches=[Match(reason="Опыт в Figma & UI", fact_ids=["f1"])],
            gaps=["Нет B2 <eng>"],
            questions=["Какой график?"],
        )
        rendered = render_evaluation(ev, cached=True)
        self.assertIn("🟢 <b>84/100</b>", rendered)
        self.assertIn("из кэша", rendered)
        self.assertNotIn("<img", rendered)
        self.assertNotIn("<eng>", rendered)
        self.assertIn("<code>[f1]</code>", rendered)

    def test_compact_reply_keyboard_and_legacy_aliases(self):
        self.assertEqual(len(KEYBOARD.keyboard), 2)
        for modern in ("📥 Очередь", "🔍 Найти вакансии", "⚙️ Автоматизация", "👤 Профиль", "📊 Пульт"):
            self.assertIn(modern, ALIASES)
        for legacy in ("Очередь откликов", "Режим и расписание", "Очистить вакансии", "Фильтры"):
            self.assertIn(legacy, ALIASES)

    def test_vacancy_actions_highlight_active_mark_in_three_rows(self):
        kb = vacancy_actions("55", current="interested")
        self.assertEqual(len(kb.inline_keyboard), 3)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any("Интересно ✓" in t for t in labels))
        self.assertFalse(any("Пропустить ✓" in t for t in labels))


class UiInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_render_edits_in_place_and_ignores_not_modified(self):
        msg = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
        await render(msg, "<b>Экран</b>", edit=True)
        msg.edit_text.assert_awaited_once()
        msg.answer.assert_not_awaited()

        # "message is not modified" should be swallowed silently without duplicate answer()
        not_modified = TelegramBadRequest(
            method=EditMessageText(chat_id=42, message_id=1, text="x"),
            message="Bad Request: message is not modified",
        )
        msg2 = SimpleNamespace(edit_text=AsyncMock(side_effect=not_modified), answer=AsyncMock())
        await render(msg2, "<b>Экран</b>", edit=True)
        msg2.answer.assert_not_awaited()

    async def test_single_message_package_card_and_automation_presets(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "db.sqlite3"
            cfg_path = Path(folder) / "search.json"
            SearchConfig().save(cfg_path)
            store = AutomationStore(db_path)
            profile = ProfileStore(db_path)
            profile.import_draft({
                "facts": [{"id": "f1", "category": "Навыки", "value": "Figma", "sources": [{"document": "cv", "page": 1, "quote": "Figma"}]}]
            })
            profile.update("f1", 1, "confirm", 42)
            profile.close()

            raw = {"id": "10", "name": "Product Designer", "alternate_url": "https://rabota.by/vacancy/10"}
            store.storage.save(raw, search_key(SearchConfig()), None, "2026-11-17T08:00:00+00:00")
            desc_id = store.storage.save_description("10", "Figma product design")
            store.enqueue(raw, search_key(SearchConfig()), snapshot(db_path)[1])
            store.configure("mode", "semi")
            store.close()

            class FakeAI:
                description_id = desc_id
                details = staticmethod(lambda vid: "Figma product design")

                def evaluate(self, vid, **kwargs):
                    if kwargs.get("application_direction"):
                        return GeneratedLetter(text="Привет! Делаю интерфейсы в Figma.", fact_ids=["f1"], questions=[]), False
                    return Evaluation(score=88, resume="web_ui", summary="Отличное совпадение", matches=[], gaps=[], questions=["Формат договора?"]), False

            service = SimpleNamespace(db_path=db_path, config_path=cfg_path, env={})
            engine = AutomationEngine(service, ai_factory=lambda *a: FakeAI())
            await engine.prepare_one(AsyncMock())

            console = AutomationConsole(db_path, engine)
            screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
            await console.show(screen, 1, edit=True)
            # Unified single card instead of 4 separate messages!
            screen.edit_text.assert_awaited_once()
            html_body = screen.edit_text.call_args.args[0]
            self.assertIn("<blockquote expandable>", html_body)
            self.assertIn("Привет! Делаю интерфейсы в Figma.", html_body)
            self.assertIn("Формат договора?", html_body)
            self.assertLess(len(html_body), 4096)

            # One-tap preset button updates threshold in-place
            cb = SimpleNamespace(data="flow:preset:threshold:80", message=screen, answer=AsyncMock())
            await console.callback(cb)
            check_store = AutomationStore(db_path)
            try:
                self.assertEqual(check_store.settings()["threshold"], 80)
            finally:
                check_store.close()

    async def test_one_tap_filter_preset_updates_config(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg_path = Path(folder) / "search.json"
            SearchConfig().save(cfg_path)
            service = Mock(config_path=str(cfg_path), env={"VACANCY_SOURCE": "web"})
            controller = BotController(service, 42)
            msg = Message(message_id=1, date=0, chat={"id": 42, "type": "private"})
            cb = SimpleNamespace(message=msg, from_user=SimpleNamespace(id=42), data="filter:set:currency:USD", answer=AsyncMock())
            with patch.object(Message, "answer", new=AsyncMock()):
                await controller.handle_callback(cb)
            self.assertEqual(SearchConfig.load(cfg_path).currency, "USD")

    async def test_unprepared_queued_jobs_adopt_updated_profile_and_bulk_retry_stale(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "db.sqlite3"
            cfg_path = Path(folder) / "search.json"
            cfg = SearchConfig()
            cfg.save(cfg_path)
            store = AutomationStore(db_path)
            profile = ProfileStore(db_path)
            profile.import_draft({
                "facts": [
                    {"id": "f1", "category": "Навыки", "value": "Figma", "sources": [{"document": "cv", "page": 1, "quote": "Figma"}]},
                    {"id": "f2", "category": "Навыки", "value": "Photoshop", "sources": [{"document": "cv", "page": 1, "quote": "PS"}]},
                ]
            })
            profile.update("f1", 1, "confirm", 42)
            old_digest = snapshot(db_path, profile)[1]

            # Job 1 & 2 queued under an old filter; Job 3 queued under current filter + old profile
            for vid, key in [("1", "old_filter"), ("2", "old_filter"), ("3", search_key(cfg))]:
                raw = {"id": vid, "name": f"Vacancy {vid}", "alternate_url": f"https://rabota.by/vacancy/{vid}"}
                store.storage.save(raw, key, None, "2026-11-17T08:00:00+00:00")
                desc_id = store.storage.save_description(vid, "Figma design")
                store.enqueue(raw, key, old_digest)
            store.configure("mode", "semi")
            store.close()

            # Confirming a second skill changes profile_hash AFTER job 3 was queued
            profile.update("f2", 1, "confirm", 42)
            profile.close()

            class FakeAI:
                description_id = desc_id
                details = staticmethod(lambda vid: "Figma design")

                def evaluate(self, vid, **kwargs):
                    if kwargs.get("application_direction"):
                        return GeneratedLetter(text="Письмо", fact_ids=["f1"], questions=[]), False
                    return Evaluation(score=85, resume="graphic", summary="OK", matches=[], gaps=[], questions=[]), False

            service = SimpleNamespace(db_path=db_path, config_path=cfg_path, env={})
            engine = AutomationEngine(service, ai_factory=lambda *a: FakeAI())

            # A single prepare_one() sweeps both old-filter rows AND immediately prepares job 3 with the new profile!
            prepared_id = await engine.prepare_one(AsyncMock())
            self.assertEqual(prepared_id, 3)

            check = AutomationStore(db_path)
            try:
                self.assertEqual(check.get(1)["state"], "STALE")
                self.assertEqual(check.get(2)["state"], "STALE")
                self.assertEqual(check.get(3)["state"], "REVIEW")
            finally:
                check.close()

            # One-tap bulk retry brings both STALE jobs back into QUEUED with current config & profile
            console = AutomationConsole(db_path, engine)
            screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
            await console.callback(SimpleNamespace(data="flow:retry_stale:0", message=screen, answer=AsyncMock()))
            check = AutomationStore(db_path)
            try:
                self.assertEqual(check.get(1)["state"], "QUEUED")
                self.assertEqual(check.get(2)["state"], "QUEUED")
            finally:
                check.close()

    async def test_unsuitable_vacancy_can_be_force_sent_or_force_prepared(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "db.sqlite3"
            cfg_path = Path(folder) / "search.json"
            cfg = SearchConfig()
            cfg.save(cfg_path)
            store = AutomationStore(db_path)
            profile = ProfileStore(db_path)
            profile.import_draft({
                "facts": [{"id": "f1", "category": "Навыки", "value": "Figma", "sources": [{"document": "cv", "page": 1, "quote": "Figma"}]}]
            })
            profile.update("f1", 1, "confirm", 42)
            digest = snapshot(db_path, profile)[1]
            profile.close()

            raw = {"id": "81", "name": "Дизайнер продукта", "alternate_url": "https://rabota.by/vacancy/81"}
            store.storage.save(raw, search_key(cfg), None, "2026-11-17T08:00:00+00:00")
            desc_id = store.storage.save_description("81", "Дизайнер продукта Figma")
            store.enqueue(raw, search_key(cfg), digest)
            store.configure("mode", "semi")
            store.configure("threshold", 75)
            store.close()

            class FakeAI:
                description_id = desc_id
                details = staticmethod(lambda vid: "Дизайнер продукта Figma")

                def evaluate(self, vid, **kwargs):
                    if kwargs.get("application_direction"):
                        return GeneratedLetter(text="Здравствуйте! Работаю в Figma над интерфейсами.", fact_ids=["f1"], questions=[]), False
                    return Evaluation(score=74, resume="web_ui", summary="74/100 чуть ниже порога", matches=[], gaps=[], questions=[]), False

            sender = SimpleNamespace(available=True, send=Mock(return_value={"revision": 1, "confirmed_at": "2026-11-17T18:55:00+00:00"}))
            service = SimpleNamespace(db_path=db_path, config_path=cfg_path, env={})
            controller = BotController(service, 42)
            controller.automation_engine = AutomationEngine(service, sender=sender, ai_factory=lambda *a: FakeAI())
            controller.automation = AutomationConsole(db_path, controller.automation_engine)

            # Normal daily preparation marks 74/100 (< 75) as UNSUITABLE without a package
            await controller.automation_engine.prepare_one(AsyncMock())
            check = AutomationStore(db_path)
            self.assertEqual(check.get(1)["state"], "UNSUITABLE")
            check.close()

            # Opening the UNSUITABLE card surfaces Force Send and Force Prepare buttons
            screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
            await controller.automation.show(screen, 1, edit=True)
            markup = screen.edit_text.call_args.kwargs["reply_markup"]
            cb_data = [b.callback_data for row in markup.inline_keyboard for b in row]
            self.assertIn("flow:forcesend:1:web_ui", cb_data)
            self.assertIn("flow:forceprep:1:web_ui", cb_data)
            self.assertIn("flow:forcedir:1", cb_data)

            # Clicking Force Send generates the package AND submits it via sender
            msg = Message(message_id=1, date=0, chat={"id": 42, "type": "private"})
            with patch.object(Message, "answer", new=AsyncMock()):
                cb = SimpleNamespace(message=msg, from_user=SimpleNamespace(id=42), data="flow:forcesend:1:web_ui", answer=AsyncMock())
                await controller.handle_callback(cb)
                await controller.analysis_task

            sender.send.assert_called_once()
            check = AutomationStore(db_path)
            try:
                self.assertEqual(check.get(1)["state"], "SENT")
                self.assertEqual(check.get(1)["package_revision"], 1)
            finally:
                check.close()


