import os, aiohttp, json, base64, asyncio, re, time, io, logging
import httpx

_log = logging.getLogger(__name__)




def _make_httpx_timeout(total: float):
    """Build an httpx timeout object compatible across httpx versions.

    Returns either httpx.Timeout or a float fallback (seconds).
    """
    try:
        # Most httpx versions accept a single "total" seconds parameter.
        return httpx.Timeout(total)
    except Exception:
        pass
    try:
        # Some versions require per-phase keyword arguments.
        return httpx.Timeout(connect=total, read=total, write=total, pool=total)
    except Exception:
        # Fallback: httpx.AsyncClient accepts a float timeout.
        return float(total)


async def _async_httpx_client(*, timeout):
    """Create an httpx.AsyncClient with best-effort compatibility across httpx versions.

    Some older httpx releases do not accept `follow_redirects` in the AsyncClient constructor.
    """
    try:
        return httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    except TypeError:
        return httpx.AsyncClient(timeout=timeout)

try:
    from groq import Groq  # type: ignore
except Exception:  # pragma: no cover
    Groq = None
def _sniff_mime(image_bytes: bytes) -> str:
    """Best-effort mime sniff by magic bytes."""
    if not image_bytes:
        return "image/jpeg"
    b = image_bytes
    if b.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if b.startswith(b"GIF87a") or b.startswith(b"GIF89a"):
        return "image/gif"
    if b.startswith(b"BM"):
        return "image/bmp"
    # WEBP: RIFF....WEBP
    if len(b) >= 12 and b[0:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _prepare_inline_image(image_bytes: bytes) -> tuple[bytes, str]:
    """
    Ensure inline_data bytes + mime are coherent for Gemini.
    If format isn't jpeg/png, try best-effort convert to jpeg (PIL optional).
    """
    mime = _sniff_mime(image_bytes)

    if mime in ("image/jpeg", "image/png"):
        return image_bytes, mime

    # Attempt convert to JPEG if PIL available.
    try:
        from PIL import Image  # type: ignore
        im = Image.open(io.BytesIO(image_bytes))
        im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=92)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        # Fallback to original bytes with sniffed mime.
        return image_bytes, mime

def _env(k: str, default: str = "") -> str:
    """Read config consistently with runtime/secrets policy.
    Prefer nixe.helpers.env_reader.get (supports runtime_env.json + secrets.json with NIXE_ALLOW_JSON_SECRETS),
    fallback to os.getenv for minimal contexts.
    """
    try:
        from .env_reader import get as _get  # lazy import to avoid cycles
        v = _get(k, default)
        return str(v).strip() if v is not None and str(v).strip() != "" else str(default)
    except Exception:
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
    # Fixed NOT_LUCKY phrases to reduce promo/banner false-positives.
    # NOTE: Do not rely on these keywords alone; strong RESULTS cues should win.
    fixed_neg = [
        # Broad promo/notice/banners
        "event", "version", "banner", "promo", "announcement", "patch notes",
        "福利", "版本", "活动",
        # Cosmetics / outfits
        "时装", "套装", "skin", "costume", "换装",
        # Store / redeem / selector UIs (inventory-like)
        "store", "purchase", "buy", "redeem", "兑换", "商店", "购买", "selector", "currency",
        # Explicit reward pages (not results screens)
        "login bonus", "available rewards", "rescue merit",
    ]
    neg = fixed_neg + _load_neg_text()
    neg_txt = ", ".join(f'"{p}"' for p in neg if p)

    return (
        "You are a game UI analyst.\n"
        "Task: Decide if an IMAGE shows a **gacha/lucky-pull RESULTS screen** or **NOT** "
        "(promotional banner/event notice/inventory/loadout/roster/etc).\n\n"
        "Guidance:\n"
        "- LUCKY/RESULTS screens usually show multiple reward tiles/cards, rarity indicators (stars/colors), and action buttons like "
        "\"Convene/Draw again\", \"10x/1x\", \"Skip\", \"Confirm\", or equivalent.\n"
        "- NOT_LUCKY includes: event banners/announcements, shop/redeem/selectors, inventory/roster/loadouts, tier lists/rankings.\n"
        "- If strong RESULTS cues are present, return lucky=true even if some negative keywords appear.\n\n"
        "Return ONLY compact JSON: {\"lucky\": <true|false>, \"score\": 0..1, \"reason\": \"...\"}.\n"
        "Score rubric: 0.90+ = clear results UI; 0.70-0.89 = likely results but partial/blurred; "
        "0.50-0.69 = ambiguous; <0.50 = not results.\n\n"
        "Negative keyword hints (do NOT treat as absolute): "
        + neg_txt
        + "\n\n"
        "Rules:\n"
        "- Be conservative: if mixed/unsure, choose not_lucky with score <= 0.4.\n"
        "- Only choose lucky with score >= 0.9 when results UI is clear."
    )

