"""Versioned candidate facts. Only explicit user confirmation makes facts usable."""

import json
import re
from datetime import datetime, timezone

from .storage import Storage


class StaleFact(ValueError):
    pass


class ProfileStore:
    def __init__(self, path):
        self.storage = Storage(path)
        self.db = self.storage.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS candidate_facts (
                id TEXT PRIMARY KEY, category TEXT NOT NULL, value TEXT NOT NULL,
                sources_json TEXT NOT NULL, status TEXT NOT NULL,
                question TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS candidate_fact_events (
                event_id INTEGER PRIMARY KEY, fact_id TEXT NOT NULL,
                revision INTEGER NOT NULL, action TEXT NOT NULL,
                snapshot_json TEXT NOT NULL, actor_id TEXT, created_at TEXT NOT NULL
            );
        """)

    def close(self):
        self.storage.close()

    def _event(self, fact, action, actor):
        self.db.execute("INSERT INTO candidate_fact_events(fact_id,revision,action,snapshot_json,actor_id,created_at) VALUES (?,?,?,?,?,?)",
                        (fact['id'], fact['revision'], action, json.dumps(fact,ensure_ascii=False),
                         str(actor) if actor is not None else None, datetime.now(timezone.utc).isoformat()))

    def import_draft(self, document):
        if not isinstance(document, dict) or not isinstance(document.get('facts'),list):
            raise ValueError("Ожидается JSON с массивом facts")
        facts = document['facts']
        ids = set()
        for fact in facts:
            if (not isinstance(fact,dict) or not re.fullmatch(r'[a-z0-9_]{1,24}',fact.get('id',''))
                    or fact['id'] in ids or not isinstance(fact.get('value'),str) or not 1 <= len(fact['value']) <= 1800
                    or not isinstance(fact.get('category'),str) or not fact['category']
                    or not isinstance(fact.get('sources'),list) or not fact['sources']
                    or not isinstance(fact.get('question',''),str)):
                raise ValueError("Некорректный факт или повтор ID")
            for source in fact['sources']:
                if (not isinstance(source,dict) or not isinstance(source.get('document'),str)
                        or not isinstance(source.get('quote'),str) or not source['quote']
                        or type(source.get('page')) is not int or source['page'] < 1):
                    raise ValueError("Нужны документ, страница и цитата источника")
            ids.add(fact['id'])
        inserted = 0
        with self.db:
            for fact in facts:
                status = 'NEEDS_CLARIFICATION' if fact.get('question') else 'UNCONFIRMED'
                cursor = self.db.execute("INSERT OR IGNORE INTO candidate_facts VALUES (?,?,?,?,?,?,1)",
                    (fact['id'],fact['category'],fact['value'],json.dumps(fact['sources'],ensure_ascii=False),status,fact.get('question','')))
                if cursor.rowcount:
                    inserted += 1
                    self._event(self.get(fact['id']),'import',None)
        return inserted

    def get(self, fact_id):
        row = self.db.execute("SELECT * FROM candidate_facts WHERE id=?",(fact_id,)).fetchone()
        if row is None:
            raise ValueError("Факт не найден")
        value = dict(row)
        value['sources'] = json.loads(value.pop('sources_json'))
        return value

    def all(self):
        return [self.get(row[0]) for row in self.db.execute("SELECT id FROM candidate_facts ORDER BY rowid")]

    def confirmed(self):
        return [fact for fact in self.all() if fact['status']=='CONFIRMED']

    def update(self, fact_id, revision, action, actor_id, text=None):
        if action not in ('confirm','reject','edit'):
            raise ValueError("Неизвестное действие")
        if action=='edit' and (not isinstance(text,str) or not 1 <= len(text.strip()) <= 1800):
            raise ValueError("Введите от 1 до 1800 символов")
        with self.db:
            fact = self.get(fact_id)
            if fact['revision'] != revision:
                raise StaleFact("Факт уже изменён. Откройте профиль заново.")
            if action=='confirm' and fact['question']:
                raise ValueError("Сначала уточните факт кнопкой «Исправить», затем подтвердите.")
            status = {'confirm':'CONFIRMED','reject':'REJECTED','edit':'UNCONFIRMED'}[action]
            value = text.strip() if action=='edit' else fact['value']
            sources = list(fact['sources'])
            if action=='edit':
                sources.append({'document':'Уточнение пользователя','page':None,'quote':value})
            question = '' if action=='edit' else fact['question']
            cursor = self.db.execute("UPDATE candidate_facts SET value=?, sources_json=?, status=?, question=?, revision=revision+1 WHERE id=? AND revision=?",
                (value,json.dumps(sources,ensure_ascii=False),status,question,fact_id,revision))
            if cursor.rowcount != 1:
                raise StaleFact("Факт уже изменён. Откройте профиль заново.")
            updated = self.get(fact_id)
            self._event(updated,action,actor_id)
        return updated
