from __future__ import annotations
import os
from pathlib import Path

def locate_env(start: str|None=None) -> str|None:
    """Walk upward from start (or cwd) to find a .env. Returns path or None."""
    cur = Path(start or os.getcwd()).resolve()
    for _ in range(12):
        p = cur / ".env"
        if p.exists():
            return str(p)
        if cur.parent == cur:
            break
        cur = cur.parent
    return None

def load_env():
    """Load .env (if present) without crashing. No dependency if dotenv missing."""
    path = locate_env()
    if not path:
        return False, None
    try:
        from dotenv import load_dotenv
    except Exception:
        return False, path
    ok = load_dotenv(path, override=False)
    return ok, path
