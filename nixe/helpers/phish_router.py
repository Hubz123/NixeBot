# nixe/helpers/phish_router.py
# Route: Gemini hint (super cepat) -> Groq executor (decision).
from __future__ import annotations
import os
from typing import Optional, Dict, Any

from .gemini_hint import quick_phish_hint_from_text
from .text_phish_scanner import scan_giftbait_text
from .groq_bridge import classify_phish_image

ALWAYS_ROUTE = os.getenv("PHISH_ROUTER_ALWAYS", "0") == "1"
HINT_ROUTE_ONLY = os.getenv("PHISH_ROUTER_HINT_ONLY", "0") == "1"

def classify_fast(*, message_text: str = "", image_url: Optional[str] = None, image_bytes: Optional[bytes] = None) -> Dict[str, Any]:
    hint = quick_phish_hint_from_text(message_text or "")
    # Text-only signature scan (celebrity crypto-casino gift-bait)
    try:
        env = os.environ
        if message_text:
            _tres = scan_giftbait_text(message_text, env)
            if _tres.get('ok'):
                return {
                  'ok': True,
                  'phish': 1,
                  'reason': _tres.get('reason','text-giftbait'),
                  'provider': 'text-only:gifting',
                  'hint': hint,
                }
    except Exception:
        pass

    if not HINT_ROUTE_ONLY:
        route = (hint.get("sus") is True) or ALWAYS_ROUTE or bool(image_url or image_bytes)
    else:
        route = (hint.get("sus") is True)

    if not route:
        return {"ok": True, "phish": 0, "reason": "no_route(hint_false)", "provider": "router:noop", "hint": hint}

    res = classify_phish_image(image_url=image_url, image_bytes=image_bytes, context_text=message_text)
    return {"ok": res.ok, "phish": res.phish, "reason": res.reason, "provider": res.provider, "hint": hint}
