"""Consistent SQLite snapshots; restoration always creates a separate database."""

import hashlib
import os
import sqlite3
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def read_database(path):
    source = Path(path).resolve()
    if not source.is_file():
        raise ValueError('Файл базы данных не найден.')
    return sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=30)


def verify(path):
    with closing(read_database(path)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Проверка целостности SQLite не пройдена.')
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'vacancies','search_results','cursors'} <= names:
            raise ValueError('Файл не является базой Rabota Copilot.')


def copy_database(source, destination):
    """Use SQLite backup API (includes WAL); publish only a verified file."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or destination.exists():
        raise ValueError('Выберите новый путь: существующая база не перезаписывается.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.backup-',suffix='.sqlite3',dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary)
    try:
        with closing(read_database(source)) as src, closing(sqlite3.connect(temporary)) as dst:
            src.backup(dst)
            dst.execute('PRAGMA journal_mode=DELETE')
        verify(temporary)
        # Exclusive creation prevents a concurrent restore from replacing another file.
        with destination.open('xb') as output, temporary.open('rb') as stream:
            try:
                while chunk := stream.read(1024*1024):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            except BaseException:
                output.close()
                destination.unlink(missing_ok=True)
                raise
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def create_backup(source, directory):
    prefix = hashlib.sha256(str(Path(source).resolve()).encode()).hexdigest()[:12]
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    return copy_database(source, Path(directory)/f'rabota-{prefix}-{stamp}-{uuid.uuid4().hex[:8]}.sqlite3')


def restore(source, output):
    verify(source)
    return copy_database(source, output)
