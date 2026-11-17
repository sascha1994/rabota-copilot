"""Interactive, separate browser profile. Credentials are entered by the user."""

import asyncio
from pathlib import Path

from .process_lock import ProcessLock


async def login(profile_dir, channel='chrome'):
    try:
        from playwright.async_api import async_playwright, Error
    except ImportError:
        raise ValueError('Установите браузерные зависимости: python -m pip install -r requirements-browser.lock') from None
    if channel not in ('chrome','msedge','chromium'):
        raise ValueError('BROWSER_CHANNEL: chrome, msedge или chromium.')
    profile = Path(profile_dir).resolve()
    profile.mkdir(parents=True,exist_ok=True)
    # Never reuse or copy the user's everyday browser profile.
    with ProcessLock(profile/'session'):
        async with async_playwright() as playwright:
            try:
                context = await playwright.chromium.launch_persistent_context(
                    str(profile),channel=channel if channel!='chromium' else None,
                    headless=False,accept_downloads=False,chromium_sandbox=True)
            except Error:
                raise ValueError('Браузер не запущен. Проверьте установленный Chrome/Edge и отсутствие второго процесса с этим профилем.') from None
            closed = asyncio.Event()
            context.on('close',lambda *args:closed.set())
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto('https://rabota.by/',wait_until='domcontentloaded',timeout=60000)
                print('Браузер открыт. При запросе пройдите проверку сайта вручную, войдите в аккаунт соискателя и закройте окно.',flush=True)
                await closed.wait()
                print('Браузер закрыт. Профиль сохранён локально; авторизация и форма отклика требуют отдельной проверки.',flush=True)
            except Error:
                if not closed.is_set():
                    raise ValueError('Не удалось открыть rabota.by. Проверьте сеть; сессия не подтверждена.') from None
            finally:
                await context.close()
