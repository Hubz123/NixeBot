from __future__ import annotations
import os, logging, asyncio
from typing import Optional, List, Tuple, Any
import discord
from discord.ext import commands
from nixe.helpers.persona_loader import load_persona, pick_line
from nixe.helpers.persona_gate import should_run_persona
try:
    from nixe.helpers.gemini_bridge import classify_lucky_pull_bytes
except Exception:
    classify_lucky_pull_bytes = None

log = logging.getLogger("nixe.cogs.a00_lpg_thread_bridge_guard")


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

    # Built-in defaults (used when env/file not provided)
    if not out:
        out = [
            "owned",
            "inventory",
            "loadout",
            "equipment",
            "equip",
            "chapter",
            "episode",
            "story",
            "mission",
            "quest",
            "reward",
            "rewards",
            "claim",
            "claimed",
            "progress",
            "progression",
            "event",
            "shop",
            "exchange",
            "stage",
            "selection",
            "select",
            "continue",
            "continue?",
            "login",
            "daily",
            "weekly",
            "ends in",
            "remaining",
            "days",
            "hours",
            "left",
            "期間",
            "終了まで",
            "終了",
            "あと",
            "日",
            "所持",
            "所有",
            "報酬",
            "任務",
            "章",
            "物語",
            "ストーリー",
            "進行",
            "挑戦",
            "選択",
            "装備",
            "編成",
            "交換",
            "ショップ",
            "ログイン",
            "受け取",
            "受取",
            "獲得",
            "クリア",
            "報酬を受け取",
        ]


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

