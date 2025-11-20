
# nixe/helpers/gemini_bridge_lucky_rules_patch_n31.py
"""Optional patched rubric for Gemini lucky-pull classification (n31).

This module is *not* wired by default, but can be used by future helpers or
overlays that want a richer prompt for differentiating gacha result screens
from deck/collection/status views.

The intent is to keep this file importable without additional dependencies.
"""

from __future__ import annotations

RUBRIC_PROMPT_N31 = """You are a vision classifier that decides whether an image
shows a **gacha result screen** (the outcome after a pull) or **not**.

Return ONLY compact JSON:
{"is_result_screen": <true|false>, "ok": <true|false>, "score": <0..1>, "reason": "..."}

=== DEFINITIONS ===
Result / Lucky Pull:
- Shows 1–10 newly obtained items or characters with clear result UI such as
  "Result", "Obtained", "New", "Rewards", or pull count.
- Often displays rarity stars, rarity colours, or celebratory visual effects.
- The UI clearly communicates the outcome of a gacha pull.

NOT Result:
- Deck / card list / collection / library screens.
- Inventory, loadout, equipment, character status, build sheets, planners.
- Save / load data screens, mission lists, profiles, settings, web pages.

=== RULES ===
1. If the screen clearly shows a deck, card list, collection, or any library of
   many cards or items (for example 14 or more visible cards in a dense grid),
   you MUST treat it as NOT a result screen. Set:
   - is_result_screen=false, ok=false,
   - score <= 0.2,
   - reason describing it as a deck/collection screen.
2. Only mark a screen as a gacha result when there is clear evidence of a
   pull outcome (result labels, pull count, reward presentation).
3. If you are uncertain, always choose NOT a result screen with low score
   (<= 0.3).

Respond with JSON only. Do not include explanations or markdown.
"""


def get_lucky_prompt_n31() -> str:
    """Return the patched rubric prompt text for n31."""
    return RUBRIC_PROMPT_N31
