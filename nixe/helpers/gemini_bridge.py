from __future__ import annotations
import os, json, logging, asyncio, re, time, hashlib
from typing import Dict, Any, List, Tuple

log = logging.getLogger("nixe.helpers.gemini_bridge")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
_CONCURRENCY = int(os.getenv("GEMINI_CONCURRENCY", "1"))
_RPM = max(1, int(os.getenv("GEMINI_RPM", "6")))
_CACHE_TTL = int(os.getenv("GEMINI_CACHE_TTL_SEC", "600"))
_SIM = (os.getenv("GEMINI_SIM_HEURISTIC", "0") == "1")

_SEM = asyncio.Semaphore(_CONCURRENCY)
_tokens = {"budget": _RPM, "reset": time.monotonic() + 60.0}
_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

def _now() -> float:
    return time.monotonic()

async def _rate_limit():
    global _tokens
    n = _now()
    if n >= _tokens["reset"]:
        _tokens["budget"] = _RPM
        _tokens["reset"] = n + 60.0
    while _tokens["budget"] <= 0:
        await asyncio.sleep(min(1.0, max(0.0, _tokens["reset"] - _now())) or 0.2)
        n = _now()
        if n >= _tokens["reset"]:
            _tokens["budget"] = _RPM
            _tokens["reset"] = n + 60.0
    _tokens["budget"] -= 1

def _sha1(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()

# ================= ENV HINTS ===================
def _get_neg_texts() -> List[str]:
    out: List[str] = []
    raw = os.getenv("LPG_NEGATIVE_TEXT")
    if raw:
        try:
            val = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(val, list):
                out.extend([str(x).strip().lower() for x in val if str(x).strip()])
        except Exception:
            pass
    if not out:
        legacy = os.getenv("LPG_NEG_TEXT_PATTERNS", "")
        out = [p.strip().lower() for p in re.split(r"[|,;]", legacy) if p.strip()]
    return out

def _get_pos_hints() -> List[str]:
    raw = os.getenv("GEMINI_LUCKY_HINTS", "")
    return [p.strip() for p in re.split(r"[|,;]", raw) if p.strip()]

def _build_prompt(context: str) -> str:
    pos = _get_pos_hints()
    neg = _get_neg_texts()
    lines = [
        "Classify whether this image is a gacha lucky-pull RESULT screen.",
        "Respond ONLY in strict JSON with keys: ok (bool), score (0..1), reason (string).",
        "score = confidence that this is a lucky-pull RESULT screen.",
        "POSITIVE cues: result grid of pulls, NEW!! popup, star/rarity, duplicate shards, banner/result panel.",
        "NEGATIVE cues: inventory, equipment/loadout/build editor, deck/card list, save data/date, settings, shop UI.",
    ]
    if pos:
        lines.append(f"Additional positive hints: {pos}.")
    if neg:
        lines.append(f"Negative phrases (strong FP filters): {neg}.")
    if context:
        lines.append(f"Context tag: {context}")
    lines.append("Answer with JSON only.")
    return "\n".join(lines)

async def _gemini_call(img_bytes: bytes, key: str, context: str) -> Dict[str, Any]:
    if _SIM:
        ok = bool(img_bytes and len(img_bytes) > 4096)
        score = 0.95 if ok else 0.0
        return {"ok": ok, "score": score, "reason": "sim_heuristic", "provider": f"gemini:{GEMINI_MODEL}"}
    try:
        import google.generativeai as genai  # type: ignore
    except Exception:
        return {"ok": False, "score": 0.0, "reason": "gemini_sdk_missing", "provider": f"gemini:{GEMINI_MODEL}"}

    try:
        genai.configure(api_key=key)
        model = genai.GenerativeModel(GEMINI_MODEL)
        prompt = _build_prompt(context)
        img_part = {"mime_type": "image/png", "data": img_bytes}
        resp = await asyncio.get_event_loop().run_in_executor(None, lambda: model.generate_content([prompt, img_part]))
        txt = getattr(resp, "text", "") or ""

        # --- Robust JSON parse from any mixed output ---
        data = {}
        try:
            data = json.loads(txt)
        except Exception:
            m = re.search(r"\{[\s\S]*\}", txt)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception:
                    data = {}

        if not isinstance(data, dict) or not data:
            return {"ok": False, "score": 0.0, "reason": "non_json_response", "provider": f"gemini:{GEMINI_MODEL}"}

        ok = bool(data.get("ok", False))
        score = float(data.get("score", 0.0))
        reason = str(data.get("reason", "")) or "model_json"

        # --- Strong negative clamp using env list ---
        neg = _get_neg_texts()
        hits: List[str] = []
        if neg:
            low_txt = txt.lower()
            hits = [w for w in neg if w and w in low_txt]
            if hits:
                score = min(score, 0.15)
                if ok and score < 0.5:
                    ok = False
                tag = ",".join(hits[:3]) + ("+more" if len(hits) > 3 else "")
                reason = (reason + f"|neg_clamp({tag})").strip("|")

        return {"ok": ok, "score": score, "reason": reason, "provider": f"gemini:{GEMINI_MODEL}"}
    except Exception as e:
        return {"ok": False, "score": 0.0, "reason": f"gemini_error:{type(e).__name__}", "provider": f"gemini:{GEMINI_MODEL}"}

def _gemini_keys() -> List[str]:
    keys = [os.getenv("GEMINI_API_KEY",""), os.getenv("GEMINI_API_KEY_B","")]
    return [k for k in keys if k]

async def classify_lucky_pull_bytes(img_bytes: bytes, context: str = "lpg") -> Dict[str, Any]:
    # cache check
    key = _sha1(img_bytes)
    now = time.monotonic()
    entry = _cache.get(key)
    if entry and (now - entry[0] <= _CACHE_TTL):
        res = dict(entry[1])
        res["reason"] = (res.get("reason") or "") + "|cache_hit"
        return res

    async with _SEM:
        await _rate_limit()
        result: Dict[str, Any] = {"ok": False, "score": 0.0, "reason": "gemini_unavailable", "provider": f"gemini:{GEMINI_MODEL}"}
        for api_key in _gemini_keys():
            for attempt in range(2):
                res = await _gemini_call(img_bytes, api_key, context)
                reason = res.get("reason","")
                if reason != "gemini_sdk_missing":
                    result = res
                    break
                if attempt == 0 and (reason == "non_json_response" or str(reason).startswith("gemini_error")):
                    await asyncio.sleep(0.3)
            if result.get("reason","") != "gemini_sdk_missing":
                break

    _cache[key] = (now, dict(result))
    return result
