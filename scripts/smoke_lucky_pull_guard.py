
# --- bootstrap path for local runs ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.abspath(__file__))
_PROJ = _os.path.abspath(_os.path.join(_ROOT, ".."))
if _PROJ not in _sys.path:
    _sys.path.insert(0, _PROJ)
# -------------------------------------

import importlib, sys
from nixe.helpers.persona_loader import list_groups, pick_line

def main():
    ok = True
    try:
        mod = importlib.import_module("nixe.cogs.lucky_pull_guard")
        assert hasattr(mod, "setup"), "setup() missing in lucky_pull_guard"
        print("[SMOKE] import OK: nixe.cogs.lucky_pull_guard")
    except Exception as e:
        print("[FAIL] import lucky_pull_guard:", e)
        ok = False
    try:
        groups = set(list_groups("yandere"))
        assert groups == {"soft","agro","sharp"}, f"groups mismatch: {groups}"
        s = pick_line("yandere", user="@u", channel="#c", reason="deteksi")
        assert s and "{user}" not in s, "persona formatting failed"
        print("[SMOKE] persona random OK")
    except Exception as e:
        print("[FAIL] persona:", e)
        ok = False
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