def _env_keys_list() -> list[str]:
    """Return LPG key list (Groq API keys) for LuckyPullGuard.

    Preferred new naming:
      - LPG_API_KEYS (CSV)
      - LPG_API_KEY / LPG_API_KEY_B / LPG_BACKUP_API_KEY

    Backward compatibility (legacy still accepted):
      - GEMINI_API_KEYS (CSV)
      - GEMINI_API_KEY / GEMINI_API_KEY_B / GEMINI_BACKUP_API_KEY
    """
    keys: list[str] = []

    # Preferred new vars
    raw = (_env("LPG_API_KEYS", "") or "").strip()
    if raw:
        keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        for kname in ("LPG_API_KEY", "LPG_API_KEY_B", "LPG_BACKUP_API_KEY"):
            kv = (_env(kname, "") or "").strip()
            if kv:
                keys.append(kv)

    # Legacy fallback
    if not keys:
        raw2 = (_env("GEMINI_API_KEYS", "") or "").strip()
        if raw2:
            keys = [k.strip() for k in raw2.split(",") if k.strip()]
    if not keys:
        for kname in ("GEMINI_API_KEY", "GEMINI_API_KEY_B", "GEMINI_BACKUP_API_KEY"):
            kv = (_env(kname, "") or "").strip()
            if kv:
                keys.append(kv)

    # Dedupe (stable)
    dedup: list[str] = []
    seen = set()
    for k in keys:
        if k and k not in seen:
            dedup.append(k)
            seen.add(k)
    return dedup
def _env_models_list() -> list[str]:
    """
    Models list for LPG classifier (Groq-only).

    Policy:
    - LPG must NOT call Google Gemini REST. Gemini is reserved for translate / OCR-translate flows.
    - LPG uses ONLY Groq vision models from env:
        - GROQ_MODEL_VISION (primary)
        - GROQ_MODEL_VISION_CANDIDATES (comma-separated)
        - GROQ_MODEL_VISION_FALLBACKS (comma-separated)

    If none are configured, return an empty list and the caller will report `no_groq_model`.
    """
    models: list[str] = []

    g_primary = _env("GROQ_MODEL_VISION", "").strip()
    if g_primary:
        models.append(g_primary)

    cand = _env("GROQ_MODEL_VISION_CANDIDATES", "").strip()
    if cand:
        for m in [x.strip() for x in cand.split(",") if x.strip()]:
            if m not in models:
                models.append(m)

    fb = _env("GROQ_MODEL_VISION_FALLBACKS", "").strip()
    if fb:
        for m in [x.strip() for x in fb.split(",") if x.strip()]:
            if m not in models:
                models.append(m)

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
        v = float(_env("LUCKYPULL_GROQ_TIMEOUT", "0") or 0)
        if v > 0:
            return v
    except Exception:
        pass
    try:
        ms = float(_env("GROQ_TIMEOUT_MS", "0") or 0)
        if ms > 0:
            return ms / 1000.0
    except Exception:
        pass
    return 6.0

