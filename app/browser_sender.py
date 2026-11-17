"""Submit through the visible rabota.by UI using the application's own profile."""

import asyncio
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .process_lock import ProcessLock
from .assessment import DIRECTIONS

SUCCESS_TEXT = re.compile(r'Вы\s+откликнулись|Отклик\s+отправлен|Вы\s+уже\s+откликнулись|Резюме\s+доставлено', re.I)
FOREIGN_WARNING = re.compile(r'^Вы\s+откликаетесь\s+на\s+вакансию\s+в\s+другой\s+стране$')
FOREIGN_CONTINUE = re.compile(r'^Вс[её]\s+равно\s+откликнуться$')
RESPONSE_LINK = '[data-qa="vacancy-response-link-top"]'
AUTO_CHECK = re.compile(r'проверка (?:вашего )?браузера|проверяем (?:ваш )?браузер|checking your browser|just a moment|провер(?:ка|яем) безопасность|проверка безопасности', re.I)
MANUAL_CHECK = re.compile(
    r'\bcaptcha\b|подтвердите,? что вы (?:не робот|человек)|'
    r'пройдите ручную проверку|(?:^|\n)\s*я не робот\s*(?=\n|$)|'
    r'verify (?:that )?you are human', re.I
)

class SubmissionBlocked(ValueError):
    """The final submit control has not been clicked; a manual retry is possible."""

class BrowserVerificationRequired(SubmissionBlocked):
    def __init__(self, *, outcome_unknown=False, window_open=False):
        self.outcome_unknown = outcome_unknown
        self.window_open = window_open
        guidance = ('Пройдите проверку в открытом окне Chrome бота.'
                    if window_open else 'Пройдите проверку вручную через browser-login и закройте это окно.')
        super().__init__(f'rabota.by требует проверки браузера. {guidance} Затем нажмите «Возобновить отправку» в «Режим и расписание».')


