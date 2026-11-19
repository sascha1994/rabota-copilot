"""Private Telegram console for vacancy search and application submission."""

import asyncio
from datetime import datetime, timezone
import json
import logging
import re
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.token import TokenValidationError, validate_token

from .assessment import DIRECTIONS, AssessmentError, AssessmentService, LocalQuotaExceeded, load_vacancy_page, render_evaluation
from .automation import AutomationEngine, AutomationStore, MINSK
from .candidate_profile import ProfileStore
from .collector import search_key
from .config import SearchConfig
from .env import load_env  # noqa: F401
from .filter_editor import (
    CURRENCY_PRESETS,
    EXPERIENCE_PRESETS,
    FIELDS,
    REGION_PRESETS,
    WORK_FORMATS,
    describe_filters,
    edit_filter,
)
from .hh_client import ApiError
from .sources import make_collector, source_name
from .storage import Storage
from .tg_automation import CATEGORY_ICONS, MODE_BADGES, AutomationConsole
from .tg_profile import ProfileConsole
from .tg_resumes import ResumeConsole
from .tg_ui import esc, expandable, fmt_dt, fmt_salary, inline_kb, render
from .tg_vacancies import STATE_BADGES, STATES, VacancyConsole, vacancy_actions
from .resumes import snapshot
from .web_search import WebSearchError

HELP = (
    "🤖 <b>Работа BY · Личный пульт</b>\n\n"
    "Пришлите ссылку на вакансию rabota.by отдельным сообщением, чтобы добавить её в очередь.\n\n"
    "<b>Основные разделы (кнопки внизу экрана):</b>\n"
    "• <b>📥 Очередь</b> (<code>/queue</code>) — проверка, правка и отправка откликов\n"
    "• <b>🔍 Найти вакансии</b> (<code>/search</code>) — добавить новые вакансии в очередь\n"
    "• <b>⚙️ Автоматизация</b> (<code>/automation</code>) — режим, расписание и бюджеты ИИ\n"
    "• <b>👤 Профиль</b> (<code>/profile</code>) — подтверждение фактов и черновики резюме\n"
    "• <b>📊 Пульт</b> (<code>/status</code>) — сводка воронки, поиск, фильтры и бэкапы\n\n"
    + expandable(
        "Дополнительные команды:\n"
        "/search — запустить сбор вакансий за сутки\n"
        "/settings — поисковые фильтры\n"
        "/resumes — черновики резюме и экспорт TXT\n"
        "/tracked — вакансии с вашими отметками\n"
        "/insights — сводка частых навыков за день\n"
        "/history ID — история снимков описания вакансии\n"
        "/cancel — отменить текущий текстовый ввод\n"
        "/id — показать ваш Telegram ID",
        800,
    )
)

# Compact 2-row reply keyboard instead of 5 tall rows.
KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📥 Очередь"), KeyboardButton(text="🔍 Найти вакансии")],
        [
            KeyboardButton(text="⚙️ Автоматизация"),
            KeyboardButton(text="👤 Профиль"),
            KeyboardButton(text="📊 Пульт"),
        ],
    ],
    resize_keyboard=True,
)

ALIASES = {
    # Modern compact buttons
    "📥 Очередь": "/queue",
    "🔍 Найти вакансии": "/search",
    "🔍 Вакансии": "/queue",
    "⚙️ Автоматизация": "/automation",
    "👤 Профиль": "/profile",
    "📊 Пульт": "/status",
    # Legacy button labels kept for seamless backwards compatibility
    "Режим и расписание": "/automation",
    "Очередь откликов": "/queue",
    "Мои отметки": "/tracked",
    "Мои резюме": "/resumes",
    "Найти вакансии": "/search",
    "Вакансии": "/queue",
    "Статус": "/status",
    "Фильтры": "/settings",
    "Очистить вакансии": "/clear",
    "Профиль": "/profile",
}


def build_filter_menu() -> InlineKeyboardMarkup:
    items = [(label, f"filter:{field}") for field, (label, _) in FIELDS.items()]
    rows = [items[i : i + 2] for i in range(0, len(items), 2)]
    rows.append([("▶️ Запустить поиск", "nav:search"), ("📥 Очередь", "flow:categories:0")])
    rows.append([("📊 На главный пульт", "nav:status")])
    return inline_kb(rows)


FILTER_MENU = build_filter_menu()

CANCEL_EDIT = inline_kb([[("← Назад к фильтрам", "filter:cancel")]])


def work_format_keyboard(selected):
    items = [
        (("☑ " if code in selected else "☐ ") + label, "format:toggle:" + code)
        for code, label in WORK_FORMATS.items()
    ]
    rows = [items[i : i + 2] for i in range(0, len(items), 2)]
    rows.append([("🌍 Любой формат", "format:clear")])
    rows.append([("✅ Сохранить", "format:apply"), ("✖️ Отмена", "filter:cancel")])
    return inline_kb(rows)


def filter_field_keyboard(field: str, current_draft=()) -> InlineKeyboardMarkup:
    if field == "work_formats":
        return work_format_keyboard(current_draft)
    if field == "areas":
        return inline_kb([
            [(lbl, f"filter:set:areas:{val}") for lbl, val in REGION_PRESETS[:2]],
            [("🌍 Любой регион", "filter:clear_region")],
            [("← Назад к фильтрам", "filter:cancel")],
        ])
    if field == "experience":
        rows = [
            [(lbl, f"filter:set:experience:{val}") for lbl, val in EXPERIENCE_PRESETS[:2]],
            [(lbl, f"filter:set:experience:{val}") for lbl, val in EXPERIENCE_PRESETS[2:4]],
            [(EXPERIENCE_PRESETS[4][0], f"filter:set:experience:{EXPERIENCE_PRESETS[4][1]}")],
            [("← Назад к фильтрам", "filter:cancel")],
        ]
        return inline_kb(rows)
    if field == "currency":
        return inline_kb([
            [(lbl, f"filter:set:currency:{val}") for lbl, val in CURRENCY_PRESETS],
            [("← Назад к фильтрам", "filter:cancel")],
        ])
    if field == "salary":
        return inline_kb([
            [("2 000", "filter:set:salary:2000"), ("3 000", "filter:set:salary:3000"), ("4 000", "filter:set:salary:4000")],
            [("✖️ Без порога зарплаты", "filter:set:salary:нет")],
            [("← Назад к фильтрам", "filter:cancel")],
        ])
    if field in ("excluded_title_words", "excluded_employer_ids"):
        return inline_kb([
            [("✖️ Очистить список", f"filter:set:{field}:нет")],
            [("← Назад к фильтрам", "filter:cancel")],
        ])
    return CANCEL_EDIT


