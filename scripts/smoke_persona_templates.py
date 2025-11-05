
# --- bootstrap path for local runs ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.abspath(__file__))
_PROJ = _os.path.abspath(_os.path.join(_ROOT, ".."))
if _PROJ not in _sys.path:
    _sys.path.insert(0, _PROJ)
# -------------------------------------


#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys

def _ensure_path():
    here = os.path.abspath(os.path.dirname(__file__))
    root = os.path.abspath(os.path.join(here, ".."))
    if root not in sys.path:
        sys.path.insert(0, root)

def main():
    _ensure_path()
    try:
        from nixe.helpers.persona_loader import load_persona, pick_line
    except Exception as e:
        print("[FAIL] loader import error:", e); sys.exit(2)
    mode, data, path = load_persona()
    if not mode or not data or not path:
        print("[FAIL] persona not found in expected paths"); sys.exit(3)
    print("[DEBUG] winner:", path.replace("\\", "/"))
    for tone in ("soft","agro","sharp"):
        line = pick_line(data, mode, tone)
        print(f"[SAMPLE {tone}] {line}")
    print("[PASS] persona loader OK")

if __name__ == "__main__":
    main()
