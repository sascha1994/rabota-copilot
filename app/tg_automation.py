"""Controls for the persisted daily application workflow."""

from datetime import datetime, timezone
import json

from aiogram.types import BufferedInputFile

from .assessment import DIRECTIONS, ROLES
from .automation import AutomationStore, MINSK
from .portfolio import LETTER_LIMIT
from .tg_ui import JOB_STATE_BADGES, bar, esc, expandable, fmt_dt, fmt_int, inline_kb, render, score_badge

MODES = {"auto": "Автоматический", "semi": "Полуручной", "paused": "Пауза"}
MODE_BADGES = {
    "auto": "🟢 Автоматический",
    "semi": "🟡 Полуручной",
    "paused": "⏸ Пауза",
}
STATES = {code: label for code, (_, label) in JOB_STATE_BADGES.items()}

CATEGORIES = {
    "review": ("Проверить и отправить", ("REVIEW", "READY")),
    "waiting": ("Ожидают обработки", ("QUEUED", "WAITING_AI")),
    "processing": ("В работе", ("PREPARING", "SENDING")),
    "attention": ("Требуют внимания", ("BLOCKED", "FAILED", "STALE", "UNKNOWN")),
    "unsuitable": ("Не подошли по порогу", ("UNSUITABLE",)),
    "sent": ("Отправлены", ("SENT",)),
    "closed": ("Сняты с очереди", ("REJECTED", "UNKNOWN_DISMISSED")),
    "all": ("Все задания", None),
}

CATEGORY_ICONS = {
    "review": "🟡",
    "waiting": "⏳",
    "processing": "⚙️",
    "attention": "⚠️",
    "unsuitable": "📉",
    "sent": "✅",
    "closed": "🗃",
    "all": "📋",
}

REJECTABLE = ("QUEUED", "WAITING_AI", "REVIEW", "READY", "BLOCKED", "FAILED", "STALE", "UNSUITABLE")

SETTING_PROMPTS = {
    "time": (
        "🕘 <b>Время ежедневного поиска</b>\nВыберите готовый вариант или отправьте время <code>ЧЧ:ММ</code> по Минску.",
        [("08:00", "08:00"), ("09:00", "09:00"), ("10:00", "10:00"), ("12:00", "12:00")],
    ),
    "insights_time": (
        "🌙 <b>Время вечерней сводки</b>\nВыберите вариант или отправьте <code>ЧЧ:ММ</code> по Минску.",
        [("20:00", "20:00"), ("21:00", "21:00"), ("22:00", "22:00"), ("23:00", "23:00")],
    ),
    "threshold": (
        "🎯 <b>Порог соответствия (0–100)</b>\nВакансии ниже порога отклоняются без генерации письма.",
        [("65", "65"), ("70", "70"), ("75", "75"), ("80", "80"), ("85", "85")],
    ),
    "daily_limit": (
        "📦 <b>Дневной лимит (1–500)</b>\nМаксимум подготовок и попыток отправки за день.",
        [("25", "25"), ("50", "50"), ("100", "100"), ("200", "200"), ("500", "500")],
    ),
    "ai_daily_calls": (
        "🤖 <b>Лимит запросов OpenAI в сутки (1–1 000)</b>\n<i>Повышение лимита может увеличить расход средств API.</i>",
        [("50", "50"), ("100", "100"), ("200", "200"), ("500", "500")],
    ),
    "ai_daily_tokens": (
        "🔢 <b>Лимит токенов OpenAI в сутки (10 000 – 10 000 000)</b>\n<i>Повышение лимита может увеличить расход средств API.</i>",
        [("200k", "200000"), ("500k", "500000"), ("1M", "1000000"), ("3M", "3000000")],
    ),
    "browser_idle_minutes": (
        "🖥 <b>Автозакрытие Chrome после простоя (1–240 мин)</b>\nВыберите интервал или отправьте число минут.",
        [("15 мин", "15"), ("30 мин", "30"), ("45 мин", "45"), ("90 мин", "90")],
    ),
}


def buttons(rows):
    return inline_kb(rows)