def admin_id_from_env(env):
    raw = env.get("ADMIN_ID", "").strip()
    if not raw:
        return None
    if not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
        raise ValueError("ADMIN_ID должен быть положительным числовым Telegram ID")
    return int(raw)


def is_owner(message, admin_id):
    return (
        admin_id is not None
        and message.chat.type == "private"
        and message.from_user is not None
        and message.from_user.id == admin_id
        and message.chat.id == admin_id
    )


def vacancy_card(item):
    title = esc(item.get("name") or "Вакансия", 260)
    employer = esc((item.get("employer") or {}).get("name") or "—", 180)
    area = esc((item.get("area") or {}).get("name") or "—", 120)
    pay = fmt_salary(item.get("salary"))
    pub = fmt_dt(item.get("published_at"))
    url = str(item.get("alternate_url") or "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ("rabota.by", "hh.ru"):
        url = ""

    lines = [
        f"💼 <b>{title}</b>",
        f"🏢 {employer} · 📍 {area}",
        f"💰 Зарплата: <b>{pay}</b> · 🕒 {pub}",
    ]
    state = item.get("_tracking_state")
    if state:
        badge = STATE_BADGES.get(state, esc(STATES.get(state, "—")))
        lines.append(f"🏷 Отметка: <b>{badge}</b>")
    if url:
        lines.append(f'🔗 <a href="{esc(url, 500)}">Открыть на rabota.by ↗</a>')
    return "\n".join(lines)


class SearchService:
    def __init__(self, config_path, db_path, user_agent, access_token="", env=None):
        self.config_path, self.db_path, self.user_agent = config_path, db_path, user_agent
        self.stop = threading.Event()
        self.access_token = access_token
        self.env = dict(env) if env is not None else {"HH_USER_AGENT": user_agent, "HH_ACCESS_TOKEN": access_token}
        self.last_web_search = None

    def client(self):
        return make_collector(self.env, None, SearchConfig.load(self.config_path))

    def collect(self, config=None):
        if source_name(self.env) == "web":
            if self.last_web_search is not None and time.monotonic() - self.last_web_search < 60:
                raise WebSearchError("Публичный поиск можно запускать не чаще раза в минуту. Подождите и повторите.")
            self.last_web_search = time.monotonic()
        config = config or SearchConfig.load(self.config_path)
        store = AutomationStore(self.db_path)
        try:
            _, digest = snapshot(self.db_path)
            queued = 0

            def enqueue(item, key):
                nonlocal queued
                queued += store.insert_job(item, key, digest)

            collector = make_collector(self.env, store.storage, config, should_stop=self.stop.is_set)
            stats = collector.collect(on_match=enqueue)
            stats.queued = queued
            return stats
        finally:
            store.close()

    def add_link(self, vid):
        store = AutomationStore(self.db_path)
        try:
            query = 'SELECT id FROM application_jobs WHERE vacancy_id=?'
            existing = store.db.execute(query, (vid,)).fetchone()
            if existing:
                return existing['id'], False
            saved = store.db.execute('SELECT raw_json FROM vacancies WHERE id=?', (vid,)).fetchone()
            if saved:
                item = json.loads(saved['raw_json'])
            else:
                title, _ = load_vacancy_page(vid)
                item = {'id': vid, 'name': title, 'alternate_url': 'https://rabota.by/vacancy/' + vid}
            key = search_key(SearchConfig.load(self.config_path))
            _, digest = snapshot(self.db_path)
            with store.db:
                store.db.execute('BEGIN IMMEDIATE')
                added = store.insert_job(item, key, digest)
                job = store.db.execute(query, (vid,)).fetchone()
                if added and not store.db.execute('SELECT 1 FROM vacancies WHERE id=?', (vid,)).fetchone():
                    store.storage.save(item, 'manual', None, datetime.now(timezone.utc).isoformat())
            return job['id'] if job else None, bool(added)
        finally:
            store.close()

    def read(self, limit=5, config=None):
        config = config or SearchConfig.load(self.config_path)
        storage = Storage(self.db_path)
        try:
            count, cursor = storage.summary(search_key(config))
            if source_name(self.env) == "web":
                cursor = storage.cursor("web:" + search_key(config))
            return config, storage.results(search_key(config), limit), count, cursor
        finally:
            storage.close()

    def clear(self, key):
        storage = Storage(self.db_path)
        try:
            return storage.clear_results(key)
        finally:
            storage.close()


class BotController:
    def __init__(self, service, admin_id, *, keep_browser_open=False):
        self.service, self.admin_id = service, admin_id
        self.search_task = None
        self.last_result = "Сбор ещё не запускался в этом процессе."
        self.pending_field = None
        self.edit_lock = asyncio.Lock()
        self.work_format_draft = ()
        self.clear_confirmation = None
        self.clearing = False
        self.profile = ProfileConsole(service.db_path)
        self.analysis_task = None
        self.backup_status = "Резервное копирование ещё не запускалось."
        self.resumes = ResumeConsole(service.db_path, service.env)
        self.vacancy_console = VacancyConsole(service.db_path)
        self.automation_engine = AutomationEngine(service, keep_browser_open=keep_browser_open)
        self.automation = AutomationConsole(service.db_path, self.automation_engine)

    async def assess_vacancy(self, message, vid):
        progress = None
        if getattr(message, "bot", None) is not None:
            progress = await render(message, f"⏳ <b>Оцениваю вакансию #{esc(vid)} через OpenAI…</b>")
        try:
            service = AssessmentService(self.service.db_path, self.service.env)
            result, cached = await asyncio.to_thread(service.evaluate, vid)
            action_row = [("📄 Подготовить резюме", f"prepare:{vid}")]
            if service.description_id:
                action_row.append((f"🕒 Описание #{service.description_id}", f"description:{service.description_id}"))
            await render(
                progress or message,
                render_evaluation(result, cached),
                edit=bool(progress),
                reply_markup=inline_kb([action_row]),
            )
        except LocalQuotaExceeded as exc:
            await render(
                progress or message,
                f"⚠️ {esc(exc)}",
                edit=bool(progress),
                reply_markup=inline_kb([[("⚙️ Изменить лимиты ИИ", "flow:settings:0")]]),
            )
        except AssessmentError as exc:
            await render(progress or message, f"⚠️ {esc(exc)}", edit=bool(progress))
        except Exception:
            await render(
                progress or message,
                "❌ Не удалось выполнить оценку. Секреты и ответ провайдера скрыты; проверьте настройки и повторите позже.",
                edit=bool(progress),
            )

    async def ask_clear(self, message, *, edit=False):
        if self.busy or self.clearing or (self.analysis_task and not self.analysis_task.done()):
            await render(message, "⏳ Дождитесь завершения поиска, оценки или очистки, затем повторите.")
            return
        config, _, count, _ = await asyncio.to_thread(self.service.read, 1)
        token = secrets.token_hex(6)
        self.clear_confirmation = (token, search_key(config), time.monotonic())
        text = (
            "🗑 <b>Очистка поисковой выдачи</b>\n\n"
            f"Удалить из текущего списка вакансий: <b>{count}</b>?\n\n"
            "<i>Настройки фильтров, профиль, очередь откликов и ваши отметки сохранятся. "
            "При следующем поиске эти вакансии смогут снова попасть в список.</i>"
        )
        kb = inline_kb([[
            ("🗑 Да, очистить", f"clear:{token}:yes"),
            ("✖️ Отмена", f"clear:{token}:no"),
        ]])
        await render(message, text, edit=edit, reply_markup=kb)

    async def handle_clear_callback(self, callback):
        parts = callback.data.split(":")
        confirmation = self.clear_confirmation
        if len(parts) != 3 or not confirmation or parts[1] != confirmation[0] or time.monotonic() - confirmation[2] > 600:
            await callback.answer("Подтверждение устарело. Запросите очистку заново.", show_alert=True)
            return
        if parts[2] == "no":
            self.clear_confirmation = None
            await callback.answer("Очистка отменена")
            if getattr(callback.message, "bot", None) is not None:
                await self.show_dashboard(callback.message, edit=True)
            return
        if parts[2] != "yes":
            await callback.answer()
            return
        if self.busy or self.clearing or (self.analysis_task and not self.analysis_task.done()):
            await callback.answer("Дождитесь завершения поиска, оценки или очистки.", show_alert=True)
            return
        try:
            if search_key(SearchConfig.load(self.service.config_path)) != confirmation[1]:
                self.clear_confirmation = None
                await callback.answer("Фильтры изменились. Запросите очистку заново.", show_alert=True)
                return
            self.clearing = True
            self.clear_confirmation = None
            await callback.answer("Очищаю список…")
            count = await asyncio.to_thread(self.service.clear, confirmation[1])
            self.last_result = "Список текущего поиска очищен. Можно запустить новый поиск."
            await render(
                callback.message,
                f"✅ <b>Список очищен.</b> Удалено карточек из выдачи: <b>{count}</b>. Фильтры сохранены.",
                edit=True,
                reply_markup=inline_kb([[("▶️ Запустить поиск", "nav:search"), ("📊 Пульт", "nav:status")]]),
            )
        except Exception:
            await render(callback.message, "❌ Не удалось завершить очистку. Проверьте список и повторите позже.")
        finally:
            self.clearing = False

    async def show_filters(self, message, *, edit=False):
        config = SearchConfig.load(self.service.config_path)
        text = (
            describe_filters(config, source_name(self.service.env))
            + "\n\n<i>Выберите параметр для изменения. Новые фильтры применяются со следующего поиска.</i>"
        )
        await render(message, text, edit=edit, reply_markup=FILTER_MENU)

    async def show_dashboard(self, message, *, edit=False):
        _, _, count, cursor = await asyncio.to_thread(self.service.read, 1)
        search_state = "🔄 выполняется…" if self.busy else "🟢 готов"
        last_collect = fmt_dt(cursor, tz=MINSK, suffix="Минск") if cursor else "ещё не было сбора"

        extra_blocks = []
        db_path = getattr(self.service, "db_path", None)
        if isinstance(db_path, (str, Path)) and Path(db_path).is_file():
            try:
                store = AutomationStore(db_path)
                profile = ProfileStore(db_path)
                try:
                    settings = store.settings()
                    found, processed = store.daily_vacancy_counts()
                    q_counts = dict(store.db.execute("SELECT state,count(*) FROM application_jobs GROUP BY state"))
                    facts = profile.all()
                finally:
                    store.close()
                    profile.close()
                review_n = q_counts.get("REVIEW", 0) + q_counts.get("READY", 0)
                attn_n = sum(q_counts.get(s, 0) for s in ("BLOCKED", "FAILED", "STALE", "UNKNOWN"))
                sent_n = q_counts.get("SENT", 0)
                conf_n = sum(f["status"] == "CONFIRMED" for f in facts)
                extra_blocks.append(
                    f"⚙️ <b>Автоматизация:</b> {MODE_BADGES[settings['mode']]} · <b>{esc(settings['time'])}</b>\n"
                    f"📦 <b>Сегодня:</b> найдено <b>{found}</b> · обработано <b>{processed}</b>\n"
                    f"📥 <b>Очередь:</b> 🟡 проверка <b>{review_n}</b> · ⚠️ внимание <b>{attn_n}</b> · ✅ отправлено <b>{sent_n}</b>\n"
                    f"👤 <b>Профиль:</b> подтверждено <b>{conf_n}/{len(facts)}</b> фактов"
                )
            except Exception:
                pass

        sections = [
            "📊 <b>Пульт управления · Работа BY</b>",
            f"🔍 <b>Поиск:</b> {search_state}\n"
            f"🗂 <b>Вакансий по фильтру:</b> <b>{count}</b>\n"
            f"🕒 <b>Последний сбор:</b> {last_collect}",
            *extra_blocks,
            f"📝 <b>Событие:</b> {esc(self.last_result, 350)}\n"
            f"💾 <b>Бэкап:</b> {esc(self.backup_status, 200)}",
        ]
        kb = inline_kb([
            [("▶️ Запустить поиск", "nav:search"), ("🎛 Фильтры", "nav:settings")],
            [("⚙️ Настройки автоматизации", "flow:settings:0")],
            [("📥 Очередь", "flow:categories:0"), ("📈 Сводка дня", "insight:open:0")],
            [("⭐ Мои отметки", "nav:tracked:1"), ("📄 Мои резюме", "resume:overview:0")],
            [("🔃 Обновить", "nav:status")],
        ])
        await render(message, "\n\n".join(sections), edit=edit, reply_markup=kb)

    async def show_insights(self, message, *, edit=False):
        from .daily_insights import DailyInsights

        insights = DailyInsights(self.service.db_path)
        store = AutomationStore(self.service.db_path)
        try:
            cutoff = store.settings()["insights_time"]
            if datetime.now(timezone.utc).astimezone(MINSK).strftime("%H:%M") < cutoff:
                await render(
                    message,
                    f"🌙 <b>Вечерняя сводка</b>\n\n"
                    f"Отчёт будет сформирован в <b>{esc(cutoff)}</b> по Минску. "
                    "Изменить время доставки можно в разделе ⚙️ Автоматизация.",
                    edit=edit,
                    reply_markup=inline_kb([[("⚙️ Автоматизация", "flow:settings:0")]]),
                )
                return
        finally:
            store.close()
        await insights.show(message, edit=edit)

    async def show_tracked(self, message, page=1):
        rows = await asyncio.to_thread(self.vacancy_console.tracked, page)
        if not rows:
            await render(
                message,
                "⭐ <b>Мои отметки</b>\n\nНа этой странице отмеченных вакансий пока нет.",
                reply_markup=inline_kb([[("📥 В очередь", "flow:categories:0")]]),
            )
            return
        await render(message, f"⭐ <b>Мои отметки · страница {page}</b>")
        for item in rows[:5]:
            await render(
                message,
                vacancy_card(item),
                reply_markup=vacancy_actions(str(item["id"]), item.get("_tracking_state")),
            )
        nav_row = []
        if page > 1:
            nav_row.append(("← Стр. " + str(page - 1), f"nav:tracked:{page - 1}"))
        if len(rows) > 5:
            nav_row.append(("Стр. " + str(page + 1) + " →", f"nav:tracked:{page + 1}"))
        if nav_row:
            await render(
                message,
                f"Навигация отметок · страница {page}",
                reply_markup=inline_kb([nav_row, [("📊 Пульт", "nav:status")]]),
            )

    async def start_search(self, message):
        self.profile.pending = None
        if self.busy or self.clearing:
            await render(message, "⏳ Поиск или очистка уже выполняется. Проверить состояние: /status")
            return
        self.service.client()
        config = SearchConfig.load(self.service.config_path)
        self.pending_field = None
        self.search_task = asyncio.create_task(self.search(message, config))
        await render(message, "🔎 <b>Поиск запущен в фоне.</b>\nДобавлю новые вакансии в очередь и сообщу результат.")

    async def handle_callback(self, callback: CallbackQuery):
        message = callback.message
        if (
            not isinstance(message, Message)
            or message.chat.type != "private"
            or callback.from_user.id != self.admin_id
            or message.chat.id != self.admin_id
        ):
            await callback.answer("Нет доступа", show_alert=True)
            return
        data = callback.data or ""

        if data.startswith("nav:"):
            self.pending_field = self.profile.pending = self.resumes.pending = self.resumes.pending_letter = None
            self.automation.pending = None
            parts = data.split(":")
            target = parts[1] if len(parts) > 1 else ""
            await callback.answer()
            if target == "status":
                await self.show_dashboard(message, edit=True)
            elif target == "settings":
                await self.show_filters(message, edit=True)
            elif target == "clear":
                await self.ask_clear(message, edit=True)
            elif target == "search":
                await self.start_search(message)
            elif target == "vacancies" and len(parts) == 3 and parts[2].isdigit():
                await self.automation.queue(message, edit=True)
            elif target == "tracked" and len(parts) == 3 and parts[2].isdigit():
                page = int(parts[2])
                if 1 <= page <= 200:
                    await self.show_tracked(message, page)
            return

        if data.startswith("flow:"):
            async with self.edit_lock:
                self.pending_field = self.profile.pending = self.resumes.pending = self.resumes.pending_letter = None
                self.automation.pending = None
                try:
                    parts = data.split(":")
                    if len(parts) == 4 and parts[1] == "send":
                        if self.busy or self.clearing or (self.analysis_task and not self.analysis_task.done()):
                            await callback.answer("Дождитесь текущей операции.", show_alert=True)
                            return
                        ident, revision = int(parts[2]), int(parts[3])
                        self.analysis_task = asyncio.create_task(self.send_package(message, ident, revision))
                        await callback.answer("Проверяю пакет и открываю браузер…")
                    elif len(parts) == 4 and parts[1] in ("forcesend", "forceprep"):
                        if self.busy or self.clearing or (self.analysis_task and not self.analysis_task.done()):
                            await callback.answer("Дождитесь текущей операции.", show_alert=True)
                            return
                        ident, direction = int(parts[2]), parts[3]
                        if direction not in DIRECTIONS:
                            raise ValueError("Неизвестное направление резюме.")
                        send_now = parts[1] == "forcesend"
                        self.analysis_task = asyncio.create_task(
                            self.force_action(message, ident, direction, send_now=send_now)
                        )
                        await callback.answer(
                            "Готовлю письмо и отправляю отклик… 🚀" if send_now else "Готовлю сопроводительное письмо… 📝"
                        )
                    else:
                        await self.automation.callback(callback)
                except ValueError as exc:
                    await callback.answer(str(exc)[:180], show_alert=True)
                except Exception:
                    await render(message, "❌ Не удалось выполнить действие. Откройте /queue.")
            return

        if data.startswith("insight:"):
            if data == "insight:open:0":
                await callback.answer()
                await self.show_insights(message, edit=True)
                return
            from .daily_insights import DailyInsights

            try:
                await DailyInsights(self.service.db_path).add(callback, self.admin_id)
            except (ValueError, IndexError, KeyError):
                await callback.answer("Предложение недоступно. Откройте /insights.", show_alert=True)
            return

        self.automation.pending = None
        if data.startswith(("mark:", "history:", "description:")):
            try:
                await self.vacancy_console.callback(callback)
            except Exception:
                await render(message, "❌ Не удалось сохранить отметку или открыть описание. Повторите позже.")
            return

        if data.startswith("letter:"):
            async with self.edit_lock:
                self.pending_field = self.profile.pending = self.resumes.pending = None
                self.resumes.pending_letter = None
                try:
                    parts = data.split(":")
                    if len(parts) == 3 and parts[1] == "generate":
                        if not parts[2].isascii() or not parts[2].isdigit() or len(parts[2]) > 15:
                            raise ValueError("Некорректная версия резюме.")
                        if self.clearing or (self.analysis_task and not self.analysis_task.done()):
                            await callback.answer("Дождитесь завершения текущей операции.", show_alert=True)
                            return
                        self.analysis_task = asyncio.create_task(self.resumes.generate_letter(message, int(parts[2])))
                        await callback.answer("Подготовка письма запущена")
                    else:
                        await self.resumes.letter_callback(callback, self.admin_id)
                except ValueError as exc:
                    await callback.answer(str(exc)[:180], show_alert=True)
                except Exception:
                    await render(message, "❌ Не удалось открыть письмо. Повторите через /resumes.")
            return

        self.resumes.pending_letter = None
        if data.startswith(("resume:", "prepare:", "draft:")):
            async with self.edit_lock:
                self.pending_field = None
                self.profile.pending = None
                try:
                    if data.startswith("prepare:"):
                        self.resumes.pending = None
                        await self.resumes.choose(callback)
                    elif data.startswith("draft:"):
                        self.resumes.pending = None
                        parts = data.split(":")
                        if (
                            len(parts) != 3
                            or parts[1] not in (*DIRECTIONS, "ai")
                            or not parts[2].isascii()
                            or not parts[2].isdigit()
                            or len(parts[2]) > 15
                        ):
                            await callback.answer("Некорректные параметры")
                            return
                        if self.clearing or (self.analysis_task and not self.analysis_task.done()):
                            await callback.answer("Дождитесь завершения текущей операции.", show_alert=True)
                            return
                        self.analysis_task = asyncio.create_task(self.resumes.generate(message, parts[1], parts[2]))
                        await callback.answer("Подготовка черновика запущена")
                    else:
                        await self.resumes.callback(callback, self.admin_id)
                except Exception:
                    await render(message, "❌ Не удалось открыть резюме. Повторите /resumes позже.")
            return

        self.resumes.pending = None
        if data.startswith("profile:"):
            async with self.edit_lock:
                self.pending_field = None
                try:
                    await self.profile.callback(callback, self.admin_id)
                except Exception:
                    await render(callback.message, "❌ Не удалось открыть профиль. Повторите /profile позже.")
            return

        if data.startswith("assess:"):
            vid = data.partition(":")[2]
            if not vid.isascii() or not vid.isdigit() or len(vid) > 15:
                await callback.answer("Некорректная вакансия", show_alert=True)
                return
            if self.analysis_task and not self.analysis_task.done():
                await callback.answer("Одна оценка уже выполняется. Дождитесь результата.", show_alert=True)
                return
            if self.clearing:
                await callback.answer("Дождитесь очистки списка.", show_alert=True)
                return
            self.analysis_task = asyncio.create_task(self.assess_vacancy(message, vid))
            await callback.answer("Оценка запущена 🧠")
            return

        self.profile.pending = None
        if data.startswith("format:"):
            await self.handle_format_callback(callback)
            return
        if data.startswith("clear:"):
            await self.handle_clear_callback(callback)
            return
        if not data or not data.startswith("filter:"):
            await callback.answer()
            return

        field_part = data.partition(":")[2]
        async with self.edit_lock:
            if field_part.startswith("set:"):
                _, target_field, raw_val = field_part.split(":", 2)
                if target_field not in FIELDS:
                    await callback.answer("Неизвестный параметр", show_alert=True)
                    return
                try:
                    config = SearchConfig.load(self.service.config_path)
                    edit_filter(config, target_field, raw_val).save(self.service.config_path)
                except (OSError, ValueError) as exc:
                    await callback.answer(str(exc)[:180], show_alert=True)
                    return
                self.pending_field = None
                await callback.answer("Фильтр обновлён ✅")
                await self.show_filters(message, edit=True)
            elif field_part == "clear_region":
                if self.pending_field != "areas":
                    await callback.answer("Откройте «Регион» заново.", show_alert=True)
                    return
                try:
                    config = SearchConfig.load(self.service.config_path)
                    edit_filter(config, "areas", "").save(self.service.config_path)
                except (OSError, ValueError):
                    await callback.answer("Не удалось сохранить регион. Повторите позже.", show_alert=True)
                    return
                self.pending_field = None
                await callback.answer("Ограничение по региону снято")
                await self.show_filters(message, edit=True)
            elif field_part == "cancel":
                self.pending_field = None
                await callback.answer("Ввод отменён")
                await self.show_filters(message, edit=True)
            elif field_part in FIELDS:
                self.pending_field = field_part
                await callback.answer()
                if field_part == "work_formats":
                    self.work_format_draft = SearchConfig.load(self.service.config_path).work_formats
                kb = filter_field_keyboard(field_part, self.work_format_draft)
                label, hint = FIELDS[field_part]
                await render(
                    message,
                    f"🎛 <b>{esc(label)}</b>\n\n{esc(hint)}\n\n<i>Отмена ввода: /cancel</i>",
                    edit=True,
                    reply_markup=kb,
                )
            else:
                await callback.answer("Неизвестный параметр", show_alert=True)

    async def handle_format_callback(self, callback):
        async with self.edit_lock:
            if self.pending_field != "work_formats":
                await callback.answer("Откройте «Формат работы» заново.", show_alert=True)
                return
            action = callback.data.removeprefix("format:")
            if action.startswith("toggle:"):
                code = action.removeprefix("toggle:")
                if code not in WORK_FORMATS:
                    await callback.answer("Неизвестный формат", show_alert=True)
                    return
                selected = set(self.work_format_draft)
                selected.symmetric_difference_update({code})
                self.work_format_draft = tuple(x for x in WORK_FORMATS if x in selected)
            elif action == "clear":
                if not self.work_format_draft:
                    await callback.answer("Уже выбран любой формат")
                    return
                self.work_format_draft = ()
            elif action == "apply":
                try:
                    config = SearchConfig.load(self.service.config_path)
                    text = ",".join(self.work_format_draft) or "нет"
                    edit_filter(config, "work_formats", text).save(self.service.config_path)
                except (OSError, ValueError):
                    await callback.answer("Не удалось сохранить. Повторите позже.", show_alert=True)
                    return
                self.pending_field = None
                await callback.answer("Формат работы сохранён ✅")
                await self.show_filters(callback.message, edit=True)
                return
            else:
                await callback.answer("Неизвестное действие", show_alert=True)
                return
            await callback.answer()
            await callback.message.edit_reply_markup(reply_markup=work_format_keyboard(self.work_format_draft))

    async def save_pending_filter(self, message):
        async with self.edit_lock:
            if self.pending_field is None:
                return
            if message.text is None:
                await render(message, "Отправьте значение текстом или нажмите /cancel.")
                return
            try:
                config = SearchConfig.load(self.service.config_path)
                updated = edit_filter(config, self.pending_field, message.text)
                updated.save(self.service.config_path)
            except ValueError as exc:
                err_msg = str(exc) if not isinstance(exc, json.JSONDecodeError) else "Не удалось прочитать файл фильтров."
                await render(message, f"⚠️ {esc(err_msg)}")
                return
            except OSError:
                await render(message, "❌ Не удалось сохранить фильтры. Старый файл сохранён; повторите ввод позже.")
                return
            self.pending_field = None
            await message.answer("✅ Фильтр сохранён и применится к следующему поиску.", reply_markup=KEYBOARD)
            await self.show_filters(message)

    @property
    def busy(self):
        return self.search_task is not None and not self.search_task.done()

    async def search(self, message, config=None):
        try:
            stats = (
                await asyncio.to_thread(self.service.collect, config)
                if config is not None
                else await asyncio.to_thread(self.service.collect)
            )
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            self.last_result = (
                f"Последний сбор: {stamp} · получено: {stats.received}, "
                f"добавлено в очередь: {stats.queued}, исключено: {stats.rejected}."
            )
            if stats.partial:
                self.last_result += " Достигнут лимит страниц (показана часть выдачи)."
            if config is not None:
                self.last_result += f" Запрос: {config.text}"
            summary_html = (
                "✅ <b>Сбор вакансий завершён</b>\n\n"
                f"🔍 Проверено: <b>{stats.received}</b> · 📥 Добавлено в очередь: <b>{stats.queued}</b> · 🚫 Исключено: <b>{stats.rejected}</b>"
            )
            if stats.partial:
                summary_html += "\n⚠️ <i>Достигнут лимит страниц — сузьте фильтры для более полного охвата.</i>"
            if config is not None:
                summary_html += f"\n🔎 Запрос: <code>{esc(config.text, 120)}</code>"
            await render(message, summary_html, reply_markup=inline_kb([[("📥 Открыть очередь", "flow:categories:0")]]))
        except Exception as exc:
            # Do not forward raw exceptions: network libraries may include a token URL.
            self.last_result = (
                exc.public_message
                if isinstance(exc, ApiError)
                else f"Последняя операция завершилась ошибкой ({type(exc).__name__})."
            )
            try:
                await render(
                    message,
                    f"⚠️ <b>{esc(self.last_result)}</b>\n<i>Ранее сохранённые данные остаются в базе.</i>",
                )
            except TelegramAPIError:
                pass

    async def handle(self, message: Message):
        if message.chat.type != "private" or message.from_user is None:
            return
        raw_text = message.text or ""
        mapped = ALIASES.get(raw_text, raw_text)
        parts = mapped.split()
        command = parts[0].split("@")[0].lower() if parts else ""
        if command == "/id":
            await render(message, f"🆔 Ваш Telegram ID: <code>{message.from_user.id}</code>")
            return
        if not is_owner(message, self.admin_id):
            await render(
                message,
                f"🔒 <b>Доступ ограничен.</b>\nВаш Telegram ID: <code>{message.from_user.id}</code>\n"
                "Укажите его в <code>ADMIN_ID</code> файла <code>.env</code> и перезапустите бота.",
            )
            return

        if self.automation.pending is not None:
            if not raw_text.startswith("/") and raw_text not in ALIASES:
                async with self.edit_lock:
                    await self.automation.save_text(message)
                return
            self.automation.pending = None

        if command == "/insights":
            await self.show_insights(message)
            return

        if command in ("/automation", "/queue", "/vacancies"):
            self.pending_field = self.profile.pending = self.resumes.pending = self.resumes.pending_letter = None
            try:
                await (self.automation.settings(message) if command == "/automation" else self.automation.queue(message))
            except Exception:
                await render(message, "❌ Не удалось открыть настройки или очередь. Повторите позже.")
            return

        if command in ("/tracked", "/history"):
            self.resumes.pending_letter = self.resumes.pending = self.profile.pending = self.pending_field = None
            try:
                if command == "/history":
                    if len(parts) != 2:
                        raise ValueError("Используйте /history ID_вакансии.")
                    await self.vacancy_console.history(message, parts[1])
                else:
                    page = int(parts[1]) if len(parts) == 2 else 1
                    if len(parts) > 2 or not 1 <= page <= 200:
                        raise ValueError("Используйте /tracked или /tracked 2 (1–200).")
                    await self.show_tracked(message, page)
            except ValueError as exc:
                await render(message, f"⚠️ {esc(exc)}")
            except Exception:
                await render(message, "❌ Не удалось открыть историю. Повторите позже.")
            return

        if self.resumes.pending_letter is not None:
            if not raw_text.startswith("/") and raw_text not in ALIASES:
                async with self.edit_lock:
                    await self.resumes.save_letter(message)
                return
            self.resumes.pending_letter = None

        if self.resumes.pending is not None:
            if not raw_text.startswith("/") and raw_text not in ALIASES:
                async with self.edit_lock:
                    await self.resumes.save_text(message)
                return
            self.resumes.pending = None

        if command == "/resumes":
            self.pending_field = None
            self.profile.pending = None
            await self.resumes.overview(message)
            return

        if self.profile.pending and not raw_text.startswith("/") and raw_text not in ALIASES:
            async with self.edit_lock:
                try:
                    await self.profile.save_text(message, self.admin_id)
                except Exception:
                    await render(message, "❌ Не удалось сохранить профиль. Повторите позже.")
            return

        if self.pending_field and command not in (
            "/search",
            "/settings",
            "/start",
            "/help",
            "/status",
            "/cancel",
            "/clear",
            "/profile",
        ):
            if raw_text.startswith("/"):
                await render(message, "Сейчас ожидается значение фильтра. Для отмены нажмите /cancel.")
                return
            await self.save_pending_filter(message)
            return

        try:
            if command == "/profile":
                self.pending_field = None
                self.profile.pending = None
                await self.profile.overview(message)
                return
            if command != "/cancel":
                self.profile.pending = None
            if command == "/cancel":
                if self.profile.pending:
                    self.profile.pending = None
                    await render(message, "✖️ Изменение профиля отменено.")
                    await self.profile.overview(message)
                    return
                self.pending_field = None
                self.clear_confirmation = None
                await message.answer("✖️ Ввод отменён.", reply_markup=KEYBOARD)
                await self.show_filters(message)
                return
            if command in ("/start", "/help"):
                self.profile.pending = None
                self.pending_field = None
                await render(message, HELP, reply_markup=KEYBOARD)
            elif command == "/search":
                await self.start_search(message)
            elif raw_text.strip().lower().startswith(("http://", "https://", "rabota.by/", "www.rabota.by/")):
                match = re.fullmatch(r'(?:https?://)?(?:www\.)?rabota\.by/vacancy/([0-9]{1,15})/?(?:[?#]\S*)?',
                                     raw_text.strip(), re.IGNORECASE)
                if not match:
                    await render(message, "Пришлите одну ссылку вида https://rabota.by/vacancy/123456.")
                    return
                try:
                    ident, added = await asyncio.to_thread(self.service.add_link, match[1])
                except AssessmentError as exc:
                    await render(message, esc(str(exc)))
                    return
                if ident is None:
                    await render(message, "Вакансия ранее пропущена или отмечена как отклик вручную.")
                    return
                status = "Добавлено в очередь" if added else "Уже в очереди"
                await render(message, f"{status} · задание #{ident}",
                             reply_markup=inline_kb([[("Открыть карточку", f"flow:view:{ident}")]]))
            elif command == "/clear":
                self.pending_field = None
                await self.ask_clear(message)
            elif command == "/settings":
                self.profile.pending = None
                self.pending_field = None
                await self.show_filters(message)
            elif command == "/status":
                await self.show_dashboard(message)
            else:
                await message.answer("Выберите раздел на нижней панели или отправьте /help.", reply_markup=KEYBOARD)
        except Exception as exc:
            hint = (
                "Проверьте файл фильтров и настройки источника в .env."
                if isinstance(exc, ValueError)
                else "Повторите позже и проверьте подключение."
            )
            await render(message, f"⚠️ <b>Не удалось выполнить команду</b> (<code>{type(exc).__name__}</code>).\n{esc(hint)}")

    async def send_package(self, message, ident, revision):
        try:
            await render(
                message,
                f"📤 <b>Отправка пакета #{ident} (вер. #{revision})</b>\n"
                "Проверяю актуальность вакансии и открываю Chrome. Итог придёт отдельным сообщением.",
            )
            result = await self.automation_engine.send(ident, revision)
            await render(message, esc(result))
            await self.automation.show(message, ident)
        except ValueError as exc:
            await render(message, f"⚠️ {esc(exc)}")
        except Exception:
            await render(message, "⚠️ Операция не завершена. Проверьте /queue перед любым повтором.")

    async def force_action(self, message, ident, direction, *, send_now=True):
        from .assessment import ROLES

        status_msg = None
        prepared_rev = None
        try:
            if send_now:
                store = AutomationStore(self.service.db_path)
                try:
                    settings = store.settings()
                finally:
                    store.close()
                if settings["browser_hold"]:
                    raise ValueError("Отправка ждёт проверки браузера. Откройте ⚙️ Автоматизация.")
                if settings["mode"] == "paused":
                    raise ValueError("Отправка на паузе. Включите «Полуручной» или «Авто» в ⚙️ Автоматизация.")

            role = esc(ROLES.get(direction, direction))
            step_title = (
                f"⏳ <b>Принудительный отклик · задание #{ident}</b>\n1/2 Готовлю сопроводительное письмо ({role})…"
                if send_now
                else f"⏳ <b>Подготовка письма · задание #{ident}</b>\nГотовлю сопроводительное письмо ({role})…"
            )
            status_msg = await render(message, step_title)
            prepared_rev = await self.automation_engine.force_prepare(ident, direction)
            if not send_now:
                await render(
                    status_msg or message,
                    f"✅ <b>Письмо подготовлено (вер. #{prepared_rev}).</b> Проверьте карточку ниже.",
                    edit=bool(status_msg),
                )
                await self.automation.show(message, ident, edit=True)
                return

            await render(
                status_msg or message,
                f"📤 <b>Принудительный отклик · задание #{ident} (вер. #{prepared_rev})</b>\n"
                "2/2 Письмо готово — открываю Chrome и отправляю отклик…",
                edit=bool(status_msg),
            )
            result = await self.automation_engine.send(ident, prepared_rev)
            await render(
                status_msg or message,
                f"🚀 <b>Задание #{ident}:</b> {esc(result)}",
                edit=bool(status_msg),
            )
            await self.automation.show(message, ident, edit=True)
        except LocalQuotaExceeded as exc:
            await render(
                status_msg or message,
                f"⚠️ {esc(exc)}",
                edit=bool(status_msg),
                reply_markup=inline_kb([[("⚙️ Изменить лимиты ИИ", "flow:settings:0")]]),
            )
        except (AssessmentError, ValueError) as exc:
            await render(status_msg or message, f"⚠️ {esc(exc)}", edit=bool(status_msg))
            if prepared_rev is not None:
                await self.automation.show(message, ident, edit=True)
        except Exception:
            await render(
                status_msg or message,
                "⚠️ Операция не завершена. Проверьте задание в /queue перед повтором.",
                edit=bool(status_msg),
            )

    async def close(self):
        self.service.stop.set()
        try:
            if self.search_task:
                await self.search_task
            if self.analysis_task:
                await self.analysis_task
        finally:
            if self.automation_engine.browser_series:
                await self.automation_engine.browser_series.close_series()


async def run_bot(env, config_path, db_path):
    from .backup import create_backup

    try:
        backup_hours = float(env.get("BACKUP_INTERVAL_HOURS", "6"))
        if not (backup_hours == 0 or 1 <= backup_hours <= 168):
            raise ValueError()
    except ValueError:
        raise ValueError("BACKUP_INTERVAL_HOURS: 0 для отключения или от 1 до 168 часов.") from None
    token = env.get("TG_BOT_TOKEN", "").strip()
    try:
        validate_token(token)
    except TokenValidationError:
        raise ValueError("Укажите TG_BOT_TOKEN из BotFather в .env") from None
    admin_id = admin_id_from_env(env)
    controller = BotController(
        SearchService(config_path, db_path, env.get("HH_USER_AGENT", ""), env.get("HH_ACCESS_TOKEN", ""), env=env),
        admin_id,
        keep_browser_open=True,
    )
    router = Router()
    router.message.register(controller.handle)
    router.callback_query.register(controller.handle_callback)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    logging.getLogger("aiogram").addHandler(logging.NullHandler())
    logging.getLogger("aiogram").propagate = False
    bot = Bot(token=token)

    async def backup_loop():
        while True:
            if Path(db_path).is_file():
                try:
                    await asyncio.to_thread(create_backup, db_path, env.get("BACKUP_DIR", "data/backups"))
                    controller.backup_status = "Проверена " + datetime.now(timezone.utc).strftime("%d.%m %H:%M UTC")
                except Exception:
                    controller.backup_status = "Ошибка копирования — проверьте каталог и место на диске."
            else:
                controller.backup_status = "Ожидает создания базы."
            await asyncio.sleep(backup_hours * 3600)

    async def automation_cycle():
        async def notify(text):
            await bot.send_message(admin_id, esc(text), parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))

        await controller.automation_engine.daily(notify)
        ident = await controller.automation_engine.prepare_one(notify)
        if ident is not None:
            anchor = await bot.send_message(admin_id, "📬 <b>Новый подготовленный пакет:</b>", parse_mode="HTML")
            await controller.automation.show(anchor, ident, edit=True)

    async def automation_loop():
        from types import SimpleNamespace
        from .daily_insights import DailyInsights

        insights = DailyInsights(db_path)

        async def report_answer(text, **kwargs):
            return await bot.send_message(admin_id, text, **kwargs)

        report_message = SimpleNamespace(answer=report_answer)
        store = AutomationStore(db_path)
        try:
            store.recover()
        finally:
            store.close()
        while True:
            try:
                await insights.due(report_message)
            except Exception:
                logging.getLogger(__name__).warning("Daily insights failed; inspect local report.")
            if not controller.busy and not controller.clearing and not (controller.analysis_task and not controller.analysis_task.done()):
                controller.analysis_task = asyncio.create_task(automation_cycle())
                try:
                    await asyncio.shield(controller.analysis_task)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.getLogger(__name__).warning("Daily workflow failed; inspect queue and connection.")
            await asyncio.sleep(30)

    automation_task = asyncio.create_task(automation_loop()) if admin_id else None
    backup_task = asyncio.create_task(backup_loop()) if backup_hours else None
    if not backup_hours:
        controller.backup_status = "Отключено в .env"
    try:
        await bot.set_my_commands([
            BotCommand(command=c, description=d)
            for c, d in [
                ("status", "📊 Пульт управления"),
                ("queue", "📥 Очередь откликов"),
                ("automation", "⚙️ Автоматизация и лимиты"),
                ("profile", "👤 Профиль кандидата"),
                ("resumes", "📄 Мои резюме"),
                ("settings", "🎛 Поисковые фильтры"),
                ("search", "▶️ Найти вакансии за сутки"),
                ("insights", "📈 Сводка навыков за день"),
                ("tracked", "⭐ Мои отметки"),
                ("history", "🕒 История описаний по ID"),
                ("cancel", "✖️ Отменить ввод"),
                ("id", "🆔 Мой Telegram ID"),
                ("help", "ℹ️ Справка"),
            ]
        ])
        print("Telegram-бот запущен. Остановка: Ctrl+C.", flush=True)
        if admin_id is None:
            print("ADMIN_ID не задан: доступны только сведения для настройки доступа.", flush=True)
        await dispatcher.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
            close_bot_session=False,
            tasks_concurrency_limit=5,
        )
    except TelegramAPIError as exc:
        raise ValueError(
            f"Telegram недоступен ({type(exc).__name__}). Проверьте токен, сеть и отсутствие другого запущенного экземпляра."
        ) from None
    finally:
        if automation_task:
            automation_task.cancel()
            try:
                await automation_task
            except asyncio.CancelledError:
                pass
        if backup_task:
            backup_task.cancel()
            try:
                await backup_task
            except asyncio.CancelledError:
                pass
        await controller.close()
        await bot.session.close()
