# nixe/helpers/gemini_bridge_lucky_rules.py
"""
Strict Gemini classifier for Lucky Pull (image bytes only).
- Conservative: prefers NOT LUCKY if ambiguities with inventory/loadout screens.
- Returns (score: 0..1, reason: str). Silent on failures -> (None, reason).
"""
import os

def _load_neg_text() -> list[str]:
    """Load LPG_NEGATIVE_TEXT from env/runtime_env (JSON list or CSV).
    Returns a cleaned list of phrases; safe if unset or malformed.
    """
    import json as _json
    raw = (os.getenv("LPG_NEGATIVE_TEXT") or "").strip()
    if not raw:
        return []
    # Try JSON list first
    try:
        obj = _json.loads(raw)
        if isinstance(obj, (list, tuple)):
            out = [str(x).strip() for x in obj if str(x).strip()]
            if out:
                return out
    except Exception:
        pass
    # Fallback: comma-separated string
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]

# Lazy import to avoid hard dependency if library missing at import time
def _lazy_client():
    try:
        import google.generativeai as genai
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not key:
            return None, "no_key"
        genai.configure(api_key=key)
        model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
        model = genai.GenerativeModel(model_name)
        return model, "ok"
    except Exception as e:
        return None, f"imp_err:{type(e).__name__}"

def classify_lucky_pull_bytes(img_bytes: bytes):
    try:
        # Force real attachment (do not use any internal sample)
        os.environ.setdefault("LPG_SMOKE_FORCE_SAMPLE","0")
        model, s = _lazy_client()
        if model is None:
            return None, s

        # Rubric: VERY explicit separation Lucky vs Loadout
        system = (
            "Task: Decide if an IMAGE shows a **gacha/lucky-pull RESULTS screen** "
            "or **NOT** (inventory/settings/loadout/etc). "
            "Return only JSON with keys {label:'lucky|not_lucky', confidence:0..1}.\n\n"
            "Strong LUCKY cues (need 2+ ideally):\n"
            "- Exactly ~10 result tiles in a 2x5 grid (common 10-pull),\n"
            "- Character/gear icons each with STAR ratings beneath (★, ★★, ★★★ ...),\n"
            "- Badges like 'NEW' on some tiles, sparkles/confetti around tiles,\n"
            "- Bottom bar showing currency change or 'Owned ...' progress.\n\n"
            "Strong NOT_LUCKY (inventory/loadout) cues:\n"
            "- Sidebars with multiple loadout slots, 'Equip', 'Dismantle in Bulk', 'Customize Favorites', 'Save data', 'Save date',\n"
            "- Long lists of cards with detailed descriptions per card, filters, trash-bin icon, or big numbers like '38150 TB',\n"
            "- Management UI (tabs, lock icons on slots, deck editors).\n\n"
            "Rules:\n"
            "- Be conservative: if mixed or unsure, prefer not_lucky (confidence <= 0.4).\n"
            "- If it clearly shows 10-pull grid with stars/New badges -> lucky with high confidence (>=0.9)."
        )
        neg = _load_neg_text()
        if neg:
            extra = (
                "\n\nAdditional NOT_LUCKY UI contexts from config:"
                "\n- Screens that mention or visually correspond to any of: "
                + ", ".join(f'"{p}"' for p in neg)
                + ". Treat these as inventory/loadout/deck/status screens, NOT gacha results."
            )
            system = system + extra

        prompt = "Classify the uploaded image strictly per the rubric. Output JSON only."

        # Construct Gemini call
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(img_bytes))
        image_part = {"mime_type": "image/png", "data": img_bytes}
        # Some SDKs accept raw bytes; this dict representation helps newer SDKs.

        res = model.generate_content([system, prompt, image_part])
        txt = (res.text or "").strip()

        # Parse minimal JSON without strict dependency
        import json, re
        try:
            m = re.search(r"\{.*\}", txt, flags=re.S)
            obj = json.loads(m.group(0)) if m else json.loads(txt)
            label = str(obj.get("label","")).lower().strip()
            conf = float(obj.get("confidence", 0.0))
        except Exception:
            # Fallback heuristic from plain text
            low = txt.lower()
            if ("not_lucky" in low or "not lucky" in low or "inventory" in low or "loadout" in low or "deck" in low or "card list" in low or "collection" in low or "save data" in low or "status screen" in low):
                label, conf = "not_lucky", 0.3
            elif "lucky" in low or "gacha" in low:
                label, conf = "lucky", 0.9
            else:
                return None, "parse_fail"

        score = conf if label == "lucky" else 1.0 - conf
        # Clamp and bias to be conservative on not_lucky
        if label != "lucky" and score > 0.5:
            score = 0.4
        return float(score), f"gemini:strict"
    except Exception as e:
        return None, f"err:{type(e).__name__}"
