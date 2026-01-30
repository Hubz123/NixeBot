# -*- coding: utf-8 -*-
"""
phish_review_memory

Stores moderator review decisions for "RAGU" phishing cases.

We keep two sets:
- false: signature -> treat as known false-positive (skip future enforcement)
- banned: signature -> treat as known confirmed attack (auto-ban next time)

File is JSON at nixe/data/phish_review_memory.json.
"""
from __future__ import annotations

import json, os, time
from typing import Any, Dict, Set

LOGICAL_PATH = os.getenv("PHISH_REVIEW_MEMORY_PATH", "nixe/data/phish_review_memory.json")

def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)

def load_memory(path: str = LOGICAL_PATH) -> Dict[str, Set[str]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f) or {}
    except Exception:
        obj = {}
    false = set((obj.get("false") or {}).keys())
    banned = set((obj.get("banned") or {}).keys())
    return {"false": false, "banned": banned}

def save_memory(false: Set[str], banned: Set[str], path: str = LOGICAL_PATH) -> None:
    _ensure_dir(path)
    payload: Dict[str, Any] = {
        "false": {k: {"ts": int(time.time())} for k in sorted(false)},
        "banned": {k: {"ts": int(time.time())} for k in sorted(banned)},
        "version": 1,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)

def mark_false(sig: str, path: str = LOGICAL_PATH) -> None:
    mem = load_memory(path)
    mem["false"].add(sig)
    mem["banned"].discard(sig)
    save_memory(mem["false"], mem["banned"], path)

def mark_banned(sig: str, path: str = LOGICAL_PATH) -> None:
    mem = load_memory(path)
    mem["banned"].add(sig)
    mem["false"].discard(sig)
    save_memory(mem["false"], mem["banned"], path)
