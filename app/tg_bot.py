"""Private Telegram console for vacancy search and application submission."""

import asyncio
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, LinkPreviewOptions, Message, ReplyKeyboardMarkup)
from aiogram.utils.token import TokenValidationError, validate_token

from .collector import search_key
from .config import SearchConfig
from .hh_client import ApiError
from .storage import Storage
from .sources import make_collector, source_name
from .web_search import WebSearchError
from .filter_editor import FIELDS, WORK_FORMATS, describe_filters, edit_filter
from .tg_profile import ProfileConsole
from .tg_resumes import ResumeConsole
from .tg_vacancies import VacancyConsole, STATES, vacancy_actions
from .automation import AutomationEngine, AutomationStore
from .tg_automation import AutomationConsole
from .assessment import AssessmentService, AssessmentError, DIRECTIONS, render_evaluation


HELP = (
    "Работа BY · Помощник\n\n"
    "/search — найти вакансии за последний день\n"
    "/vacancies — последние сохранённые вакансии\n"
    "/vacancies 2 — следующая страница\n"
    "/clear — очистить вакансии текущих фильтров\n"
    "/automation — режим, время и критерии ежедневной обработки\n"
    "/queue — очередь подготовленных откликов\n"
    "/insights — частые навыки и критерии за день\n"
    "/tracked — отмеченные вакансии\n"
    "/history ID — сохранённые описания вакансии\n"
    "/resumes — мои резюме, версии и выгрузка TXT\n"
    "/profile — профиль кандидата и проверка фактов\n"
    "/status — состояние поиска и базы\n"
    "/settings — посмотреть и изменить фильтры\n"
    "/cancel — отменить ввод фильтра\n"
    "/id — ваш Telegram ID\n"
    "/help — справка\n\n"
    "Отклики отправляются из очереди. Режим и расписание — /automation. "
    "Кнопка «Фильтры» открывает редактор."
)
KEYBOARD = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Найти вакансии"), KeyboardButton(text="Вакансии")],
    [KeyboardButton(text="Статус"), KeyboardButton(text="Фильтры")],
    [KeyboardButton(text="Мои резюме"), KeyboardButton(text="Мои отметки")],
    [KeyboardButton(text="Режим и расписание"), KeyboardButton(text="Очередь откликов")],
    [KeyboardButton(text="Профиль"), KeyboardButton(text="Очистить вакансии")],
], resize_keyboard=True)
ALIASES = {"Режим и расписание": "/automation", "Очередь откликов": "/queue", "Мои отметки": "/tracked", "Мои резюме": "/resumes", "Найти вакансии": "/search", "Вакансии": "/vacancies",
           "Статус": "/status", "Фильтры": "/settings", "Очистить вакансии": "/clear", "Профиль": "/profile"}
FILTER_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text=label, callback_data="filter:" + field)]
    for field, (label, _) in FIELDS.items()
])
CANCEL_EDIT = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Отмена", callback_data="filter:cancel")]
])


