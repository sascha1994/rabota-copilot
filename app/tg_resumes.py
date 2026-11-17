"""Private resume review, versioning and TXT export."""

import asyncio
import difflib
import json

from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from .assessment import AssessmentService, AssessmentError, DIRECTIONS, ROLES
from .resumes import ResumeStore, snapshot
from .letters import LetterStore


def keyboard(actions):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data)] for label, data in actions])


class ResumeConsole:
    def __init__(self, db_path, env):
        self.db_path, self.env, self.pending = db_path, env, None
        self.pending_letter = None

    async def overview(self, message):
        store = ResumeStore(self.db_path)
        try:
            lines = ['Мои резюме', 'Базовые тексты собираются из подтверждённых фактов. '
                     'Адаптация под вакансию доступна из её карточки.']
            for direction in DIRECTIONS:
                item = store.latest(direction)
                lines.append(f"{ROLES[direction]}: " + (f"версия #{item['id']} · " +
                             ('утверждена' if item['approved_at'] else 'черновик') if item else 'ещё нет'))
        finally:
            store.close()
        await message.answer('\n\n'.join(lines), parse_mode=None, reply_markup=keyboard([
            (ROLES[d], 'resume:open:' + d) for d in DIRECTIONS]))

    async def show(self, message, item):
        _, digest = snapshot(self.db_path)
        state = 'Утверждена' if item['approved_at'] else 'Черновик'
        if digest != item['profile_hash']:
            state += ' · профиль изменился'
        header = f"{ROLES[item['direction']]} · версия #{item['id']}\n{state}"
        if item['vacancy_id']:
            header += f"\nПод вакансию {item['vacancy_id']}"
        await message.answer(header, parse_mode=None)
        # Never truncate a resume during review.
        for start in range(0, len(item['text']), 3500):
            await message.answer(item['text'][start:start+3500], parse_mode=None)
        metadata = json.loads(item['metadata_json'])
        if metadata.get('description_id'):
            await message.answer(f"Описание вакансии: снимок #{metadata['description_id']}",
                reply_markup=keyboard([('Посмотреть описание', f"description:{metadata['description_id']}")]))
        questions = metadata.get('questions', [])
        if questions:
            await message.answer('Нужно уточнить (не включено в резюме):\n' + '\n'.join(questions), parse_mode=None)
        actions = [('Исправить текст', f"resume:edit:{item['id']}"),
                   ('Утвердить эту версию', f"resume:approve:{item['id']}"),
                   ('Выгрузить TXT', f"resume:export:{item['id']}"),
                   ('Новый базовый черновик', 'resume:base:' + item['direction'])]
        if item['parent_id']:
            actions += [('Показать изменения', f"resume:diff:{item['id']}"),
                        ('Предыдущая версия', f"resume:view:{item['parent_id']}")]
        if item['approved_at'] and item['vacancy_id']:
            actions.append(('Сопроводительное письмо', f"letter:open:{item['id']}"))
        await message.answer('Проверьте весь текст перед утверждением. Утверждение сохраняет версию в приложении.',
                             reply_markup=keyboard(actions))

    async def choose(self, callback):
        vid = callback.data.partition(':')[2]
        if not vid.isascii() or not vid.isdigit() or len(vid)>15:
            await callback.answer('Некорректная вакансия')
            return
        await callback.answer()
        await callback.message.answer('Выберите направление. Описание вакансии и подтверждённые '
            'профессиональные факты будут переданы в OpenAI для подготовки черновика.',
            reply_markup=keyboard([(ROLES[d], f'draft:{d}:{vid}') for d in DIRECTIONS]))

    async def generate(self, message, direction, vid):
        try:
            _, digest = snapshot(self.db_path)
            store = ResumeStore(self.db_path)
            try:
                parent = store.latest(direction)
            finally:
                store.close()
            service = AssessmentService(self.db_path, self.env)
            result, cached = await asyncio.to_thread(service.evaluate, vid, draft_direction=direction)
            text = ROLES[direction] + '\n\n' + '\n\n'.join(b.heading+'\n'+b.text for b in result.blocks)
            store = ResumeStore(self.db_path)
            try:
                item = store.create(direction, text, vacancy_id=vid,
                    parent_id=parent['id'] if parent else None, expected_hash=digest,
                    metadata={'description_id':service.description_id, 'blocks':[b.model_dump() for b in result.blocks],
                              'questions':result.questions, 'model':self.env.get('OPENAI_MODEL','gpt-5.4-mini')})
            finally:
                store.close()
            await message.answer('Черновик сохранён.' + (' Ответ OpenAI взят из кэша.' if cached else ''))
            await self.show(message, item)
        except (AssessmentError, ValueError) as exc:
            await message.answer(str(exc), parse_mode=None)
        except Exception:
            await message.answer('Не удалось подготовить резюме. Сохранённые версии доступны в /resumes.')

    async def callback(self, callback, actor):
        self.pending = None
        parts = (callback.data or '').split(':')
        if len(parts) != 3:
            await callback.answer('Неизвестное действие')
            return
        _, action, value = parts
        store = ResumeStore(self.db_path)
        try:
            if action in ('open', 'base'):
                if value not in (*DIRECTIONS,'ai'):
                    raise ValueError('Неизвестное направление.')
                item = store.latest(value) if action == 'open' else None
                item = item or store.base(value)
            else:
                item = store.get(int(value))
                if action == 'approve':
                    item = store.approve(item['id'], actor)
                elif action == 'edit':
                    self.pending = item['id']
                elif action == 'export':
                    stale = snapshot(self.db_path)[1] != item['profile_hash']
                    label = ('УТВЕРЖДЕНО' if item['approved_at'] else 'ЧЕРНОВИК') + (' · ПРОФИЛЬ ИЗМЕНИЛСЯ' if stale else '')
                    content = f"{label} · версия #{item['id']}\n\n{item['text']}\n"
                    await callback.answer()
                    await callback.message.answer_document(BufferedInputFile(content.encode('utf-8-sig'),
                        filename=f"resume_{item['direction']}_v{item['id']}.txt"))
                    return
                elif action == 'diff':
                    if not item['parent_id']:
                        raise ValueError('Это первая версия.')
                    parent = store.get(item['parent_id'])
                    diff = '\n'.join(difflib.unified_diff(parent['text'].splitlines(), item['text'].splitlines(),
                        fromfile=f"Версия {parent['id']}", tofile=f"Версия {item['id']}", lineterm='')) or 'Текст не изменился.'
                    await callback.answer()
                    await callback.message.answer_document(BufferedInputFile(diff.encode('utf-8-sig'), filename='resume_changes.txt'))
                    return
                elif action != 'view':
                    raise ValueError('Неизвестное действие.')
        except ValueError as exc:
            await callback.answer(str(exc)[:180], show_alert=True)
            return
        finally:
            store.close()
        await callback.answer()
        if action == 'edit':
            await callback.message.answer('Пришлите полный исправленный текст одним сообщением. '
                'Будет создана новая версия без утверждения. /cancel — отмена.')
        else:
            await self.show(callback.message, item)

    async def save_text(self, message):
        store = ResumeStore(self.db_path)
        try:
            item = store.edit(self.pending, message.text)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        finally:
            store.close()
        self.pending = None
        await self.show(message, item)

    async def show_letter(self, message, item):
        store = LetterStore(self.db_path)
        try:
            try:
                store.resume(item['resume_version'])
                stale = ''
            except ValueError:
                stale = '\nСвязанное резюме устарело; подготовьте новую версию.'
        finally:
            store.close()
        await message.answer(f"Письмо #{item['id']} · резюме #{item['resume_version']} · вакансия {item['vacancy_id']}\n"
            + ('Утверждено' if item['approved_at'] else 'Черновик') + stale + '\n\n' + item['text'],
            parse_mode=None)
        metadata = json.loads(item['metadata_json'])
        if metadata.get('description_id'):
            await message.answer(f"Описание вакансии: снимок #{metadata['description_id']}",
                reply_markup=keyboard([('Посмотреть описание', f"description:{metadata['description_id']}")]))
        questions = metadata.get('questions', [])
        if questions:
            await message.answer('Уточнить:\n' + '\n'.join(questions), parse_mode=None)
        actions = [(label, f"letter:{action}:{item['id']}") for label, action in
                   [('Исправить письмо','edit'), ('Утвердить письмо','approve'), ('Выгрузить TXT','export')]]
        if item['parent_id']:
            actions.append(('Предыдущая версия', f"letter:view:{item['parent_id']}"))
        actions.append(('Новый черновик через OpenAI', f"letter:generate:{item['resume_version']}"))
        await message.answer('Проверьте письмо перед утверждением. Для ручного отклика скачайте TXT.',
                             reply_markup=keyboard(actions))

    async def generate_letter(self, message, resume_id):
        try:
            store = LetterStore(self.db_path)
            try:
                resume = store.resume(resume_id)
                parent = store.latest(resume_id)
            finally:
                store.close()
            service = AssessmentService(self.db_path, self.env)
            result, cached = await asyncio.to_thread(service.evaluate, resume['vacancy_id'], letter_resume_id=resume_id)
            store = LetterStore(self.db_path)
            try:
                item = store.create(resume_id, result.text, parent_id=parent['id'] if parent else None,
                    metadata={'description_id':service.description_id, 'fact_ids':result.fact_ids,'questions':result.questions,
                              'model':self.env.get('OPENAI_MODEL','gpt-5.4-mini')})
            finally:
                store.close()
            await message.answer('Письмо сохранено.' + (' Ответ взят из кэша.' if cached else ''))
            await self.show_letter(message, item)
        except (AssessmentError, ValueError) as exc:
            await message.answer(str(exc), parse_mode=None)
        except Exception:
            await message.answer('Не удалось подготовить письмо. Попробуйте открыть его через /resumes.')

    async def letter_callback(self, callback, actor):
        self.pending_letter = None
        parts = (callback.data or '').split(':')
        if len(parts) != 3:
            await callback.answer('Неизвестное действие')
            return
        store = LetterStore(self.db_path)
        try:
            action, ident = parts[1], int(parts[2])
            if action == 'open':
                store.resume(ident)
                item = store.latest(ident)
                if item is None:
                    await callback.answer()
                    await callback.message.answer('Описание вакансии и подтверждённые профессиональные факты '
                        'будут переданы в OpenAI. Письмо будет связано с выбранной утверждённой версией резюме.',
                        reply_markup=keyboard([('Подготовить письмо', f'letter:generate:{ident}')]))
                    return
            else:
                item = store.get(ident)
                if action == 'approve':
                    item = store.approve(ident, actor)
                elif action == 'edit':
                    store.resume(item['resume_version'])
                    self.pending_letter = ident
                elif action == 'export':
                    try:
                        store.resume(item['resume_version'])
                        stale = ''
                    except ValueError:
                        stale = ' · РЕЗЮМЕ УСТАРЕЛО'
                    label = 'УТВЕРЖДЕНО' if item['approved_at'] else 'ЧЕРНОВИК'
                    content = f"{label}{stale} · письмо #{ident} · резюме #{item['resume_version']}\nВакансия {item['vacancy_id']}\n\n{item['text']}\n"
                    await callback.answer()
                    await callback.message.answer_document(BufferedInputFile(content.encode('utf-8-sig'), filename=f'letter_v{ident}.txt'))
                    return
                elif action != 'view':
                    raise ValueError('Неизвестное действие.')
        except ValueError as exc:
            await callback.answer(str(exc)[:180], show_alert=True)
            return
        finally:
            store.close()
        await callback.answer()
        if action == 'edit':
            await callback.message.answer('Пришлите полный текст письма до 1800 символов. '
                                          'Правка создаст новую неутверждённую версию. /cancel — отмена.')
        else:
            await self.show_letter(callback.message, item)

    async def save_letter(self, message):
        store = LetterStore(self.db_path)
        try:
            original = store.get(self.pending_letter)
            item = store.create(original['resume_version'], message.text, parent_id=original['id'],
                                metadata={'user_edit': True, 'description_id':json.loads(original['metadata_json']).get('description_id')})
        except ValueError as exc:
            await message.answer(str(exc), parse_mode=None)
            return
        finally:
            store.close()
        self.pending_letter = None
        await self.show_letter(message, item)
