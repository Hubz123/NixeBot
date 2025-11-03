import re
from typing import Dict, List

def _count_hits(pattern: str, text: str) -> int:
    try:
        return len(set(m.group(0).lower() for m in re.finditer(pattern, text or "", flags=re.I)))
    except re.error:
        return 0

def scan_giftbait_text(text: str, env: Dict[str, str]) -> Dict[str, object]:
    strong_pat = env.get("PHISH_TEXT_STRONG_PATTERNS", "")
    weak_pat   = env.get("PHISH_TEXT_WEAK_PATTERNS", "")
    brands_pat = env.get("PHISH_NEWS_BRAND_WORDS", "")
    celeb_pat  = env.get("PHISH_CELEB_BAIT_WORDS", "")

    strong_hits = _count_hits(strong_pat, text)
    weak_hits   = _count_hits(weak_pat,   text)
    brand_hits  = _count_hits(brands_pat, text)
    celeb_hits  = _count_hits(celeb_pat,  text)

    score = strong_hits * 1.5 + weak_hits * 0.5 + (brand_hits + celeb_hits) * 0.75
    labels: List[str] = []
    if strong_hits: labels.append(f"strong:{strong_hits}")
    if weak_hits:   labels.append(f"weak:{weak_hits}")
    if brand_hits:  labels.append("brand")
    if celeb_hits:  labels.append("celeb")

    strong_min = int(env.get("PHISH_TEXT_STRONG_MIN_TOKENS", "3"))
    autoban    = int(env.get("PHISH_TEXT_AUTO_BAN_TOKENS", "5"))

    is_autoban = (strong_hits >= autoban) or (strong_hits >= strong_min and (brand_hits or celeb_hits))
    ok = score >= 3.0 or is_autoban

    reason = f"text-giftbait score={score:.3f} strong={strong_hits} weak={weak_hits} brand={brand_hits} celeb={celeb_hits}"
    return {"ok": ok, "score": float(f"{score:.3f}"), "labels": labels, "reason": reason, "autoban": bool(is_autoban)}