class AutomationConsole:
    def __init__(self, path, engine):
        self.path, self.engine, self.pending = path, engine, None

    async def settings(self, message, *, edit=False):
        from .ai_limits import effective_limits

        call_limit, token_limit = effective_limits(self.path, self.engine.service.env)
        store = AutomationStore(self.path)
        try:
            values = store.settings()
            progress = store.queue_status()
            found, processed = store.daily_vacancy_counts()
            usage = (0, 0)
            if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='ai_call_usage'").fetchone():
                usage = store.db.execute(
                    "SELECT count(*),coalesce(sum(tokens),0) FROM ai_call_usage WHERE day=?",
                    (datetime.now(timezone.utc).date().isoformat(),),
                ).fetchone()
        finally:
            store.close()

        series = self.engine.browser_series
        sender_state = (
            "⏸ <b>пауза до проверки браузера</b>"
            if values["browser_hold"]
            else ("🟢 подключена (Chrome)" if self.engine.sender.available else "⚪ не подключена (пакеты сохраняются)")
        )
        browser_line = f"\n🖥 <b>Простой Chrome:</b> автозакрытие через {values['browser_idle_minutes']} мин." if series else ""

        hold_alert = ""
        if values["browser_hold"]:
            hold_alert = (
                "\n\n⚠️ <b>Требуется ручная проверка браузера:</b>\n"
                "Пройдите проверку в открытом окне Chrome бота и оставьте его открытым.\n"
                "Если окно закрыто, выполните:\n"
                "<code>.\\.venv\\Scripts\\python.exe -m app browser-login</code>\n"
                "Пройдите проверку, закройте запущенное этой командой окно и нажмите <b>«▶️ Возобновить отправку»</b>."
            )

        hint = expandable(
            "Режимы применяются к следующим операциям: переключение в «Авто» не отправляет ранее подготовленные пакеты задним числом. "
            "Вакансии с вопросами или расхождениями всегда остаются на ручной проверке. Счётчики OpenAI сбрасываются в 03:00 по Минску.",
            500,
        )

        text = (
            "⚙️ <b>Автоматизация и лимиты</b>\n\n"
            f"Режим: <b>{MODE_BADGES[values['mode']]}</b>\n"
            f"🕘 Поиск: <b>{esc(values['time'])}</b> · 🌙 Сводка: <b>{esc(values['insights_time'])}</b> <i>(Минск)</i>\n"
            f"🎯 Порог отбора: <b>{values['threshold']}/100</b>\n\n"
            f"📦 <b>Сегодня:</b> найдено <b>{found}</b> · обработано <b>{processed}</b>\n"
            f"📋 <b>Попытки подготовки:</b> <b>{progress['prepared']}/{values['daily_limit']}</b> · ждут: <b>{progress['waiting']}</b>\n"
            f"🤖 <b>Запросы ИИ:</b> <code>{bar(usage[0], call_limit)}</code> <b>{usage[0]}/{call_limit}</b>\n"
            f"🔢 <b>Токены ИИ:</b> <code>{fmt_int(usage[1])} / {fmt_int(token_limit)}</code>\n"
            f"📨 <b>Отправка:</b> {sender_state}{browser_line}"
            f"{hold_alert}\n\n"
            f"{hint}"
        )

        mode_row = [
            (("● " if values["mode"] == m else "○ ") + short, f"flow:mode:{m}")
            for m, short in [("auto", "Авто"), ("semi", "Полуручной"), ("paused", "Пауза")]
        ]
        rows = [
            mode_row,
            [
                (f"🕘 Поиск ({values['time']})", "flow:setting:time"),
                (f"🎯 Порог ({values['threshold']})", "flow:setting:threshold"),
            ],
            [
                (f"📦 Лимит ({values['daily_limit']}/д)", "flow:setting:daily_limit"),
                (f"🌙 Сводка ({values['insights_time']})", "flow:setting:insights_time"),
            ],
            [
                (f"🤖 Запросы ({call_limit})", "flow:setting:ai_daily_calls"),
                ("🔢 Токены ИИ", "flow:setting:ai_daily_tokens"),
            ],
        ]
        if series:
            rows.append([(f"🖥 Простой браузера ({values['browser_idle_minutes']} м)", "flow:setting:browser_idle_minutes")])
        if values["browser_hold"]:
            rows.append([("▶️ Возобновить отправку", "flow:browserresume:0")])
        rows.append([("📥 Очередь откликов", "flow:categories:0"), ("📈 Сводка", "insight:open:0")])
        rows.append([("📊 На главный пульт", "nav:status")])
        await render(message, text, edit=edit, reply_markup=buttons(rows))

    async def queue(self, message, page=0, category=None, *, edit=False):
        if category is not None and category not in CATEGORIES:
            raise ValueError("Неизвестная категория очереди.")
        store = AutomationStore(self.path)
        try:
            counts = dict(store.db.execute("SELECT state,count(*) FROM application_jobs GROUP BY state"))
            if category is None:
                rows = []
                for key, (label, states) in CATEGORIES.items():
                    cnt = sum(counts.values()) if states is None else sum(counts.get(s, 0) for s in states)
                    icon = CATEGORY_ICONS.get(key, "•")
                    rows.append([(f"{icon} {label} · {cnt}", f"flow:category:{key}:0")])
                text = (
                    "📥 <b>Очередь откликов</b>\n\n"
                    "Выберите категорию для просмотра пакетов:\n"
                    "• <b>Требуют внимания</b> — ошибки, устаревшие пакеты и неизвестный исход отправки.\n"
                    "• <b>Сняты с очереди</b> — отклонённые, не прошедшие порог и архивированные."
                )
                await render(message, text, edit=edit, reply_markup=buttons(rows))
                return
            label, states = CATEGORIES[category]
            count = sum(counts.values()) if states is None else sum(counts.get(s, 0) for s in states)
            pages = max(1, (count + 19) // 20)
            page = min(max(0, page), pages - 1)
            jobs = store.jobs(states, limit=20, offset=page * 20, newest_first=True)
        finally:
            store.close()

        icon = CATEGORY_ICONS.get(category, "📋")
        if not jobs:
            await render(
                message,
                f"{icon} <b>{esc(label)}:</b> заданий пока нет.",
                edit=edit,
                reply_markup=buttons([[("🗂 Категории", "flow:categories:0"), ("⚙️ Настройки", "flow:settings:0")]]),
            )
            return

        rows = []
        for j in jobs:
            st_icon, _ = JOB_STATE_BADGES.get(j["state"], ("•", j["state"]))
            score_part = ""
            if j["evaluation_json"]:
                try:
                    score_part = f" · {json.loads(j['evaluation_json'])['score']}%"
                except (KeyError, ValueError, TypeError):
                    pass
            title = str(json.loads(j["raw_json"]).get("name", "Вакансия"))[:38]
            rows.append([(f"{st_icon} #{j['id']}{score_part} · {title}", f"flow:view:{j['id']}")])

        navigation = []
        if page > 0:
            navigation.append(("← Новее", f"flow:category:{category}:{page - 1}"))
        if page + 1 < pages:
            navigation.append(("Раньше →", f"flow:category:{category}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        if pages > 1:
            rows.append([
                ("⏮ Самые новые", f"flow:category:{category}:0"),
                ("Самые первые ⏭", f"flow:category:{category}:{pages - 1}"),
            ])
        stale_count = counts.get("STALE", 0)
        if category == "attention" and stale_count > 0:
            rows.append([
                (f"🔄 Пересобрать устар. ({stale_count})", "flow:retry_stale:0"),
                (f"✖️ Отклонить устар. ({stale_count})", "flow:reject_stale:0"),
            ])
        rows.append([("🗂 Категории", "flow:categories:0"), ("⚙️ Настройки", "flow:settings:0")])
        await render(
            message,
            f"{icon} <b>{esc(label)}</b> · страница {page + 1} из {pages} · заданий: <b>{count}</b>",
            edit=edit,
            reply_markup=buttons(rows),
        )

    async def show(self, message, ident, *, edit=False):
        from .tg_bot import vacancy_card

        store = AutomationStore(self.path)
        try:
            job = store.get(ident)
            package = store.package(ident) if job["package_revision"] else None
            receipt_row = store.db.execute("SELECT receipt_json FROM application_receipts WHERE job_id=?", (ident,)).fetchone()
            receipt = json.loads(receipt_row[0]) if receipt_row else None
            letter = None
            if package:
                if job["state"] == "SENT":
                    letter = receipt.get("letter_text", package["letter_text"]) if isinstance(receipt, dict) else package["letter_text"]
                else:
                    letter = self.engine.outgoing_letter(store, job, package)
            progress = store.queue_status(ident)
            ai_pause = store.db.execute("SELECT retry_at FROM automation_ai_pause WHERE id=1").fetchone()
        finally:
            store.close()

        st_icon, st_label = JOB_STATE_BADGES.get(job["state"], ("•", job["state"]))
        if job["state"] == "STALE" and not package:
            st_label = "Очередь устарела (до подготовки)"
        rev_tag = f" · вер. #{package['revision']}" if package else ""
        sections = [f"{st_icon} <b>Задание #{ident} · {esc(st_label)}</b>{rev_tag}"]

        sections.append(vacancy_card(json.loads(job["raw_json"])))

        if job["evaluation_json"]:
            evaluation = json.loads(job["evaluation_json"])
            sections.append(f"🧠 Соответствие: {score_badge(evaluation['score'])}\n{esc(evaluation['summary'], 450)}")

        if job["error"]:
            sections.append(f"⚠️ <b>Причина / статус:</b> {esc(job['error'], 350)}")
        elif job["state"] == "STALE":
            sections.append(
                "⚠️ <b>Причина / статус:</b> Фильтры поиска или профиль изменились после постановки в очередь. "
                "Нажмите «🔄 Подготовить заново», чтобы обработать вакансию с актуальными настройками."
            )

        if job["state"] == "SENT" and receipt:
            confirmed = fmt_dt(receipt.get("confirmed_at"), suffix="UTC")
            sections.append(
                f"✅ <b>Доставка подтверждена площадкой</b> · версия #{receipt['revision']}\n"
                f"Время подтверждения: <code>{confirmed}</code>"
            )
        if job["state"] in ("UNKNOWN", "UNKNOWN_DISMISSED"):
            sections.append("⚠️ <b>Повторная отправка заблокирована.</b> Проверьте раздел «Отклики» на rabota.by.")
        if job["state"] == "WAITING_AI":
            reset = (
                datetime.fromisoformat(ai_pause[0]).astimezone(MINSK).strftime("%d.%m %H:%M")
                if ai_pause
                else "при следующей обработке"
            )
            sections.append(
                f"⏸ Локальный лимит OpenAI исчерпан. Продолжение автоматически: <b>{reset}</b> по Минску "
                "(если бот запущен)."
            )
        if job["state"] == "QUEUED":
            queue_info = (
                f"⏳ Пакет ожидает подготовки.\n"
                f"Позиция в очереди: {progress['position']} из {progress['waiting']}\n"
                f"Обработано сегодня: {progress['prepared']}/{progress['daily_limit']}"
            )
            if progress["mode"] == "paused":
                queue_info += "\n<i>Обработка на паузе — включите режим в настройках.</i>"
            elif progress["remaining"] == 0:
                queue_info += f"\n⚠️ <b>Дневной лимит исчерпан.</b> Обновление: {progress['reset']} по Минску."
            else:
                queue_info += "\n<i>Обработка начнётся автоматически по очереди.</i>"
            sections.append(queue_info)

        actions = []
        if package:
            metadata = json.loads(package["metadata_json"])
            key = metadata.get("direction")
            role_label = ROLES.get(key, "—")
            questions = metadata.get("questions", []) + metadata.get("gaps", [])
            if questions:
                q_list = "\n".join(f"• {esc(q, 180)}" for q in questions[:6])
                sections.append(f"❓ <b>Проверить перед отправкой:</b>\n{q_list}")

            char_badge = f"<code>{fmt_int(len(letter))}/{fmt_int(LETTER_LIMIT)}</code> симв."
            sections.append(
                f"📄 <b>Резюме:</b> {esc(role_label)}\n"
                f"✉️ <b>Сопроводительное письмо</b> ({char_badge}):\n"
                f"{expandable(letter, 1950)}"
            )

            prefix = f"{ident}:{package['revision']}"
            primary_row = []
            if job["state"] in ("REVIEW", "READY", "BLOCKED"):
                is_current = True
                if hasattr(self.engine, "current"):
                    try:
                        chk = self.engine.current(job)
                        if isinstance(chk, bool):
                            is_current = chk
                    except Exception:
                        pass
                if not is_current:
                    sections.append(
                        "⚠️ <b>Фильтры или профиль изменились после подготовки этого пакета.</b>\n"
                        "Нажмите <b>«🔄 Подготовить заново»</b> перед отправкой."
                    )
                elif key in DIRECTIONS:
                    if progress["browser_hold"]:
                        sections.append("⏸ <i>Отправка приостановлена до ручной проверки окна браузера (см. Настройки).</i>")
                    elif len(letter) <= LETTER_LIMIT:
                        primary_row.append(("✅ Отправить", f"flow:send:{prefix}"))
                    else:
                        sections.append(f"⚠️ <b>Сократите письмо:</b> допустимо не более {fmt_int(LETTER_LIMIT)} символов со ссылками.")
                    primary_row.append(("✏️ Изменить письмо", f"flow:editletter:{prefix}"))
                else:
                    sections.append("⚠️ <i>Пакет старого AI-направления — нажмите «Подготовить заново».</i>")
            if primary_row:
                actions.append(primary_row)
        elif job["state"] == "UNSUITABLE":
            eval_dir = json.loads(job["evaluation_json"] or "{}").get("resume")
            if eval_dir in DIRECTIONS:
                sections.append(f"📄 <b>Рекомендованное резюме:</b> {esc(ROLES[eval_dir])}")
                actions.append([("🚀 Принудительно отправить", f"flow:forcesend:{ident}:{eval_dir}")])
                actions.append([
                    ("📝 Подготовить письмо", f"flow:forceprep:{ident}:{eval_dir}"),
                    ("📄 Другое резюме", f"flow:forcedir:{ident}"),
                ])
            else:
                actions.append([("🚀 Принудительный отклик (выбрать резюме)", f"flow:forcedir:{ident}")])

        secondary_row = []
        if job["state"] in ("FAILED", "STALE", "REVIEW", "READY", "BLOCKED"):
            secondary_row.append(("🔄 Подготовить заново", f"flow:retry:{ident}"))
        revision = job["package_revision"] or 0
        if job["state"] in REJECTABLE:
            secondary_row.append(("✖️ Отклонить", f"flow:reject:{ident}:{revision}"))
        elif job["state"] == "UNKNOWN":
            secondary_row.append(("🗃 Убрать из внимания", f"flow:dismiss:{ident}:{revision}"))
        if secondary_row:
            actions.append(secondary_row)

        util_row = []
        if package:
            util_row.append(("📥 Письмо TXT", f"flow:export:{ident}:{package['revision']}"))
        util_row.append(("🔃 Обновить", f"flow:view:{ident}"))
        actions.append(util_row)

        category = next((k for k, (_, sts) in CATEGORIES.items() if sts and job["state"] in sts), "all")
        cat_icon = CATEGORY_ICONS.get(category, "📋")
        actions.append([
            (f"← {cat_icon} В категорию", f"flow:category:{category}:0"),
            ("🗂 Категории", "flow:categories:0"),
            ("⚙️ Настройки", "flow:settings:0"),
        ])

        await render(message, "\n\n".join(sections), edit=edit, reply_markup=buttons(actions))

    def _apply_setting(self, store, field, raw_value):
        settings = store.configure(field, raw_value)
        if field in ("ai_daily_calls", "ai_daily_tokens"):
            with store.db:
                store.db.execute("DELETE FROM automation_ai_pause")
                store.db.execute("UPDATE application_jobs SET state='QUEUED',error=NULL WHERE state='WAITING_AI'")
        return settings

    async def callback(self, callback):
        self.pending = None
        parts = callback.data.split(":")
        if len(parts) < 3:
            raise ValueError("Некорректная кнопка.")
        action, value = parts[1:3]
        if action in ("categories", "category"):
            await callback.answer()
            await self.queue(
                callback.message,
                int(parts[3]) if action == "category" and len(parts) == 4 else 0,
                value if action == "category" else None,
                edit=True,
            )
            return
        store = AutomationStore(self.path)
        try:
            if action == "browserresume":
                await callback.answer()
                try:
                    if self.engine.browser_series:
                        await self.engine.browser_series.resume_in_series(store.settings()["browser_idle_minutes"])
                except ValueError as exc:
                    await render(callback.message, f"⚠️ {esc(exc)}")
                    return
                store.configure("browser_hold", False)
                await self.settings(callback.message, edit=True)
                return
            if action == "mode":
                store.configure("mode", value)
                await callback.answer(f"Режим: {MODES.get(value, value)}")
                await self.settings(callback.message, edit=True)
                return
            if action == "preset":
                raw_val = ":".join(parts[3:])
                if value not in SETTING_PROMPTS or not raw_val:
                    raise ValueError("Неизвестная настройка.")
                settings = self._apply_setting(store, value, raw_val)
                if value == "browser_idle_minutes" and self.engine.browser_series:
                    await self.engine.browser_series.set_idle_minutes(settings["browser_idle_minutes"])
                await callback.answer("Сохранено ✅")
                await self.settings(callback.message, edit=True)
                return
            if action == "setting":
                if value not in SETTING_PROMPTS:
                    raise ValueError("Неизвестная настройка.")
                self.pending = ("setting", value)
                prompt_text, presets = SETTING_PROMPTS[value]
                preset_row = [(lbl, f"flow:preset:{value}:{val}") for lbl, val in presets]
                await callback.answer()
                await render(
                    callback.message,
                    f"{prompt_text}\n\n<i>Или отправьте своё значение сообщением (отмена: /cancel).</i>",
                    edit=True,
                    reply_markup=buttons([preset_row, [("← Назад к настройкам", "flow:settings:0")]]),
                )
                return
            if action in ("settings", "list"):
                await callback.answer()
                if action == "settings":
                    await self.settings(callback.message, edit=True)
                else:
                    await self.queue(callback.message, int(value), "all", edit=True)
                return
            if action in ("retry_stale", "reject_stale"):
                from .collector import search_key
                from .config import SearchConfig
                from .resumes import snapshot

                stamp = datetime.now(timezone.utc).isoformat()
                if action == "retry_stale":
                    cur_key = search_key(SearchConfig.load(self.engine.service.config_path))
                    digest = snapshot(self.path)[1]
                    with store.db:
                        store.db.execute("BEGIN IMMEDIATE")
                        ids = [r[0] for r in store.db.execute("SELECT id FROM application_jobs WHERE state='STALE'").fetchall()]
                        if ids:
                            store.db.execute(
                                "UPDATE application_jobs SET state='QUEUED',config_key=?,profile_hash=?,error=NULL,updated_at=? WHERE state='STALE'",
                                (cur_key, digest, stamp),
                            )
                            store.db.executemany(
                                "INSERT INTO application_events(job_id,state,created_at) VALUES (?,'QUEUED',?)",
                                [(jid, stamp) for jid in ids],
                            )
                    await callback.answer(f"В очередь возвращено: {len(ids)} 🔄")
                else:
                    with store.db:
                        store.db.execute("BEGIN IMMEDIATE")
                        ids = [r[0] for r in store.db.execute("SELECT id FROM application_jobs WHERE state='STALE'").fetchall()]
                        if ids:
                            store.db.execute(
                                "UPDATE application_jobs SET state='REJECTED',updated_at=? WHERE state='STALE'",
                                (stamp,),
                            )
                            store.db.executemany(
                                "INSERT INTO application_events(job_id,state,created_at) VALUES (?,'REJECTED',?)",
                                [(jid, stamp) for jid in ids],
                            )
                    await callback.answer(f"Отклонено устаревших: {len(ids)} ✖️")
                await self.queue(callback.message, category="attention", edit=True)
                return
            ident = int(value)
            job = store.get(ident)
            if action == "view":
                await callback.answer()
                await self.show(callback.message, ident, edit=True)
                return
            if action == "forcedir":
                if job["state"] not in ("UNSUITABLE", "REJECTED", "FAILED", "STALE"):
                    raise ValueError("Задание уже подготовлено или в работе.")
                title = str(json.loads(job["raw_json"]).get("name", "Вакансия"))
                dir_rows = [
                    [(f"🚀 {ROLES[d]}", f"flow:forcesend:{ident}:{d}"), ("📝 На проверку", f"flow:forceprep:{ident}:{d}")]
                    for d in DIRECTIONS
                ]
                dir_rows.append([("← Назад к заданию", f"flow:view:{ident}")])
                await callback.answer()
                await render(
                    callback.message,
                    f"🚀 <b>Принудительный отклик · задание #{ident}</b>\n"
                    f"💼 {esc(title, 180)}\n\n"
                    "Выберите направление резюме:\n"
                    "• <b>🚀 Роль</b> — сразу подготовить письмо и отправить отклик через Chrome\n"
                    "• <b>📝 На проверку</b> — только подготовить письмо для просмотра и правки",
                    edit=True,
                    reply_markup=buttons(dir_rows),
                )
                return
            if action == "retry":
                from .collector import search_key
                from .config import SearchConfig
                from .resumes import snapshot

                digest = snapshot(self.path)[1]
                with store.db:
                    store.db.execute("BEGIN IMMEDIATE")
                    job = store.get(ident)
                    if job["state"] not in ("FAILED", "STALE", "REVIEW", "READY", "BLOCKED", "UNSUITABLE"):
                        raise ValueError("Повтор этого задания запрещён.")
                    store.db.execute(
                        "UPDATE application_jobs SET state='QUEUED',config_key=?,profile_hash=?,error=NULL WHERE id=?",
                        (search_key(SearchConfig.load(self.engine.service.config_path)), digest, ident),
                    )
                await callback.answer("Поставлено в очередь на повторную подготовку 🔄")
                await self.show(callback.message, ident, edit=True)
                return
            if len(parts) != 4 or int(parts[3]) != (job["package_revision"] or 0):
                raise ValueError("Версия пакета изменилась. Откройте очередь заново.")
            if action == "reject":
                store.transition(ident, "REJECTED", expected=REJECTABLE, expected_revision=int(parts[3]))
            elif action == "dismiss":
                store.transition(
                    ident, "UNKNOWN_DISMISSED", expected=("UNKNOWN",), expected_revision=int(parts[3]), error=job["error"]
                )
            elif action == "editletter":
                if job["state"] not in ("REVIEW", "READY", "BLOCKED"):
                    raise ValueError("Это задание нельзя изменять.")
                self.pending = (action, ident, int(parts[3]))
            elif action == "export":
                package = store.package(ident, int(parts[3]))
                if job["state"] == "SENT":
                    row = store.db.execute("SELECT receipt_json FROM application_receipts WHERE job_id=?", (ident,)).fetchone()
                    receipt = json.loads(row[0]) if row else None
                    letter = receipt.get("letter_text", package["letter_text"]) if isinstance(receipt, dict) else package["letter_text"]
                else:
                    letter = self.engine.outgoing_letter(store, job, package)
                direction = json.loads(package["metadata_json"]).get("direction")
                text = (
                    f"Задание #{ident} · версия {package['revision']} · {STATES[job['state']]}\n"
                    f"Резюме: {ROLES.get(direction, '—')}\n\n{letter}"
                )
                await callback.answer()
                await callback.message.answer_document(
                    BufferedInputFile(
                        text.encode("utf-8-sig"), filename=f"package_{ident}_v{package['revision']}.txt"
                    )
                )
                return
            else:
                raise ValueError("Неизвестное действие.")
        finally:
            store.close()

        if action in ("reject", "dismiss") and job["state"] in CATEGORIES["attention"][1]:
            await callback.answer("Вакансия отклонена" if action == "reject" else "Задание убрано из внимания")
            await self.queue(callback.message, category="attention", edit=True)
            return
        await callback.answer("Отклонено ✖️" if action == "reject" else "")
        if action == "editletter":
            await render(
                callback.message,
                f"✏️ <b>Правка письма · задание #{ident}</b>\n\n"
                "Пришлите новый текст письма одним сообщением — бот автоматически добавит нужные ссылки на портфолио.\n"
                "<i>Отмена: /cancel</i>",
                reply_markup=buttons([[("✖️ Отмена", f"flow:view:{ident}")]]),
            )
        else:
            await self.show(callback.message, ident, edit=True)

    async def save_text(self, message):
        if not message.text:
            await render(message, "Отправьте значение текстом или нажмите /cancel.")
            return
        store = AutomationStore(self.path)
        try:
            pending = self.pending
            if pending[0] == "setting":
                settings = self._apply_setting(store, pending[1], message.text)
                if pending[1] == "browser_idle_minutes" and self.engine.browser_series:
                    await self.engine.browser_series.set_idle_minutes(settings["browser_idle_minutes"])
            else:
                _, ident, revision = pending
                package = store.package(ident, revision)
                letter = self.engine.outgoing_letter(store, store.get(ident), dict(package, letter_text=message.text))
                if len(letter) > LETTER_LIMIT:
                    raise ValueError(f"Сократите письмо: вместе со ссылками допускается до {LETTER_LIMIT} символов.")
                store.save_package(
                    ident,
                    "",
                    message.text,
                    package["description_id"],
                    dict(json.loads(package["metadata_json"]), user_edit=True),
                    expected_revision=revision,
                )
        except ValueError as exc:
            await render(message, f"⚠️ {esc(exc)}")
            return
        finally:
            store.close()
        self.pending = None
        await render(message, "✅ Изменение сохранено.")
        await (self.settings(message) if pending[0] == "setting" else self.show(message, pending[1]))
