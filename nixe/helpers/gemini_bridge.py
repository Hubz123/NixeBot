
import os, aiohttp, json, base64

def _env(k: str, default: str = "") -> str:
    v = os.getenv(k)
    return v if v is not None and v != "" else default

def _load_neg_text() -> list[str]:
    """Load LPG_NEGATIVE_TEXT from env/runtime_env (JSON list or CSV)."""
    import json as _json
    raw = (os.getenv("LPG_NEGATIVE_TEXT") or "").strip()
    if not raw:
        return []
    try:
        obj = _json.loads(raw)
        if isinstance(obj, (list, tuple)):
            out = [str(x).strip() for x in obj if str(x).strip()]
            if out:
                return out
    except Exception:
        pass
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def _build_sys_prompt() -> str:
    # Fixed NOT_LUCKY phrases to reduce event/banner false-positives.
    fixed_neg = [
        "event", "version", "banner", "promo", "announcement", "福利", "版本", "活动",
        "免费", "领取", "时装", "套装", "抽+", "抽", "换装", "skin", "costume",
        "reward", "login bonus", "patch notes"
        "rescue merit", "available rewards", "guaranteed", "only once", "obtain", "not owned", "reward list", "reward select", "claim reward", "exchange", "shop", "store", "purchase", "selector", "currency", "merit", "redeem",
    ]
    neg = fixed_neg + _load_neg_text()
    neg_txt = ", ".join(f'"{p}"' for p in neg if p)

    return (
        "You are a game UI analyst.\n"
        "Task: Decide if an IMAGE shows a **gacha/lucky-pull RESULTS screen** "
        "or **NOT** (promotional banner/event notice/inventory/loadout/etc).\n\n"
        "Return ONLY compact JSON: {\"lucky\": <true|false>, \"score\": 0..1, \"reason\": \"...\"}.\n\n"
        "Strong LUCKY cues (need 2+ at the SAME time):\n"
        "- Exactly ~10 result tiles in a 2x5 grid (common 10-pull),\n"
        "- Each tile is a character/weapon/gear ICON with STAR rarity markers beneath (★ etc),\n"
        "- Result UI buttons like Confirm/Skip/Continue and currency change bar.\n\n"
        "Strong NOT_LUCKY cues:\n"
        "- Promotional/event banners or announcements with BIG headline text,\n"
        "- Collage of multiple character arts without in-game result grid/UI,\n"
        "- Screens mentioning or corresponding to any of: " + neg_txt + ".\n\n"
        "Rules:\n"
        "- Be conservative: if mixed/unsure, choose not_lucky with score <= 0.4.\n"
        "- Only choose lucky with score >= 0.9 when results UI is clear."
    )
async def classify_lucky_pull_bytes(image_bytes: bytes):
    key = _env("GEMINI_API_KEY", _env("GEMINI_API_KEY_B", _env("GEMINI_BACKUP_API_KEY", "")))
    if not key:
        return False, 0.0, "none", "no_api_key"
    model = _env("GEMINI_MODEL", "gemini-2.5-flash-lite")
    sys_prompt = _build_sys_prompt()
    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": sys_prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
            ]
        }],
        "generationConfig": {"temperature": 0.0, "topP": 0.0, "topK": 1, "candidateCount": 1, "maxOutputTokens": 128},
        "response_mime_type": "application/json"
    }
    timeout = 6.0
    async with aiohttp.ClientSession() as s:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        async with s.post(url, json=payload) as resp:
            txt = await resp.text()
            try:
                data = json.loads(txt)
                cand = data.get("candidates", [{}])[0]
                parts = ((cand.get("content") or {}).get("parts") or [])
                out = ""
                for p in parts:
                    if "text" in p: out += p["text"]
                obj = json.loads(out.strip())
                return bool(obj.get("lucky", False)), float(obj.get("score", 0.0)), f"gemini:{model}", str(obj.get("reason",""))
            except Exception:
                return False, 0.0, f"gemini:{model}", "parse_error"