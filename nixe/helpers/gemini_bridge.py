from __future__ import annotations
import os, json, logging, asyncio, re
from typing import Dict, Any, List

log = logging.getLogger("nixe.helpers.gemini_bridge")

# === ENV READ (read-only; 100% follow runtime_env.json) ======================

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

def _get_neg_texts() -> List[str]:
    """
    Source of truth: LPG_NEGATIVE_TEXT (JSON array in runtime_env.json).
    Fallbacks:
      - LPG_NEG_TEXT_PATTERNS (delimited str: ',', ';', '|')
    """
    raw = os.getenv("LPG_NEGATIVE_TEXT")
    out: List[str] = []
    if raw:
        try:
            val = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(val, list):
                out.extend([str(x).strip() for x in val if str(x).strip()])
        except Exception:
            pass
    if not out:
        legacy = os.getenv("LPG_NEG_TEXT_PATTERNS", "")
        out = [p.strip() for p in re.split(r"[|,;]", legacy) if p.strip()]
    return [s.lower() for s in out]

def _get_pos_hints() -> List[str]:
    """
    Positive hints: GEMINI_LUCKY_HINTS (delimited str). Optional.
    """
    raw = os.getenv("GEMINI_LUCKY_HINTS", "")
    return [p.strip() for p in re.split(r"[|,;]", raw) if p.strip()]

def _build_prompt(context: str) -> str:
    pos = _get_pos_hints()
    neg = _get_neg_texts()
    lines = [
        "Classify whether this image is a *gacha lucky-pull result* screenshot.",
        "Respond in strict JSON: {\\\"ok\\\": bool, \\\"score\\\": number, \\\"reason\\\": string}.",
        "score in [0,1] = confidence that this is a lucky-pull RESULT screen.",
        "POSITIVE cues include: summon/pull result grid, NEW! popup, star/rarity burst, duplicate shards, banner/result panel.",
        "NEGATIVE cues include: inventory, equipment/loadout/build editor, deck/card list, save data/date, settings, profile, shop UI.",
    ]
    if pos:
        lines.append(f"Additional positive hints: {pos}.")
    if neg:
        lines.append(f"Negative phrases (strong FP filters): {neg}.")
    if context:
        lines.append(f"Context tag: {context}")
    lines.append("Answer with JSON only.")
    return "\n".join(lines)

# === Actual Gemini call (no heuristic unless explicitly enabled) ============

async def _gemini_call(img_bytes: bytes, key: str, context: str) -> Dict[str, Any]:
    if os.getenv("GEMINI_SIM_HEURISTIC","0") == "1":
        ok = bool(img_bytes and len(img_bytes) > 4096)
        score = 0.95 if ok else 0.0
        return { "ok": ok, "score": score, "reason": "sim_heuristic", "provider": f"gemini:{GEMINI_MODEL}" }

    try:
        import google.generativeai as genai  # type: ignore
    except Exception:
        return { "ok": False, "score": 0.0, "reason": "gemini_sdk_missing", "provider": f"gemini:{GEMINI_MODEL}" }

    try:
        genai.configure(api_key=key)
        model = genai.GenerativeModel(GEMINI_MODEL)
        prompt = _build_prompt(context)
        img_part = { "mime_type": "image/png", "data": img_bytes }
        resp = await asyncio.get_event_loop().run_in_executor(None, lambda: model.generate_content([prompt, img_part]))
        txt = getattr(resp, "text", None) or ""

        try:
            data = json.loads(txt)
        except Exception:
            m = re.search(r"\{[\s\S]*\}", txt)
            data = json.loads(m.group(0)) if m else {}

        ok = bool(data.get("ok", False))
        score = float(data.get("score", 0.0))
        reason = str(data.get("reason", "")) or "model_json"
        neg = _get_neg_texts()
        if neg and any(k in txt.lower() for k in neg):
            score = min(score, 0.15)
            if ok and score < 0.5:
                ok = False
                reason = "neg_hint_match"

        return { "ok": ok, "score": score, "reason": reason, "provider": f"gemini:{GEMINI_MODEL}" }
    except Exception as e:
        return { "ok": False, "score": 0.0, "reason": f"gemini_error:{type(e).__name__}", "provider": f"gemini:{GEMINI_MODEL}" }

# === Public API =============================================================

def _gemini_keys() -> List[str]:
    keys = [os.getenv("GEMINI_API_KEY",""), os.getenv("GEMINI_API_KEY_B","")]
    return [k for k in keys if k]

async def classify_lucky_pull_bytes(img_bytes: bytes, context: str = "lpg") -> Dict[str, Any]:
    for k in _gemini_keys():
        res = await _gemini_call(img_bytes, k, context)
        if res.get("reason") != "gemini_sdk_missing":
            return res
    return { "ok": False, "score": 0.0, "reason": "gemini_unavailable", "provider": f"gemini:{GEMINI_MODEL}" }
