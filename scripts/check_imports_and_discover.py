
# --- bootstrap path for local runs ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.abspath(__file__))
_PROJ = _os.path.abspath(_os.path.join(_ROOT, ".."))
if _PROJ not in _sys.path:
    _sys.path.insert(0, _PROJ)
# -------------------------------------

# --- force local 'nixe' package for all import checks (anti-shadowing) ---
import os, sys, importlib, inspect

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

def _force_local_nixe():
    """
    Pastikan 'import nixe' mengarah ke paket lokal di repo ini,
    bukan ke modul lain di site-packages / path lain.
    """
    importlib.invalidate_caches()
    try:
        import nixe
        nfile = inspect.getfile(nixe)
        if not os.path.abspath(nfile).startswith(os.path.abspath(ROOT)):
            # buang nixe yang salah dari sys.modules lalu pakai yang lokal
            for k in list(sys.modules.keys()):
                if k == "nixe" or k.startswith("nixe."):
                    sys.modules.pop(k, None)
            if ROOT not in sys.path:
                sys.path.insert(0, ROOT)
            importlib.invalidate_caches()
    except Exception:
        # kalau belum pernah ke-import, diam-diam saja
        pass

def smoke_import(modname: str):
    """Wrapper supaya setiap import nixe.* selalu pakai yang lokal + tulis path modulnya."""
    _force_local_nixe()
    try:
        m = importlib.import_module(modname)
        p = getattr(m, "__file__", "?")
        print(f"[PASS] import: {modname} ({p})")
        return True, m
    except Exception as e:
        print(f"[FAIL] import: {modname}")
        import traceback; traceback.print_exc(limit=3)
        return False, None
# --------------------------------------------------------------------------
import os, sys, importlib

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

for m in ["nixe.helpers.phash_board","nixe.config_phash","nixe.cogs_loader"]:
    importlib.import_module(m)
print("[OK] imports passed")

from nixe.cogs_loader import discover
files = discover()
print("[OK] discover returned", len(files), "file(s)")
for f in files:
    print(" -", f)
