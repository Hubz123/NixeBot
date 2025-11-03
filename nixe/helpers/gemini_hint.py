# nixe/helpers/gemini_hint.py
# Quick, regex-only phishing hint (Gemini-side "first detect").
# Tidak memakai API; gratis & cepat — hanya memutuskan apakah perlu route ke Groq.

import os, re
from typing import Dict, Any

PHISH_HINT_ENABLE = os.getenv("PHISH_HINT_ENABLE", "1") == "1"

_PATTERNS = [
    r"free\s+nitro",
    r"nitro\s+(gift|redeem|claim)",
    r"claim\s+(gift|reward|nitro)",
    r"(steam|discord)\s+gift",
    r"(wallet|crypto|usdt|airdrop)\s+(bonus|reward|gift|claim|free)",
    r"linktr\.ee|bit\.ly|tinyurl|cutt\.ly|is\.gd|s\.id",
    r"discord(app)?\.com\/(gifts|nitro)",
    r"qr|scan\s+this\s+code",
]

def quick_phish_hint_from_text(text: str) -> Dict[str, Any]:
    if not PHISH_HINT_ENABLE:
        return {"sus": True, "reason": "hint_disabled->route_anyway"}
    t = (text or "").lower()
    for pat in _PATTERNS:
        if re.search(pat, t):
            return {"sus": True, "reason": f"pattern:{pat}"}
    return {"sus": False, "reason": "no_pattern"}