def work_format_keyboard(selected):
    rows = [[InlineKeyboardButton(text=("☑ " if code in selected else "☐ ") + label,
                                  callback_data="format:toggle:" + code)] for code, label in WORK_FORMATS.items()]
    rows += [[InlineKeyboardButton(text="Любой формат", callback_data="format:clear")],
             [InlineKeyboardButton(text="Сохранить", callback_data="format:apply"),
              InlineKeyboardButton(text="Отмена", callback_data="filter:cancel")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_id_from_env(env):
    raw = env.get("ADMIN_ID", "").strip()
    if not raw:
        return None
    if not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
        raise ValueError("ADMIN_ID должен быть положительным числовым Telegram ID")
    return int(raw)


def is_owner(message, admin_id):
    return (admin_id is not None and message.chat.type == "private"
            and message.from_user is not None and message.from_user.id == admin_id
            and message.chat.id == admin_id)


def vacancy_card(item):
    salary = item.get("salary") or {}
    pay = (f"{salary.get('from') or '—'}–{salary.get('to') or '—'} {salary.get('currency', '')}"
           if salary else "не указана")
    area = (item.get("area") or {}).get("name", "—")
    url = item.get("alternate_url") or ""
    if urlparse(url).scheme != "https" or urlparse(url).hostname not in ("rabota.by", "hh.ru"):
        url = ""
    return (f"{str(item.get('name', 'Вакансия'))[:300]}\n"
            f"Компания: {str((item.get('employer') or {}).get('name', '—'))[:200]}\n"
            f"Регион: {str(area)[:150]}\nЗарплата: {pay[:100]}\n"
            f"Опубликовано: {str(item.get('published_at', '—'))[:40]}\n{url[:500]}"
            + ("\nОтметка: " + STATES.get(item['_tracking_state'], '—') if item.get('_tracking_state') else ""))


class SearchService:
    def __init__(self, config_path, db_path, user_agent, access_token="", env=None):
        self.config_path, self.db_path, self.user_agent = config_path, db_path, user_agent
        self.stop = threading.Event()
        self.access_token = access_token
        self.env = dict(env) if env is not None else {"HH_USER_AGENT": user_agent, "HH_ACCESS_TOKEN": access_token}
        self.last_web_search = None

    def client(self):
        # Validate selected source without network traffic or opening a database.
        return make_collector(self.env, None, SearchConfig.load(self.config_path))

    def collect(self, config=None):
        if source_name(self.env) == "web":
            if self.last_web_search is not None and time.monotonic() - self.last_web_search < 60:
                raise WebSearchError("Публичный поиск можно запускать не чаще раза в минуту. Подождите и повторите.")
            self.last_web_search = time.monotonic()
        config = config or SearchConfig.load(self.config_path)
        storage = Storage(self.db_path)
        try:
            return make_collector(self.env, storage, config, should_stop=self.stop.is_set).collect()
        finally:
            storage.close()

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
        self.automation_engine = AutomationEngine(service,keep_browser_open=keep_browser_open)
        self.automation = AutomationConsole(service.db_path,self.automation_engine)

    async def assess_vacancy(self, message, vid):
        try:
            service = AssessmentService(self.service.db_path, self.service.env)
            result, cached = await asyncio.to_thread(service.evaluate, vid)
            await message.answer(render_evaluation(result,cached),parse_mode=None)
            if service.description_id:
                await message.answer(f"Описание вакансии: снимок #{service.description_id}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                        text="Посмотреть описание",callback_data=f"description:{service.description_id}")]]))
        except AssessmentError as exc:
            await message.answer(str(exc))
        except Exception:
            await message.answer("Не удалось выполнить оценку. Секреты и ответ провайдера скрыты; проверьте настройки и повторите позже.")

    async def ask_clear(self, message):
        if self.busy or self.clearing or (self.analysis_task and not self.analysis_task.done()):
            await message.answer("Дождитесь завершения поиска, оценки или очистки, затем повторите.")
            return
        config, _, count, _ = await asyncio.to_thread(self.service.read, 1)
        token = secrets.token_hex(6)
        self.clear_confirmation = (token, search_key(config), time.monotonic())
        await message.answer(f"Очистить список для текущих фильтров? Вакансий: {count}.\n"
                             "Фильтры и списки других поисков сохранятся. Старые сообщения в Telegram останутся; следующий поиск сможет снова найти эти вакансии.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                 InlineKeyboardButton(text="Очистить список", callback_data=f"clear:{token}:yes"),
                                 InlineKeyboardButton(text="Отмена", callback_data=f"clear:{token}:no"),
                             ]]))

    async def handle_clear_callback(self, callback):
        parts = callback.data.split(":")
        confirmation = self.clear_confirmation
        if len(parts) != 3 or not confirmation or parts[1] != confirmation[0] or time.monotonic() - confirmation[2] > 600:
            await callback.answer("Подтверждение устарело. Нажмите «Очистить вакансии» заново.", show_alert=True)
            return
        if parts[2] == "no":
            self.clear_confirmation = None
            await callback.answer("Очистка отменена")
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
            self.clearing = True  # Set before yielding; /search cannot race the deletion.
            self.clear_confirmation = None
            await callback.answer("Очищаю список")
            count = await asyncio.to_thread(self.service.clear, confirmation[1])
            self.last_result = "Список текущего поиска очищен. Можно запустить новый поиск."
            await callback.message.answer(f"Список очищен. Удалено из списка вакансий: {count}. Фильтры сохранены.", reply_markup=KEYBOARD)
        except Exception:
            await callback.message.answer("Не удалось завершить очистку. Проверьте список и повторите позже.")
        finally:
            self.clearing = False

    async def show_filters(self, message):
        config = SearchConfig.load(self.service.config_path)
        await message.answer(describe_filters(config, source_name(self.service.env)) +
                             "\n\nВыберите параметр. После изменения запустите новый поиск; сохранённые вакансии показываются для текущего набора фильтров.",
                             reply_markup=FILTER_MENU, parse_mode=None)

    async def handle_callback(self, callback: CallbackQuery):
        message = callback.message
        if (not isinstance(message, Message) or message.chat.type != "private"
                or callback.from_user.id != self.admin_id or message.chat.id != self.admin_id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if (callback.data or '').startswith('flow:'):
            async with self.edit_lock:
                self.pending_field = self.profile.pending = self.resumes.pending = self.resumes.pending_letter = None
                self.automation.pending = None
                try:
                    parts = callback.data.split(':')
                    if len(parts)==4 and parts[1]=='send':
                        if self.busy or self.clearing or (self.analysis_task and not self.analysis_task.done()):
                            await callback.answer('Дождитесь текущей операции.',show_alert=True)
                            return
                        ident, revision = int(parts[2]), int(parts[3])
                        self.analysis_task = asyncio.create_task(self.send_package(message,ident,revision))
                        await callback.answer('Проверяю пакет и возможность отправки')
                    else:
                        await self.automation.callback(callback)
                except ValueError as exc:
                    await callback.answer(str(exc)[:180],show_alert=True)
                except Exception:
                    await message.answer('Не удалось выполнить действие. Откройте /queue.')
            return
        if (callback.data or '').startswith('insight:'):
            from .daily_insights import DailyInsights
            try:
                await DailyInsights(self.service.db_path).add(callback,self.admin_id)
            except (ValueError,IndexError,KeyError):
                await callback.answer('Предложение недоступно. Откройте /insights.',show_alert=True)
            return
        self.automation.pending = None
        if (callback.data or '').startswith(('mark:', 'history:', 'description:')):
            try:
                await self.vacancy_console.callback(callback)
            except Exception:
                await message.answer('Не удалось сохранить отметку или открыть описание. Повторите позже.')
            return
        if (callback.data or '').startswith('letter:'):
            async with self.edit_lock:
                self.pending_field = self.profile.pending = self.resumes.pending = None
                self.resumes.pending_letter = None
                try:
                    parts = callback.data.split(':')
                    if len(parts) == 3 and parts[1] == 'generate':
                        if not parts[2].isascii() or not parts[2].isdigit() or len(parts[2])>15:
                            raise ValueError('Некорректная версия резюме.')
                        if self.clearing or (self.analysis_task and not self.analysis_task.done()):
                            await callback.answer('Дождитесь завершения текущей операции.', show_alert=True)
                            return
                        self.analysis_task = asyncio.create_task(self.resumes.generate_letter(message, int(parts[2])))
                        await callback.answer('Подготовка письма запущена')
                    else:
                        await self.resumes.letter_callback(callback, self.admin_id)
                except ValueError as exc:
                    await callback.answer(str(exc)[:180], show_alert=True)
                except Exception:
                    await message.answer('Не удалось открыть письмо. Повторите через /resumes.')
            return
        self.resumes.pending_letter = None
        if (callback.data or "").startswith(("resume:", "prepare:", "draft:")):
            async with self.edit_lock:
                self.pending_field = None
                self.profile.pending = None
                try:
                    if callback.data.startswith("prepare:"):
                        self.resumes.pending = None
                        await self.resumes.choose(callback)
                    elif callback.data.startswith("draft:"):
                        self.resumes.pending = None
                        parts = callback.data.split(':')
                        if (len(parts) != 3 or parts[1] not in (*DIRECTIONS,'ai')
                                or not parts[2].isascii() or not parts[2].isdigit() or len(parts[2])>15):
                            await callback.answer('Некорректные параметры')
                            return
                        if self.clearing or (self.analysis_task and not self.analysis_task.done()):
                            await callback.answer('Дождитесь завершения текущей операции.', show_alert=True)
                            return
                        self.analysis_task = asyncio.create_task(self.resumes.generate(message, parts[1], parts[2]))
                        await callback.answer('Подготовка черновика запущена')
                    else:
                        await self.resumes.callback(callback, self.admin_id)
                except Exception:
                    await message.answer('Не удалось открыть резюме. Повторите /resumes позже.')
            return
        self.resumes.pending = None
        if (callback.data or "").startswith("profile:"):
            async with self.edit_lock:
                self.pending_field = None
                try:
                    await self.profile.callback(callback, self.admin_id)
                except Exception:
                    await callback.message.answer("Не удалось открыть профиль. Повторите /profile позже.")
            return
        if (callback.data or "").startswith("assess:"):
            vid=callback.data.partition(':')[2]
            if not vid.isascii() or not vid.isdigit() or len(vid)>15:
                await callback.answer("Некорректная вакансия",show_alert=True)
                return
            if self.analysis_task and not self.analysis_task.done():
                await callback.answer("Одна оценка уже выполняется. Дождитесь результата.",show_alert=True)
                return
            if self.clearing:
                await callback.answer("Дождитесь очистки списка.",show_alert=True)
                return
            self.analysis_task=asyncio.create_task(self.assess_vacancy(message,vid))
            await callback.answer("Оценка запущена")
            return
        self.profile.pending = None
        if (callback.data or "").startswith("format:"):
            await self.handle_format_callback(callback)
            return
        if (callback.data or "").startswith("clear:"):
            await self.handle_clear_callback(callback)
            return
        if not callback.data or not callback.data.startswith("filter:"):
            await callback.answer()
            return
        field = callback.data.partition(":")[2]
        async with self.edit_lock:
            if field == "clear_region":
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
                await self.show_filters(message)
            elif field == "cancel":
                self.pending_field = None
                await callback.answer("Ввод отменён")
                await self.show_filters(message)
            elif field in FIELDS:
                self.pending_field = field
                await callback.answer()
                keyboard = CANCEL_EDIT
                if field == "work_formats":
                    self.work_format_draft = SearchConfig.load(self.service.config_path).work_formats
                    keyboard = work_format_keyboard(self.work_format_draft)
                elif field == "areas":
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="Любой регион", callback_data="filter:clear_region")],
                        [InlineKeyboardButton(text="Отмена", callback_data="filter:cancel")],
                    ])
                await message.answer(FIELDS[field][1] + "\nОтмена: /cancel", reply_markup=keyboard)
            else:
                await callback.answer("Неизвестный параметр", show_alert=True)

    async def handle_format_callback(self, callback):
        # Called only after the private-chat and owner checks above.
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
                await callback.answer("Формат работы сохранён")
                await self.show_filters(callback.message)
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
                await message.answer("Отправьте значение текстом или /cancel.")
                return
            try:
                config = SearchConfig.load(self.service.config_path)
                updated = edit_filter(config, self.pending_field, message.text)
                updated.save(self.service.config_path)
            except ValueError as exc:
                await message.answer(str(exc) if not isinstance(exc, json.JSONDecodeError) else "Не удалось прочитать файл фильтров.")
                return
            except OSError:
                await message.answer("Не удалось сохранить фильтры. Старый файл сохранён; повторите ввод позже.")
                return
            self.pending_field = None
            await message.answer("Фильтр сохранён. Он применяется к следующему поиску.", reply_markup=KEYBOARD)
            await self.show_filters(message)

    @property
    def busy(self):
        return self.search_task is not None and not self.search_task.done()

    async def cards(self, message, page=1, config=None):
        size = 5
        if config is None:
            _, rows, count, _ = await asyncio.to_thread(self.service.read, page * size)
        else:
            _, rows, count, _ = await asyncio.to_thread(self.service.read, page * size, config)
        rows = rows[(page - 1) * size:page * size]
        if not rows:
            await message.answer("На этой странице вакансий нет. Запустите /search или откройте /vacancies.")
            return
        await message.answer(f"Страница {page}. Сохранено подходящих вакансий: {count}.")
        for item in rows:
            await message.answer(vacancy_card(item) + "\n\nПри оценке описание и подтверждённые профессиональные сведения профиля передаются в OpenAI.", parse_mode=None,
                                 reply_markup=vacancy_actions(str(item['id'])),
                                 link_preview_options=LinkPreviewOptions(is_disabled=True))
            await asyncio.sleep(0.4)
        if count > page * size:
            await message.answer(f"Далее: /vacancies {page + 1}")

    async def search(self, message, config=None):
        try:
            stats = await asyncio.to_thread(self.service.collect, config) if config is not None else await asyncio.to_thread(self.service.collect)
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            self.last_result = (f"Последний сбор: {stamp}\nПолучено: {stats.received}, "
                                f"новых в базе: {stats.new}, исключено: {stats.rejected}.")
            if stats.partial:
                self.last_result += "\nДостигнут лимит страниц; показана часть выдачи. Сузьте фильтры для более полного просмотра."
            if config is not None:
                self.last_result += f"\nЗапрос этого сбора: {config.text}"
            await message.answer(self.last_result)
            await self.cards(message, config=config)
        except Exception as exc:
            # Do not forward exceptions: network libraries may include a token URL.
            self.last_result = exc.public_message if isinstance(exc, ApiError) else f"Последняя операция завершилась ошибкой ({type(exc).__name__})."
            try:
                await message.answer(self.last_result + " Сохранённые данные остаются в базе.")
            except TelegramAPIError:
                pass

    async def handle(self, message: Message):
        # Never expose the private console in group chats, including /id.
        if message.chat.type != "private" or message.from_user is None:
            return
        parts = ALIASES.get(message.text or "", message.text or "").split()
        command = parts[0].split("@")[0].lower() if parts else ""
        if command == "/id":
            await message.answer(f"Ваш Telegram ID: {message.from_user.id}")
            return
        if not is_owner(message, self.admin_id):
            await message.answer("Доступ не настроен или ограничен. Ваш ID: " + str(message.from_user.id) +
                                 ". Владелец должен указать ADMIN_ID в .env и перезапустить бота.")
            return
        if self.automation.pending is not None:
            if not (message.text or '').startswith('/') and (message.text or '') not in ALIASES:
                async with self.edit_lock:
                    await self.automation.save_text(message)
                return
            self.automation.pending = None
        if command=='/insights':
            from .daily_insights import DailyInsights
            insights=DailyInsights(self.service.db_path)
            store=AutomationStore(self.service.db_path)
            try:
                from .automation import MINSK
                time=store.settings()['insights_time']
                if datetime.now(timezone.utc).astimezone(MINSK).strftime('%H:%M')<time:
                    await message.answer(f'Сводка будет доступна в {time} по Минску. Время можно изменить в /automation.')
                    return
            finally:
                store.close()
            await insights.show(message)
            return
        if command in ('/automation','/queue'):
            self.pending_field = self.profile.pending = self.resumes.pending = self.resumes.pending_letter = None
            try:
                await (self.automation.settings(message) if command=='/automation' else self.automation.queue(message))
            except Exception:
                await message.answer('Не удалось открыть настройки или очередь. Повторите позже.')
            return
        if command in ('/tracked', '/history'):
            self.resumes.pending_letter = self.resumes.pending = self.profile.pending = self.pending_field = None
            try:
                if command == '/history':
                    if len(parts)!=2:
                        raise ValueError('Используйте /history ID_вакансии.')
                    await self.vacancy_console.history(message,parts[1])
                else:
                    page = int(parts[1]) if len(parts)==2 else 1
                    if len(parts)>2 or not 1<=page<=200:
                        raise ValueError('Используйте /tracked или /tracked 2 (1–200).')
                    rows = await asyncio.to_thread(self.vacancy_console.tracked,page)
                    await message.answer(f'Мои отметки · страница {page}.' if rows else 'На этой странице отметок нет.')
                    for item in rows[:5]:
                        await message.answer(vacancy_card(item),parse_mode=None,
                            reply_markup=vacancy_actions(str(item['id'])),
                            link_preview_options=LinkPreviewOptions(is_disabled=True))
                    if len(rows)>5:
                        await message.answer(f'Далее: /tracked {page+1}')
            except ValueError as exc:
                await message.answer(str(exc))
            except Exception:
                await message.answer('Не удалось открыть историю. Повторите позже.')
            return
        if self.resumes.pending_letter is not None:
            if not (message.text or '').startswith('/') and (message.text or '') not in ALIASES:
                async with self.edit_lock:
                    await self.resumes.save_letter(message)
                return
            self.resumes.pending_letter = None
        if self.resumes.pending is not None:
            if not (message.text or '').startswith('/') and (message.text or '') not in ALIASES:
                async with self.edit_lock:
                    await self.resumes.save_text(message)
                return
            self.resumes.pending = None
        if command == '/resumes':
            self.pending_field = None
            self.profile.pending = None
            await self.resumes.overview(message)
            return
        if self.profile.pending and not (message.text or '').startswith('/') and (message.text or '') not in ALIASES:
            async with self.edit_lock:
                try:
                    await self.profile.save_text(message, self.admin_id)
                except Exception:
                    await message.answer("Не удалось сохранить профиль. Повторите позже.")
            return
        if self.pending_field and command not in ("/search", "/settings", "/start", "/help", "/status", "/vacancies", "/cancel", "/clear", "/profile"):
            if (message.text or "").startswith("/"):
                await message.answer("Сейчас ожидается значение фильтра. Для отмены — /cancel.")
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
                    await message.answer("Изменение профиля отменено.")
                    await self.profile.overview(message)
                    return
                self.pending_field = None
                self.clear_confirmation = None
                await message.answer("Ввод отменён.", reply_markup=KEYBOARD)
                await self.show_filters(message)
                return
            if command in ("/start", "/help"):
                self.profile.pending = None
                self.pending_field = None
                await message.answer(HELP, reply_markup=KEYBOARD)
            elif command == "/search":
                self.profile.pending = None
                if self.busy or self.clearing:
                    await message.answer("Поиск или очистка уже выполняется. Проверить: /status")
                    return
                # Validate HH settings before accepting a background job.
                self.service.client()
                config = SearchConfig.load(self.service.config_path)
                self.pending_field = None
                # Assign before yielding so two updates cannot start two collectors.
                self.search_task = asyncio.create_task(self.search(message, config))
                await message.answer("Поиск запущен. Пришлю результат; бот продолжает отвечать на команды.")
            elif command == "/clear":
                self.pending_field = None
                await self.ask_clear(message)
            elif command == "/vacancies":
                try:
                    page = int(parts[1]) if len(parts) == 2 else 1
                except ValueError:
                    page = 0
                if not 1 <= page <= 200 or len(parts) > 2:
                    await message.answer("Используйте /vacancies или /vacancies 2 (страница от 1 до 200).")
                    return
                await self.cards(message, page)
            elif command == "/settings":
                self.profile.pending = None
                self.pending_field = None
                await self.show_filters(message)
            elif command == "/status":
                config, _, count, cursor = await asyncio.to_thread(self.service.read, 1)
                await message.answer(f"Поиск: {'выполняется' if self.busy else 'не запущен'}\n"
                                     f"Вакансий по текущим фильтрам: {count}\n"
                                     f"Последний завершённый сбор: {cursor or 'ещё не было сбора'}\n{self.last_result}\n{self.backup_status}")
            else:
                await message.answer("Выберите действие на клавиатуре или отправьте /help.")
        except Exception as exc:
            text = "Проверьте файл фильтров и настройки выбранного источника в .env." if isinstance(exc, ValueError) else "Повторите позже и проверьте подключение."
            await message.answer(f"Не удалось выполнить команду ({type(exc).__name__}). {text}")

    async def send_package(self,message,ident,revision):
        try:
            await message.answer(f'Отправка пакета #{ident}, версия {revision}: проверяю актуальность и открываю браузер. '
                                 'Результат придёт отдельным сообщением. Повторно нажимать кнопку не нужно.')
            result = await self.automation_engine.send(ident,revision)
            await message.answer(result)
            await self.automation.show(message,ident)
        except ValueError as exc:
            await message.answer(str(exc),parse_mode=None)
        except Exception:
            await message.answer('Операция не завершена. Проверьте /queue перед любым повтором.')

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
    from pathlib import Path
    from .backup import create_backup
    try:
        backup_hours = float(env.get('BACKUP_INTERVAL_HOURS','6'))
        if not (backup_hours == 0 or 1 <= backup_hours <= 168):
            raise ValueError()
    except ValueError:
        raise ValueError('BACKUP_INTERVAL_HOURS: 0 для отключения или от 1 до 168 часов.') from None
    token = env.get("TG_BOT_TOKEN", "").strip()
    try:
        validate_token(token)
    except TokenValidationError:
        raise ValueError("Укажите TG_BOT_TOKEN из BotFather в .env") from None
    admin_id = admin_id_from_env(env)
    controller = BotController(SearchService(config_path, db_path, env.get("HH_USER_AGENT", ""), env.get("HH_ACCESS_TOKEN", ""), env=env),
                               admin_id,keep_browser_open=True)
    router = Router()
    router.message.register(controller.handle)
    router.callback_query.register(controller.handle_callback)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    # aiogram's error messages can contain HTTP details; do not emit them verbatim.
    logging.getLogger("aiogram").addHandler(logging.NullHandler())
    logging.getLogger("aiogram").propagate = False
    bot = Bot(token=token)
    async def backup_loop():
        while True:
            if Path(db_path).is_file():
                try:
                    await asyncio.to_thread(create_backup,db_path,env.get('BACKUP_DIR','data/backups'))
                    controller.backup_status = 'Резервная копия проверена: ' + datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
                except Exception:
                    controller.backup_status = 'Ошибка резервного копирования. Проверьте каталог копий и свободное место.'
            else:
                controller.backup_status = 'Резервная копия ожидает создания базы.'
            await asyncio.sleep(backup_hours*3600)
    async def automation_cycle():
        async def notify(text):
            await bot.send_message(admin_id,text,parse_mode=None)
        await controller.automation_engine.daily(notify)
        ident = await controller.automation_engine.prepare_one(notify)
        if ident is not None:
            message = await bot.send_message(admin_id,'Подготовленный пакет для просмотра:')
            await controller.automation.show(message,ident)

    async def automation_loop():
        from .daily_insights import DailyInsights
        from types import SimpleNamespace
        insights=DailyInsights(db_path)
        async def report_answer(text,**kwargs):
            return await bot.send_message(admin_id,text,**kwargs)
        report_message=SimpleNamespace(answer=report_answer)
        store = AutomationStore(db_path)
        try:
            store.recover()
        finally:
            store.close()
        while True:
            try:
                await insights.due(report_message)
            except Exception:
                logging.getLogger(__name__).warning('Daily insights failed; inspect local report.')
            if not controller.busy and not controller.clearing and not (controller.analysis_task and not controller.analysis_task.done()):
                controller.analysis_task = asyncio.create_task(automation_cycle())
                try:
                    await asyncio.shield(controller.analysis_task)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.getLogger(__name__).warning('Daily workflow failed; inspect queue and connection.')
            await asyncio.sleep(30)

    automation_task = asyncio.create_task(automation_loop()) if admin_id else None
    backup_task = asyncio.create_task(backup_loop()) if backup_hours else None
    if not backup_hours:
        controller.backup_status = 'Автоматическое резервное копирование отключено.'
    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in [
            ("automation", "Режим и расписание"), ("queue", "Очередь откликов"),
            ("insights", "Навыки и критерии за день"),
            ("search", "Найти вакансии"), ("vacancies", "Сохранённые вакансии"),
            ("clear", "Очистить список вакансий"),
            ("tracked", "Мои отметки"), ("history", "История описаний по ID"),
            ("profile", "Профиль кандидата"), ("resumes", "Мои резюме"),
            ("status", "Статус поиска"), ("settings", "Изменить фильтры"), ("cancel", "Отменить ввод"),
            ("id", "Мой Telegram ID"), ("help", "Справка")]])
        print("Telegram-бот запущен. Остановка: Ctrl+C.", flush=True)
        if admin_id is None:
            print("ADMIN_ID не задан: доступны только сведения для настройки доступа.", flush=True)
        await dispatcher.start_polling(bot, allowed_updates=["message", "callback_query"], close_bot_session=False,
                                       tasks_concurrency_limit=5)
    except TelegramAPIError as exc:
        raise ValueError(f"Telegram недоступен ({type(exc).__name__}). Проверьте токен, сеть и отсутствие другого запущенного экземпляра.") from None
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
