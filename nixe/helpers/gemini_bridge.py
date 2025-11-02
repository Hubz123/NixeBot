#!/usr/bin/env python3
import os, time, asyncio, logging, base64, re, json as _json
from typing import List, Optional, Tuple, Dict, Any

try:
    import google.generativeai as genai
except Exception as _e:
    genai = None
    logging.warning("[gemini_bridge] google-generativeai not available: %s", _e)

_SEMA: Optional[asyncio.Semaphore] = None
_STATE: Dict[str, Dict[str, float]] = {}

def _split_keys(val: str) -> List[str]:
    return [p for p in re.split(r'[\s,;|]+', val.strip()) if p] if val else []

def _load_list(name: str, default=None):
    v = os.getenv(name)
    if not v: return default or []
    v = v.strip()
    if v.startswith('['):
        try:
            arr = _json.loads(v)
            return [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            pass
    return [s.strip() for s in v.split(',') if s.strip()]

def _load_gemini_keys() -> List[str]:
    keys: List[str] = []
    keys += _split_keys(os.getenv("GEMINI_API_KEY",""))
    keys += _split_keys(os.getenv("GEMINI_API_KEY_B",""))
    if keys:
        return keys
    alt = os.getenv("GEMINI_KEYS","").strip()
    if alt:
        if alt.startswith('['):
            try:
                arr = _json.loads(alt); return [str(x).strip() for x in arr if str(x).strip()]
            except Exception: pass
        return [s.strip() for s in alt.split(',') if s.strip()]
    return []

def _sema(n: int) -> asyncio.Semaphore:
    global _SEMA
    if _SEMA is None:
        _SEMA = asyncio.Semaphore(max(1, n))
    return _SEMA

def _now(): return time.time()

def _pick_key(keys: List[str]) -> Optional[str]:
    now = _now()
    for k in keys:
        st = _STATE.get(k) or {"cooldown_until": 0, "fail": 0}
        if st.get("cooldown_until", 0) <= now:
            return k
    return None

def _cooldown_key(key: str, seconds: int):
    st = _STATE.setdefault(key, {"cooldown_until": 0, "fail": 0})
    st["fail"] = st.get("fail", 0) + 1
    st["cooldown_until"] = _now() + max(1, int(seconds))

def _visible_tail(k): return (k or "none")[-4:]

def _get_models() -> List[str]:
    models = _load_list("GEMINI_MODELS", ["gemini-2.5-flash-lite", "gemini-2.5-flash"])
    return models or ["gemini-2.5-flash-lite"]

def _as_b64img(b):
    return {"mime_type": "image/jpeg", "data": base64.b64encode(b).decode("ascii")}

async def _gemini_call_image(img, api_key, model, timeout_ms):
    if genai is None: raise RuntimeError("google-generativeai missing")
    genai.configure(api_key=api_key)
    prompt = (
        "You are a detector that checks if an image is a gacha/lucky-pull result screenshot "
        "(from games like Genshin/Honkai/HSR/Wuthering Waves/Arknights, etc.). "
        "Return ONLY a JSON with fields: ok(bool), score(float 0..1), reason(short string). "
        "ok=true if it's likely a lucky-pull screenshot; score indicates confidence."
    )
    model_obj = genai.GenerativeModel(model)
    try:
        async def _inner():
            return model_obj.generate_content(
                [{"text": prompt}, {"inline_data": _as_b64img(img)}],
                request_options={"timeout": max(1, int(timeout_ms/1000))}
            )
        resp = await asyncio.wait_for(_inner(), timeout=max(1, int(timeout_ms/1000)+2))
    except asyncio.TimeoutError:
        raise RuntimeError("gemini_timeout")
    except Exception as e:
        msg = str(e).lower()
        if ("429" in msg) or ("rate" in msg and "limit" in msg):
            raise RuntimeError("gemini_rate_limit")
        raise
    text = ""
    try: text = resp.text or ""
    except Exception: text = ""
    ok, score, reason = False, 0.0, "unparsed"
    m = re.search(r'\{.*\}', text, re.S)
    if m:
        try:
            data = _json.loads(m.group(0))
            ok = bool(data.get("ok", False))
            score = float(data.get("score", 0.0))
            reason = str(data.get("reason", "ok"))
        except Exception: pass
    if score == 0.0 and not ok:
        kw = ["gacha","lucky","pull","wish","warp","banner","obtained","x1","x10","5★","4★","ssr"]
        hits = sum(1 for k in kw if k in text.lower())
        score = min(1.0, hits/6.0)
        ok = score >= float(os.getenv("GEMINI_LUCKY_THRESHOLD","0.75"))
        reason = "heuristic" if text else "empty"
    return ok, max(0.0, min(1.0, score)), reason

async def classify_lucky_pull_bytes(img: bytes, timeout_ms: int=20000, context: str="lpg"):
    if context == "phish" and os.getenv("PHISH_PROVIDER","groq").lower() == "groq":
        return {"ok": False, "score": 0.0, "provider": "gemini", "reason": "blocked_by_provider_policy"}
    keys = _load_gemini_keys()
    if not keys:
        return {"ok": False, "score": 0.0, "provider": "gemini", "reason": "no_api_key"}
    models = _get_models()
    cooldown = int(os.getenv("GEMINI_COOLDOWN_SEC","600"))
    retries = int(os.getenv("GEMINI_MAX_RETRIES","2"))
    maxc = int(os.getenv("GEMINI_MAX_CONCURRENT","1"))
    async with _sema(maxc):
        err = None
        for attempt in range(retries+1):
            key = _pick_key(keys)
            if not key:
                return {"ok": False, "score": 0.0, "provider": "gemini", "reason": "all_keys_on_cooldown"}
            model = models[min(attempt, len(models)-1)]
            try:
                ok, score, reason = await _gemini_call_image(img, key, model, timeout_ms)
                return {"ok": bool(ok), "score": float(score), "provider": f"gemini:{model}", "reason": str(reason)}
            except RuntimeError as e:
                msg = str(e)
                if "gemini_rate_limit" in msg:
                    logging.warning("[gemini] 429 on key ****%s -> cooldown=%ss", _visible_tail(key), cooldown)
                    _cooldown_key(key, cooldown); err="429"; continue
                if "gemini_timeout" in msg:
                    logging.warning("[gemini] timeout on key ****%s", _visible_tail(key))
                    err="timeout"; continue
                logging.warning("[gemini] error on key ****%s: %s", _visible_tail(key), msg)
                err = msg; continue
        return {"ok": False, "score": 0.0, "provider": f"gemini:{models[0]}", "reason": f"failed:{err or 'unknown'}"}
