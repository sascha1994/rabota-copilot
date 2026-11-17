import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.backup import create_backup, restore, verify
from app.process_lock import ProcessLock
from app.storage import Storage


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.path = self.root/'live.sqlite3'
        self.store = Storage(self.path)
        self.store.save({'id':'1','name':'Дизайнер'},'search',None,'2026-11-15')

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def test_live_wal_backup_and_restore_all_tables(self):
        self.store.mark('1','interested')
        ident = self.store.save_description('1','Текст описания')
        copy = create_backup(self.path,self.root/'backups')
        verify(copy)
        self.store.mark('1','applied')
        output = restore(copy,self.root/'restored.sqlite3')
        db = sqlite3.connect(output)
        try:
            self.assertEqual(db.execute('SELECT state FROM vacancy_tracking').fetchone()[0],'interested')
            self.assertEqual(db.execute('SELECT text FROM vacancy_descriptions WHERE id=?',(ident,)).fetchone()[0],'Текст описания')
        finally:
            db.close()

    def test_restore_refuses_existing_target_and_leaves_it_intact(self):
        copy = create_backup(self.path,self.root/'backups')
        with self.assertRaises(ValueError):
            restore(copy,self.path)
        self.assertEqual(self.store.summary('search')[0],1)

    def test_invalid_backup_never_creates_target(self):
        bad = self.root/'bad.sqlite3'
        bad.write_text('not sqlite',encoding='utf-8')
        target = self.root/'restored.sqlite3'
        with self.assertRaises((ValueError,sqlite3.Error)):
            restore(bad,target)
        self.assertFalse(target.exists())

    def test_missing_source_does_not_create_empty_db(self):
        missing = self.root/'missing.sqlite3'
        with self.assertRaises(ValueError):
            create_backup(missing,self.root/'backups')
        self.assertFalse(missing.exists())

    def test_os_lock_blocks_second_instance_and_releases(self):
        with ProcessLock(self.path):
            with self.assertRaisesRegex(ValueError,'уже запущен'):
                with ProcessLock(self.path):
                    pass
        with ProcessLock(self.path):
            pass