class BrowserSender:
    available = True

    def __init__(self, env):
        self.env = dict(env)
        self.profile = Path(env.get('BROWSER_PROFILE_DIR','data/browser-profile')).resolve()
        self.channel = env.get('BROWSER_CHANNEL','chrome')
        self.resumes = {key:env.get('RABOTA_RESUME_'+key.upper(),'').strip() for key in DIRECTIONS}

    def validate(self, package):
        metadata = json.loads(package['metadata_json'])
        resume_id = self.resumes.get(metadata.get('direction'))
        if not resume_id or not re.fullmatch(r'[a-f0-9]{20,64}',resume_id):
            raise SubmissionBlocked('Для выбранного направления не настроен ID опубликованного резюме.')
        if not re.fullmatch(r'[0-9]{1,15}',str(package['vacancy_id'])):
            raise SubmissionBlocked('Некорректный ID вакансии.')
        if self.channel not in ('chrome','msedge','chromium'):
            raise SubmissionBlocked('Неизвестный BROWSER_CHANNEL.')
        if not self.profile.is_dir():
            raise SubmissionBlocked('Сначала выполните browser-login.')
        return resume_id

    def _browser_verification(self, page):
        deadline = time.monotonic()+20
        while True:
            text = page.locator('body').inner_text()
            if MANUAL_CHECK.search(text):
                raise BrowserVerificationRequired()
            if not AUTO_CHECK.search(text):
                return
            if time.monotonic()>=deadline:
                raise BrowserVerificationRequired()
            time.sleep(0.5)

    def guard(self, page):
        self._check_origin(page)
        self._browser_verification(page)
        self._check_origin(page)
        if page.get_by_role('button',name='Разрешить и продолжить',exact=True).is_visible():
            raise SubmissionBlocked('Сайт требует отдельное согласие. Выполните действие вручную через browser-login.')

    @staticmethod
    def _check_origin(page):
        url = urlsplit(page.url)
        if url.scheme!='https' or url.hostname!='rabota.by':
            raise SubmissionBlocked('Переход за пределы rabota.by или на вход. Откройте browser-login.')

    def _only_visible(self, locator, label):
        candidates = [item for item in locator.all() if item.is_visible()]
        if len(candidates)!=1:
            raise SubmissionBlocked('Не удалось однозначно найти '+label+'. Разметка сайта требует проверки.')
        return candidates[0]

    def _continue_foreign_warning(self, page, before_continue):
        self.guard(page)
        title = page.get_by_text(FOREIGN_WARNING)
        self._only_visible(title,'предупреждение о другой стране')
        button = self._only_visible(page.get_by_role('button',name=FOREIGN_CONTINUE),
                                    'кнопку продолжения отклика в другой стране')
        if before_continue is None:
            raise SubmissionBlocked('Обнаружено предупреждение о другой стране. Проверка без отправки остановлена.')
        before_continue()
        button.click()
        title.wait_for(state='hidden',timeout=15000)

    def _open_response(self, page, resume_id, before_warning=None):
        buttons = page.get_by_role('button',name='Откликнуться',exact=True)
        links = page.locator(RESPONSE_LINK).filter(has_text=re.compile(r'^\s*Откликнуться\s*$'))
        visible = [control for control in buttons.or_(links).all()
                   if control.is_visible() and control.is_enabled()]
        if not visible:
            if page.get_by_text(SUCCESS_TEXT).filter(visible=True).count():
                raise SubmissionBlocked('На rabota.by уже есть отклик на эту вакансию.')
            if page.get_by_text(re.compile(r'^Вакансия в архиве$')).filter(visible=True).count():
                raise SubmissionBlocked('Работодатель перенёс вакансию в архив. Отклик недоступен.')
            raise SubmissionBlocked('На странице не найдено активное действие «Откликнуться». Откройте вакансию на rabota.by, чтобы проверить её состояние.')
        visible[0].click()
        self._browser_verification(page)
        form_title = page.get_by_text('Отклик на вакансию',exact=True)
        consent = page.get_by_role('button',name='Разрешить и продолжить',exact=True)
        warning = page.get_by_text(FOREIGN_WARNING)
        for _ in range(3):
            form_title.or_(consent).or_(warning).first.wait_for(state='visible',timeout=15000)
            if warning.is_visible():
                self._continue_foreign_warning(page,before_warning)
            elif consent.is_visible():
                text = page.locator('body').inner_text()
                countries = ('Россия','Казахстан','Кыргызстан','Азербайджан','Грузия')
                if self.env.get('RABOTA_FOREIGN_CONSENT')!='1' or not all(c in text for c in countries):
                    raise SubmissionBlocked('Нужно согласие на передачу данных за границу. Подтвердите его вручную.')
                consent.click()
                consent.wait_for(state='hidden',timeout=15000)
            else:
                break
        form_title.first.wait_for(state='visible',timeout=15000)
        self.guard(page)
        # Select the exact resume by its site ID, not by an ambiguous title.
        selected = page.get_by_role('button',name=re.compile(r'.+дизайнер.*',re.I))
        self._only_visible(selected,'выбор резюме').click()
        self.stage = 'выбор резюме по ID'
        radio = page.locator(f'input[type="radio"][value="{resume_id}"]')
        radio.wait_for(state='attached',timeout=10000)
        radio.locator('xpath=ancestor::label[1]').click()
        self.stage = 'поле сопроводительного письма'
        add_letter = page.get_by_role('button',name='Добавить сопроводительное',exact=True)
        if add_letter.is_visible():
            add_letter.click()
        field = page.locator('[data-qa="vacancy-response-popup-form-letter-input"]')
        field.wait_for(state='visible',timeout=10000)
        return field

    def _launch_browser(self, stack):
        from playwright.sync_api import sync_playwright
        stack.enter_context(ProcessLock(self.profile/'session'))
        playwright = stack.enter_context(sync_playwright())
        context = playwright.chromium.launch_persistent_context(
            str(self.profile), channel=self.channel if self.channel!='chromium' else None,
            headless=False, accept_downloads=False, chromium_sandbox=True)
        stack.callback(context.close)
        return context

    @contextmanager
    def _browser_context(self):
        with ExitStack() as stack:
            yield self._launch_browser(stack)

    def _preserve_verification_page(self, page):
        return False

    def send(self, package):
        resume_id = self.validate(package)
        submitted = False
        window_open = False
        try:
            with self._browser_context() as context:
                page = None
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.set_default_timeout(15000)
                    page.goto('https://rabota.by/vacancy/'+str(package['vacancy_id']),wait_until='domcontentloaded')
                    self.guard(page)
                    def before_warning():
                        nonlocal submitted
                        if package.get('_before_submit'):
                            package['_before_submit']()
                        # Treat continuation conservatively: the site may submit on this click.
                        submitted = True
                    field = self._open_response(page,resume_id,before_warning=before_warning)
                    field.fill(package['letter_text'])
                    if field.input_value()!=package['letter_text']:
                        raise SubmissionBlocked('Текст письма не совпал с пакетом.')
                    self.guard(page)
                    submit = self._only_visible(page.locator('[data-qa="vacancy-response-submit-popup"]'),'кнопку отправки формы')
                    if package.get('_before_submit'):
                        package['_before_submit']()
                    submitted = True  # Any failure after the click may be an actual submission.
                    submit.click()
                    self._browser_verification(page)
                    success = page.get_by_text(SUCCESS_TEXT)
                    warning = page.get_by_text(FOREIGN_WARNING)
                    success.or_(warning).first.wait_for(state='visible',timeout=20000)
                    if warning.is_visible():
                        self._continue_foreign_warning(page,before_warning)
                    success.first.wait_for(state='visible',timeout=20000)
                    self.guard(page)
                    receipt = {'vacancy_id':package['vacancy_id'],'resume_id':resume_id,
                        'job_id':package['job_id'],'revision':package['revision'],
                        'letter_text':package['letter_text'],
                        'letter_hash':hashlib.sha256(package['letter_text'].encode()).hexdigest(),
                        'confirmed_at':datetime.now(timezone.utc).isoformat()}
                    proof_dir = self.profile.parent/'submission-receipts'
                    proof_dir.mkdir(parents=True,exist_ok=True)
                    proof = proof_dir/f"job-{int(package['job_id'])}-v{int(package['revision'])}.png"
                    try:
                        page.screenshot(path=str(proof))
                        receipt['screenshot'] = str(proof)
                    except Exception:
                        pass  # A missing screenshot does not invalidate a visible success.
                    return receipt
                except BrowserVerificationRequired:
                    window_open = self._preserve_verification_page(page)
                    raise
                except Exception:
                    if page is not None and not page.is_closed():
                        try:
                            self._browser_verification(page)
                        except BrowserVerificationRequired:
                            window_open = self._preserve_verification_page(page)
                            raise
                    raise
        except BrowserVerificationRequired:
            raise BrowserVerificationRequired(outcome_unknown=submitted,window_open=window_open) from None
        except SubmissionBlocked:
            if submitted:
                raise RuntimeError('Submission outcome unknown') from None
            raise
        except Exception:
            if submitted:
                raise RuntimeError('Submission outcome unknown') from None
            raise SubmissionBlocked('Отклик не отправлялся. Проверьте вход, браузерную сессию и форму сайта. Если окно browser-login открыто, закройте его.') from None

    def check(self, package, *, status_only=False):
        """Verify the existing resume selection and response controls without submitting."""
        resume_id = self.validate(package)
        from playwright.sync_api import sync_playwright
        with ProcessLock(self.profile/'session'), sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(str(self.profile),
                channel=self.channel if self.channel!='chromium' else None,headless=False,
                accept_downloads=False,chromium_sandbox=True)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                self.stage = 'открытие вакансии'
                page.set_default_timeout(15000)
                page.goto('https://rabota.by/vacancy/'+str(package['vacancy_id']),wait_until='domcontentloaded')
                self.guard(page)
                if status_only:
                    actions = page.locator(RESPONSE_LINK)
                    return {
                        'vacancy_id': package['vacancy_id'],
                        'already_applied': page.get_by_text(SUCCESS_TEXT).filter(visible=True).count()>0,
                        'sign_in_visible': page.get_by_text('Войти',exact=True).filter(visible=True).count()>0,
                        'response_controls': [
                            {'tag':item.evaluate('element => element.tagName'),
                             'text':item.inner_text()[:100],'enabled':item.is_enabled()}
                            for item in actions.filter(visible=True).all()
                        ],
                        'submitted':False,
                    }
                self.stage = 'форма отклика'
                self._open_response(page,resume_id)
                submit = self._only_visible(page.locator('[data-qa="vacancy-response-submit-popup"]'),'кнопку отправки формы')
                if not submit.is_enabled():
                    raise SubmissionBlocked('Форма требует дополнительных действий. Отправка не выполнялась.')
                return {'resume_id':resume_id,'vacancy_id':package['vacancy_id'],'form_ready':True,'submitted':False}
            except Exception as exc:
                page.screenshot(path=str(self.profile.parent/'sender-check.png'))
                choices = [{'id':r.get_attribute('id'),'value':r.get_attribute('value')}
                           for r in page.get_by_role('radio').all()]
                visible_buttons = [{'text':b.inner_text(),'qa':b.get_attribute('data-qa'),'type':b.get_attribute('type')}
                    for b in page.get_by_role('button',name='Откликнуться',exact=True).all() if b.is_visible()]
                (self.profile.parent/'sender-check.json').write_text(json.dumps({'stage':getattr(self,'stage','unknown'),
                    'error_type':type(exc).__name__,'radio_choices':choices,'submit_buttons':visible_buttons},ensure_ascii=False),encoding='utf-8')
                if isinstance(exc,SubmissionBlocked):
                    raise
                raise SubmissionBlocked('Проверка формы остановлена: '+getattr(self,'stage','unknown')+'. Локальный снимок: data/sender-check.png. Отклик не отправлялся.') from None
            finally:
                context.close()