def _pick_total_budget_sec(per_timeout: float, n_models: int | None = None, n_keys: int | None = None) -> float:
    """Pick an overall wall-clock budget for LPG classify.
    Caller may pass n_models/n_keys; older versions ignored them.
    Default remains close to prior behavior (1.6x per_timeout),
    with a modest bump when multiple model/key combinations exist.
    """
    try:
        v = float(_env("LUCKYPULL_GROQ_TOTAL_TIMEOUT_SEC", "0") or 0)
        if v > 0:
            return max(per_timeout, v)
    except Exception:
        pass
    base = per_timeout * 1.6
    try:
        nm = int(n_models) if n_models is not None else 1
        nk = int(n_keys) if n_keys is not None else 1
        combos = max(1, nm * nk)
        if combos > 1:
            bump = min(per_timeout * 0.25 * float(combos - 1), per_timeout * 2.0)
            base += bump
    except Exception:
        pass
    return base

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


async def _call_groq_lpg_once(
    key: str,
    model: str,
    sys_prompt: str,
    img_bytes: bytes,
    mime: str,
    timeout_sec: float,
) -> tuple[bool, float, str, str]:
    """Call Groq (OpenAI-compatible) for Lucky Pull Guard.

    Returns:
        (lucky, score, via, reason) with via shaped as "gemini:<model>" for backward compatibility.
    """
    via = f"gemini:{model}"
    if not key:
        return False, 0.0, via, "no_api_key"
    if not model:
        return False, 0.0, via, "no_model"

    # Build OpenAI-compatible chat/completions request with inline data URL image.
    try:
        b64 = base64.b64encode(img_bytes).decode("ascii")
    except Exception:
        return False, 0.0, via, "b64_error"

    data_url = f"data:{mime};base64,{b64}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Classify this image."},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        "temperature": 0,
        "max_tokens": 128,
    }

    # Tight, cancellable http timeout (no executor/thread leaks).
    _t = float(timeout_sec or 0.0)
    if _t <= 0:
        _t = 6.0
    timeout = _make_httpx_timeout(_t)

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    url = "https://api.groq.com/openai/v1/chat/completions"

    try:
        async with (await _async_httpx_client(timeout=timeout)) as client:
            resp = await client.post(url, headers=headers, json=payload)
            # Ensure we always see a request/response line even if httpx logging isn't enabled.
            _log.info("[lpg] groq chat.completions status=%s model=%s", resp.status_code, model)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        # httpx timeout exception class names vary across versions; do not reference a missing attribute
        # in an `except httpx.X` clause (can itself raise AttributeError).
        _timeout_types = []
        for _n in ("TimeoutException", "TimeoutError", "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout"):
            _t = getattr(httpx, _n, None)
            if isinstance(_t, type):
                _timeout_types.append(_t)
        if isinstance(e, asyncio.TimeoutError) or (_timeout_types and isinstance(e, tuple(_timeout_types))):
            return False, 0.0, via, "timeout"
        return False, 0.0, via, f"error:{type(e).__name__}:{e}"

    # Extract assistant text
    txt = ""
    try:
        choices = data.get("choices") or []
        msg = (choices[0] or {}).get("message") or {}
        content = msg.get("content", "")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    txt += str(part.get("text") or "")
        else:
            txt = str(content or "")
    except Exception:
        txt = ""

    js = _extract_json_obj(txt)
    if not js:
        return False, 0.0, via, "parse_error"
    try:
        obj = json.loads(js)
    except Exception:
        return False, 0.0, via, "parse_error"

    lucky = bool(obj.get("lucky", obj.get("is_lucky", False)))
    score = float(obj.get("score", obj.get("confidence", 0.0)) or 0.0)
    reason = str(obj.get("reason", "") or "")
    return lucky, score, via, reason or "early(ok)"



