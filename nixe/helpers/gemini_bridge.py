#!/usr/bin/env python3
# nixe/helpers/gemini_bridge.py (v16) — model sanitize + tiny-image guard + clearer 400 mapping
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
    import re as _re
    return [p for p in _re.split(r"[\s,;|]+", val.strip()) if p] if val else []

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

def _sanitize_models(raw: str):
    if not raw:
        return []
    models = []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            arr = _json.loads(raw)
            models = [str(x) for x in arr]
        except Exception:
            models = [raw]
    else:
        models = [s for s in re.split(r"[\s,]+", raw) if s]
    cleaned = []
    for m in models:
        m = str(m).lower().strip()
        m = re.sub(r"[^a-z0-9.\-_/]", "", m)
        if m.startswith("models/"):
            m = m[len("models/"):]
        cleaned.append(m)
    seen = set(); out = []
    for m in cleaned:
        if m and m not in seen:
            seen.add(m); out.append(m)
    return out

def _sema(n: int) -> asyncio.Semaphore:
    global _SEMA
    if _SEMA is None:
        _SEMA = asyncio.Semaphore(max(1, n))
    return _SEMA

def _now(): return time.time()

def _pick_key(keys):
    now = _now()
    for k in keys:
        st = _STATE.get(k) or {"cooldown_until": 0}
        if st.get("cooldown_until", 0) <= now:
            return k
    return None

def _cooldown_key(key, seconds):
    st = _STATE.setdefault(key, {"cooldown_until": 0})
    st["cooldown_until"] = _now() + max(1, int(seconds))

def _as_b64img(b):
    return {"mime_type": "image/jpeg", "data": base64.b64encode(b).decode("ascii")}

def _negative_phrases():
    v = os.getenv("LPG_NEGATIVE_TEXT", "")
    if v.startswith("["):
        try: return [s.lower() for s in _json.loads(v)]
        except Exception: pass
    return [s.strip().lower() for s in v.split(",") if s.strip()]

def _strict_prompt():
    negatives = "; ".join(_negative_phrases() or [])
    parts = [
        "Task: Determine if the IMAGE is a gacha pull RESULT screen (not loadout/deck/inventory).\n",
        "Rules:\n",
        "1) Positive evidence: overlays like 'Obtained', 'Result', 'Wish', 'Warp', 'x10 Pull', 'Skip/Tap to reveal', or a result panel for newly acquired items.\n",
        "2) Hard negatives — if ANY of these appear anywhere, it is NOT a pull result: ",
        negatives + ".\n",
        "Output JSON only: {\"ok\":true|false,\"score\":0..1,\"reason\":\"short\"}\n",
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
        if "unable to process input image" in msg or "invalid image" in msg:
            raise RuntimeError("gemini_unprocessable_image")
        if ("429" in msg) or ("rate" in msg and "limit" in msg):
            raise RuntimeError("gemini_rate_limit")
        raise
    text = ""
    try: text = resp.text or ""
    except Exception: text = ""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return False, 0.0, "non_json_response"
    try:
        data = _json.loads(text[start:end+1])
    except Exception:
        return False, 0.0, "json_parse_error"
    ok = bool(data.get("ok", False))
    score = float(data.get("score", 0.0))
    reason = str(data.get("reason", "uncertain"))
    negs = _negative_phrases()
    if any(x in reason.lower() for x in negs):
        ok = False
    return ok, max(0.0, min(1.0, score)), reason

async def classify_lucky_pull_bytes(img: bytes, timeout_ms: int = None, context: str = "lpg"):
    min_bytes = int(os.getenv("LPG_MIN_IMAGE_BYTES", "4096"))
    if not img or len(img) < min_bytes:
        ln = 0 if not img else len(img)
        return {"ok": False, "score": 0.0, "provider": "gemini", "reason": f"image_too_small(len={ln})"}

    if context == "phish" and os.getenv("PHISH_PROVIDER", "groq").lower() == "groq":
        return {"ok": False, "score": 0.0, "provider": "gemini", "reason": "blocked_by_provider_policy"}
    keys = _load_gemini_keys()
    if not keys:
        return {"ok": False, "score": 0.0, "provider": "gemini", "reason": "no_api_key"}
    raw_models = os.getenv("GEMINI_MODELS", "gemini-2.5-flash-lite,gemini-2.5-flash")
    models = _sanitize_models(raw_models) or ["gemini-2.5-flash-lite", "gemini-2.5-flash"]
    cooldown = int(os.getenv("GEMINI_COOLDOWN_SEC", "600"))
    retries = int(os.getenv("GEMINI_MAX_RETRIES", "1"))
    maxc = int(os.getenv("GEMINI_MAX_CONCURRENT", "1"))
    tmo = int(timeout_ms or int(os.getenv("GEMINI_TIMEOUT_MS", "12000")))

    logging.info("[gemini] models=%s", ",".join(models))
    async with _sema(maxc):
        err = None
        for attempt in range(retries + 1):
            key = _pick_key(keys)
            if not key:
                return {"ok": False, "score": 0.0, "provider": "gemini", "reason": "all_keys_on_cooldown"}
            model = models[min(attempt, len(models) - 1)]
            logging.info("[gemini] classify attempt=%s model=%s key=****%s", attempt, model, key[-4:])
            try:
                ok, score, reason = await _gemini_call_image(img, key, model, tmo)
                logging.info("[gemini] result ok=%s score=%.2f reason=%s", ok, score, reason)
                return {"ok": bool(ok), "score": float(score), "provider": f"gemini:{model}", "reason": str(reason)}
            except RuntimeError as e:
                msg = str(e)
                if "gemini_rate_limit" in msg:
                    logging.warning("[gemini] 429 on key ****%s -> cooldown=%ss", key[-4:], cooldown)
                    _cooldown_key(key, cooldown); err = "429"; continue
                if "gemini_timeout" in msg:
                    logging.warning("[gemini] timeout key ****%s tmo=%sms", key[-4:], tmo)
                    err = "timeout"; continue
                if "gemini_unprocessable_image" in msg:
                    logging.warning("[gemini] unprocessable image")
                    err = "unprocessable_image"; break
                logging.warning("[gemini] error key ****%s: %s", key[-4:], msg)
                err = msg; continue
        return {"ok": False, "score": 0.0, "provider": f"gemini:{models[0]}", "reason": f"failed:{err or 'unknown'}"}
