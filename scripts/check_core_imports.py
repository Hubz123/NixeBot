
# --- bootstrap path for local runs ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.abspath(__file__))
_PROJ = _os.path.abspath(_os.path.join(_ROOT, ".."))
if _PROJ not in _sys.path:
    _sys.path.insert(0, _PROJ)
# -------------------------------------

#!/usr/bin/env python3
import os, sys, importlib
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
mods = ["nixe.helpers.phash_board","nixe.config_phash","nixe.cogs_loader"]
for m in mods:
    importlib.import_module(m)
print("[OK] core imports")
from nixe.cogs_loader import discover
print("[OK] discover ->", len(discover()))