class SeriesBrowserSender(BrowserSender):
    """Keep Playwright and the profile lock on the same worker for a series of sends."""
    def __init__(self, env):
        super().__init__(env)
        self._worker = ThreadPoolExecutor(max_workers=1,thread_name_prefix='rabota-browser')
        self._operations = asyncio.Lock()
        self._stack = None
        self._context = None
        self._verification_page = None
        self._idle_timer = None
        self._last_activity = None

    @contextmanager
    def _browser_context(self):
        if self._context is not None and not self._context.pages:
            self._close_browser()
        if self._context is None:
            with ExitStack() as stack:
                self._context = self._launch_browser(stack)
                self._stack = stack.pop_all()
        yield self._context

    def _preserve_verification_page(self, page):
        self._verification_page = page
        return True

    def _close_browser(self):
        stack, self._stack = self._stack, None
        self._context = None
        self._verification_page = None
        if stack:
            stack.close()

    def _expire_window(self):
        if self._verification_page is None:
            self._close_browser()

    def _cancel_idle_timer(self):
        if self._idle_timer:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _schedule_idle_close(self, idle_minutes):
        self._cancel_idle_timer()
        if self._last_activity is not None:
            loop = asyncio.get_running_loop()
            self._idle_timer = loop.call_at(self._last_activity + idle_minutes*60,
                                            self._worker.submit,self._expire_window)

    async def _run(self, operation, idle_minutes, *args):
        async with self._operations:
            self._cancel_idle_timer()
            loop = asyncio.get_running_loop()
            try:
                return await loop.run_in_executor(self._worker,operation,*args)
            finally:
                self._last_activity = loop.time()
                self._schedule_idle_close(idle_minutes)

    async def send_in_series(self, package, idle_minutes):
        return await self._run(self.send,idle_minutes,package)

    def _check_manual_verification(self):
        page = self._verification_page
        if page is None:
            return
        if page.is_closed():
            self._close_browser()
            raise SubmissionBlocked('Окно проверки закрыто. Откройте профиль через browser-login, выполните проверку вручную и снова нажмите «Возобновить отправку».')
        try:
            self.guard(page)
        except BrowserVerificationRequired:
            raise BrowserVerificationRequired(window_open=True) from None
        self._verification_page = None

    async def resume_in_series(self, idle_minutes):
        await self._run(self._check_manual_verification,idle_minutes)

    async def set_idle_minutes(self, idle_minutes):
        async with self._operations:
            self._schedule_idle_close(idle_minutes)

    async def close_series(self):
        async with self._operations:
            self._cancel_idle_timer()
            try:
                await asyncio.get_running_loop().run_in_executor(self._worker,self._close_browser)
            finally:
                self._worker.shutdown(wait=False)


def make_sender(env, *, keep_open=False):
    from .automation import UnconfiguredSender
    mode = env.get('APPLICATION_SENDER','none').strip().lower()
    if mode=='none':
        return UnconfiguredSender()
    if mode!='browser':
        raise ValueError('APPLICATION_SENDER: none или browser.')
    return SeriesBrowserSender(env) if keep_open else BrowserSender(env)
