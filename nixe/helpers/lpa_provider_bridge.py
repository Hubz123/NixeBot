# nixe/helpers/lpa_provider_bridge.py — image-first provider dispatch; no ENV change
import os, importlib

# --- Groq model sanitizer (prevents hardcoded maverick/scout) ---
try:
    _gm = (os.getenv("GROQ_MODEL") or os.getenv("LPG_GROQ_MODEL") or "").strip()
    if (not _gm) or ("maverick" in _gm.lower()) or ("scout" in _gm.lower()) or (_gm.lower() in {"auto","default"}):
        os.environ["GROQ_MODEL"] = os.environ.get("LPG_GROQ_FALLBACK","llama-3.1-8b-instant")
except Exception:
    pass
# ---------------------------------------------------------------


def _try_import(path: str):
    try: return importlib.import_module(path)
    except Exception: return None

def _call_tuple(fn, *args, **kw):
    try:
        res = fn(*args, **kw)
    except Exception as e:
        return None, f"err:{type(e).__name__}"
    if not isinstance(res, tuple) or len(res) < 2:
        return None, "bad_tuple"
    a0, a1 = res[0], res[1]
    if isinstance(a0, (int,float)) and 0.0 <= float(a0) <= 1.0:
        return float(a0), "ok"
    if isinstance(a0, bool) and isinstance(a1, (int,float)):
        ok, conf = bool(a0), float(a1)
        return (conf if ok else 0.0), "ok"
    return None, "unknown_shape"

def _iter(order_csv: str):
    order = [p.strip().lower() for p in (order_csv or "gemini,groq").split(",") if p.strip()]
    for p in order:
        if p == "gemini":
            yield p, (_try_import("nixe.helpers.gemini_bridge_lucky_rules")
                      or _try_import("nixe.helpers.gemini_bridge")
                      or _try_import("nixe.helpers.lpg_provider"))
        elif p == "groq":
            yield p, (_try_import("nixe.helpers.groq_bridge")
                      or _try_import("nixe.helpers.lpg_provider"))
        else:
            yield p, _try_import(p)

def classify_with_image_bytes(img_bytes: bytes, order: str = ""):
    try: os.environ.setdefault("LPG_SMOKE_FORCE_SAMPLE","0")
    except Exception: pass
    last = "provider_unavailable"
    for name, mod in _iter(order):
        if not mod: continue
        for fn_name in ("classify_lucky_pull_bytes","classify_image_bytes"):
            fn = getattr(mod, fn_name, None)
            if not fn: continue
            score, status = _call_tuple(fn, img_bytes)
            if isinstance(score, float): return score, f"{name}:{fn_name}"
            last = f"{name}:{status}"; break
    return None, last

def classify(text: str, order: str = ""):
    last = "provider_unavailable"
    for name, mod in _iter(order):
        if not mod: continue
        for fn_name in ("classify_lucky_pull","classify_lucky_pull_text"):
            fn = getattr(mod, fn_name, None)
            if not fn: continue
            score, status = _call_tuple(fn, text)
            if isinstance(score, float): return score, f"{name}:{fn_name}"
            last = f"{name}:{status}"; break
    return None, last


# Guarantee tuple return even in unexpected paths
def _tuple_return(prob: Any, via: Any) -> Tuple[float, str]:
    return _norm_prob(prob), str(via)
