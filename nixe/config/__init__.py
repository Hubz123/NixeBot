"""nixe.config helpers.

Submodules expect a safe `load()` function.

- Reads JSON from `<this_dir>/<name>.json`
- Returns `{}` (or `default`) if the file is missing or invalid.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

_BASE_DIR = Path(__file__).resolve().parent


def load(name: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if default is None:
        default = {}
    fname = (name or "").strip()
    if not fname:
        return dict(default)

    path = _BASE_DIR / f"{fname}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else dict(default)
    except FileNotFoundError:
        return dict(default)
    except Exception:
        return dict(default)


__all__ = ["load"]
