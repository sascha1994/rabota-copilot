"""Private resume review, versioning and TXT export."""

import asyncio
import difflib
import json

from aiogram.types import BufferedInputFile

from .assessment import AssessmentError, AssessmentService, DIRECTIONS, ROLES
from .letters import LetterStore
from .portfolio import LETTER_LIMIT
from .resumes import ResumeStore, snapshot
from .tg_ui import esc, expandable, fmt_int, inline_kb, render


class ResumeConsole:
    def __init__(self, db_path, env):
        self.db_path, self.env, self.pending = db_path, env, None
        self.pending_letter = None

    async def overview(self, message, *, edit=False):
        _, current_digest = snapshot(self.db_path)
        store = ResumeStore(self.db_path)
        try:
            lines = [
                "📄 <b>Мои резюме (черновики)</b>\n"
                "<i>Базовые версии собираются из подтверждённых фактов профиля, "
                "а адаптация под конкретную вакансию запускается из её карточки.</i>\n"
            ]
            for direction in DIRECTIONS:
                item = store.latest(direction)
                if not item:
                    status = "⚪ ещё не создано"
                else:
                    stale = item["profile_hash"] != current_digest
                    badge = "✅ утверждена" if item["approved_at"] else "📝 черновик"
                    if stale:
                        badge += " · ⚠️ профиль изменён"
                    status = f"вер. #{item['id']} · {badge}"
                lines.append(f"• <b>{esc(ROLES[direction])}:</b> {status}")
        finally:
            store.close()
        dirs = list(DIRECTIONS)
        rows = [
            [(f"📄 {ROLES[d]}", f"resume:open:{d}") for d in dirs[i : i + 2]]
            for i in range(0, len(dirs), 2)
        ]
        rows.append([("👤 Профиль кандидата", "profile:overview:0")])
        await render(message, "\n".join(lines), edit=edit, reply_markup=inline_kb(rows))

    async def show(self, message, item, *, edit=False):
        _, digest = snapshot(self.db_path)
        stale = digest != item["profile_hash"]
        badge = "✅ Утверждена" if item["approved_at"] else "📝 Черновик"
        if stale:
            badge += " · ⚠️ профиль изменился"
        header = f"📄 <b>{esc(ROLES[item['direction']])}</b> · вер. #{item['id']}\nСтатус: <b>{badge}</b>"
        if item["vacancy_id"]:
            header += f" · вакансия <code>#{esc(item['vacancy_id'])}</code>"

        metadata = json.loads(item["metadata_json"])
        questions = metadata.get("questions", [])
        q_block = ""
        if questions:
            q_items = "\n".join(f"• {esc(q, 280)}" for q in questions[:6])
            q_block = f"\n\n❓ <b>Уточнить (не включено в резюме):</b>\n{q_items}"

        actions = [
            [
                ("✅ Утвердить" + (" ✓" if item["approved_at"] else ""), f"resume:approve:{item['id']}"),
                ("✏️ Исправить", f"resume:edit:{item['id']}"),
            ],
        ]
        if item["approved_at"] and item["vacancy_id"]:
            actions.append([("✉️ Сопроводительное письмо", f"letter:open:{item['id']}")])
        if item["parent_id"]:
            actions.append([
                ("🔍 Изменения", f"resume:diff:{item['id']}"),
                ("⏪ Пред. версия", f"resume:view:{item['parent_id']}"),
            ])
        extra_row = [
            ("📥 TXT", f"resume:export:{item['id']}"),
            ("🔄 Базовый", f"resume:base:{item['direction']}"),
        ]
        if metadata.get("description_id"):
            extra_row.append((f"🕒 Описание #{metadata['description_id']}", f"description:{metadata['description_id']}"))
        actions.append(extra_row)
        actions.append([("← Все резюме", "resume:overview:0")])
        kb = inline_kb(actions)

        # Never truncate resume text during review: single card if compact, chunked otherwise.
        full_text = item["text"]
        if len(full_text) <= 2300:
            body = (
                f"{header}\n\n"
                f"{esc(full_text)}"
                f"{q_block}\n\n"
                "<i>Проверьте текст перед утверждением.</i>"
            )
            await render(message, body, edit=edit, reply_markup=kb)
        else:
            await render(message, header)
            for start in range(0, len(full_text), 3500):
                await render(message, esc(full_text[start : start + 3500]))
            footer = (q_block.lstrip() + "\n\n" if q_block else "") + "<i>Проверьте весь текст выше перед утверждением.</i>"
            await render(message, footer, reply_markup=kb)

    async def choose(self, callback):
        vid = callback.data.partition(":")[2]
        if not vid.isascii() or not vid.isdigit() or len(vid) > 15:
            await callback.answer("Некорректная вакансия")
            return
        await callback.answer()
        dirs = list(DIRECTIONS)
        rows = [
            [(f"📄 {ROLES[d]}", f"draft:{d}:{vid}") for d in dirs[i : i + 2]]
            for i in range(0, len(dirs), 2)
        ]
        await render(
            callback.message,
            f"📄 <b>Адаптация резюме · вакансия #{esc(vid)}</b>\n\n"
            "Выберите целевое направление. Описание вакансии и подтверждённые факты профиля "
            "будут переданы в OpenAI.",
            reply_markup=inline_kb(rows),
        )

    async def generate(self, message, direction, vid):
        progress = await render(message, f"⏳ <b>Готовлю черновик резюме ({esc(ROLES[direction])})…</b>")
        try:
            _, digest = snapshot(self.db_path)
            store = ResumeStore(self.db_path)
            try:
                parent = store.latest(direction)
            finally:
                store.close()
            service = AssessmentService(self.db_path, self.env)
            result, cached = await asyncio.to_thread(service.evaluate, vid, draft_direction=direction)
            text = ROLES[direction] + "\n\n" + "\n\n".join(b.heading + "\n" + b.text for b in result.blocks)
            store = ResumeStore(self.db_path)
            try:
                item = store.create(
                    direction,
                    text,
                    vacancy_id=vid,
                    parent_id=parent["id"] if parent else None,
                    expected_hash=digest,
                    metadata={
                        "description_id": service.description_id,
                        "blocks": [b.model_dump() for b in result.blocks],
                        "questions": result.questions,
                        "model": self.env.get("OPENAI_MODEL", "gpt-5.4-mini"),
                    },
                )
            finally:
                store.close()
            note = " <i>(ответ OpenAI из кэша)</i>" if cached else ""
            await render(progress or message, f"✅ <b>Черновик резюме сохранён</b>{note}", edit=bool(progress))
            await self.show(message, item)
        except (AssessmentError, ValueError) as exc:
            await render(progress or message, f"⚠️ <b>Ошибка подготовки:</b>\n{esc(exc)}", edit=bool(progress))
        except Exception:
            await render(
                progress or message,
                "❌ Не удалось подготовить резюме. Сохранённые версии доступны в /resumes.",
                edit=bool(progress),
            )

    async def callback(self, callback, actor):
        self.pending = None
        parts = (callback.data or "").split(":")
        if len(parts) != 3:
            await callback.answer("Неизвестное действие")
            return
        _, action, value = parts
        if action == "overview":
            await callback.answer()
            await self.overview(callback.message, edit=True)
            return
        store = ResumeStore(self.db_path)
        try:
            if action in ("open", "base"):
                if value not in (*DIRECTIONS, "ai"):
                    raise ValueError("Неизвестное направление.")
                item = store.latest(value) if action == "open" else None
                item = item or store.base(value)
            else:
                item = store.get(int(value))
                if action == "approve":
                    item = store.approve(item["id"], actor)
                elif action == "edit":
                    self.pending = item["id"]
                elif action == "export":
                    stale = snapshot(self.db_path)[1] != item["profile_hash"]
                    label = ("УТВЕРЖДЕНО" if item["approved_at"] else "ЧЕРНОВИК") + (
                        " · ПРОФИЛЬ ИЗМЕНИЛСЯ" if stale else ""
                    )
                    content = f"{label} · версия #{item['id']}\n\n{item['text']}\n"
                    await callback.answer()
                    await callback.message.answer_document(
                        BufferedInputFile(
                            content.encode("utf-8-sig"),
                            filename=f"resume_{item['direction']}_v{item['id']}.txt",
                        )
                    )
                    return
                elif action == "diff":
                    if not item["parent_id"]:
                        raise ValueError("Это первая версия.")
                    parent = store.get(item["parent_id"])
                    diff = "\n".join(
                        difflib.unified_diff(
                            parent["text"].splitlines(),
                            item["text"].splitlines(),
                            fromfile=f"Версия {parent['id']}",
                            tofile=f"Версия {item['id']}",
                            lineterm="",
                        )
                    ) or "Текст не изменился."
                    await callback.answer()
                    if len(diff) <= 2200:
                        await render(
                            callback.message,
                            f"🔍 <b>Изменения резюме: #{parent['id']} → #{item['id']}</b>\n"
                            f"{expandable(diff, 2200)}",
                            reply_markup=inline_kb([[("← К резюме", f"resume:view:{item['id']}")]]),
                        )
                    else:
                        await callback.message.answer_document(
                            BufferedInputFile(diff.encode("utf-8-sig"), filename="resume_changes.txt")
                        )
                    return
                elif action != "view":
                    raise ValueError("Неизвестное действие.")
        except ValueError as exc:
            await callback.answer(str(exc)[:180], show_alert=True)
            return
        finally:
            store.close()
        if action == "edit":
            await callback.answer()
            await render(
                callback.message,
                f"✏️ <b>Редактирование резюме · вер. #{item['id']}</b>\n\n"
                "Пришлите полный исправленный текст одним сообщением — будет создана новая версия без утверждения.",
                reply_markup=inline_kb([[("✖️ Отмена", f"resume:view:{item['id']}")]]),
            )
        else:
            await callback.answer("Версия утверждена ✅" if action == "approve" else "")
            await self.show(callback.message, item, edit=True)

    async def save_text(self, message):
        store = ResumeStore(self.db_path)
        try:
            item = store.edit(self.pending, message.text)
        except ValueError as exc:
            await render(message, f"⚠️ {esc(exc)}")
            return
        finally:
            store.close()
        self.pending = None
        await self.show(message, item)

    async def show_letter(self, message, item, *, edit=False):
        store = LetterStore(self.db_path)
        try:
            try:
                store.resume(item["resume_version"])
                stale = ""
            except ValueError:
                stale = "\n⚠️ <b>Связанное резюме устарело</b> — подготовьте новую версию."
        finally:
            store.close()
        badge = "✅ Утверждено" if item["approved_at"] else "📝 Черновик"
        metadata = json.loads(item["metadata_json"])
        questions = metadata.get("questions", [])
        q_block = ""
        if questions:
            q_items = "\n".join(f"• {esc(q, 280)}" for q in questions[:5])
            q_block = f"\n\n❓ <b>Уточнить:</b>\n{q_items}"

        text = (
            f"✉️ <b>Письмо #{item['id']}</b> · резюме #{item['resume_version']} · вакансия <code>#{esc(item['vacancy_id'])}</code>\n"
            f"Статус: <b>{badge}</b> · <code>{fmt_int(len(item['text']))}/{fmt_int(LETTER_LIMIT)}</code> симв.{stale}\n\n"
            f"{expandable(item['text'], 2000)}"
            f"{q_block}"
        )
        actions = [
            [
                ("✅ Утвердить" + (" ✓" if item["approved_at"] else ""), f"letter:approve:{item['id']}"),
                ("✏️ Исправить", f"letter:edit:{item['id']}"),
            ],
            [
                ("📥 TXT", f"letter:export:{item['id']}"),
                ("🔄 Пересобрать ИИ", f"letter:generate:{item['resume_version']}"),
            ],
        ]
        nav = []
        if item["parent_id"]:
            nav.append(("⏪ Пред. версия", f"letter:view:{item['parent_id']}"))
        if metadata.get("description_id"):
            nav.append((f"🕒 Описание #{metadata['description_id']}", f"description:{metadata['description_id']}"))
        if nav:
            actions.append(nav)
        actions.append([(f"← К резюме #{item['resume_version']}", f"resume:view:{item['resume_version']}")])
        await render(message, text, edit=edit, reply_markup=inline_kb(actions))

    async def generate_letter(self, message, resume_id):
        progress = await render(message, "⏳ <b>Готовлю сопроводительное письмо через OpenAI…</b>")
        try:
            store = LetterStore(self.db_path)
            try:
                resume = store.resume(resume_id)
                parent = store.latest(resume_id)
            finally:
                store.close()
            service = AssessmentService(self.db_path, self.env)
            result, cached = await asyncio.to_thread(service.evaluate, resume["vacancy_id"], letter_resume_id=resume_id)
            store = LetterStore(self.db_path)
            try:
                item = store.create(
                    resume_id,
                    result.text,
                    parent_id=parent["id"] if parent else None,
                    metadata={
                        "description_id": service.description_id,
                        "fact_ids": result.fact_ids,
                        "questions": result.questions,
                        "model": self.env.get("OPENAI_MODEL", "gpt-5.4-mini"),
                    },
                )
            finally:
                store.close()
            note = " <i>(из кэша)</i>" if cached else ""
            await render(progress or message, f"✅ <b>Письмо сохранено</b>{note}", edit=bool(progress))
            await self.show_letter(message, item)
        except (AssessmentError, ValueError) as exc:
            await render(progress or message, f"⚠️ <b>Ошибка подготовки письма:</b>\n{esc(exc)}", edit=bool(progress))
        except Exception:
            await render(progress or message, "❌ Не удалось подготовить письмо. Откройте /resumes.", edit=bool(progress))

    async def letter_callback(self, callback, actor):
        self.pending_letter = None
        parts = (callback.data or "").split(":")
        if len(parts) != 3:
            await callback.answer("Неизвестное действие")
            return
        store = LetterStore(self.db_path)
        try:
            action, ident = parts[1], int(parts[2])
            if action == "open":
                store.resume(ident)
                item = store.latest(ident)
                if item is None:
                    await callback.answer()
                    await render(
                        callback.message,
                        f"✉️ <b>Сопроводительное письмо · резюме #{ident}</b>\n\n"
                        "Описание вакансии и подтверждённые профессиональные факты будут переданы в OpenAI.",
                        reply_markup=inline_kb([
                            [("✨ Подготовить письмо", f"letter:generate:{ident}")],
                            [("← К резюме", f"resume:view:{ident}")],
                        ]),
                    )
                    return
            else:
                item = store.get(ident)
                if action == "approve":
                    item = store.approve(ident, actor)
                elif action == "edit":
                    store.resume(item["resume_version"])
                    self.pending_letter = ident
                elif action == "export":
                    try:
                        store.resume(item["resume_version"])
                        stale = ""
                    except ValueError:
                        stale = " · РЕЗЮМЕ УСТАРЕЛО"
                    label = "УТВЕРЖДЕНО" if item["approved_at"] else "ЧЕРНОВИК"
                    content = (
                        f"{label}{stale} · письмо #{ident} · резюме #{item['resume_version']}\n"
                        f"Вакансия {item['vacancy_id']}\n\n{item['text']}\n"
                    )
                    await callback.answer()
                    await callback.message.answer_document(
                        BufferedInputFile(content.encode("utf-8-sig"), filename=f"letter_v{ident}.txt")
                    )
                    return
                elif action != "view":
                    raise ValueError("Неизвестное действие.")
        except ValueError as exc:
            await callback.answer(str(exc)[:180], show_alert=True)
            return
        finally:
            store.close()
        if action == "edit":
            await callback.answer()
            await render(
                callback.message,
                f"✏️ <b>Редактирование письма #{ident}</b>\n\n"
                f"Пришлите новый текст письма (до {fmt_int(LETTER_LIMIT)} символов). "
                "Правка создаст новую неутверждённую версию.",
                reply_markup=inline_kb([[("✖️ Отмена", f"letter:view:{ident}")]]),
            )
        else:
            await callback.answer("Письмо утверждено ✅" if action == "approve" else "")
            await self.show_letter(callback.message, item, edit=True)

    async def save_letter(self, message):
        store = LetterStore(self.db_path)
        try:
            original = store.get(self.pending_letter)
            item = store.create(
                original["resume_version"],
                message.text,
                parent_id=original["id"],
                metadata={
                    "user_edit": True,
                    "description_id": json.loads(original["metadata_json"]).get("description_id"),
                },
            )
        except ValueError as exc:
            await render(message, f"⚠️ {esc(exc)}")
            return
        finally:
            store.close()
        self.pending_letter = None
        await self.show_letter(message, item)
