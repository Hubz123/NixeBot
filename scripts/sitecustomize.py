
# --- bootstrap path for local runs ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.abspath(__file__))
_PROJ = _os.path.abspath(_os.path.join(_ROOT, ".."))
if _PROJ not in _sys.path:
    _sys.path.insert(0, _PROJ)
# -------------------------------------

import os, sys, importlib, inspect
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "."))
PARENT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for p in [ROOT, PARENT]:
    if p not in sys.path:
        sys.path.insert(0, p)
# purge non-local 'nixe' if any
try:
    import nixe, inspect
    nfile = inspect.getfile(nixe)
    if not os.path.abspath(nfile).startswith(PARENT):
        for k in list(sys.modules.keys()):
            if k == "nixe" or k.startswith("nixe."):
                sys.modules.pop(k, None)
except Exception:
    pass
