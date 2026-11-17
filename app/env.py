"""Load simple KEY=value settings without executing or expanding their values."""

import os
import re
from pathlib import Path


def load_env(path=".env"):
    """Read from the working directory; process environment has priority.

    Supports blank lines, full-line comments and optional matching outer quotes.
    Inline comments, interpolation and multiline values are intentionally unsupported.
    Never changes the process environment.
    """
    source = Path(path)
    values = {}
    if source.exists():
        for number, raw in enumerate(source.read_text(encoding="utf-8-sig").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"Некорректная настройка .env в строке {number}")
            if value.startswith(('"', "'")):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ValueError(f"Незакрытая кавычка .env в строке {number}")
                value = value[1:-1]
            values[key] = value
    return {**values, **os.environ}
