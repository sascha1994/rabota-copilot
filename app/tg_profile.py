"""Telegram views for explicit review of candidate facts."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .candidate_profile import ProfileStore, StaleFact
from .tg_ui import bar, esc, expandable, inline_kb, render

STATUS = {
    "UNCONFIRMED": "Не подтверждено",
    "NEEDS_CLARIFICATION": "Нужно уточнить",
    "CONFIRMED": "Подтверждено",
    "REJECTED": "Исключено",
}

STATUS_BADGES = {
    "UNCONFIRMED": "🟡 Не подтверждено",
    "NEEDS_CLARIFICATION": "❓ Нужно уточнить",
    "CONFIRMED": "✅ Подтверждено",
    "REJECTED": "🚫 Исключено",
}


class ProfileConsole:
    def __init__(self, db_path):
        self.db_path = db_path
        self.pending = None

    def facts(self):
        store = ProfileStore(self.db_path)
        try:
            return store.all()
        finally:
            store.close()

    async def overview(self, message, *, edit=False):
        facts = self.facts()
        if not facts:
            await render(
                message,
                "👤 <b>Профиль кандидата</b>\n\n"
                "Профиль пока пуст. Импортируйте черновик фактов командой:\n"
                "<code>python -m app profile-import</code>",
                edit=edit,
                reply_markup=inline_kb([[("📄 Мои резюме", "resume:overview:0")]]),
            )
            return
        counts = {status: sum(f["status"] == status for f in facts) for status in STATUS}
        total = len(facts)
        confirmed = counts["CONFIRMED"]
        pending_count = counts["UNCONFIRMED"] + counts["NEEDS_CLARIFICATION"]
        text = (
            "👤 <b>Профиль кандидата</b>\n\n"
            f"Готовность базы фактов: <code>{bar(confirmed, total)}</code> <b>{confirmed}/{total}</b>\n\n"
            f"✅ Подтверждено: <b>{confirmed}</b>\n"
            f"🟡 Не подтверждено: <b>{counts['UNCONFIRMED']}</b>\n"
            f"❓ Нужно уточнить: <b>{counts['NEEDS_CLARIFICATION']}</b>\n"
            f"🚫 Исключено: <b>{counts['REJECTED']}</b>\n\n"
            "<i>В оценку вакансий и генерацию текстов попадают только подтверждённые факты. "
            "Условия профиля не меняют поисковые фильтры автоматически.</i>"
        )
        rows = [[("🔍 Все факты (с 1-го)", "profile:page:0")]]
        if pending_count:
            first_pending = next(
                (i for i, f in enumerate(facts) if f["status"] in ("UNCONFIRMED", "NEEDS_CLARIFICATION")),
                0,
            )
            rows.append([(f"🟡 Проверить ожидающие ({pending_count})", f"profile:page:{first_pending}")])
        rows.append([("📄 Мои резюме", "resume:overview:0")])
        await render(message, text, edit=edit, reply_markup=inline_kb(rows))

    async def show_fact(self, message, index, *, edit=False):
        facts = self.facts()
        if not 0 <= index < len(facts):
            await self.overview(message, edit=edit)
            return
        fact = facts[index]
        badge = STATUS_BADGES.get(fact["status"], esc(fact["status"]))
        text = (
            f"👤 <b>Факт {index + 1} из {len(facts)}</b> · <code>{esc(fact['category'], 80)}</code>\n"
            f"{badge} · вер. #{fact['revision']}\n\n"
            f"{esc(fact['value'], 1800)}"
        )
        if fact["question"]:
            text += f"\n\n❓ <b>Уточните перед подтверждением:</b>\n{esc(fact['question'], 450)}"
        sources = fact["sources"][-3:]
        if sources:
            src_lines = "\n".join(
                f"• {s['document'][:100]}"
                + (f", стр. {s['page']}" if s.get("page") else "")
                + f": {s['quote'][:240]}"
                for s in sources
            )
            text += f"\n\n📎 <b>Источники ({len(sources)}):</b>\n{expandable(src_lines, 1000)}"

        fid, rev = fact["id"], fact["revision"]
        action_row = [
            InlineKeyboardButton(
                text="✅ Подтвердить" + (" ✓" if fact["status"] == "CONFIRMED" else ""),
                callback_data=f"profile:confirm:{fid}:{rev}",
            ),
            InlineKeyboardButton(text="✏️ Исправить", callback_data=f"profile:edit:{fid}:{rev}"),
            InlineKeyboardButton(
                text="🚫 Исключить" + (" ✓" if fact["status"] == "REJECTED" else ""),
                callback_data=f"profile:reject:{fid}:{rev}",
            ),
        ]
        nav_row = []
        if index > 0:
            nav_row.append(InlineKeyboardButton(text="← Назад", callback_data=f"profile:page:{index - 1}"))
        nav_row.append(InlineKeyboardButton(text=f"📋 {index + 1}/{len(facts)}", callback_data="profile:overview:0"))
        if index + 1 < len(facts):
            nav_row.append(InlineKeyboardButton(text="Далее →", callback_data=f"profile:page:{index + 1}"))
        rows = [action_row, nav_row]

        next_pending = next(
            (
                (i + index + 1) % len(facts)
                for i in range(len(facts) - 1)
                if facts[(i + index + 1) % len(facts)]["status"] in ("UNCONFIRMED", "NEEDS_CLARIFICATION")
            ),
            None,
        )
        if next_pending is not None:
            rows.append([
                InlineKeyboardButton(
                    text=f"⏭ След. неподтверждённый (#{next_pending + 1})",
                    callback_data=f"profile:page:{next_pending}",
                )
            ])
        await render(message, text, edit=edit, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def callback(self, callback, actor_id):
        parts = (callback.data or "").split(":")
        self.pending = None
        if len(parts) == 3 and parts[1] == "overview":
            await callback.answer()
            await self.overview(callback.message, edit=True)
            return
        if len(parts) == 3 and parts[1] == "page":
            try:
                index = int(parts[2])
            except ValueError:
                await callback.answer("Неизвестная страница")
                return
            await callback.answer()
            await self.show_fact(callback.message, index, edit=True)
            return
        if len(parts) != 4 or parts[1] not in ("confirm", "reject", "edit"):
            await callback.answer("Неизвестное действие")
            return
        action, fact_id = parts[1:3]
        store = ProfileStore(self.db_path)
        try:
            revision = int(parts[3])
            fact = store.get(fact_id)
            if fact["revision"] != revision:
                raise StaleFact("Факт изменён. Откройте профиль заново.")
            index = next(i for i, f in enumerate(store.all()) if f["id"] == fact_id)
            if action == "edit":
                self.pending = (fact_id, revision, index)
            else:
                store.update(fact_id, revision, action, actor_id)
        except (ValueError, StopIteration) as exc:
            await callback.answer(str(exc)[:180], show_alert=True)
            return
        finally:
            store.close()
        if action == "edit":
            await callback.answer()
            await render(
                callback.message,
                f"✏️ <b>Исправление факта #{index + 1}</b>\n\n"
                "Отправьте актуальную формулировку одним сообщением (до 1 800 символов).\n"
                "<i>После сохранения статус станет «Не подтверждено» — проверьте текст и нажмите «Подтвердить».</i>",
                reply_markup=inline_kb([[("✖️ Отмена", f"profile:page:{index}")]]),
            )
        else:
            await callback.answer("Подтверждено ✅" if action == "confirm" else "Исключено 🚫")
            await self.show_fact(callback.message, index, edit=True)

    async def save_text(self, message, actor_id):
        if not self.pending:
            return
        if not message.text:
            await render(message, "Отправьте формулировку текстом или нажмите /cancel.")
            return
        fact_id, revision, index = self.pending
        store = ProfileStore(self.db_path)
        try:
            store.update(fact_id, revision, "edit", actor_id, message.text)
        except StaleFact:
            self.pending = None
            await render(message, "⚠️ Факт был изменён в другом окне. Откройте /profile заново.")
            return
        except ValueError as exc:
            await render(message, f"⚠️ {esc(exc)}")
            return
        finally:
            store.close()
        self.pending = None
        await render(message, "✏️ Изменение сохранено. Проверьте обновлённый текст ниже и подтвердите его:")
        await self.show_fact(message, index)
