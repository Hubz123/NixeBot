from __future__ import annotations
import os, logging, asyncio
from nixe.helpers.env_reader import get as _cfg_get
from typing import Optional, List, Tuple, Any
import discord
from discord.ext import commands
from nixe.helpers.persona_loader import load_persona, pick_line
from nixe.helpers.persona_gate import should_run_persona
import nixe.helpers.gemini_bridge as gb
classify_lucky_pull_bytes = gb.classify_lucky_pull_bytes  # resolved via gemini_bridge (Groq-only for LPG)

log = logging.getLogger("nixe.cogs.a00_lpg_thread_bridge_guard")

# --- Groq rate-limit guard (avoid 429 storms on Render) ---
LPG_GROQ_MAX_CONCURRENCY = int(os.getenv('LPG_GROQ_MAX_CONCURRENCY', '1') or '1')
LPG_GROQ_MAX_RPM = int(os.getenv('LPG_GROQ_MAX_RPM', '8') or '8')
LPG_GROQ_429_COOLDOWN_SEC = int(os.getenv('LPG_GROQ_429_COOLDOWN_SEC', '900') or '900')
_LPG_GROQ_SEM = asyncio.Semaphore(max(1, LPG_GROQ_MAX_CONCURRENCY))
_LPG_GROQ_LOCK = asyncio.Lock()
_LPG_GROQ_LAST_CALL = 0.0
_LPG_GROQ_COOLDOWN_UNTIL = 0.0

async def _lpg_groq_gate():
    global _LPG_GROQ_LAST_CALL, _LPG_GROQ_COOLDOWN_UNTIL
    now = asyncio.get_running_loop().time()
    if _LPG_GROQ_COOLDOWN_UNTIL and now < _LPG_GROQ_COOLDOWN_UNTIL:
        await asyncio.sleep((_LPG_GROQ_COOLDOWN_UNTIL - now) + 0.05)
    rpm = max(1, LPG_GROQ_MAX_RPM)
    min_interval = 60.0 / float(rpm)
    async with _LPG_GROQ_LOCK:
        now = asyncio.get_running_loop().time()
        wait = (_LPG_GROQ_LAST_CALL + min_interval) - now
        if wait > 0:
            await asyncio.sleep(wait)
        _LPG_GROQ_LAST_CALL = asyncio.get_running_loop().time()

def _lpg_groq_mark_429():
    global _LPG_GROQ_COOLDOWN_UNTIL
    try:
        now = asyncio.get_running_loop().time()
    except RuntimeError:
        import time as _t; now = _t.monotonic()
    cd = max(1, int(LPG_GROQ_429_COOLDOWN_SEC))
    _LPG_GROQ_COOLDOWN_UNTIL = now + float(cd)



from nixe.helpers.once import once_sync as _once

def _sniff_fmt(image_bytes: bytes) -> str:
    """Return: jpeg|png|webp|gif|other by magic bytes."""
    if not image_bytes:
        return "other"
    b = image_bytes
    if b.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if b.startswith(b"GIF87a") or b.startswith(b"GIF89a"):
        return "gif"
    # WEBP: RIFF .... WEBP
    if len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    return "other"


def _compress_to_under(raw: bytes, max_bytes: int) -> bytes:
    """Best-effort compress/resize into JPEG under max_bytes (for LPG payload)."""
    try:
        if len(raw) <= max_bytes:
            return raw
        import io
        from PIL import Image  # type: ignore

        im = Image.open(io.BytesIO(raw))
        # Normalize to RGB to avoid alpha issues
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        # Downscale gently
        im.thumbnail((900, 900))

        # Try qualities from 80 down to 40
        for q in (80, 75, 70, 65, 60, 55, 50, 45, 40):
            buf = io.BytesIO()
            im.save(buf, format="JPEG", optimize=True, quality=q, subsampling=1)
            data = buf.getvalue()
            if len(data) <= max_bytes:
                return data

        # As a last resort, shrink more
        im.thumbnail((640, 640))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", optimize=True, quality=45, subsampling=1)
        data = buf.getvalue()
        return data if data else raw
    except Exception:
        return raw


# -- simple pHash + helpers (minimal) --
from io import BytesIO
import json
import re, base64
try:
    import aiohttp
except Exception:  # pragma: no cover
    aiohttp = None
from pathlib import Path
try:
    from PIL import Image
except Exception:
    Image = None
try:
    import numpy as np
except Exception:
    np = None


def _dct2(a):
    return np.real(np.fft.fft2(a)) if np is not None else None


def _phash64_bytes(img_bytes: bytes) -> Optional[int]:
    """Compute 64-bit perceptual hash from raw image bytes.
    Returns None on any failure but logs the reason for easier debugging.
    """
    if Image is None or np is None:
        log.warning(
            "[lpg-thread-bridge] pHash disabled (Image=%r, np=%r)",
            Image,
            np,
        )
        return None
    try:
        im = Image.open(BytesIO(img_bytes)).convert("L").resize((32, 32))
        arr = np.asarray(im, dtype=np.float32)
        d = _dct2(arr)[:8, :8]
        med = float(np.median(d[1:, 1:]))
        bits = (d[1:, 1:] > med).astype(np.uint8).flatten()
        val = 0
        for b in bits:
            val = (val << 1) | int(b)
        return int(val)
    except Exception as e:
        log.warning("[lpg-thread-bridge] _phash64_bytes failed: %r", e)
        return None



