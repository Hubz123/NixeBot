
# -*- coding: utf-8 -*-
from __future__ import annotations
import os, base64, json, logging, aiohttp

log = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = os.getenv("LUCKYPULL_GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_FALLBACK      = os.getenv("LUCKYPULL_GEMINI_FALLBACK", "gemini-2.0-flash")
GEMINI_FALLBACK2     = os.getenv("LUCKYPULL_GEMINI_FALLBACK2", "gemini-2.5-flash-lite")
GEMINI_API_KEY       = (
    os.getenv("TRANSLATE_GEMINI_API_KEY")
    or os.getenv("GEMINI_API_KEY")
    or os.getenv("GEMINI_API_KEY_B")
    or os.getenv("GEMINI_BACKUP_API_KEY")
)
GEMINI_TIMEOUT       = int(os.getenv("LUCKYPULL_GEMINI_TIMEOUT", "6") or "6")
GEMINI_MIN_CONF      = float(os.getenv("LUCKYPULL_GEMINI_MIN_CONF", "0.55") or "0.55")
GEMINI_MAX_BYTES     = int(os.getenv("LUCKYPULL_GEMINI_MAX_BYTES", "1048576") or "1048576")

VISION_PROMPT = (
    "Detect if an image is a gacha/lucky-pull result screen. "
    "Return ONLY JSON: {\"gacha\": true|false, \"confidence\": 0..1}"
)

def _b64(raw: bytes, limit: int) -> str:
    if len(raw) > limit:
        raw = raw[:limit]
    return base64.b64encode(raw).decode("ascii")

async def _call(model: str, parts: list) -> dict | None:
    if not model:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
    body = {
        "contents": [{"role":"user","parts": parts}],
        "generationConfig": {"responseMimeType":"application/json"}
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=GEMINI_TIMEOUT)) as sess:
            async with sess.post(url, json=body) as resp:
                if resp.status != 200:
                    log.warning("gemini http %s on %s", resp.status, model)
                    return None
                return await resp.json()
    except Exception as e:
        log.warning("gemini error on %s: %r", model, e)
        return None

async def gemini_judge_images(attachments, model: str | None = None):
    if not GEMINI_API_KEY:
        return None
    parts = [{"text": VISION_PROMPT}]
    added = 0
    for a in attachments:
        try:
            raw = await a.read()
        except Exception:
            raw = None
        if not raw:
            continue
        parts.append({"inline_data":{"mime_type":"image/png","data": _b64(raw, GEMINI_MAX_BYTES)}})
        added += 1
        if added >= 2: break
    if added == 0:
        return None

    for mdl in [model or DEFAULT_GEMINI_MODEL, GEMINI_FALLBACK, GEMINI_FALLBACK2]:
        data = await _call(mdl, parts)
        if not data: 
            continue
        try:
            cand = (data.get("candidates") or [{}])[0]
            txt = ((cand.get("content") or {}).get("parts") or [{}])[0].get("text") or ""
            obj = json.loads(txt)
            gacha = bool(obj.get("gacha"))
            conf  = float(obj.get("confidence") or 0.0)
            return (gacha, conf, mdl)
        except Exception:
            try:
                gacha = bool(data.get("gacha"))
                conf  = float(data.get("confidence") or 0.0)
                return (gacha, conf, mdl)
            except Exception:
                pass
    return None
