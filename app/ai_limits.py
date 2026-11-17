"""Shared, persistent local API budgets for all model operations."""
import json
from .storage import Storage


def effective_limits(path, env):
    store = Storage(path)
    try:
        settings = {}
        if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='automation_settings'").fetchone():
            row = store.db.execute('SELECT json FROM automation_settings WHERE id=1').fetchone()
            settings = json.loads(row[0]) if row else {}
        calls = int(settings.get('ai_daily_calls') or env.get('AI_DAILY_CALL_LIMIT','20'))
        tokens = int(settings.get('ai_daily_tokens') or env.get('AI_DAILY_TOKEN_LIMIT','200000'))
        if not 1<=calls<=100 or not 10000<=tokens<=2000000:
            raise ValueError('Недопустимые локальные лимиты OpenAI.')
        return calls,tokens
    finally:
        store.close()