def _prepare_status_embed_image(image_bytes: bytes, max_bytes: int) -> tuple[bytes | None, str]:
    """Prepare image bytes for posting inside the LPG status/cache embed.

    Returns (bytes, filename). If bytes is None, caller should skip attaching.
    """
    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        return None, ""
    b = bytes(image_bytes)

    fmt = _sniff_fmt(b)
    # Prefer original extension when possible.
    if fmt == "png":
        filename = "lpg_lucky.png"
    else:
        filename = "lpg_lucky.jpg"

    # Already within limits → keep original.
    if len(b) <= int(max_bytes):
        return b, filename

    # Best-effort compress using PIL if available.
    if Image is None:
        return b, filename  # will be tried; caller should gracefully fallback on send failure

    try:
        im = Image.open(BytesIO(b))
        im = im.convert("RGB")
    except Exception:
        return b, filename

    # Try quality ladder first, then downscale.
    target = int(max_bytes)
    quality_steps = [85, 75, 65, 55, 45, 35, 30]
    scale = 1.0
    for _ in range(6):
        w, h = im.size
        if scale < 0.999:
            nw = max(1, int(w * scale))
            nh = max(1, int(h * scale))
            try:
                im2 = im.resize((nw, nh))
            except Exception:
                im2 = im
        else:
            im2 = im

        for q in quality_steps:
            out = BytesIO()
            try:
                im2.save(out, format="JPEG", quality=q, optimize=True)
                buf = out.getvalue()
                if len(buf) <= target:
                    return buf, "lpg_lucky.jpg"
            except Exception:
                continue

        # If still too big, downscale further and retry.
        scale *= 0.85

    # Give up: return the best attempt (smallest found) if any, else original.
    return b, filename


async def _post_status_embed(
    bot, *, title: str, fields: List[Tuple[str, str, bool]], color: int = 0x2B6CB0,
    image_bytes: bytes | None = None,
    footer_text: str | None = None,
):
    # Hardcoded permanent-memory thread
    tid = 1435924665615908965
    try:
        ch = bot.get_channel(tid) or await bot.fetch_channel(tid)
        if ch:
            # Guard: memory thread must only store LUCKY entries
            try:
                _is_lucky = True
                for _n, _v, _inl in (fields or []):
                    if str(_n).strip().lower() == 'result':
                        vv = str(_v or '').lower()
                        if ('not lucky' in vv) or ('❌' in str(_v or '')):
                            _is_lucky = False
                        break
                if not _is_lucky:
                    return
            except Exception:
                pass
            import discord

            emb = discord.Embed(title=title, color=color)
            for name, value, inline in fields:
                emb.add_field(name=name, value=value, inline=inline)
            if footer_text:
                try:
                    emb.set_footer(text=footer_text)
                except Exception:
                    pass

            # Attach preview image only when provided (LUCKY-only).
            if image_bytes:
                maxb = _env_int("LPG_EMBED_IMAGE_MAX_BYTES", 7500000)
                buf, fname = _prepare_status_embed_image(image_bytes, maxb)
                try:
                    if buf and fname:
                        fobj = discord.File(BytesIO(buf), filename=fname)
                        emb.set_image(url=f"attachment://{fname}")
                        await ch.send(embed=emb, file=fobj)
                    else:
                        await ch.send(embed=emb)
                except Exception:
                    # Fallback: do not fail status logging just because attachment failed.
                    try:
                        await ch.send(embed=emb)
                    except Exception:
                        pass
            else:
                await ch.send(embed=emb)
    except Exception:
        pass



def _cache_path(thread_id: int) -> Path:
    base = Path(os.getenv("LPG_CACHE_DIR", "data/phash_cache"))
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{thread_id}.json"


def _load_cache(thread_id: int) -> dict:
    p = _cache_path(thread_id)
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(thread_id: int, data: dict) -> None:
    p = _cache_path(thread_id)
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _parse_ids(val: str) -> list[int]:
    if not val:
        return []
    out = []
    for part in str(val).replace(";", ",").split(","):
        s = part.strip()
        if not s:
            continue
        try:
            out.append(int(s))
        except Exception:
            pass
    return out


def _cid(ch) -> int:
    try:
        return int(getattr(ch, "id", 0) or 0)
    except Exception:
        return 0


def _pid(ch) -> int:
    try:
        return int(getattr(ch, "parent_id", 0) or 0)
    except Exception:
        return 0


def _norm_tone(t: str) -> str:
    t = (t or "").lower().strip()
    if t in ("soft", "agro", "sharp"):
        return t
    if t in ("harsh", "hard"):
        return "agro"
    if t in ("gentle", "calm", "auto", ""):
        return "soft"
    return "soft"


def _env_bool(key: str, default: bool = False) -> bool:
    v = str(os.getenv(key, str(int(default))) or "").strip().lower()
    return v in ("1", "true", "yes", "y", "on")

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except Exception:
        return default