async def _classify_one(
    img_bytes: bytes,
    mime: str,
    sys_prompt: str,
    model: str,
    api_key: str,
    timeout_sec: float,
) -> tuple[bool, float, str, str]:
    """One LPG classify attempt.
    Routes to Groq (OpenAI-compatible via `groq` SDK) for LPG.
    NOTE: Google Gemini REST is explicitly disallowed in LPG; Gemini is reserved for translate flows (TRANSLATE_GEMINI_API_KEY) only.
    """
    m = (model or "").strip().lower()
    use_gemini = m.startswith("gemini") or m.startswith("models/")
    if use_gemini:
        # LPG must not use Google Gemini REST.
        return False, 0.0, f"none:{model}", "gemini_model_disallowed"
    # Default: Groq path (keeps legacy `via` prefix in _call_groq_lpg_once)
    return await _call_groq_lpg_once(
        key=api_key,
        model=model,
        sys_prompt=sys_prompt,
        img_bytes=img_bytes,
        mime=mime,
        timeout_sec=timeout_sec,
    )


async def _classify_lucky_pull_bytes_core(image_bytes: bytes):
    """
    Lucky Pull Guard classifier.

    Return format:
        (ok: bool, score: float, via: str, reason: str)

    Notes:
    - LPG uses Groq vision models (see _env_models_list) and LPG keys (see _env_keys_list).
    - To avoid premature classify_timeout, we clamp per-attempt timeout and total budget to the caller's
      outer LPG_TIMEOUT_SEC (if configured), with small safety margins.
    """
    keys = _env_keys_list()
    if not keys:
        return False, 0.0, "none", "no_api_key"

    models = _env_models_list()
    if not models:
        return False, 0.0, "none", "no_groq_model"

    sys_prompt = _build_sys_prompt()
    img_bytes, mime = _prepare_inline_image(image_bytes)

    per_timeout = float(_pick_timeout_sec() or 0.0)
    total_budget = float(
        _pick_total_budget_sec(
            per_timeout=per_timeout,
            n_models=len(models),
            n_keys=len(keys),
        )
        or 0.0
    )

    # Align internal budgets with runtime outer timeout (do NOT shrink as a percentage).
    try:
        outer = float(_env("LPG_TIMEOUT_SEC", "0") or 0.0)
    except Exception:
        outer = 0.0
    if outer and outer > 0:
        # Keep small safety margins for JSON parsing / network jitter.
        per_timeout = min(per_timeout, max(2.5, outer - 0.9))
        total_budget = min(total_budget, max(3.0, outer - 0.4))

    if per_timeout <= 0:
        per_timeout = 6.0
    if total_budget <= 0:
        total_budget = max(3.0, per_timeout)

    last_via, last_reason = "none", "no_attempt"
    deadline = time.time() + max(3.0, float(total_budget))

    for model in models:
        for key in keys:
            if time.time() > deadline:
                return False, 0.0, last_via, "timeout_total_budget"

            # Do not exceed remaining total budget.
            remaining = max(1.5, deadline - time.time())
            timeout_sec = min(float(per_timeout), float(remaining))

            try:
                ok, score, via, reason = await _classify_one(
                    img_bytes=img_bytes,
                    mime=mime,
                    sys_prompt=sys_prompt,
                    model=model,
                    api_key=key,
                    timeout_sec=timeout_sec,
                )
            except Exception as e:
                ok, score, via, reason = False, 0.0, f"gemini:{model}", f"error:{type(e).__name__}:{e}"

            last_via, last_reason = via, reason
            score = float(score or 0.0)

            # Fast accept on very confident "lucky".
            if bool(ok) and score >= 0.9:
                return True, score, via, reason or "early(ok)"

            # Retry on transient/low-signal reasons.
            rlow = (reason or "").lower()
            if ("timeout" in rlow) or ("error" in rlow) or ("parse_error" in rlow):
                continue

            # First definitive answer.
            return bool(ok), score, via, reason or "early(ok)"

    return False, 0.0, last_via, last_reason or "parse_error"



# -----------------------------
# Stable, overlay-safe entrypoints
# -----------------------------
# NOTE:
# Some overlays monkeypatch classify_lucky_pull_bytes() with their own asyncio.wait_for timeouts.
# LPG guard must be able to bypass those wrappers to avoid false classify_timeout.
# Use classify_lucky_pull_bytes_raw() from guards; it calls the core implementation directly.
async def classify_lucky_pull_bytes(image_bytes: bytes):
    return await _classify_lucky_pull_bytes_core(image_bytes)

