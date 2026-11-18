import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.tg_bot import BotController, admin_id_from_env, vacancy_card
from app.hh_client import ApiError


def message(text, uid=42, chat_type="private"):
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=uid),
                           chat=SimpleNamespace(id=uid, type=chat_type), answer=AsyncMock())


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_button_starts_one_operation_and_blocks_stranger(self):
        from aiogram.types import Message
        controller = BotController(Mock(),42)
        gate = asyncio.Event()
        async def send(msg,ident,revision):
            await gate.wait()
        callback = SimpleNamespace(message=Message(message_id=1,date=0,chat={'id':42,'type':'private'}),
            from_user=SimpleNamespace(id=99),data='flow:send:3:1',answer=AsyncMock())
        with patch.object(controller,'send_package',side_effect=send) as action:
            await controller.handle_callback(callback)
            action.assert_not_called()
            callback.from_user.id = 42
            await controller.handle_callback(callback)
            await controller.handle_callback(callback)
            action.assert_called_once_with(callback.message,3,1)
            gate.set()
            await controller.analysis_task

    async def test_send_result_is_reported_and_card_refreshed(self):
        controller = BotController(Mock(),42)
        controller.automation_engine.send = AsyncMock(return_value='Confirmed')
        controller.automation.show = AsyncMock()
        msg = message('')
        await controller.send_package(msg,3,1)
        controller.automation_engine.send.assert_awaited_once_with(3,1)
        self.assertIn('Confirmed',[call.args[0] for call in msg.answer.call_args_list])
        controller.automation.show.assert_awaited_once_with(msg,3)

    async def test_only_one_assessment_runs_and_stranger_is_blocked(self):
        from aiogram.types import Message
        controller=BotController(Mock(),42)
        gate=asyncio.Event()
        async def assess(msg,vid):
            await gate.wait()
        callback=SimpleNamespace(message=Message(message_id=1,date=0,chat={'id':42,'type':'private'}),
            from_user=SimpleNamespace(id=99),data='assess:123',answer=AsyncMock())
        with patch.object(controller,'assess_vacancy',side_effect=assess) as action:
            await controller.handle_callback(callback)
            action.assert_not_called()
            callback.from_user.id=42
            await controller.handle_callback(callback)
            await controller.handle_callback(callback)
            action.assert_called_once()
            gate.set()
            await controller.analysis_task

    async def test_hh_403_has_actionable_safe_message(self):
        service = Mock()
        service.collect.side_effect = ApiError('secret-raw-body', status=403)
        controller = BotController(service, 42)
        msg = message('/search')
        await controller.search(msg)
        self.assertIn('403', controller.last_result)
        self.assertIn('HH_ACCESS_TOKEN', controller.last_result)
        self.assertNotIn('secret-raw-body', str(msg.answer.call_args))

    async def test_unauthorized_user_cannot_read_or_collect(self):
        service = Mock()
        controller = BotController(service, 42)
        msg = message('/search', uid=99)
        await controller.handle(msg)
        service.collect.assert_not_called()
        service.read.assert_not_called()
        self.assertFalse(controller.busy)

    async def test_group_chat_is_ignored_even_for_owner(self):
        msg = message('/id', chat_type='group')
        await BotController(Mock(), 42).handle(msg)
        msg.answer.assert_not_called()

    async def test_id_available_without_admin_but_private_data_blocked(self):
        controller = BotController(Mock(), None)
        msg = message('/id')
        await controller.handle(msg)
        self.assertIn('42', msg.answer.call_args.args[0])
        msg = message('/vacancies')
        await controller.handle(msg)
        controller.service.read.assert_not_called()

    async def test_invalid_page_does_not_read_storage(self):
        controller = BotController(Mock(), 42)
        for text in ('/vacancies 0', '/vacancies abc', '/vacancies 201', '/vacancies 1 extra'):
            await controller.handle(message(text))
        controller.service.read.assert_not_called()

    async def test_duplicate_search_uses_one_background_job(self):
        service = Mock(user_agent='Test/1.0 (me@example.com)', config_path='search.example.json')
        controller = BotController(service, 42)
        gate = asyncio.Event()
        async def slow_search(msg, config=None):
            await gate.wait()
        with patch.object(controller, 'search', side_effect=slow_search) as search:
            await controller.handle(message('/search'))
            self.assertTrue(controller.busy)
            msg = message('/search')
            await controller.handle(msg)
            search.assert_called_once()
            self.assertIn('уже', msg.answer.call_args.args[0])
            gate.set()
            await controller.search_task

    async def test_background_failure_is_sanitized(self):
        service = Mock()
        service.collect.side_effect = RuntimeError('https://api.telegram.org/botSECRET')
        controller = BotController(service, 42)
        msg = message('/search')
        await controller.search(msg)
        self.assertNotIn('SECRET', str(msg.answer.call_args))
        self.assertNotIn('SECRET', controller.last_result)

    async def test_old_vacancies_link_opens_queue(self):
        controller = BotController(Mock(), 42)
        controller.automation.queue = AsyncMock()
        msg = message('/vacancies 2')
        await controller.handle(msg)
        controller.automation.queue.assert_awaited_once_with(msg)


class BotFormatTests(unittest.TestCase):
    def test_admin_validation(self):
        self.assertIsNone(admin_id_from_env({}))
        self.assertEqual(admin_id_from_env({'ADMIN_ID': '42'}), 42)
        for value in ('0', '-1', 'owner'):
            with self.assertRaises(ValueError):
                admin_id_from_env({'ADMIN_ID': value})

    def test_card_bounds_and_untrusted_link(self):
        text = vacancy_card({'name': 'x' * 10000, 'employer': None,
                             'alternate_url': 'https://evil.example/steal'})
        self.assertLess(len(text), 4096)
        self.assertNotIn('evil.example', text)
        self.assertIn('не указана', text)
