"""Telegram views for explicit review of candidate facts."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .candidate_profile import ProfileStore, StaleFact

STATUS = {'UNCONFIRMED':'Не подтверждено', 'NEEDS_CLARIFICATION':'Нужно уточнить',
          'CONFIRMED':'Подтверждено', 'REJECTED':'Исключено'}


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

    async def overview(self, message):
        facts = self.facts()
        if not facts:
            await message.answer('Профиль пока не заполнен.')
            return
        counts = {status:sum(f['status']==status for f in facts) for status in STATUS}
        text = 'Профиль кандидата\n\n' + '\n'.join(f'{STATUS[k]}: {v}' for k,v in counts.items())
        text += ('\n\nПункты извлечены из резюме и требуют вашей проверки. Для будущей оценки вакансий '
                 'будут использоваться только подтверждённые сведения. Условия профиля не меняют фильтры поиска автоматически.')
        await message.answer(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='Проверить факты',callback_data='profile:page:0')]]))

    async def show_fact(self, message, index):
        facts = self.facts()
        if not 0 <= index < len(facts):
            await self.overview(message)
            return
        fact = facts[index]
        text = f"Пункт {index+1}/{len(facts)} · {fact['category']}\n{STATUS[fact['status']]} · версия {fact['revision']}\n\n{fact['value']}"
        if fact['question']:
            text += '\n\nУточните: ' + fact['question']
        # Original provenance remains in the database; after edits show the latest user source.
        sources = fact['sources'][-3:]
        text += '\n\nИсточники:\n' + '\n'.join(
            f"{s['document'][:120]}" + (f", стр. {s['page']}" if s.get('page') else '') + f": {s['quote'][:260]}"
            for s in sources)
        prefix = f"profile:"
        rows = [[InlineKeyboardButton(text=label,callback_data=f"{prefix}{action}:{fact['id']}:{fact['revision']}")
                 for action,label in [('confirm','Подтвердить'),('edit','Исправить'),('reject','Исключить')]]]
        rows.append([InlineKeyboardButton(text=label,callback_data=f'profile:page:{target}')
                     for label,target in [('← Назад',index-1),('Далее →',index+1)] if 0 <= target < len(facts)])
        rows = [row for row in rows if row]
        await message.answer(text[:4000],parse_mode=None,reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def callback(self, callback, actor_id):
        parts = (callback.data or '').split(':')
        self.pending = None
        if len(parts)==3 and parts[1]=='page':
            try:
                index=int(parts[2])
            except ValueError:
                await callback.answer('Неизвестная страница')
                return
            await callback.answer()
            await self.show_fact(callback.message,index)
            return
        if len(parts)!=4 or parts[1] not in ('confirm','reject','edit'):
            await callback.answer('Неизвестное действие')
            return
        action,fact_id=parts[1:3]
        store = ProfileStore(self.db_path)
        try:
            revision=int(parts[3])
            fact=store.get(fact_id)
            if fact['revision']!=revision:
                raise StaleFact('Факт изменён. Откройте профиль заново.')
            index=next(i for i,f in enumerate(store.all()) if f['id']==fact_id)
            if action=='edit':
                self.pending=(fact_id,revision,index)
            else:
                store.update(fact_id,revision,action,actor_id)
        except (ValueError, StopIteration) as exc:
            await callback.answer(str(exc)[:180],show_alert=True)
            return
        finally:
            store.close()
        await callback.answer()
        if action=='edit':
            await callback.message.answer('Отправьте актуальную формулировку одним сообщением (до 1800 символов). '
                                          'Она сохранится как неподтверждённая; затем нажмите «Подтвердить». Отмена: /cancel.')
        else:
            await self.show_fact(callback.message,index)

    async def save_text(self, message, actor_id):
        if not self.pending:
            return
        if not message.text:
            await message.answer('Отправьте формулировку текстом или /cancel.')
            return
        fact_id,revision,index=self.pending
        store=ProfileStore(self.db_path)
        try:
            store.update(fact_id,revision,'edit',actor_id,message.text)
        except StaleFact:
            self.pending=None
            await message.answer('Факт изменён. Откройте /profile заново.')
            return
        except ValueError as exc:
            await message.answer(str(exc))
            return
        finally:
            store.close()
        self.pending=None
        await message.answer('Изменение сохранено. Проверьте текст и подтвердите его отдельно.')
        await self.show_fact(message,index)
