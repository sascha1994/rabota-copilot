import json
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class Storage:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS vacancies (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, employer TEXT,
                published_at TEXT, url TEXT, raw_json TEXT NOT NULL,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS search_results (
                search_key TEXT NOT NULL, vacancy_id TEXT NOT NULL,
                rejection TEXT, PRIMARY KEY(search_key, vacancy_id)
            );
            CREATE TABLE IF NOT EXISTS cursors (
                search_key TEXT PRIMARY KEY, completed_until TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS vacancy_tracking (
                vacancy_id TEXT PRIMARY KEY, state TEXT NOT NULL,
                raw_json TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS vacancy_tracking_events (
                id INTEGER PRIMARY KEY, vacancy_id TEXT NOT NULL,
                state TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS vacancy_descriptions (
                id INTEGER PRIMARY KEY, vacancy_id TEXT NOT NULL,
                content_hash TEXT NOT NULL, text TEXT NOT NULL,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS description_vacancy
                ON vacancy_descriptions(vacancy_id,id);
        """)

    def close(self):
        self.db.close()

    def save(self, item, search_key, rejection, now):
        employer = item.get("employer") or {}
        with self.db:
            exists = self.db.execute("SELECT 1 FROM vacancies WHERE id=?", (str(item["id"]),)).fetchone()
            self.db.execute("""INSERT INTO vacancies VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, employer=excluded.employer,
                published_at=excluded.published_at, url=excluded.url, raw_json=excluded.raw_json,
                last_seen=excluded.last_seen""", (str(item["id"]), item.get("name", ""),
                    employer.get("name"), item.get("published_at"), item.get("alternate_url"),
                    json.dumps(item, ensure_ascii=False), now, now))
            self.db.execute("INSERT INTO search_results VALUES (?, ?, ?) ON CONFLICT(search_key, vacancy_id) DO UPDATE SET rejection=excluded.rejection",
                            (search_key, str(item["id"]), rejection))
        return not exists

    def cursor(self, key):
        row = self.db.execute("SELECT completed_until FROM cursors WHERE search_key=?", (key,)).fetchone()
        return row[0] if row else None

    def advance(self, key, until):
        with self.db:
            self.db.execute("INSERT INTO cursors VALUES (?, ?) ON CONFLICT(search_key) DO UPDATE SET completed_until=MAX(completed_until, excluded.completed_until)", (key, until))

    def results(self, key, limit=100):
        rows = self.db.execute("""SELECT v.raw_json FROM vacancies v JOIN search_results s ON v.id=s.vacancy_id
            WHERE s.search_key=? AND s.rejection IS NULL ORDER BY v.published_at DESC, v.id LIMIT ?""", (key, limit))
        items = [json.loads(row[0]) for row in rows]
        for item in items:
            marked = self.db.execute('SELECT state FROM vacancy_tracking WHERE vacancy_id=?', (str(item['id']),)).fetchone()
            if marked:
                item['_tracking_state'] = marked[0]
        return items

    def mark(self, vid, state):
        if state not in ('interested', 'skipped', 'applied', 'new'):
            raise ValueError('Неизвестная отметка.')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT raw_json FROM vacancies WHERE id=?', (vid,)).fetchone()
            previous = self.db.execute('SELECT state,raw_json FROM vacancy_tracking WHERE vacancy_id=?', (vid,)).fetchone()
            if row is None and previous is None:
                raise ValueError('Вакансия не найдена. Запустите поиск заново.')
            if previous is not None and previous[0] == state:
                return
            stamp = datetime.now(timezone.utc).isoformat()
            self.db.execute('''INSERT INTO vacancy_tracking VALUES (?,?,?,?)
                ON CONFLICT(vacancy_id) DO UPDATE SET state=excluded.state,
                raw_json=excluded.raw_json, updated_at=excluded.updated_at''',
                (vid,state,row[0] if row else previous[1],stamp))
            self.db.execute('INSERT INTO vacancy_tracking_events(vacancy_id,state,created_at) VALUES (?,?,?)', (vid,state,stamp))

    def tracked(self, offset=0, limit=5):
        rows = self.db.execute('''SELECT t.state,coalesce(v.raw_json,t.raw_json) AS raw
            FROM vacancy_tracking t LEFT JOIN vacancies v ON v.id=t.vacancy_id
            WHERE t.state!='new' ORDER BY t.updated_at DESC,t.vacancy_id LIMIT ? OFFSET ?''', (limit,offset))
        return [dict(json.loads(row['raw']), _tracking_state=row['state']) for row in rows]

    def save_description(self, vid, text):
        if not isinstance(text,str) or not text.strip() or len(text)>24000:
            raise ValueError('Некорректное описание вакансии.')
        digest = hashlib.sha256(text.encode()).hexdigest()
        stamp = datetime.now(timezone.utc).isoformat()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            previous = self.db.execute('SELECT id,content_hash FROM vacancy_descriptions WHERE vacancy_id=? ORDER BY id DESC LIMIT 1', (vid,)).fetchone()
            if previous and previous['content_hash'] == digest:
                self.db.execute('UPDATE vacancy_descriptions SET last_seen=? WHERE id=?', (stamp,previous['id']))
                return previous['id']
            return self.db.execute('''INSERT INTO vacancy_descriptions
                (vacancy_id,content_hash,text,first_seen,last_seen) VALUES (?,?,?,?,?)''',
                (vid,digest,text,stamp,stamp)).lastrowid

    def descriptions(self, vid, limit=10):
        return [dict(row) for row in self.db.execute('''SELECT id,first_seen,last_seen,content_hash
            FROM vacancy_descriptions WHERE vacancy_id=? ORDER BY id DESC LIMIT ?''', (vid,limit))]

    def description(self, ident):
        row = self.db.execute('SELECT * FROM vacancy_descriptions WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise ValueError('Описание не найдено.')
        return dict(row)

    def summary(self, key):
        count = self.db.execute("SELECT COUNT(*) FROM search_results WHERE search_key=? AND rejection IS NULL", (key,)).fetchone()[0]
        return count, self.cursor(key)

    def clear_results(self, key):
        """Clear one search atomically, retaining vacancies used by other searches."""
        with self.db:
            count = self.summary(key)[0]
            self.db.execute("DELETE FROM search_results WHERE search_key=?", (key,))
            self.db.execute("DELETE FROM cursors WHERE search_key IN (?, ?)", (key, "web:" + key))
            keep_jobs = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_jobs'").fetchone()
            self.db.execute("DELETE FROM vacancies WHERE NOT EXISTS (SELECT 1 FROM search_results s WHERE s.vacancy_id=vacancies.id)" +
                (" AND NOT EXISTS (SELECT 1 FROM application_jobs j WHERE j.vacancy_id=vacancies.id)" if keep_jobs else ""))
        return count