def _load_pos_text() -> list[str]:
    """Load positive-text cues for true gacha result screens."""
    out: list[str] = []

    raw = (os.getenv("LPG_POSITIVE_TEXT") or "").strip()
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

    path = (os.getenv("LPG_POSITIVE_TEXT_FILE") or "").strip()
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#"):
                        continue
                    out.append(s)
            log.info(f"[lpg-postext] loaded file={path} tokens={len(out)}")
        except FileNotFoundError:
            log.warning(f"[lpg-postext] file not found: {path} (using inline list only)")
        except Exception as e:
            log.warning(f"[lpg-postext] failed to read {path}: {e} (using inline list only)")

    if not out:
        out = [
        "draw",
        "pull",
        "gacha",
        "result",
        "results",
        "x10",
        "10x",
        "ten pull",
        "confirm",
        "skip",
        "again",
        "recruit",
        "summon",
        "ガチャ",
        "結果",
        "10連",
        "十連",
        "引く",
        "確定",
        "スキップ",
        "もう一度",
        "引き直し",
        "召喚",
        "募集",
        "抽選",
        "出現",
        "獲得",
        ]

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
    OCR image with Gemini Vision, returning (ok, ocr_text, reason).
    Uses same key pool as LPG classify (GEMINI_API_KEYS or legacy).
    """
    if aiohttp is None:
        return False, "", "aiohttp_missing"

    keys_raw = (os.getenv("GEMINI_API_KEYS", "") or "").strip()

    keys: list[str] = []
    if keys_raw:
        keys = [k.strip() for k in keys_raw.split(",") if k.strip()]
    if not keys:
        for kn in ("GEMINI_API_KEY", "GEMINI_API_KEY_B", "GEMINI_BACKUP_API_KEY"):
            kv = (os.getenv(kn, "") or "").strip()
            if kv:
                keys.append(kv)
    if not keys:
        return False, "", "no_gemini_key"

    model = (os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite") or "").strip()
    if not model:
        model = "gemini-2.5-flash-lite"

    img_bytes, mime = _maybe_convert_to_jpeg(image_bytes)
    # OCR preprocessing: improve faint UI text and include a top-left crop (banner/timer text).
    sys_prompt = (
        "You are an OCR engine. Extract all readable text from the provided image(s). "
        "Merge results. Return ONLY compact JSON: {\"text\": \"...\"}."
    )

    parts = [{"text": sys_prompt}]
    try:
        if Image is not None:
            from PIL import ImageOps, ImageFilter
            im = Image.open(BytesIO(img_bytes)).convert("RGB")
            mx = max(im.size[0], im.size[1])
            if mx and mx < 1100:
                scale = 1100.0 / float(mx)
                im = im.resize((max(1, int(im.size[0] * scale)), max(1, int(im.size[1] * scale))))
            gimg = ImageOps.grayscale(im)
            gimg = ImageOps.autocontrast(gimg)
            gimg = gimg.filter(ImageFilter.SHARPEN)
            im2 = gimg.convert("RGB")

            buf = BytesIO()
            im2.save(buf, format="JPEG", quality=90)
            main_bytes = buf.getvalue()

            w, h = im2.size
            crop = im2.crop((0, 0, int(w * 0.52), int(h * 0.42)))
            buf2 = BytesIO()
            crop.save(buf2, format="JPEG", quality=90)
            crop_bytes = buf2.getvalue()

            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(main_bytes).decode("utf-8")}})
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(crop_bytes).decode("utf-8")}})
        else:
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(img_bytes).decode("utf-8")}})
    except Exception:
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(img_bytes).decode("utf-8")}})

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": parts,
            }
        ],
        "generationConfig": {
            "temperature": 0.0,
            "topP": 0.1,
            "topK": 1,
            "maxOutputTokens": 384,
            "responseMimeType": "application/json",
        },
    }

    last_err = ""
    tsec = max(1.5, float(timeout_ms) / 1000.0)
    timeout = aiohttp.ClientTimeout(total=tsec)

    for key in keys:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        last_err = f"http_{resp.status}"
                        continue
                    try:
                        j = json.loads(txt)
                        cand = (j.get("candidates") or [{}])[0]
                        parts = cand.get("content", {}).get("parts") or []
                        out = "".join([p.get("text", "") for p in parts]).strip()
                        if not out:
                            last_err = "empty_output"
                            continue
                        # out may be json or raw text
                        if out.startswith("{"):
                            try:
                                oj = json.loads(out)
                                ocr_text = str(oj.get("text", "") or "")
                            except Exception:
                                ocr_text = out
                        else:
                            ocr_text = out
                        ocr_text = (ocr_text or "").strip()
                        if not ocr_text:
                            last_err = "empty_ocr"
                            continue
                        return True, ocr_text, "ok"
                    except Exception:
                        # salvage braces
                        m = re.search(r"\{.*\}", txt, flags=re.S)
                        if m:
                            try:
                                oj = json.loads(m.group(0))
                                ocr_text = str(oj.get("text", "") or "").strip()
                                if ocr_text:
                                    return True, ocr_text, "salvaged"
                            except Exception:
                                pass
                        last_err = "parse_error"
                        continue
        except Exception as e:
            last_err = f"vision_failed:{e.__class__.__name__}"
            continue

    return False, "", last_err or "vision_failed"

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
        self.timeout = float(
            os.getenv("LPG_TIMEOUT_SEC", os.getenv("LUCKYPULL_TIMEOUT_SEC", "10"))
        )
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
        self.neg_minlen = _env_int("LPG_NEGATIVE_HARD_VETO_MINLEN", 2)
        self.neg_tokens = [t.lower() for t in _load_neg_text() if t and len(t.strip()) >= self.neg_minlen]
        self.neg_ocr_timeout_ms = _env_int("LPG_NEGATIVE_OCR_TIMEOUT_MS", 3500)

        # Positive text confirm (OCR) for LUCKY results: if OCR succeeds and is non-empty,
        # require at least one positive cue; otherwise treat as NOT LUCKY. (Reduces grid UI false positives.)
        self.pos_confirm = _env_bool("LPG_POSITIVE_OCR_CONFIRM", True)
        self.pos_tokens = [t.lower() for t in _load_pos_text() if t and len(t.strip()) >= 2]
        self.pos_ocr_minlen = _env_int("LPG_POSITIVE_OCR_MINLEN", 12)


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
        """OCR-based veto/confirm for candidate LUCKY results.

        Policy:
        - If OCR succeeds and any NEG token is present => veto.
        - If OCR succeeds, text length >= pos_ocr_minlen, and no POS token is present (when pos_confirm=1) => veto.
        - If OCR fails/empty => do not veto (avoid false negatives).
        """
        if (not self.neg_tokens) and (not (self.pos_confirm and self.pos_tokens)):
            return False, "ocr_disabled"

        ok, ocr_text, r = await _ocr_neg_text(image_bytes, timeout_ms=self.neg_ocr_timeout_ms)
        if not ok or not ocr_text:
            return False, f"ocr_skip({r})"

        low = " ".join(str(ocr_text).split()).lower()

        # Negative tokens
        if self.neg_tokens and self.neg_hard_veto:
            for tok in self.neg_tokens:
                if tok and tok in low:
                    return True, f"ocr_neg({tok})"

        # Positive confirm for true gacha results
        if self.pos_confirm and self.pos_tokens and len(low) >= int(self.pos_ocr_minlen or 0):
            hit = False
            for tok in self.pos_tokens:
                if tok and tok in low:
                    hit = True
                    break
            if not hit:
                return True, "ocr_no_positive"

        return False, "ocr_ok"


    async def _classify(self, message: discord.Message) -> tuple[bool, float, str, str]:
        """Classify image with resilient Gemini + BURST fallback.
        Returns: (lucky_ok, score, provider, reason)
        """
        if not classify_lucky_pull_bytes:
            return (False, 0.0, "none", "classifier_missing")

        imgs = [a for a in (message.attachments or []) if self._is_image(a)]
        if not imgs:
            return (False, 0.0, "none", "no_image")

        data: Optional[bytes] = None
        try:
            data = await imgs[0].read()
            if not data:
                return (False, 0.0, "none", "empty_bytes")
            if len(data) > self.max_bytes:
                data = data[: self.max_bytes]

            # Pre-check denylist (delete=banish): skip expensive classify if known false-positive
            sha1 = ""
            ah = ""
            try:
                from nixe.helpers import lpg_cache_memory as _cache
                sha1, ah, _wh = _cache.fingerprint_bytes(bytes(data))
                from nixe.helpers import lpg_denylist as _deny
                denied, dwhy = _deny.is_denied(sha1, ah)
                if denied:
                    log.info("[lpg-thread-bridge] DENYLIST_HIT lucky=False reason=%s", dwhy)
                    return (False, 0.0, "denylist", dwhy)
            except Exception:
                pass

            # primary path: gemini_bridge (may be monkeypatched by overlay)
            res = await asyncio.wait_for(
                classify_lucky_pull_bytes(data), timeout=self.timeout
            )

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

            # If Gemini gave a parse / no_result style error, escalate to BURST once.
            bad_reason = False
            rlow = reason.lower()
            if ("parse_error" in rlow) or ("none:no_result" in rlow) or (
                "classify_exception" in rlow
            ):
                bad_reason = True
            if not rlow and provider.startswith("gemini:") and (not ok) and score <= 0.0:
                bad_reason = True

            if bad_reason and provider.startswith("gemini:") and data:
                try:
                    try:
                        from nixe.helpers.gemini_lpg_burst import (
                            classify_lucky_pull_bytes_burst as _burst,
                        )
                    except Exception:
                        try:
                            from nixe.helpers.gemini_lpg_burst import (
                                classify_lucky_pull_bytes as _burst,
                            )
                        except Exception:
                            _burst = None
                    if _burst is not None:
                        fb_ms = int(os.getenv("LPG_GUARD_LASTCHANCE_MS", "1200"))
                        os.environ["LPG_BURST_TIMEOUT_MS"] = str(fb_ms)
                        bok, bscore, bvia, breason = await _burst(data)
                        bscore = float(bscore or 0.0)
                        r2 = str(breason or "")
                        r2low = r2.lower()
                        if not (r2low.startswith("early(") or "early(ok)" in r2low or r2low.startswith("ok")):
                            r2 = f"lastchance({breason})"
                        verdict_ok = bool(bok and bscore >= self.thr)
                        if verdict_ok:
                            try:
                                vetoed, vreason = await self._negtext_veto(data)
                                if vetoed:
                                    log.info("[lpg-thread-bridge] OCR_VETO lucky=False reason=%s", vreason)
                                    return (False, float(bscore or 0.0), str(bvia or "gemini:burst"), f"ocr_veto({vreason})")
                            except Exception:
                                pass
                        return (
                            verdict_ok,
                            bscore,
                            str(bvia or "gemini:burst"),
                            r2,
                        )
                except Exception as e:
                    log.debug(
                        "[lpg-thread-bridge] lastchance burst on-parse-error failed: %r",
                        e,
                    )

            verdict_ok = bool(ok and score >= self.thr)
            if verdict_ok:
                try:
                    vetoed, vreason = await self._negtext_veto(data)
                    if vetoed:
                        log.info("[lpg-thread-bridge] OCR_VETO lucky=False reason=%s", vreason)
                        return (False, float(score or 0.0), str(provider or "gemini"), f"ocr_veto({vreason})")
                except Exception:
                    pass
            return (verdict_ok, score, provider, reason or "classified")

        except asyncio.TimeoutError:
            # Guard hard-timeout hit; do one last-chance BURST retry (short) to avoid false negatives
            if not data:
                return (False, 0.0, "timeout", "classify_timeout")
            try:
                try:
                    from nixe.helpers.gemini_lpg_burst import (
                        classify_lucky_pull_bytes_burst as _burst,
                    )
                except Exception:
                    try:
                        from nixe.helpers.gemini_lpg_burst import (
                            classify_lucky_pull_bytes as _burst,
                        )
                    except Exception:
                        _burst = None
                if _burst is not None:
                    fb_ms = int(os.getenv("LPG_GUARD_LASTCHANCE_MS", "1200"))
                    os.environ["LPG_BURST_TIMEOUT_MS"] = str(fb_ms)
                    ok, score, via, reason = await _burst(data)
                    score = float(score or 0.0)
                    verdict_ok = bool(ok and score >= self.thr)
                    r2 = str(reason or "")
                    r2low = r2.lower()
                    if not (r2low.startswith("early(") or "early(ok)" in r2low or r2low.startswith("ok")):
                        r2 = f"lastchance({reason})"
                    if verdict_ok:
                        try:
                            vetoed, vreason = await self._negtext_veto(data)
                            if vetoed:
                                log.info("[lpg-thread-bridge] OCR_VETO lucky=False reason=%s", vreason)
                                return (False, float(score or 0.0), str(via or "gemini:burst"), f"ocr_veto({vreason})")
                        except Exception:
                            pass
                    return (
                        verdict_ok,
                        score,
                        str(via or "gemini:burst"),
                        r2,
                    )
            except Exception:
                pass
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
                    raw_bytes = _compress_to_under(bytes(raw_bytes), LPG_MAX_SEND)
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
            lucky, score, provider, reason = await self._classify(message)
            log.info(
                "[lpg-thread-bridge] classify: lucky=%s score=%.3f via=%s reason=%s",
                lucky,
                score,
                provider,
                reason,
            )
            provider_hint = (provider or "").lower()

                # Permanent thread memory policy:
        # - Only LUCKY entries are posted into the memory thread (ID hardcoded).
        # - NOT LUCKY is intentionally not posted (prevents log spam; supports delete=unlearn semantics).

        # Assume-lucky on fallback timeouts if explicitly enabled
        if not lucky:
            try:
                assume = os.getenv("LPG_ASSUME_LUCKY_ON_FALLBACK", "0") == "1"
                if assume and isinstance(reason, str) and (
                    "lastchance(" in reason or "shield_fallback(" in reason
                ):
                    log.warning(
                        "[lpg-thread-bridge] assume_lucky_fallback active → forcing redirect (reason=%s)",
                        reason,
                    )
                    lucky = True
                else:
                    return
            except Exception:
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