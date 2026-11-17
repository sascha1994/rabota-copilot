"""An OS lock released automatically when the bot process exits."""

import os
from pathlib import Path


class ProcessLock:
    def __init__(self, db_path):
        self.path = Path(str(Path(db_path).resolve()) + '.bot.lock')
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.stream.seek(0,2)
                if self.stream.tell()==0:
                    self.stream.write(b'0')
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise ValueError('Для этой базы уже запущен экземпляр бота.') from None
        return self

    def __exit__(self,*args):
        if self.stream:
            if os.name == 'nt':
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self.stream,fcntl.LOCK_UN)
            self.stream.close()
