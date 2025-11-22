import os, aiohttp, json, base64, asyncio, re, time

# --- image mime sniff / optional convert for Gemini Vision ---
def _sniff_mime(image_bytes: bytes) -> str:
    hb = image_bytes[:16] if image_bytes else b''
    if hb.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if hb.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if len(hb) >= 12 and hb[:4] == b'RIFF' and hb[8:12] == b'WEBP':
        return 'image/webp'
    if hb.startswith(b'GIF87a') or hb.startswith(b'GIF89a'):
        return 'image/gif'
    if hb.startswith(b'BM'):
        return 'image/bmp'
    return 'application/octet-stream'

def _maybe_convert_to_jpeg(image_bytes: bytes, mime: str) -> tuple[bytes, str]:
    # Gemini reliably supports JPEG/PNG; convert others if PIL available.
    if mime in ('image/jpeg', 'image/png'):
        return image_bytes, mime
    try:
        from PIL import Image  # optional dependency
        import io
        im = Image.open(io.BytesIO(image_bytes))
        im = im.convert('RGB')
        buf = io.BytesIO()
        im.save(buf, format='JPEG', quality=92)
        return buf.getvalue(), 'image/jpeg'
    except Exception:
        return image_bytes, mime


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

def _env_keys_list() -> list[str]:
    """
    Prefer GEMINI_API_KEYS (CSV). Fallback to legacy single-key vars.
    """
    raw = (_env("GEMINI_API_KEYS", "") or "").strip()
    keys: list[str] = []
    if raw:
        keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        for kname in ("GEMINI_API_KEY", "GEMINI_API_KEY_B", "GEMINI_BACKUP_API_KEY"):
            kv = _env(kname, "").strip()
            if kv:
                keys.append(kv)
    dedup = []
    seen = set()
    for k in keys:
        if k not in seen:
            dedup.append(k); seen.add(k)
    return dedup

def _env_models_list() -> list[str]:
    """
    Primary model + optional fallbacks.
    """
    models: list[str] = []
    primary = _env("GEMINI_MODEL", "gemini-2.5-flash-lite").strip()
    if primary:
        models.append(primary)
    for kname in (
        "LUCKYPULL_GEMINI_FALLBACK",
        "LUCKYPULL_GEMINI_FALLBACK2",
        "GEMINI_FALLBACK_MODEL",
        "GEMINI_FALLBACK_MODEL2",
    ):
        mv = _env(kname, "").strip()
        if mv and mv not in models:
            models.append(mv)
    return models

def _extract_json_obj(text: str) -> str:
    """
    Salvage first JSON object from Gemini free-form output.
    Strips ```json fences and surrounding prose.
    """
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t, flags=re.I).strip()
    t = re.sub(r"```$", "", t).strip()
    m = re.search(r"\{[\s\S]*\}", t)
    return m.group(0).strip() if m else ""

def _parse_candidate_text(data: dict) -> str:
    cand = (data.get("candidates") or [{}])[0]
    parts = ((cand.get("content") or {}).get("parts") or [])
    out = ""
    for p in parts:
        if isinstance(p, dict) and "text" in p:
            out += str(p["text"])
    return out.strip()

def _pick_timeout_sec() -> float:
    try:
        v = float(_env("LUCKYPULL_GEMINI_TIMEOUT", "0") or 0)
        if v > 0:
            return v
    except Exception:
        pass
    try:
        ms = float(_env("GEMINI_TIMEOUT_MS", "0") or 0)
        if ms > 0:
            return ms / 1000.0
    except Exception:
        pass
    return 6.0

def _pick_total_budget_sec(per_timeout: float) -> float:
    try:
        v = float(_env("LUCKYPULL_GEMINI_TOTAL_TIMEOUT_SEC", "0") or 0)
        if v > 0:
            return max(per_timeout, v)
    except Exception:
        pass
    return per_timeout * 1.6

async def _call_gemini_once(session: aiohttp.ClientSession, key: str, model: str, payload: dict) -> tuple[bool, float, str, str]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    try:
        async with session.post(url, json=payload) as resp:
            txt = await resp.text()
            if resp.status != 200:
                return False, 0.0, f"gemini:{model}", f"http_{resp.status}"
            data = json.loads(txt)
            out = _parse_candidate_text(data)
            js = _extract_json_obj(out)
            if not js:
                return False, 0.0, f"gemini:{model}", "parse_error"
            obj = json.loads(js)
            lucky = bool(obj.get("lucky", False))
            score = float(obj.get("score", 0.0) or 0.0)
            reason = str(obj.get("reason", "") or "")
            return lucky, score, f"gemini:{model}", reason or "early(ok)"
    except asyncio.TimeoutError:
        return False, 0.0, f"gemini:{model}", "timeout"
    except Exception:
        return False, 0.0, f"gemini:{model}", "parse_error"

async def classify_lucky_pull_bytes(image_bytes: bytes):
    keys = _env_keys_list()
    if not keys:
        return False, 0.0, "none", "no_api_key"

    models = _env_models_list()
    sys_prompt = _build_sys_prompt()

    mime = _sniff_mime(image_bytes)
    img2, mime2 = _maybe_convert_to_jpeg(image_bytes, mime)
    b64 = base64.b64encode(img2).decode("ascii")
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": sys_prompt},
                {"inline_data": {"mime_type": mime2, "data": b64}}
            ]
        }],
        "generationConfig": {"temperature": 0.0, "topP": 0.0, "topK": 1, "candidateCount": 1, "maxOutputTokens": 128},
        "response_mime_type": "application/json"
    }

    per_timeout = _pick_timeout_sec()
    total_budget = _pick_total_budget_sec(per_timeout)
    timeout_cfg = aiohttp.ClientTimeout(total=per_timeout)

    t0 = time.monotonic()
    last_reason = "parse_error"
    last_via = f"gemini:{models[0]}"

    async with aiohttp.ClientSession(timeout=timeout_cfg) as s:
        for model in models:
            for key in keys:
                if (time.monotonic() - t0) > total_budget:
                    return False, 0.0, last_via, "timeout_budget"
                ok, score, via, reason = await _call_gemini_once(s, key, model, payload)
                last_reason, last_via = reason, via

                if bool(ok) and float(score or 0.0) >= 0.9:
                    return True, float(score or 0.0), via, reason or "early(ok)"

                rlow = (reason or "").lower()
                if ("http_429" in rlow) or ("http_5" in rlow) or ("timeout" in rlow) or ("parse_error" in rlow):
                    continue

                return bool(ok), float(score or 0.0), via, reason or "early(ok)"

    return False, 0.0, last_via, last_reason or "parse_error"