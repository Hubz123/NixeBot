
# --- bootstrap path for local runs ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.abspath(__file__))
_PROJ = _os.path.abspath(_os.path.join(_ROOT, ".."))
if _PROJ not in _sys.path:
    _sys.path.insert(0, _PROJ)
# -------------------------------------

# -*- coding: utf-8 -*-
"""
Smoke: Persona loader & line sampling
- Loads persona via nixe.helpers.persona_loader.load_persona()
- Prints one line for soft/agro/sharp to ensure pick_line() works and no kwargs error
"""
import sys
from nixe.helpers.persona_loader import load_persona, pick_line

mode, data, path = load_persona()
print(f"[persona] mode={mode} path={path}")
for tone in ("soft","agro","sharp"):
    line = pick_line(data, mode, tone)
    print(f"[persona:{tone}] {line}")