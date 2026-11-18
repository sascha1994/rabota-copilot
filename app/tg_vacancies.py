"""Local vacancy decisions and saved description snapshots."""

import json

from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from .storage import Storage
from .tg_ui import esc, fmt_dt, inline_kb, render

STATES = {
    "interested": "Интересно",
    "skipped": "Пропустить",
    "applied": "Откликнулся вручную",
    "new": "Без отметки",
}

STATE_BADGES = {
    "interested": "⭐ Интересно",
    "skipped": "⏭ Пропущено",
    "applied": "📨 Откликнулся вручную",
}


def vacancy_actions(vid, current=None):
    def mark_btn(state, icon, short_label):
        checked = " ✓" if current == state else ""
        return InlineKeyboardButton(text=f"{icon} {short_label}{checked}", callback_data=f"mark:{state}:{vid}")

    rows = [
        [
            InlineKeyboardButton(text="🧠 Оценить", callback_data=f"assess:{vid}"),
            InlineKeyboardButton(text="📄 Резюме", callback_data=f"prepare:{vid}"),
        ],
        [
            mark_btn("interested", "⭐", "Интересно"),
            mark_btn("skipped", "⏭", "Пропустить"),
        ],
        [
            mark_btn("applied", "📨", "Вручную"),
            InlineKeyboardButton(text="✖ Сброс", callback_data=f"mark:new:{vid}"),
            InlineKeyboardButton(text="🕒 История", callback_data=f"history:{vid}"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


class VacancyConsole:
    def __init__(self, db_path):
        self.db_path = db_path

    async def history(self, message, vid, *, edit=False):
        if not vid.isascii() or not vid.isdigit() or len(vid) > 15:
            raise ValueError("Некорректный ID вакансии.")
        store = Storage(self.db_path)
        try:
            rows = store.descriptions(vid)
        finally:
            store.close()
        if not rows:
            await render(
                message,
                f"🕒 <b>История описания · #{esc(vid)}</b>\n\n"
                "Сохранённых снимков ещё нет — текст сохранится автоматически при первой оценке или подготовке резюме.",
                edit=edit,
            )
            return
        text = (
            f"🕒 <b>История описания · вакансия #{esc(vid)}</b>\n"
            f"Сохранено снимков: <b>{len(rows)}</b> (UTC)\n\n"
            "<i>Нажмите на снимок, чтобы выгрузить полный текст описания в TXT.</i>"
        )
        kb = inline_kb([
            [(f"📄 Снимок #{r['id']} · {fmt_dt(r['first_seen'], suffix='UTC')}", f"description:{r['id']}")]
            for r in rows
        ])
        await render(message, text, edit=edit, reply_markup=kb)

    async def callback(self, callback):
        from .tg_bot import vacancy_card

        parts = callback.data.split(":")
        store = Storage(self.db_path)
        try:
            if len(parts) == 3 and parts[0] == "mark":
                state, vid = parts[1:]
                if not vid.isascii() or not vid.isdigit() or len(vid) > 15:
                    raise ValueError("Некорректный ID вакансии.")
                store.mark(vid, state)
                row = store.db.execute("SELECT raw_json FROM vacancies WHERE id=?", (vid,)).fetchone()
                item = json.loads(row[0]) if row else None
                if item is not None and state != "new":
                    item["_tracking_state"] = state
            elif len(parts) == 2 and parts[0] == "history":
                store.close()
                store = None
                await callback.answer()
                await self.history(callback.message, parts[1])
                return
            elif len(parts) == 2 and parts[0] == "description":
                item = store.description(int(parts[1]))
                text = (
                    f"Вакансия {item['vacancy_id']} · снимок #{item['id']}\n"
                    f"Получено: {item['first_seen']}\n"
                    f"Последняя проверка: {item['last_seen']}\n\n"
                    f"{item['text']}\n"
                )
                await callback.answer()
                await callback.message.answer_document(
                    BufferedInputFile(
                        text.encode("utf-8-sig"),
                        filename=f"vacancy_{item['vacancy_id']}_snapshot_{item['id']}.txt",
                    )
                )
                return
            else:
                raise ValueError("Неизвестное действие.")
        except ValueError as exc:
            await callback.answer(str(exc)[:180], show_alert=True)
            return
        finally:
            if store is not None:
                store.close()

        toast = f"Отметка: {STATES[state]}"
        await callback.answer(toast)
        active = None if state == "new" else state
        if item is not None and getattr(callback.message, "bot", None) is not None:
            await render(
                callback.message,
                vacancy_card(item),
                edit=True,
                reply_markup=vacancy_actions(vid, active),
            )
        else:
            note = "Это ваша отметка о ручном отклике." if state == "applied" else "Список отметок: /tracked"
            await render(callback.message, f"<b>Вакансия #{esc(vid)}:</b> {esc(STATES[state])}\n<i>{note}</i>")

    def tracked(self, page):
        store = Storage(self.db_path)
        try:
            return store.tracked((page - 1) * 5, 6)
        finally:
            store.close()