# Guard/diagnostics should call this to bypass monkeypatch wrappers safely.
classify_lucky_pull_bytes_raw = _classify_lucky_pull_bytes_core

async def _classify_lucky_pull_bytes_suspicious_core(image_bytes: bytes, max_bytes: int | None = None):
    """
    Suspicious-gate variant for JPG/PNG/JPEG only.

    - Best-effort shrink payload to <= max_bytes (default 0.5MB) to reduce cost/latency.
    - Prefer GEMINI_API_KEY_B when the final payload <= max_bytes.
    - Does NOT change the behavior of classify_lucky_pull_bytes().

    Return format:
        (ok: bool, score: float, via: str, reason: str)
    """
    if max_bytes is None:
        max_bytes = int((_env("SUS_ATTACH_LPG_SUSPICIOUS_MAX_BYTES", "524288") or "524288").strip() or 524288)

    # Shrink first (best-effort), then compute key preference.
    shrunk, _mime = _shrink_for_gemini(image_bytes, max_bytes)
    keys = _env_keys_list()
    kb = (_env("GEMINI_API_KEY_B", "") or "").strip()

    if kb and shrunk and (len(shrunk) <= max_bytes):
        keys = [kb] + [k for k in keys if k != kb]

    if not keys:
        return False, 0.0, "none", "no_api_key"

    models = _env_models_list()
    if not models:
        models = ["meta-llama/llama-4-scout-17b-16e-instruct"]

    sys_prompt = _build_sys_prompt()
    img_bytes, mime = _prepare_inline_image(shrunk)

    per_timeout = _pick_timeout_sec()
    total_budget = _pick_total_budget_sec(
        per_timeout=per_timeout,
        n_models=len(models),
        n_keys=len(keys),
    )

    last_via, last_reason = "none", "no_attempt"
    deadline = time.time() + max(3.0, float(total_budget))

    for model in models:
        for key in keys:
            if time.time() > deadline:
                return False, 0.0, last_via, "timeout_total_budget"
            try:
                ok, score, via, reason = await _classify_one(
                    img_bytes=img_bytes,
                    mime=mime,
                    sys_prompt=sys_prompt,
                    model=model,
                    api_key=key,
                    timeout_sec=per_timeout,
                )
            except Exception as e:
                last_via, last_reason = "err", f"err:{type(e).__name__}"
                continue

            last_via, last_reason = via, reason
            score = float(score or 0.0)

            if bool(ok) and score >= 0.9:
                return True, score, via, reason or "early(ok)"

            rlow = (reason or "").lower()
            if ("timeout" in rlow) or ("error" in rlow) or ("parse_error" in rlow):
                continue

            return bool(ok), score, via, reason or "early(ok)"

    return False, 0.0, last_via, last_reason or "parse_error"
def _shrink_for_gemini(image_bytes: bytes, max_bytes: int) -> tuple[bytes, str]:
    """Try to ensure payload is <= max_bytes by converting to JPEG and lowering quality."""
    mime = _sniff_mime(image_bytes)
    if not image_bytes:
        return image_bytes, mime
    if len(image_bytes) <= max_bytes:
        return image_bytes, mime

    # If PIL is unavailable, we cannot reliably shrink; return as-is.
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return image_bytes, mime

    try:
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        q = int((_env("LPG_IMG_JPEG_QUALITY", "85") or "85").strip() or 85)
        q = max(40, min(92, q))
        best = None
        for _ in range(10):
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=q, optimize=True)
            b = buf.getvalue()
            if len(b) <= max_bytes:
                return b, "image/jpeg"
            best = b
            q = max(30, q - 8)
        # Fallback: return smallest we got (even if still > max_bytes)
        if best is not None:
            return best, "image/jpeg"
    except Exception:
        pass

    return image_bytes, mime



# Overlay-safe suspicious entrypoint
async def classify_lucky_pull_bytes_suspicious(image_bytes: bytes):
    return await _classify_lucky_pull_bytes_suspicious_core(image_bytes)

classify_lucky_pull_bytes_suspicious_raw = _classify_lucky_pull_bytes_suspicious_core