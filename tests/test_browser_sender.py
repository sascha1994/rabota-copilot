import asyncio
import json
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch
from threading import get_ident

from app.browser_sender import BrowserSender, SeriesBrowserSender, BrowserVerificationRequired, SubmissionBlocked, SUCCESS_TEXT, FOREIGN_WARNING, FOREIGN_CONTINUE, RESPONSE_LINK


class BrowserSenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sender = BrowserSender({'BROWSER_PROFILE_DIR':self.temp.name,
            'RABOTA_RESUME_GRAPHIC':'a'*35})
        self.package = {'job_id':1,'revision':1,'vacancy_id':'123','metadata_json':json.dumps({'direction':'graphic'}),
                        'resume_text':'Резюме','letter_text':'Письмо'}

    def tearDown(self):
        self.temp.cleanup()

    def context(self):
        page = Mock()
        warning = Mock()
        warning.is_visible.return_value = False
        page.get_by_text.side_effect = lambda pattern,**kwargs: warning if pattern is FOREIGN_WARNING else page.get_by_text.return_value
        field = Mock()
        field.input_value.return_value = 'Письмо'
        submit = Mock()
        submit.is_visible.return_value = True
        field.locator.return_value.get_by_role.return_value.all.return_value = [submit]
        page.locator.return_value.all.return_value = [submit]
        page.locator.return_value.inner_text.return_value = ''
        context = Mock(pages=[page])
        manager = MagicMock()
        manager.__enter__.return_value.chromium.launch_persistent_context.return_value = context
        return manager,page,field,submit

    def test_no_site_resume_mapping_fails_before_browser(self):
        self.package['metadata_json'] = json.dumps({'direction':'web_ui'})
        with self.assertRaises(SubmissionBlocked):
            self.sender.validate(self.package)

    def test_web_and_ux_are_distinct_and_legacy_ai_cannot_select_narrative(self):
        sender = BrowserSender({'BROWSER_PROFILE_DIR': self.temp.name,
            'RABOTA_RESUME_WEB': 'a'*35, 'RABOTA_RESUME_WEB_UI': 'b'*35,
            'RABOTA_RESUME_GRAPHIC': 'c'*35, 'RABOTA_RESUME_NARRATIVE': 'd'*35,
            'RABOTA_RESUME_AI': 'd'*35})
        for direction, expected in [('web','a'),('web_ui','b'),('graphic','c'),('narrative','d')]:
            package = dict(self.package,metadata_json=json.dumps({'direction':direction}))
            self.assertEqual(sender.validate(package),expected*35)
        with self.assertRaises(SubmissionBlocked):
            sender.validate(dict(self.package,metadata_json=json.dumps({'direction':'ai'})))

    def test_send_uses_existing_resume_and_fills_only_the_letter(self):
        manager,page,field,submit = self.context()
        with patch('playwright.sync_api.sync_playwright',return_value=manager), \
             patch.object(self.sender,'guard'), \
             patch.object(self.sender,'_open_response',return_value=field) as open_response:
            receipt = self.sender.send(self.package)
        self.assertEqual(receipt['resume_id'],'a'*35)
        self.assertEqual(receipt['vacancy_id'],'123')
        page.goto.assert_called_once_with('https://rabota.by/vacancy/123',wait_until='domcontentloaded')
        self.assertEqual(open_response.call_args.args,(page,'a'*35))
        field.fill.assert_called_once_with('Письмо')
        launch = manager.__enter__.return_value.chromium.launch_persistent_context
        self.assertTrue(launch.call_args.kwargs['chromium_sandbox'])
        self.assertEqual(len(receipt['letter_hash']),64)
        self.assertNotIn('resume_hash',receipt)
        submit.click.assert_called_once()

    def test_error_after_click_is_ambiguous(self):
        manager,page,field,submit = self.context()
        page.get_by_text.return_value.first.wait_for.side_effect = TimeoutError()
        with patch('playwright.sync_api.sync_playwright',return_value=manager), \
             patch.object(self.sender,'guard'), \
             patch.object(self.sender,'_open_response',return_value=field):
            with self.assertRaises(RuntimeError):
                self.sender.send(self.package)
        submit.click.assert_called_once()

    def test_pause_guard_prevents_final_click(self):
        manager,page,field,submit = self.context()
        self.package['_before_submit'] = Mock(side_effect=SubmissionBlocked('Пауза'))
        with patch('playwright.sync_api.sync_playwright',return_value=manager), \
             patch.object(self.sender,'guard'), \
             patch.object(self.sender,'_open_response',return_value=field):
            with self.assertRaises(SubmissionBlocked):
                self.sender.send(self.package)
        submit.click.assert_not_called()

    def test_guard_rejects_login_redirect_and_new_consent(self):
        page = Mock(url='https://account.hh.ru/login')
        with self.assertRaises(SubmissionBlocked):
            self.sender.guard(page)
        page.url = 'https://rabota.by/vacancy/123'
        page.locator.return_value.inner_text.return_value = 'Даю согласие на передачу персональных данных'
        with self.assertRaises(SubmissionBlocked):
            self.sender.guard(page)

    def test_site_confirmation_handles_nonbreaking_spaces(self):
        for text in ('Вы\u00a0откликнулись', 'Вы\u202fоткликнулись', 'Резюме доставлено', 'Отклик отправлен'):
            self.assertIsNotNone(SUCCESS_TEXT.search(text))
        self.assertIsNone(SUCCESS_TEXT.search('Откликнуться'))

    def warning_page(self):
        page = Mock()
        title,button = Mock(),Mock()
        title.all.return_value = [title]
        button.all.return_value = [button]
        page.get_by_text.return_value = title
        page.get_by_role.return_value = button
        return page,title,button

    def test_foreign_warning_checks_package_before_continuing(self):
        page,title,button = self.warning_page()
        before = Mock()
        with patch.object(self.sender,'guard'):
            self.sender._continue_foreign_warning(page,before)
        before.assert_called_once()
        button.click.assert_called_once()
        title.wait_for.assert_called_once_with(state='hidden',timeout=15000)
        for label in ('Все равно откликнуться','Всё равно откликнуться'):
            self.assertIsNotNone(FOREIGN_CONTINUE.fullmatch(label))

    def test_foreign_warning_check_mode_does_not_continue(self):
        page,title,button = self.warning_page()
        with patch.object(self.sender,'guard'),self.assertRaises(SubmissionBlocked):
            self.sender._continue_foreign_warning(page,None)
        button.click.assert_not_called()

    def test_foreign_warning_respects_pause(self):
        page,title,button = self.warning_page()
        with patch.object(self.sender,'guard'),self.assertRaises(SubmissionBlocked):
            self.sender._continue_foreign_warning(page,Mock(side_effect=SubmissionBlocked('Пауза')))
        button.click.assert_not_called()

    def test_foreign_warning_after_submit_requires_success(self):
        manager,page,field,submit = self.context()
        warning = Mock()
        page.get_by_text.side_effect = lambda pattern,**kwargs: warning if pattern is FOREIGN_WARNING else page.get_by_text.return_value
        with patch('playwright.sync_api.sync_playwright',return_value=manager), \
             patch.object(self.sender,'guard'), \
             patch.object(self.sender,'_open_response',return_value=field), \
             patch.object(self.sender,'_continue_foreign_warning') as proceed:
            receipt = self.sender.send(self.package)
        proceed.assert_called_once()
        submit.click.assert_called_once()
        self.assertEqual(receipt['vacancy_id'],'123')

    def test_foreign_warning_before_form_is_handled(self):
        page = Mock()
        page.locator.return_value.inner_text.return_value = ''
        title,warning,consent = Mock(),Mock(),Mock()
        warning.is_visible.side_effect = [True,False]
        consent.is_visible.return_value = False
        response_link = Mock()
        actions = Mock()
        actions.all.return_value = [response_link]
        response_button = Mock()
        response_button.or_.return_value = actions
        page.get_by_role.side_effect = lambda role,**kwargs: response_button if kwargs.get('name')=='Откликнуться' else consent
        page.get_by_text.side_effect = lambda pattern,**kw: warning if pattern is FOREIGN_WARNING else title
        selected = Mock()
        before = Mock()
        with patch.object(self.sender,'guard'),patch.object(self.sender,'_only_visible',return_value=selected), \
             patch.object(self.sender,'_continue_foreign_warning') as proceed:
            field = self.sender._open_response(page,'a'*35,before_warning=before)
        proceed.assert_called_once_with(page,before)
        response_link.click.assert_called_once()
        page.locator.assert_any_call(RESPONSE_LINK)
        self.assertEqual(field,page.locator.return_value)

    def test_browser_check_waits_for_the_site_to_finish_and_never_solves_captcha(self):
        page = Mock()
        text = page.locator.return_value.inner_text
        text.side_effect = ['Проверка вашего браузера', 'Вакансия открыта']
        with patch('app.browser_sender.time.sleep'):
            self.sender._browser_verification(page)
        self.assertEqual(text.call_count,2)
        text.side_effect = None
        for message in ('Подтвердите, что вы не робот',
                        'Проверка браузера перед переходом на rabota.by\n'
                        'Не удалось проверить ваш браузер автоматически. Чтобы продолжить, пройдите ручную проверку',
                        'Проверка браузера\nЯ не робот\nDDOS-GUARD'):
            text.return_value = message
            with self.subTest(message=message), patch('app.browser_sender.time.sleep') as wait:
                with self.assertRaises(BrowserVerificationRequired):
                    self.sender._browser_verification(page)
                wait.assert_not_called()


class BrowserSeriesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sender = SeriesBrowserSender({'BROWSER_PROFILE_DIR':self.temp.name})
        self.addAsyncCleanup(self.sender.close_series)
        self.page = Mock()
        self.page.is_closed.return_value = False
        self.context = Mock(pages=[self.page])
        self.closed = asyncio.Event()
        self.threads = []
        loop = asyncio.get_running_loop()

        def close():
            self.threads.append(get_ident())
            loop.call_soon_threadsafe(self.closed.set)

        def launch(stack):
            self.threads.append(get_ident())
            stack.callback(close)
            return self.context

        self.sender._launch_browser = Mock(side_effect=launch)

    def visit(self, package):
        with self.sender._browser_context() as context:
            self.threads.append(get_ident())
            return context

    async def test_consecutive_sends_reuse_window_then_idle_timer_closes_it(self):
        with patch.object(self.sender,'send',side_effect=self.visit):
            self.assertIs(await self.sender.send_in_series({},45),self.context)
            self.assertIs(await self.sender.send_in_series({},45),self.context)
        self.sender._launch_browser.assert_called_once()
        self.assertFalse(self.closed.is_set())
        await self.sender.set_idle_minutes(0)
        await asyncio.wait_for(self.closed.wait(),2)
        self.assertEqual(len(set(self.threads)),1)
        self.assertNotEqual(self.threads[0],get_ident())

    async def test_verification_window_stays_open_until_manual_check_passes(self):
        def challenge(package):
            self.visit(package)
            self.sender._preserve_verification_page(self.page)
            raise BrowserVerificationRequired(window_open=True)

        with patch.object(self.sender,'send',side_effect=challenge), self.assertRaises(BrowserVerificationRequired):
            await self.sender.send_in_series({},45)
        await self.sender.set_idle_minutes(0)
        with patch.object(self.sender,'guard',side_effect=[BrowserVerificationRequired(),None]) as guard:
            with self.assertRaises(BrowserVerificationRequired):
                await self.sender.resume_in_series(0)
            self.assertFalse(self.closed.is_set())
            await self.sender.resume_in_series(0)
            self.assertEqual(guard.call_count,2)
        await asyncio.wait_for(self.closed.wait(),2)
        self.sender._launch_browser.assert_called_once()
