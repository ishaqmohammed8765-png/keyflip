from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> bool:
    """Minimal .env loader (no external deps).

    Rules:
    - Lines starting with '#' are ignored.
    - KEY=VALUE pairs only; VALUE may be quoted with single or double quotes.
    - Existing environment variables are not overwritten.
    """
    p = Path(path)
    if not p.exists() or not p.is_file():
        return False
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return False

    changed = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key or key.startswith("#"):
            continue
        value = value.strip()
        if len(value) >= 2 and ((value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")):
            value = value[1:-1]
        if key in os.environ:
            continue
        os.environ[key] = value
        changed = True
    return changed

