
#!/usr/bin/env python3
import os, time, asyncio, logging, base64, re, json as _json
from typing import Optional, Dict

try:
    import google.generativeai as genai
except Exception as _e:
    genai = None
    logging.warning("[gemini_bridge] google-generativeai not available: %s", _e)

_SEMA: Optional[asyncio.Semaphore] = None
_STATE: Dict[str, Dict[str, float]] = {}

def _split_keys(val: str):
    return [p for p in re.split(r"[\s,;|]+", val.strip()) if p] if val else []

def _load_list(name: str, default=None):
    v = os.getenv(name)
    if not v:
        return default or []
    v = v.strip()
    if v.startswith("["):
        try:
            arr = _json.loads(v)
            return [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            pass
    return [s.strip() for s in v.split(",") if s.strip()]

def _load_gemini_keys():
    keys = []
    keys += _split_keys(os.getenv("GEMINI_API_KEY", ""))
    keys += _split_keys(os.getenv("GEMINI_API_KEY_B", ""))
    if keys:
        return keys
    raw = os.getenv("GEMINI_KEYS", "").strip()
    if raw:
        if raw.startswith("["):
            try:
                arr = _json.loads(raw)
                return [str(x).strip() for x in arr if str(x).strip()]
            except Exception:
                pass
        return [s.strip() for s in raw.split(",") if s.strip()]
    return []

def _sema(n: int) -> asyncio.Semaphore:
    global _SEMA
    if _SEMA is None:
        _SEMA = asyncio.Semaphore(max(1, n))
    return _SEMA

def _now():
    return time.time()

def _pick_key(keys):
    now = _now()
    for k in keys:
        st = _STATE.get(k) or {"cooldown_until": 0, "fail": 0}
        if st.get("cooldown_until", 0) <= now:
            return k
    return None

def _cooldown_key(key, seconds):
    st = _STATE.setdefault(key, {"cooldown_until": 0, "fail": 0})
    st["fail"] = st.get("fail", 0) + 1
    st["cooldown_until"] = _now() + max(1, int(seconds))

def _visible_tail(k):
    return (k or "none")[-4:]

def _get_models():
    models = _load_list("GEMINI_MODELS", ["gemini-2.5-flash-lite", "gemini-2.5-flash"])
    return models or ["gemini-2.5-flash-lite"]

def _as_b64img(b):
    return {"mime_type": "image/jpeg", "data": base64.b64encode(b).decode("ascii")}

def _negative_phrases():
    defaults = [
        "save data","card count","obtained equipment","loadout","deck","inventory",
        "preset","save slot","stage select","quest","mission","profile","edit loadout"
    ]
    return [s.lower() for s in _load_list("LPG_NEGATIVE_TEXT", defaults)]

def _strict_prompt():
    negatives = "; ".join(_negative_phrases())
    parts = [
        "Task: Determine if the IMAGE is a gacha pull RESULT screen (e.g., Wish/Warp/10-pull results), not a loadout/deck/inventory screen.\n",
        "Decide using these RULES:\n",
        "1) Positive evidence required (at least ONE): overlays like 'Obtained', 'Result', 'Wish', 'Warp', 'x10 Pull', 'Tap to reveal/Skip', or a reveal/result panel showing newly acquired items/characters.\n",
        "2) Hard negatives — if ANY of these words/phrases appear anywhere in the UI, it is NOT a pull result: ",
        negatives + ".\n",
        "3) Return output STRICTLY as compact JSON only (no prose): ",
        "{\"ok\":true|false,\"score\":0..1,\"reason\":\"short_snake_case\"}\n",
        "   - ok=true only if rule (1) is met and rule (2) is not triggered.\n",
        "   - score is your confidence.\n",
        "   - reason is a short label like has_obtained_overlay, loadout_ui, inventory_screen, uncertain.\n",
    ]
    return "".join(parts)

async def _gemini_call_image(img, api_key, model, timeout_ms):
    if genai is None:
        raise RuntimeError("google-generativeai missing")
    genai.configure(api_key=api_key)
    model_obj = genai.GenerativeModel(model)
    prompt = _strict_prompt()
    try:
        async def _inner():
            return model_obj.generate_content(
                [{"text": prompt}, {"inline_data": _as_b64img(img)}],
                request_options={"timeout": max(1, int(timeout_ms/1000))}
            )
        resp = await asyncio.wait_for(_inner(), timeout=max(1, int(timeout_ms/1000) + 2))
    except asyncio.TimeoutError:
        raise RuntimeError("gemini_timeout")
    except Exception as e:
        msg = str(e).lower()
        if ("429" in msg) or ("rate" in msg and "limit" in msg):
            raise RuntimeError("gemini_rate_limit")
        raise

    # Extract JSON object from response text
    text = ""
    try:
        text = resp.text or ""
    except Exception:
        text = ""
    # find first JSON-like block
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return False, 0.0, "non_json_response"
    blob = text[start:end+1]
    try:
        data = _json.loads(blob)
    except Exception:
        return False, 0.0, "json_parse_error"

    ok = bool(data.get("ok", False))
    score = float(data.get("score", 0.0))
    reason = str(data.get("reason", "uncertain"))

    # Guard by negatives in reason text
    negs = _negative_phrases()
    if any(x in reason.lower() for x in negs):
        ok = False
    return ok, max(0.0, min(1.0, score)), reason

async def classify_lucky_pull_bytes(img: bytes, timeout_ms: int = 20000, context: str = "lpg"):
    if context == "phish" and os.getenv("PHISH_PROVIDER", "groq").lower() == "groq":
        return {"ok": False, "score": 0.0, "provider": "gemini", "reason": "blocked_by_provider_policy"}
    keys = _load_gemini_keys()
    if not keys:
        return {"ok": False, "score": 0.0, "provider": "gemini", "reason": "no_api_key"}
    models = _get_models()
    cooldown = int(os.getenv("GEMINI_COOLDOWN_SEC", "600"))
    retries = int(os.getenv("GEMINI_MAX_RETRIES", "2"))
    maxc = int(os.getenv("GEMINI_MAX_CONCURRENT", "1"))
    async with _sema(maxc):
        err = None
        for attempt in range(retries + 1):
            key = _pick_key(keys)
            if not key:
                return {"ok": False, "score": 0.0, "provider": "gemini", "reason": "all_keys_on_cooldown"}
            model = models[min(attempt, len(models) - 1)]
            try:
                ok, score, reason = await _gemini_call_image(img, key, model, timeout_ms)
                return {"ok": bool(ok), "score": float(score), "provider": f"gemini:{model}", "reason": str(reason)}
            except RuntimeError as e:
                msg = str(e)
                if "gemini_rate_limit" in msg:
                    logging.warning("[gemini] 429 on key ****%s -> cooldown=%ss", _visible_tail(key), cooldown)
                    _cooldown_key(key, cooldown); err = "429"; continue
                if "gemini_timeout" in msg:
                    logging.warning("[gemini] timeout on key ****%s", _visible_tail(key))
                    err = "timeout"; continue
                logging.warning("[gemini] error on key ****%s: %s", _visible_tail(key), msg)
                err = msg; continue
        return {"ok": False, "score": 0.0, "provider": f"gemini:{models[0]}", "reason": f"failed:{err or 'unknown'}"}