def _load_neg_text() -> list[str]:
    """
    Load negative-text cues for Lucky Pull guard.

    Sources (merged):
    - LPG_NEGATIVE_TEXT from environment (JSON list string or comma/semicolon/newline separated).
    - LPG_NEGATIVE_TEXT_FILE (optional): UTF-8 text file, one token per line, "#" comments allowed.

    Returns a lowercased, de-duplicated list.
    """
    out: list[str] = []

    raw = (os.getenv("LPG_NEGATIVE_TEXT") or "").strip()
    if raw:
        try:
            if raw.startswith("[") or raw.startswith("{"):
                j = json.loads(raw)
                if isinstance(j, list):
                    out.extend([str(x).strip() for x in j if str(x).strip()])
                elif isinstance(j, str):
                    raw2 = j
                    for part in str(raw2).replace(";", ",").replace("\n", ",").split(","):
                        s = part.strip()
                        if s:
                            out.append(s)
                else:
                    raw2 = raw
                    for part in str(raw2).replace(";", ",").replace("\n", ",").split(","):
                        s = part.strip()
                        if s:
                            out.append(s)
            else:
                raw2 = raw
                for part in str(raw2).replace(";", ",").replace("\n", ",").split(","):
                    s = part.strip()
                    if s:
                        out.append(s)
        except Exception:
            raw2 = raw
            for part in str(raw2).replace(";", ",").replace("\n", ",").split(","):
                s = part.strip()
                if s:
                    out.append(s)

    path = (os.getenv("LPG_NEGATIVE_TEXT_FILE") or "").strip()
    if path:
        log = logging.getLogger(__name__)
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#"):
                        continue
                    out.append(s)
            log.info(f"[lpg-negtext] loaded file={path} tokens={len(out)}")
        except FileNotFoundError:
            log.warning(f"[lpg-negtext] file not found: {path} (using inline list only)")
        except Exception as e:
            log.warning(f"[lpg-negtext] failed to read {path}: {e} (using inline list only)")

    if not out:
        return []

    # normalize + dedup (case-insensitive), preserve first-seen order
    seen: set[str] = set()
    norm: list[str] = []
    for t in out:
        s = str(t).strip().lower()
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        norm.append(s)
    return norm
def _detect_image_mime(image_bytes: bytes) -> str:
    # quick magic
    if image_bytes[:4] == b"\x89PNG":
        return "image/png"
    if image_bytes[:2] == b"\xff\xd8":
        return "image/jpeg"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes[:3] == b"GIF":
        return "image/gif"
    return "image/png"

def _maybe_convert_to_jpeg(image_bytes: bytes) -> tuple[bytes, str]:
    """
    Convert to JPEG for Gemini Vision if PIL is available and mime is not jpeg/png.
    Returns (bytes, mime).
    """
    mime = _detect_image_mime(image_bytes)
    if mime in ("image/jpeg", "image/png"):
        return image_bytes, mime
    if Image is None:
        return image_bytes, mime
    try:
        im = Image.open(BytesIO(image_bytes)).convert("RGB")
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=92)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return image_bytes, mime

async def _ocr_neg_text(image_bytes: bytes, timeout_ms: int = 3500) -> tuple[bool, str, str]:
    """
    OCR image via Groq Vision (OpenAI-compatible endpoint) using GEMINI_* keys.

    IMPORTANT POLICY:
    - For this project, Google Gemini is reserved for translate flows only.
    - LPG must NOT call Google Gemini REST endpoints.
    - The user's configuration provides Groq API keys for LPG; preferred vars are LPG_API_*, legacy GEMINI_* is still accepted.

    Returns: (ok, ocr_text, reason)
    """
    try:
        if aiohttp is None:
            return False, "", "aiohttp_missing"

        # Keys (Groq API keys reserved for LPG)
        # Preferred: LPG_API_*
        # Backward compatibility: GEMINI_* (legacy naming)
        keys_raw = (_cfg_get("LPG_API_KEYS", "") or "").strip()
        keys: list[str] = []
        if keys_raw:
            keys = [k.strip() for k in keys_raw.split(",") if k.strip()]
        if not keys:
            for kn in ("LPG_API_KEY", "LPG_API_KEY_B", "LPG_BACKUP_API_KEY"):
                kv = (_cfg_get(kn, "") or "").strip()
                if kv:
                    keys.append(kv)

        # Legacy fallback
        if not keys:
            keys_raw2 = (_cfg_get("GEMINI_API_KEYS", "") or "").strip()
            if keys_raw2:
                keys = [k.strip() for k in keys_raw2.split(",") if k.strip()]
        if not keys:
            for kn in ("GEMINI_API_KEY", "GEMINI_API_KEY_B", "GEMINI_BACKUP_API_KEY"):
                kv = (_cfg_get(kn, "") or "").strip()
                if kv:
                    keys.append(kv)

        if not keys:
            return False, "", "no_key(LPG_API_* or legacy GEMINI_*)"

        # Model selection: prefer dedicated OCR model, else reuse GROQ_MODEL_VISION.
        model = (os.getenv("GROQ_MODEL_VISION_OCR", "") or "").strip()
        if not model:
            model = (os.getenv("GROQ_MODEL_VISION", "") or "").strip()
        if not model:
            cand = (os.getenv("GROQ_MODEL_VISION_CANDIDATES", "") or "").strip()
            if cand:
                model = [x.strip() for x in cand.split(",") if x.strip()][0]
        if not model:
            return False, "", "no_groq_vision_model"

        img_bytes, mime = _maybe_convert_to_jpeg(image_bytes)
        b64 = base64.b64encode(img_bytes).decode("utf-8")
        data_url = f"data:{mime};base64,{b64}"

        sys_prompt = (
            "You are an OCR engine. Extract all readable text from the image. "
            "Return ONLY compact JSON: {\"text\": \"...\"}. No commentary."
        )
        payload = {
            "model": model,
            "temperature": 0,
            "max_tokens": 900,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": "OCR this image. Output JSON only."},
                    ],
                },
            ],
        }

        timeout = aiohttp.ClientTimeout(total=max(1.0, float(timeout_ms) / 1000.0))
        last_err = "no_result"
        url = "https://api.groq.com/openai/v1/chat/completions"

        for key in keys:
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with _LPG_GROQ_SEM:
                        await _lpg_groq_gate()
                        async with session.post(
                            url,
                            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                            json=payload,
                        ) as resp:
                            if resp.status == 429:
                                _lpg_groq_mark_429()
                                last_err = 'http_429'
                                log.warning('[lpg] 429 rate-limited; cooldown=%ss', LPG_GROQ_429_COOLDOWN_SEC)
                                continue
                            if resp.status != 200:
                                last_err = f"http_{resp.status}"
                                continue
                            js = await resp.json()
            except asyncio.TimeoutError:
                # OCR timeout: fail open (no veto) but keep the return signature stable.
                return False, "", "timeout"

            except Exception as e:
                last_err = f"vision_failed:{e.__class__.__name__}"
                continue

            try:
                content = (js.get("choices") or [{}])[0].get("message", {}).get("content", "")
            except Exception:
                content = ""

            if not content:
                last_err = "no_result"
                continue

            # Prefer strict JSON, but salvage if model returns raw text.
            ocr_text = ""
            try:
                obj = json.loads(content)
                ocr_text = str(obj.get("text", "") or "").strip()
            except Exception:
                try:
                    mm = re.search(r"\{\s*\"text\"\s*:\s*\".*?\"\s*\}", content, flags=re.S)
                    if mm:
                        obj = json.loads(mm.group(0))
                        ocr_text = str(obj.get("text", "") or "").strip()
                    else:
                        ocr_text = str(content).strip()
                except Exception:
                    ocr_text = str(content).strip()

            if ocr_text:
                return True, ocr_text, "ok"

            last_err = "empty_text"

        return False, "", last_err
    except Exception as e:
        return False, "", "exc:" + type(e).__name__

