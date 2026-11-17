"""Local vacancy decisions and saved description snapshots."""

from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from .storage import Storage

STATES = {'interested':'Интересно', 'skipped':'Пропустить',
          'applied':'Откликнулся вручную', 'new':'Без отметки'}


def vacancy_actions(vid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='Оценить вакансию',callback_data=f'assess:{vid}')],
        [InlineKeyboardButton(text='Подготовить резюме',callback_data=f'prepare:{vid}')],
        [InlineKeyboardButton(text=STATES[state],callback_data=f'mark:{state}:{vid}')
         for state in ('interested','skipped')],
        [InlineKeyboardButton(text=STATES['applied'],callback_data=f'mark:applied:{vid}')],
        [InlineKeyboardButton(text='Снять отметку',callback_data=f'mark:new:{vid}'),
         InlineKeyboardButton(text='История описания',callback_data=f'history:{vid}')]])


class VacancyConsole:
    def __init__(self, db_path):
        self.db_path = db_path

    async def history(self, message, vid):
        if not vid.isascii() or not vid.isdigit() or len(vid)>15:
            raise ValueError('Некорректный ID вакансии.')
        store = Storage(self.db_path)
        try:
            rows = store.descriptions(vid)
        finally:
            store.close()
        if not rows:
            await message.answer('Сохранённых описаний ещё нет. Снимок появится при оценке или подготовке резюме.')
            return
        await message.answer(f'Вакансия {vid}: последние {len(rows)} снимков описания. Время указано в UTC. '
            'Это история полученного текста; она не подтверждает, что вакансия открыта сейчас.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"#{r['id']} · {r['first_seen'][:19]}",callback_data=f"description:{r['id']}")]
                for r in rows]))

    async def callback(self, callback):
        parts = callback.data.split(':')
        store = Storage(self.db_path)
        try:
            if len(parts)==3 and parts[0]=='mark':
                state, vid = parts[1:]
                if not vid.isascii() or not vid.isdigit() or len(vid)>15:
                    raise ValueError('Некорректный ID вакансии.')
                store.mark(vid,state)
                await callback.answer('Отметка сохранена')
                await callback.message.answer(f'Вакансия {vid}: {STATES[state]}. '
                    + ('Это ваша отметка о ручном отклике.' if state=='applied' else 'Список отметок: /tracked'))
            elif len(parts)==2 and parts[0]=='history':
                await callback.answer()
                await self.history(callback.message,parts[1])
            elif len(parts)==2 and parts[0]=='description':
                item = store.description(int(parts[1]))
                text = f"Вакансия {item['vacancy_id']} · снимок #{item['id']}\nПолучено: {item['first_seen']}\nПоследняя проверка: {item['last_seen']}\n\n{item['text']}\n"
                await callback.answer()
                await callback.message.answer_document(BufferedInputFile(text.encode('utf-8-sig'),
                    filename=f"vacancy_{item['vacancy_id']}_snapshot_{item['id']}.txt"))
            else:
                raise ValueError('Неизвестное действие.')
        except ValueError as exc:
            await callback.answer(str(exc)[:180],show_alert=True)
        finally:
            store.close()

    def tracked(self, page):
        store = Storage(self.db_path)
        try:
            return store.tracked((page-1)*5,6)
        finally:
            store.close()
