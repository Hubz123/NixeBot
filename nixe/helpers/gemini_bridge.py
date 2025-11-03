# nixe/helpers/gemini_bridge.py
# Policy: Gemini ONLY for Lucky Pull. Any phishing call is redirected to Groq.
import os, asyncio
from typing import Dict, Any, Optional

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

# ---- Minimal Lucky Pull classifier (adapter) -------------------------
# NOTE: This is a façade: it decides key selection + failover & shapes output.
#       The real Gemini API call should live in _gemini_call(). Replace that
#       with your existing implementation if needed.

async def _gemini_call(img_bytes: bytes, key: str, context: str) -> Dict[str, Any]:
    """
    Placeholder for your actual Gemini vision classification.
    MUST return: {"ok": bool, "score": float, "reason": str}
    Where score∈[0,1] means lucky-pull-likeness.
    """
    # Dummy heuristic to keep this adapter standalone if SDK unavailable.
    # You SHOULD replace with proper Google SDK usage.
    ok = bool(img_bytes and len(img_bytes) > 4096)
    score = 0.95 if ok else 0.0
    reason = "heuristic_large_image" if ok else "image_too_small(len<=4096)"
    return {"ok": ok, "score": score, "reason": reason}

def _keys() -> list[str]:
    A = os.getenv("GEMINI_API_KEY", "")
    B = os.getenv("GEMINI_API_KEY_B", "")
    return [k for k in (A, B) if k]

async def classify_lucky_pull_bytes(img_bytes: bytes, context: str = "lpg") -> Dict[str, Any]:
    """
    Lucky Pull only (Gemini). Multi-key failover:
      - use GEMINI_API_KEY first
      - on failure/ratelimit, fallback to GEMINI_API_KEY_B
    Returns:
      {"ok": bool, "score": float, "reason": str, "provider": "gemini:<model>"}
    """
    model = GEMINI_MODEL
    for key in _keys():
        try:
            res = await _gemini_call(img_bytes, key, context)
            res["provider"] = f"gemini:{model}"
            return res
        except Exception as e:
            # Continue to next key
            last_err = e
    # Total failure
    return {"ok": False, "score": 0.0, "reason": "no_api_key_or_all_failed", "provider": f"gemini:{model}"}

# ---- Phishing: block + redirect to Groq ------------------------------

async def classify_phishing_bytes(*args, **kwargs) -> Dict[str, Any]:
    """
    This bridge intentionally does NOT classify phishing with Gemini.
    It always asks caller to forward to Groq.
    """
    return {
        "ok": True,
        "phish": 1,  # signal to downstream to treat as suspicious flow
        "reason": "redirect_to_groq",
        "provider": f"gemini:{GEMINI_MODEL}",
        "redirect": "groq"
    }