class LPGThreadBridgeGuard(commands.Cog):
    """Lucky Pull guard (thread-aware) — fully env-aligned.
    - Only deletes when image is classified as Lucky (Gemini) unless LPG_REQUIRE_CLASSIFY=0.
    - Persona strictly follows LPG_PERSONA_* / PERSONA_* in runtime_env.json.
    - Wiring keys read-only; format preserved.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = os.getenv("LPG_BRIDGE_ENABLE", "1") == "1"
        self.strict = (
            os.getenv("LPG_STRICT_ON_GUARD")
            or os.getenv("LUCKYPULL_STRICT_ON_GUARD")
            or os.getenv("STRICT_ON_GUARD")
            or "1"
        ) == "1"
        self.redirect_id = int(
            os.getenv("LPG_REDIRECT_CHANNEL_ID")
            or os.getenv("LUCKYPULL_REDIRECT_CHANNEL_ID")
            or "0"
        )
        self.guard_ids = set(
            _parse_ids(
                os.getenv("LPG_GUARD_CHANNELS", "")
                or os.getenv("LUCKYPULL_GUARD_CHANNELS", "")
            )
        )
        # IMPORTANT: read from env_reader so runtime_env.json wiring is honored even before env-hybrid export.
        try:
            from nixe.helpers.env_reader import get as _get
            self.timeout = float(_get("LPG_TIMEOUT_SEC", _get("LUCKYPULL_TIMEOUT_SEC", "10")))
        except Exception:
            self.timeout = float(os.getenv("LPG_TIMEOUT_SEC", os.getenv("LUCKYPULL_TIMEOUT_SEC", "10")))

        # Classification timeout retry cap (seconds). If initial classify hits timeout, retry once with larger timeout.
        self.timeout_retry_cap = float(os.getenv("LPG_CLASSIFY_TIMEOUT_RETRY_CAP", "25") or "25")
        self.thr = float(
            os.getenv("LPG_GEMINI_THRESHOLD")
            or os.getenv("GEMINI_LUCKY_THRESHOLD", "0.85")
        )
        self.require_classify = (os.getenv("LPG_REQUIRE_CLASSIFY") or "1") == "1"
        try:
            self.max_bytes = int(os.getenv("LPG_MAX_BYTES") or 8_000_000)
        except Exception:
            self.max_bytes = 8_000_000

        # Persona
        self.persona_enable = (
            os.getenv("LPG_PERSONA_ENABLE") or os.getenv("PERSONA_ENABLE") or "1"
        ) == "1"
        self.persona_mode = (
            os.getenv("LPG_PERSONA_MODE") or os.getenv("PERSONA_MODE") or "yandere"
        )
        self.persona_tone = _norm_tone(
            os.getenv("LPG_PERSONA_TONE")
            or os.getenv("PERSONA_TONE")
            or os.getenv("PERSONA_GROUP")
            or "soft"
        )
        self.persona_reason = (
            os.getenv("LPG_PERSONA_REASON")
            or os.getenv("PERSONA_REASON")
            or "Tebaran Garam"
        )
        self.persona_context = (
            os.getenv("LPG_PERSONA_CONTEXT") or "lucky"
        ).strip().lower()
        try:
            self.persona_delete_after = float(
                os.getenv("LPG_PERSONA_DELETE_AFTER")
                or os.getenv("PERSONA_DELETE_AFTER")
                or "12"
            )
        except Exception:
            self.persona_delete_after = 12.0
        # Persona scoping from runtime_env
        self.persona_only_for = [
            s.strip().lower()
            for s in str(os.getenv("LPG_PERSONA_ONLY_FOR", "")).split(",")
            if s.strip()
        ]
        self.persona_allowed_providers = [
            s.strip().lower()
            for s in str(os.getenv("LPG_PERSONA_ALLOWED_PROVIDERS", "")).split(",")
            if s.strip()
        ]
        # Negative text hard-veto (OCR) to prevent false positives on reward/selection UIs
        self.neg_hard_veto = _env_bool("LPG_NEGATIVE_HARD_VETO", True)
        self.neg_minlen = _env_int("LPG_NEGATIVE_HARD_VETO_MINLEN", 3)
        self.neg_tokens = [t.lower() for t in _load_neg_text() if t and len(t.strip()) >= self.neg_minlen]
        self.neg_ocr_timeout_ms = _env_int("LPG_NEGATIVE_OCR_TIMEOUT_MS", 12000)


        log.warning(
            "[lpg-thread-bridge] enabled=%s guards=%s redirect=%s thr=%.2f strict=%s timeout=%.1fs require_classify=%s | persona: enable=%s mode=%s tone=%s reason=%s only_for=%s allow_prov=%s",
            self.enabled,
            sorted(self.guard_ids),
            self.redirect_id,
            self.thr,
            self.strict,
            self.timeout,
            self.require_classify,
            self.persona_enable,
            self.persona_mode,
            self.persona_tone,
            self.persona_reason,
            self.persona_only_for,
            self.persona_allowed_providers,
        )

    def _in_guard(self, ch) -> bool:
        return (_cid(ch) in self.guard_ids) or (
            _pid(ch) in self.guard_ids and _pid(ch) != 0
        )

    def _is_image(self, att: discord.Attachment) -> bool:
        ct = (getattr(att, "content_type", None) or "").lower()
        if ct.startswith("image/"):
            return True
        name = (getattr(att, "filename", "") or "").lower()
        return name.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"))

    async def _negtext_veto(self, image_bytes: bytes) -> Tuple[bool, str]:
        """
        Run OCR and hard-veto lucky classification if any negative token is found.
        Returns (vetoed, reason).
        """
        if not self.neg_hard_veto or not self.neg_tokens:
            return False, "veto_disabled"
        try:

            _ret = await _ocr_neg_text(image_bytes, timeout_ms=self.neg_ocr_timeout_ms)

            if (not isinstance(_ret, tuple)) or (len(_ret) != 3):

                ok, ocr_text, r = False, "", "invalid_return(" + type(_ret).__name__ + ")"

            else:

                ok, ocr_text, r = _ret

        except Exception as e:

            ok, ocr_text, r = False, "", "ocr_exc(" + type(e).__name__ + ")"
        if not ok or not ocr_text:
            return False, f"ocr_skip({r})"
        low = ocr_text.lower()
        # Extra guard: roster/collection grids often show many "Lv. 90" labels.
        # If OCR detects multiple Lv.<num> occurrences, treat as NOT-LUCKY (prevents roster false positives).
        try:
            lv_hits = len(re.findall(r"\blv\.?\s*\d{1,3}\b", low))
            if lv_hits >= 3:
                return True, f"negtext_veto(ocr:lv_grid:{lv_hits})"
        except Exception:
            pass

        # Tier-list / ranking grids: common false positives (T1/T2/S-tier, "tier list", "ranking")
        try:
            tier_hits = len(re.findall(r"\bt\s*[0-6]\b", low))
            has_tier_words = any(w in low for w in ("tier list", "tierlist", "ranking", "s-tier", "a-tier", "b-tier"))
            # Require multiple signals to reduce accidental matches.
            if (tier_hits >= 2) or (has_tier_words and (tier_hits >= 1 or "\n" in low)):
                return True, f"negtext_veto(ocr:tierlist:t_hits={tier_hits})"
        except Exception:
            pass

        for tok in self.neg_tokens:
            if tok and tok in low:
                return True, f"negtext_veto(ocr:{tok})"
        return False, "no_neg_match"


    def _temp_env(self, mapping: dict[str, str]):
        """Temporarily set os.environ for the duration of a classify call.
        This does NOT mutate runtime_env.json; it only adjusts the current process env,
        and values are restored immediately after the call.
        """
        class _EnvCtx:
            def __init__(self, mp: dict[str, str]):
                self.mp = mp
                self.prev: dict[str, str | None] = {}
            def __enter__(self):
                for k, v in self.mp.items():
                    self.prev[k] = os.environ.get(k)
                    os.environ[k] = str(v)
                return self
            def __exit__(self, exc_type, exc, tb):
                for k, prev in self.prev.items():
                    if prev is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = prev
                return False
        return _EnvCtx(mapping)

    def _get_timeout_sec(self) -> float:
        """Resolve the active LPG timeout from runtime/env, with sane fallbacks."""
        try:
            from nixe.helpers.env_reader import get as _get  # type: ignore
            v = _get("LPG_TIMEOUT_SEC", _get("LUCKYPULL_TIMEOUT_SEC", self.timeout))
        except Exception:
            v = os.getenv("LPG_TIMEOUT_SEC", os.getenv("LUCKYPULL_TIMEOUT_SEC", str(self.timeout)))
        try:
            t = float(v or 0.0)
        except Exception:
            t = float(self._get_timeout_sec() or 0.0)
        if t <= 0:
            t = 10.0
        return t

    def _sync_groq_budget_env(self, total_timeout_sec: float):
        """Align gemini_bridge internal LPG budgets with guard timeout to prevent premature timeouts."""
        try:
            t = float(total_timeout_sec or 0.0)
        except Exception:
            t = 0.0
        if t <= 0:
            return {}
        # Keep a small safety margin so asyncio.wait_for does not kill mid-parse.
        total_budget = max(3.0, t - 0.5)
        # Per-attempt timeout should be smaller than total budget.
        per_attempt = max(2.8, min(total_budget - 0.3, total_budget - 1.0))  # ~= total_budget-1.0
        return {
            "LUCKYPULL_GROQ_TOTAL_TIMEOUT_SEC": f"{total_budget:.3f}",
            "LUCKYPULL_GROQ_TIMEOUT": f"{per_attempt:.3f}",
        }
    async def _classify(self, message: discord.Message, *, image_bytes: Optional[bytes] = None) -> tuple[bool, float, str, str]:
        """Classify image with resilient Gemini + BURST fallback.
        Returns: (lucky_ok, score, provider, reason)
        """
        if not classify_lucky_pull_bytes:
            return (False, 0.0, "none", "classifier_missing")

        data: Optional[bytes] = None
        try:
            if isinstance(image_bytes, (bytes, bytearray)) and image_bytes:
                data = bytes(image_bytes)
            else:
                imgs = [a for a in (message.attachments or []) if self._is_image(a)]
                if not imgs:
                    return (False, 0.0, "none", "no_image")
                data = await imgs[0].read()
            if not data:
                return (False, 0.0, "none", "empty_bytes")
            if len(data) > self.max_bytes:
                data = await asyncio.to_thread(_compress_to_under, data, int(self.max_bytes))

            # Hard negative-text veto via OCR (prevents Epiphany/reward selection false positives)
            vetoed, vreason = await self._negtext_veto(data)
            if vetoed:
                log.info("[lpg-thread-bridge] NEG_VETO lucky=False reason=%s", vreason)
                return (False, 0.0, "negtext_veto", vreason)

            # Primary path: gemini_bridge (may be monkeypatched by overlay).
            # Fetch from module at call-time to respect overlays.
            import nixe.helpers.gemini_bridge as _gb
            try:

                with self._temp_env(self._sync_groq_budget_env(float(self._get_timeout_sec() or 0.0))):
                    res = await asyncio.wait_for(getattr(_gb,'classify_lucky_pull_bytes_raw', _gb.classify_lucky_pull_bytes)(data), timeout=self._get_timeout_sec())

            except asyncio.TimeoutError:

                # Retry once with a larger timeout to avoid transient latency causing false negatives.

                _t2 = max(float(self._get_timeout_sec() or 0.0) * 2.0, float(self._get_timeout_sec() or 0.0) + 2.0)

                _t2 = min(_t2, float(self.timeout_retry_cap or 25.0))

                with self._temp_env(self._sync_groq_budget_env(float(_t2 or 0.0))):
                    res = await asyncio.wait_for(getattr(_gb,'classify_lucky_pull_bytes_raw', _gb.classify_lucky_pull_bytes)(data), timeout=float(_t2 or self._get_timeout_sec()))

            ok: bool = False
            score: float = 0.0
            provider: str = "unknown"
            reason: str = ""

            if isinstance(res, tuple) and len(res) >= 4:
                ok, score, provider, reason = (
                    res[0],
                    float(res[1] or 0.0),
                    str(res[2]),
                    str(res[3]),
                )
            elif isinstance(res, dict):
                ok = bool(res.get("ok", False))
                score = float(res.get("score", 0.0))
                provider = str(res.get("provider", "unknown"))
                reason = str(res.get("reason", ""))

            provider = provider or "unknown"
            reason = reason or ""
            score = float(score or 0.0)

            # Enforce provider re-check: do not trust cache hits for deletion decisions.
            if str(provider or "").startswith("cache:"):
                return (False, 0.0, str(provider), "cache_disallowed")

            verdict_ok = bool(ok and score >= self.thr)
            return (verdict_ok, score, provider, reason or "classified")

        except asyncio.TimeoutError:
            # Burst/lastchance disabled by policy (Groq-only for LPG)
            return (False, 0.0, "timeout", "classify_timeout")

        except Exception as e:
            log.warning("[lpg-thread-bridge] classify error: %r", e)
            return (False, 0.0, "error", "classify_exception")

    async def _delete_redirect_persona(
        self,
        message: discord.Message,
        lucky: bool,
        score: float,
        provider: str,
        reason: str,
        provider_hint: Optional[str] = None,
    ):
        # Delete
        try:
            await message.delete()
            log.info(
                "[lpg-thread-bridge] message deleted | user=%s ch=%s",
                getattr(message.author, "id", None),
                _cid(message.channel),
            )
        except Exception as e:
            log.debug("[lpg-thread-bridge] delete failed: %r", e)
        # Redirect mention
        mention = None
        try:
            if self.redirect_id:
                ch = message.guild.get_channel(self.redirect_id) or await self.bot.fetch_channel(  # type: ignore[arg-type]
                    self.redirect_id
                )
                mention = ch.mention if ch else f"<#{self.redirect_id}>"
        except Exception as e:
            log.debug("[lpg-thread-bridge] redirect resolve failed: %r", e)
        # If NOT lucky: strict cleanup only (no persona/notice spam)
        if not bool(lucky):
            return

        # Persona
        text = None
        persona_ok = False
        try:
            ok_run, _pm = should_run_persona(
                {
                    "ok": bool(lucky),
                    "score": float(score or 0.0),
                    "kind": self.persona_context,
                    "provider": provider,
                    "reason": reason,
                }
            )
            if self.persona_enable and ok_run:
                if self.persona_only_for and self.persona_context not in self.persona_only_for:
                    raise RuntimeError("persona_context_not_allowed")
                if self.persona_allowed_providers:
                    prov = (provider_hint or "").lower()
                    if not any(prov.startswith(prefix) for prefix in self.persona_allowed_providers):
                        raise RuntimeError("persona_provider_not_allowed")
                pmode, pdata, ppath = load_persona()
                if not pdata:
                    raise RuntimeError("persona_data_empty")
                use_mode = self.persona_mode or pmode or "yandere"
                text = pick_line(
                    pdata,
                    mode=use_mode,
                    tone=self.persona_tone,
                    user=getattr(message.author, "mention", "@user"),
                    channel=mention or "",
                    reason=self.persona_reason,
                )
                persona_ok = True
        except Exception as e:
            log.debug("[lpg-thread-bridge] persona load/guard failed: %r", e)

        if not text:
            base = f"{getattr(message.author, 'mention', '@user')}, *Lucky Pull* terdeteksi."
            text = base + (f" Ke {mention} ya." if mention else "")

        try:
            await message.channel.send(
                text, delete_after=self.persona_delete_after if persona_ok else 10
            )
            if mention:
                log.info("[lpg-thread-bridge] redirect -> %s", mention)
            log.info(
                "[lpg-thread-bridge] persona notice sent (gate=%s)",
                "ok" if persona_ok else "skip",
            )
        except Exception as e:
            log.debug("[lpg-thread-bridge] notice send failed: %r", e)

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if not self.enabled:
            return
        if not message or (
            getattr(message, "author", None) and message.author.bot
        ):
            return
        ch = getattr(message, "channel", None)
        if not ch:
            return
        cid = _cid(ch)
        pid = _pid(ch)
        if not self._in_guard(ch):
            return
        # Must have image
        if not any(self._is_image(a) for a in (message.attachments or [])):
            return

        # Prepare raw bytes & pHash for logging/caching (local only, robust for discord.py slots)
        raw_bytes = None
        ph_val: Optional[int] = None
        # Select a true JPG/PNG/JPEG attachment for LPG.
        # - If the filename/content-type lies (e.g. .png but actually WEBP), we skip LPG for it.
        # - Payload sent to classifier is best-effort compressed under ~0.5MB.
        LPG_MAX_SEND = _env_int("LPG_MAX_SEND_BYTES", 512000)  # <= 0.5MB
        LPG_MAX_FETCH = _env_int("LPG_MAX_FETCH_BYTES", 8000000)
        LPG_SEEN_TTL = _env_int("LPG_SEEN_TTL_SEC", 600)

        # Hard de-dupe per Discord message id (prevents double-processing on reconnect/cog reload).
        try:
            mid = int(getattr(message, "id", 0) or 0)
            if mid and (not _once(f"lpg:msg:{mid}", ttl=LPG_SEEN_TTL)):
                return
        except Exception:
            pass

        selected = None
        try:
            for a in (message.attachments or []):
                if not self._is_image(a):
                    continue
                ct = (getattr(a, "content_type", None) or "").lower()
                fn = (getattr(a, "filename", "") or "").lower()
                # Only JPG/PNG/JPEG go through LPG (per policy)
                if not (ct in ("image/jpeg", "image/jpg", "image/png") or fn.endswith((".jpg", ".jpeg", ".png"))):
                    continue
                url = (getattr(a, "url", "") or "")
                if url and not _once(f"lpg:att:{url}", ttl=LPG_SEEN_TTL):
                    continue
                size = int(getattr(a, "size", 0) or 0)
                if size and size > LPG_MAX_FETCH:
                    continue
                selected = a
                break

            if selected is not None:
                raw_bytes = await selected.read()
                fmt = _sniff_fmt(raw_bytes if isinstance(raw_bytes, (bytes, bytearray)) else b"")
                # If it's actually WEBP, do not send to LPG (handled by phishing WEBP pipeline)
                if fmt == "webp" or fmt not in ("jpeg", "png"):
                    raw_bytes = None
                elif isinstance(raw_bytes, (bytes, bytearray)) and len(raw_bytes) > LPG_MAX_SEND:
                    raw_bytes = await asyncio.to_thread(_compress_to_under, bytes(raw_bytes), LPG_MAX_SEND)
        except Exception as e:
            log.debug("[lpg-thread-bridge] failed to read bytes for LPG: %r", e)
            raw_bytes = None

        if isinstance(raw_bytes, (bytes, bytearray)):
            try:
                ph_val = _phash64_bytes(raw_bytes)
            except Exception as e:
                log.warning(
                    "[lpg-thread-bridge] _phash64_bytes failed in on_message: %r",
                    e,
                )
                ph_val = None

        # Best-effort stash into message for any consumers that support dynamic attrs
        d = getattr(message, "__dict__", None)
        if isinstance(d, dict):
            d["_nixe_imgbytes"] = raw_bytes
            d["_nixe_phash"] = ph_val

        # Require classification by default
        lucky: bool = False
        score: float = 0.0
        provider: str = "none"
        reason: str = "skip(require_classify=0)"
        provider_hint: Optional[str] = None

        if self.require_classify:
            if not isinstance(raw_bytes, (bytes, bytearray)) or not raw_bytes:
                # Still enforce strict guard cleanup even when we cannot read bytes.
                lucky, score, provider, reason = (False, 0.0, "none", "empty_bytes")
            else:
                lucky, score, provider, reason = await self._classify(message, image_bytes=bytes(raw_bytes))
            log.info(
                "[lpg-thread-bridge] classify: lucky=%s score=%.3f via=%s reason=%s",
                lucky,
                score,
                provider,
                reason,
            )
            provider_hint = (provider or "").lower()
            # Extra false-positive guard: roster/collection grids show many "Lv. <num>" labels.
            # Run OCR only when classifier says LUCKY and neg-token list is empty (avoids extra OCR calls).
            if lucky and (not self.neg_tokens) and isinstance(raw_bytes, (bytes, bytearray)) and raw_bytes:
                try:
                    try:

                        _ret2 = await _ocr_neg_text(bytes(raw_bytes), timeout_ms=self.neg_ocr_timeout_ms)

                        if (not isinstance(_ret2, tuple)) or (len(_ret2) != 3):

                            ok2, ocr_text2, r2 = False, "", "invalid_return(" + type(_ret2).__name__ + ")"

                        else:

                            ok2, ocr_text2, r2 = _ret2

                    except Exception as e:

                        ok2, ocr_text2, r2 = False, "", "ocr_exc(" + type(e).__name__ + ")"
                    if ok2 and ocr_text2:
                        low2 = ocr_text2.lower()
                        lv_hits2 = len(re.findall(r"\blv\.?\s*\d{1,3}\b", low2))
                        if lv_hits2 >= 3:
                            lucky = False
                            score = 0.0
                            provider = "negtext_veto"
                            reason = f"ocr:lv_grid:{lv_hits2}"
                except Exception:
                    pass


                # Permanent thread memory policy:
        # - Only LUCKY entries are posted into the memory thread (ID hardcoded).
        # - NOT LUCKY is intentionally not posted (prevents log spam; supports delete=unlearn semantics).

        # Never force lucky based on fallback markers. If provider says NOT_LUCKY, stop.
        if not lucky:
            return

# Post LUCKY classification result to permanent memory thread (pHash + image + footer sha1/ahash)
        try:
            # Compute pHash for status embed.
            ph = ph_val
            if not isinstance(ph, int) and isinstance(raw_bytes, (bytes, bytearray)):
                try:
                    ph = _phash64_bytes(raw_bytes)
                except Exception as e:
                    log.warning("[lpg-thread-bridge] _phash64_bytes failed in status embed: %r", e)
                    ph = None

            ph_str = f"{int(ph):016X}" if isinstance(ph, int) else "-"
            fields = [
                ("Result", "✅ LUCKY", True),
                ("Score", f"{float(score or 0.0):.3f}", True),
                ("Provider", provider or "-", True),
                ("Reason", reason or "-", False),
                ("Message ID", str(message.id), True),
                ("Channel", f"<#{getattr(message.channel, 'id', 0)}>", True),
                ("pHash", ph_str, True),
            ]

            footer_text = None
            if isinstance(raw_bytes, (bytes, bytearray)):
                try:
                    from nixe.helpers import lpg_cache_memory as _cache
                    ent = _cache.put(bytes(raw_bytes), True, float(score or 0.0), str(provider or "-"), str(reason or "-"))
                    sha1 = str(ent.get("sha1") or "")
                    ah = str(ent.get("ahash") or "")
                    if sha1 and ah:
                        footer_text = f"lpgmem sha1={sha1} ahash={ah}"
                except Exception:
                    footer_text = None

            await _post_status_embed(
                self.bot,
                title="Lucky Pull Classification",
                fields=fields,
                color=0x22C55E,
                image_bytes=(bytes(raw_bytes) if isinstance(raw_bytes, (bytes, bytearray)) else None),
                footer_text=footer_text,
            )
        except Exception:
            pass

        log.info(
            "[lpg-thread-bridge] STRICT_ON_GUARD delete | ch=%s parent=%s type=%s",
            cid,
            pid,
            type(ch).__name__ if ch else None,
        )
        await self._delete_redirect_persona(
            message, lucky, score, provider, reason, provider_hint=provider_hint
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(LPGThreadBridgeGuard(bot))
