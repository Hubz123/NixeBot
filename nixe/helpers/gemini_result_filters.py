
"""nixe.helpers.gemini_result_filters
--------------------------------------
Safety filters for Gemini lucky-pull JSON responses.

This module adds an extra defensive layer on top of the model output,
so that obviously non-result screens such as deck/collection/status
views are forced to NOT LUCKY even if the model is over-confident.

The functions here are deliberately small and dependency-free so they
can be safely imported from helpers like gemini_lpg_burst.
"""

from __future__ import annotations
from typing import Any, Dict, List

# Textual hints that typically indicate a deck / collection / non-result UI
_DECK_HINTS: List[str] = [
    "deck",
    "card deck",
    "card list",
    "card collection",
    "collection of cards",
    "skill card list",
    "card library",
    "build deck",
    "building deck",
    "edit deck",
    "editing deck",
    "collection screen",
    "deck screen",
    "inventory",
    "loadout",
]


def apply_deck_hardening(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a hardened copy of the Gemini JSON payload.

    Expected input shape is the JSON returned by the Gemini prompt used in
    gemini_lpg_burst, e.g.:

        {
          "lucky": bool,
          "score": float,
          "reason": str,
          "flags": [...],
          "screen_type": str,
          "slot_count": int,
          "is_multi_result_screen": bool,
          ...
        }

    The function is intentionally tolerant: if keys are missing or types
    are unexpected, it falls back to safe defaults and never raises.
    """
    if not isinstance(payload, dict):
        return {"lucky": False, "score": 0.0, "reason": "invalid_payload", "flags": []}

    data: Dict[str, Any] = dict(payload)  # shallow copy so we do not mutate callers unintentionally

    # Normalised textual fields
    reason = str(data.get("reason") or "").lower()
    screen_type = str(data.get("screen_type") or "").lower()

    raw_flags = data.get("flags") or []
    if not isinstance(raw_flags, list):
        raw_flags = [raw_flags]
    flags: List[str] = [str(x).lower() for x in raw_flags]
    flags_text = " ".join(flags)

    # Slot count (may be approximate from the model)
    slot_count_raw = data.get("slot_count", None)
    try:
        slot_count = int(slot_count_raw) if slot_count_raw is not None else None
    except Exception:
        slot_count = None

    # Best-effort current lucky/score
    lucky = bool(data.get("lucky", False))
    try:
        score = float(data.get("score", 0.0) or 0.0)
    except Exception:
        score = 0.0

    # Detect deck/collection style screens
    joined = " ".join([reason, screen_type, flags_text])
    deck_signal = any(h in joined for h in _DECK_HINTS)

    # Additional heuristic: too many cards/items at once almost always means a deck,
    # collection, or planner view, not a 10-pull gacha result.
    if slot_count is not None and slot_count >= 15:
        deck_signal = True

    if deck_signal:
        lucky = False
        # cap score to a low value so downstream thresholds never treat this as lucky
        score = min(score, 0.2)
        # normalise metadata
        if not screen_type:
            data["screen_type"] = "deck_many_cards"
        data["is_multi_result_screen"] = False
        data["is_result_screen"] = False
        data["ok"] = False
        # prefix reason for easier debugging
        existing_reason = str(data.get("reason") or "")
        if "deck_screen" not in existing_reason.lower():
            data["reason"] = ("deck_screen:" + existing_reason) if existing_reason else "deck_screen"
    else:
        # Ensure basic keys exist for downstream callers
        data.setdefault("is_result_screen", bool(lucky))
        data.setdefault("ok", bool(lucky))

    # Clamp score defensively
    try:
        s = float(score)
    except Exception:
        s = 0.0
    data["score"] = max(0.0, min(1.0, s))
    data["lucky"] = bool(lucky)

    return data
