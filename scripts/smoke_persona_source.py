
# --- bootstrap path for local runs ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.abspath(__file__))
_PROJ = _os.path.abspath(_os.path.join(_ROOT, ".."))
if _PROJ not in _sys.path:
    _sys.path.insert(0, _PROJ)
# -------------------------------------

import sys, os
sys.path.insert(0, '.')
from nixe.helpers.persona_loader import list_groups, pick_line
g = set(list_groups('yandere'))
ok = bool(g)
print('[DEBUG] groups:', g)
s = pick_line('yandere', user='kamu', channel='#test', reason='lucky pull')
print('[DEBUG] sample:', s)
if not g or not s:
    print('[FAIL] persona source not usable')
    sys.exit(1)
print('[PASS] yandere.json is active and usable')
