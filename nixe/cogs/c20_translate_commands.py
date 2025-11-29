"""
c20_translate_commands.py

Translate cog (guild-only, add-only):
- Provides Message Context Menu "Translate (Nixe)" and optional /translate slash.
- Supports translating plain text, embeds (when message content is empty or only a URL),
  and images via Gemini Vision OCR + translation.
- Uses separate API keys (TRANSLATE_*) so it does NOT touch LPG Gemini or Phish Groq keys.

Secrets (.env only):
  TRANSLATE_GEMINI_API_KEY=...
  TRANSLATE_GROQ_API_KEY=...

Optional configs (runtime_env.json or env):
  TRANSLATE_ENABLE=1
  TRANSLATE_PROVIDER=gemini|groq   (default: gemini if key present else groq)
  TRANSLATE_TARGET_LANG=id        (default: id)
  TRANSLATE_GEMINI_MODEL=gemini-2.5-flash-lite
  TRANSLATE_GROQ_MODEL=llama-3.1-8b-instant
  TRANSLATE_IMAGE_MODEL=gemini-2.5-flash
  TRANSLATE_TIMEOUT_SEC=12
  TRANSLATE_MAX_CHARS=1800
  TRANSLATE_COOLDOWN_SEC=5
  TRANSLATE_EPHEMERAL=1
  TRANSLATE_CTX_NAME="Translate (Nixe)"
  TRANSLATE_SLASH_ENABLE=1
  TRANSLATE_GUILD_ID=<single guild id>
  TRANSLATE_GUILD_IDS=<comma separated ids>
  TRANSLATE_ALLOW_FALLBACK=1  (allow fallback to GEMINI_API_KEY / GEMINI_API_KEY_B)
  TRANSLATE_JA_DUAL_ENABLE=1   (if target_lang is JA, output formal+casual+romaji)
  TRANSLATE_JA_ROMAJI_ENABLE=1 (enable romaji field in JA dual mode)
  REVERSE_IMAGE_ENABLE=1       (enable Reverse image context menu)
  REVERSE_IMAGE_CTX_NAME="Reverse image (Nixe)"
  REVERSE_IMAGE_MAX_IMAGES=3
  REVERSE_IMAGE_EPHEMERAL=1
  REVERSE_IMAGE_COOLDOWN_SEC=5
"""

from __future__ import annotations

import os, json, logging, re, asyncio, base64
from typing import Optional, Tuple, List, Dict, Any
from urllib import parse as urllib_parse

import discord
import aiohttp
from nixe.translate import resolve_lang
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

# -------------------------
# small helpers
# -------------------------

def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)

def _as_bool(key: str, default: bool = False) -> bool:
    v = _env(key, "1" if default else "0").strip().lower()
    return v in ("1", "true", "yes", "y", "on")

def _as_float(key: str, default: float = 0.0) -> float:
    try:
        return float(_env(key, str(default)))
    except Exception:
        return default

def _clean_output(s: str) -> str:
    s = s.strip()
    # strip code fences if model adds them
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    return s.strip()

def _normalize_for_compare(s: str) -> str:
    s = re.sub(r"https?://\S+", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s

def _seems_untranslated(src: str, out: str, target_lang: str) -> bool:
    ns = _normalize_for_compare(src)
    no = _normalize_for_compare(out)
    if not ns or not no:
        return False
    if ns == no:
        return True
    # rough similarity based on char overlap
    common = sum(1 for a, b in zip(ns, no) if a == b)
    sim = common / max(len(ns), len(no))
    if sim > 0.90:
        return True
    # if target is latin-based but output is heavy CJK/Hangul, likely untranslated
    if target_lang.lower() in ("id", "en", "ms", "fr", "es", "de", "pt", "it", "vi", "tl"):
        nonlatin = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", out))
        if nonlatin > 8 and nonlatin / max(1, len(out)) > 0.20:
            return True
    return False


def _chunk_text(text: str, max_chars: int) -> List[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: List[str] = []
    buf = ""
    for p in text.split("\n\n"):
        cand = (buf + "\n\n" + p) if buf else p
        if len(cand) <= max_chars:
            buf = cand
            continue
        if buf:
            chunks.append(buf); buf = ""
        if len(p) > max_chars:
            for k in range(0, len(p), max_chars):
                chunks.append(p[k:k+max_chars])
        else:
            buf = p
    if buf:
        chunks.append(buf)
    return chunks

def _looks_like_only_urls(text: str) -> bool:
    if not text:
        return True
    t = re.sub(r"https?://\S+", " ", text, flags=re.I)
    t = re.sub(r"[\W_]+", " ", t)
    return len(t.strip()) == 0

def _extract_text_from_embeds(embeds: List[discord.Embed]) -> str:
    """Extract readable text from embeds, preferring description."""
    parts: List[str] = []
    for e in embeds or []:
        if e.description:
            parts.append(str(e.description))

        # fields as fallback
        for f in getattr(e, "fields", []) or []:
            if getattr(f, "value", None):
                parts.append(str(f.value))

        # title/author/footer only if still empty later
        if e.title:
            parts.append(str(e.title))
        try:
            if e.author and e.author.name:
                parts.append(str(e.author.name))
        except Exception:
            pass
        try:
            if e.footer and e.footer.text:
                parts.append(str(e.footer.text))
        except Exception:
            pass

    text = "\n".join(p.strip() for p in parts if p and p.strip())
    # light cleaning for common platform noise
    cleaned: List[str] = []
    for line in text.splitlines():
        ln = line.strip()
        if not ln:
            continue
        if re.fullmatch(r"https?://\S+", ln, flags=re.I):
            continue
        if ln.lower() in ("x", "twitter", "view on x", "open in x"):
            continue
        if re.fullmatch(r"@\w+", ln):
            continue
        cleaned.append(ln)
    return "\n".join(cleaned).strip()
# -------------------------
# Extra helpers for context-menu translate
# - forwarded/reply wrappers
# - embed-only images (link previews)
# -------------------------

def _is_message_effectively_empty(msg) -> bool:
    """Return True if message-like object has no usable text/embeds/attachments."""
    try:
        content = (getattr(msg, "content", "") or "").strip()
        if content:
            return False
        atts = getattr(msg, "attachments", None) or []
        if atts:
            return False
        embeds = getattr(msg, "embeds", None) or []
        if embeds:
            emb_text = _extract_text_from_embeds(list(embeds))
            if (emb_text or "").strip():
                return False
        return True
    except Exception:
        return True
def _pick_best_source_message(msg):
    """Select best message-like source for translation (unwrap reply/forward wrappers)."""
    if not _is_message_effectively_empty(msg):
        return msg

    debug = False
    try:
        debug = _as_bool("TRANSLATE_DEBUG_LOG", False)
    except Exception:
        debug = False

    # 1) Reply wrapper via reference.resolved
    try:
        ref = getattr(msg, "reference", None)
        resolved = getattr(ref, "resolved", None) if ref else None
        if resolved and not _is_message_effectively_empty(resolved):
            if debug:
                log.info("[translate] unwrap reference -> %s", type(resolved).__name__)
            return resolved
    except Exception:
        pass

    # 2) Forwarded posts via message_snapshots (discord.py 2.4+).
    try:
        snaps = getattr(msg, "message_snapshots", None) or []
        for snap in snaps:
            inner = getattr(snap, "message", None) or getattr(snap, "resolved", None) or snap
            if inner and not _is_message_effectively_empty(inner):
                if debug:
                    log.info("[translate] unwrap snapshot -> %s", type(inner).__name__)
                return inner
    except Exception:
        pass

    return msg
def _extract_image_urls_from_embeds(embeds: List[discord.Embed]) -> List[str]:
    urls: List[str] = []
    for e in embeds or []:
        try:
            img = getattr(e, "image", None)
            if img and getattr(img, "url", None):
                urls.append(str(img.url))
            th = getattr(e, "thumbnail", None)
            if th and getattr(th, "url", None):
                urls.append(str(th.url))
        except Exception:
            continue
    # de-dupe while preserving order
    out: List[str] = []
    seen = set()
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out

async def _fetch_image_bytes(url: str, max_bytes: int = 6_000_000) -> Optional[bytes]:
    if not url:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url) as resp:
                if resp.status != 200:
                    return None
                ct = (resp.headers.get("Content-Type") or "").lower()
                if not any(x in ct for x in ("image/", "octet-stream")):
                    # allow discord proxy images which sometimes use octet-stream
                    return None
                data = await resp.content.read(max_bytes + 1)
                if len(data) > max_bytes:
                    return None
                return data
    except Exception:
        return None

async def _find_any_image_bytes(msg) -> Optional[bytes]:
    """Find first image bytes from attachments or embeds. Works for snapshot-like objects."""
    # 1) attachments (scan all)
    try:
        for att in (getattr(msg, "attachments", None) or []):
            fn = (getattr(att, "filename", "") or "").lower()
            url = (getattr(att, "url", None) or getattr(att, "proxy_url", None) or "")
            ct = (getattr(att, "content_type", "") or "").lower()

            is_img = (
                any(fn.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"))
                or any(str(url).lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"))
                or ("image/" in ct)
            )
            if not is_img:
                continue

            # try read() if available
            try:
                read_fn = getattr(att, "read", None)
                if callable(read_fn):
                    b = await read_fn()
                    if b:
                        return b
            except Exception:
                pass

            # url fetch fallback (for snapshot attachments)
            if url:
                b = await _fetch_image_bytes(str(url))
                if b:
                    return b
    except Exception:
        pass

    # 2) embed images (link previews / bot embeds)
    try:
        embeds = list(getattr(msg, "embeds", None) or [])
        for u in _extract_image_urls_from_embeds(embeds):
            b = await _fetch_image_bytes(u)
            if b:
                return b
    except Exception:
        pass

    return None
def _translate_guild_ids() -> List[int]:
    # new format
    raw = _env("TRANSLATE_GUILD_IDS", "").strip()
    if raw:
        out: List[int] = []
        for tok in raw.split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                out.append(int(tok))
            except Exception:
                pass
        return out
    # legacy single id
    raw2 = _env("TRANSLATE_GUILD_ID", "").strip()
    if raw2:
        try:
            return [int(raw2)]
        except Exception:
            return []
    return []

def _pick_provider() -> str:
    """
    Provider selector for translate.

    For Nixe translate we hard-lock to Gemini. Groq is reserved exclusively
    for phishing classification and MUST NOT be used for translate.
    """
    return "gemini"
def _pick_gemini_key() -> str:
    key = _env("TRANSLATE_GEMINI_API_KEY", "")
    if key:
        return key
    if _as_bool("TRANSLATE_ALLOW_FALLBACK", False):
        return _env("GEMINI_API_KEY", _env("GEMINI_API_KEY_B", _env("GEMINI_BACKUP_API_KEY", "")))
    return ""

def _pick_groq_key() -> str:
    key = _env("TRANSLATE_GROQ_API_KEY", "")
    if key:
        return key
    if _as_bool("TRANSLATE_ALLOW_FALLBACK", False):
        return _env("GROQ_API_KEY", "")
    return ""

# -------------------------
# Gemini / Groq text translate
# -------------------------

async def _gemini_translate_text(text: str, target_lang: str) -> Tuple[bool, str]:
    key = _pick_gemini_key()
    if not key:
        return False, "missing TRANSLATE_GEMINI_API_KEY"
    model = _env("TRANSLATE_GEMINI_MODEL", "gemini-2.5-flash-lite")
    schema = _env("TRANSLATE_SCHEMA", '{"translation": "...", "reason": "..."}')

    base_sys = _env(
        "TRANSLATE_SYS_MSG",
        f"You are a translation engine. Translate the user's text into {target_lang}. "
        "Do NOT leave any part in the source language except proper nouns, usernames, or URLs. "
        f"If the text is already in {target_lang}, return it unchanged. "
        f"Return ONLY compact JSON matching this schema: {schema}. No prose."
    )
    strict_sys = _env(
        "TRANSLATE_SYS_MSG_STRICT",
        f"STRICT MODE. Translate ALL user text into {target_lang}. "
        "No source-language remnants except proper nouns/usernames/URLs. "
        f"Return ONLY compact JSON matching this schema: {schema}. No prose."
    )

    async def _call(sys_msg: str) -> Tuple[bool, str]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        payload = {
            "contents": [
                {"role": "user", "parts": [{"text": sys_msg + "\n\nTEXT:\n" + text}]}
            ],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
        }
        try:
            import aiohttp  # type: ignore
        except Exception as e:
            return False, f"aiohttp missing: {e!r}"

        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=payload, timeout=20) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        return False, f"Gemini HTTP {resp.status}: {body[:200]}"
                    j = await resp.json()
                    cand = (j.get("candidates") or [{}])[0]
                    parts = (((cand.get("content") or {}).get("parts")) or [])
                    out = ""
                    for p in parts:
                        if "text" in p:
                            out += p["text"]
                    out = _clean_output(out)
                    # accept JSON or plain text fallback
                    try:
                        jj = json.loads(out)
                        out2 = str(jj.get("translation", "") or out)
                        return True, out2.strip() or "(empty)"
                    except Exception:
                        return True, out.strip() or "(empty)"
        except Exception as e:
            return False, f"Gemini request failed: {e!r}"

    ok, out = await _call(base_sys)
    if ok and _seems_untranslated(text, out, target_lang):
        ok2, out2 = await _call(strict_sys)
        if ok2 and out2:
            out = out2
    return ok, out


async def _gemini_translate_text_ja_multi(text: str) -> Tuple[bool, Dict[str, str]]:
    """
    Gemini helper for Japanese dual-style translation + romaji.

    Returns (ok, data) where data has keys:
      - "formal": formal/polite Japanese
      - "casual": casual/everyday Japanese
      - "romaji": romaji (Latin transcription)
      - "reason": optional reason/explanation
      - "raw": raw model output (for debugging)
    """
    key = _pick_gemini_key()
    if not key:
        return False, {
            "formal": "",
            "casual": "",
            "romaji": "",
            "reason": "missing TRANSLATE_GEMINI_API_KEY",
            "raw": "",
        }

    model = _env("TRANSLATE_GEMINI_MODEL", "gemini-2.5-flash-lite")
    schema = _env(
        "TRANSLATE_JA_SCHEMA",
        '{"formal": "...", "casual": "...", "romaji": "...", "reason": "..."}',
    )
    sys_msg = _env(
        "TRANSLATE_JA_SYS_MSG",
        "You are a Japanese translation engine. Given the user's text, produce THREE outputs:\n"
        "1) Formal polite Japanese (keigo but natural for chat).\n"
        "2) Casual everyday Japanese.\n"
        "3) Romaji (Latin transcription) of the Japanese text (NO English translation).\n"
        f"Return ONLY compact JSON matching this schema: {schema}. No prose. Do not wrap in code fences."
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": sys_msg + "\n\nTEXT:\n" + text}],
            }
        ],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
    }
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, json=payload, timeout=20) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raw = f"Gemini HTTP {resp.status}: {body[:200]}"
                    return False, {
                        "formal": "",
                        "casual": "",
                        "romaji": "",
                        "reason": raw,
                        "raw": raw,
                    }
                j = await resp.json()
    except Exception as e:
        raw = f"Gemini JA multi request failed: {e!r}"
        return False, {
            "formal": "",
            "casual": "",
            "romaji": "",
            "reason": raw,
            "raw": raw,
        }

    try:
        cand = (j.get("candidates") or [{}])[0]
        parts = (((cand.get("content") or {}).get("parts")) or [])
        out = ""
        for p in parts:
            if isinstance(p, dict) and "text" in p:
                out += str(p["text"])
        out = _clean_output(out)
        try:
            jj = json.loads(out)
        except Exception:
            # Non-JSON; treat whole output as "formal" best-effort.
            return True, {
                "formal": out.strip(),
                "casual": "",
                "romaji": "",
                "reason": "non_json_output",
                "raw": out,
            }

        formal = str(
            jj.get("formal")
            or jj.get("formal_translation")
            or jj.get("translation_formal")
            or ""
        )
        casual = str(
            jj.get("casual")
            or jj.get("casual_translation")
            or jj.get("translation_casual")
            or ""
        )
        romaji = str(
            jj.get("romaji")
            or jj.get("kana_romaji")
            or jj.get("romanization")
            or ""
        )
        reason = str(jj.get("reason") or "")

        return True, {
            "formal": formal.strip(),
            "casual": casual.strip(),
            "romaji": romaji.strip(),
            "reason": reason.strip(),
            "raw": out,
        }
    except Exception as e:
        raw = f"Gemini JA multi parse failed: {e!r}"
        return False, {
            "formal": "",
            "casual": "",
            "romaji": "",
            "reason": raw,
            "raw": raw,
        }

async def _groq_translate_text(text: str, target_lang: str) -> Tuple[bool, str]:
    key = _pick_groq_key()
    if not key:
        return False, "missing TRANSLATE_GROQ_API_KEY"
    model = _env("TRANSLATE_GROQ_MODEL", "llama-3.1-8b-instant")
    sys_msg = (
        f"You are a translation engine. Translate user text to {target_lang}. "
        "Output ONLY the translation, no commentary."
    )
    try:
        from groq import Groq  # type: ignore
    except Exception as e:
        return False, f"Groq SDK missing: {e!r}"

    try:
        client = Groq(api_key=key)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": text},
            ],
            temperature=0.2,
        )
        out = (resp.choices[0].message.content or "").strip()
        return True, out or "(empty)"
    except Exception as e:
        return False, f"Groq request failed: {e!r}"

# -------------------------
# Gemini Vision OCR + translate image
# -------------------------

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



async def _gemini_translate_text_ko_multi(text: str) -> Tuple[bool, Dict[str, str]]:
    """Gemini helper for Korean dual-style translation + romanization."""
    key = _pick_gemini_key()
    if not key:
        return False, {
            "formal": "",
            "casual": "",
            "romaji": "",
            "reason": "missing TRANSLATE_GEMINI_API_KEY",
            "raw": "",
        }

    model = _env("TRANSLATE_GEMINI_MODEL", "gemini-2.5-flash-lite")
    schema = _env(
        "TRANSLATE_KO_SCHEMA",
        '{"formal": "...", "casual": "...", "romaji": "...", "reason": "..."}',
    )
    sys_msg = _env(
        "TRANSLATE_KO_SYS_MSG",
        "You are a Korean translation engine. Given the user's text, produce THREE outputs:\n"
        "1) Formal polite Korean (존댓말 / jondaetmal, natural for chat).\n"
        "2) Casual everyday Korean.\n"
        "3) Romanization (Latin transcription) of the Korean text (NO English translation).\n"
        f"Return ONLY compact JSON matching this schema: {schema}. No prose. Do not wrap in code fences."
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": sys_msg + "\n\nTEXT:\n" + text}],
            }
        ],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
    }
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, json=payload, timeout=20) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raw = f"Gemini HTTP {resp.status}: {body[:200]}"
                    return False, {
                        "formal": "",
                        "casual": "",
                        "romaji": "",
                        "reason": raw,
                        "raw": raw,
                    }
                j = await resp.json()
    except Exception as e:
        raw = f"Gemini KO multi request failed: {e!r}"
        return False, {
            "formal": "",
            "casual": "",
            "romaji": "",
            "reason": raw,
            "raw": raw,
        }

    try:
        cand = (j.get("candidates") or [{}])[0]
        parts = (((cand.get("content") or {}).get("parts")) or [])
        out = ""
        for p in parts:
            if isinstance(p, dict) and "text" in p:
                out += str(p["text"])
        out = _clean_output(out)
        try:
            jj = json.loads(out)
        except Exception:
            return True, {
                "formal": out.strip(),
                "casual": "",
                "romaji": "",
                "reason": "non_json_output",
                "raw": out,
            }

        formal = str(
            jj.get("formal")
            or jj.get("formal_translation")
            or jj.get("translation_formal")
            or ""
        )
        casual = str(
            jj.get("casual")
            or jj.get("casual_translation")
            or jj.get("translation_casual")
            or ""
        )
        romaji = str(
            jj.get("romaji")
            or jj.get("kana_romaji")
            or jj.get("romanization")
            or ""
        )
        reason = str(jj.get("reason") or "")

        return True, {
            "formal": formal.strip(),
            "casual": casual.strip(),
            "romaji": romaji.strip(),
            "reason": reason.strip(),
            "raw": out,
        }
    except Exception as e:
        raw = f"Gemini KO multi parse failed: {e!r}"
        return False, {
            "formal": "",
            "casual": "",
            "romaji": "",
            "reason": raw,
            "raw": raw,
        }


async def _gemini_translate_text_zh_multi(text: str) -> Tuple[bool, Dict[str, str]]:
    """Gemini helper for Chinese dual-style translation + pinyin romanization."""
    key = _pick_gemini_key()
    if not key:
        return False, {
            "formal": "",
            "casual": "",
            "romaji": "",
            "reason": "missing TRANSLATE_GEMINI_API_KEY",
            "raw": "",
        }

    model = _env("TRANSLATE_GEMINI_MODEL", "gemini-2.5-flash-lite")
    schema = _env(
        "TRANSLATE_ZH_SCHEMA",
        '{"formal": "...", "casual": "...", "romaji": "...", "reason": "..."}',
    )
    sys_msg = _env(
        "TRANSLATE_ZH_SYS_MSG",
        "You are a Chinese (Mandarin) translation engine. Given the user's text, produce THREE outputs:\n"
        "1) Formal written Chinese (natural, suitable for polite chat).\n"
        "2) Casual everyday spoken-style Chinese.\n"
        "3) Pinyin (Latin romanization with tone marks if possible) of the Chinese text (NO English translation).\n"
        f"Return ONLY compact JSON matching this schema: {schema}. No prose. Do not wrap in code fences."
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": sys_msg + "\n\nTEXT:\n" + text}],
            }
        ],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
    }
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, json=payload, timeout=20) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raw = f"Gemini HTTP {resp.status}: {body[:200]}"
                    return False, {
                        "formal": "",
                        "casual": "",
                        "romaji": "",
                        "reason": raw,
                        "raw": raw,
                    }
                j = await resp.json()
    except Exception as e:
        raw = f"Gemini ZH multi request failed: {e!r}"
        return False, {
            "formal": "",
            "casual": "",
            "romaji": "",
            "reason": raw,
            "raw": raw,
        }

    try:
        cand = (j.get("candidates") or [{}])[0]
        parts = (((cand.get("content") or {}).get("parts")) or [])
        out = ""
        for p in parts:
            if isinstance(p, dict) and "text" in p:
                out += str(p["text"])
        out = _clean_output(out)
        try:
            jj = json.loads(out)
        except Exception:
            return True, {
                "formal": out.strip(),
                "casual": "",
                "romaji": "",
                "reason": "non_json_output",
                "raw": out,
            }

        formal = str(
            jj.get("formal")
            or jj.get("formal_translation")
            or jj.get("translation_formal")
            or ""
        )
        casual = str(
            jj.get("casual")
            or jj.get("casual_translation")
            or jj.get("translation_casual")
            or ""
        )
        romaji = str(
            jj.get("romaji")
            or jj.get("kana_romaji")
            or jj.get("romanization")
            or ""
        )
        reason = str(jj.get("reason") or "")

        return True, {
            "formal": formal.strip(),
            "casual": casual.strip(),
            "romaji": romaji.strip(),
            "reason": reason.strip(),
            "raw": out,
        }
    except Exception as e:
        raw = f"Gemini ZH multi parse failed: {e!r}"
        return False, {
            "formal": "",
            "casual": "",
            "romaji": "",
            "reason": raw,
            "raw": raw,
        }

async def _translate_image_gemini(image_bytes: bytes, target_lang: str) -> Tuple[bool, str, str, str]:
    """
    OCR+translate an image using Gemini Vision REST API.

    Note: We intentionally avoid the google-genai SDK here because several environments
    (including yours) ship with an httpx version that is incompatible with recent SDK
    releases, causing noisy follow_redirects / destructor errors. REST is stable.
    """
    key = _pick_gemini_key()
    if not key:
        return False, "", "", "missing TRANSLATE_GEMINI_API_KEY"
    model = _env("TRANSLATE_IMAGE_MODEL", _env("TRANSLATE_GEMINI_MODEL", "gemini-2.5-flash"))
    schema = _env("TRANSLATE_SCHEMA", '{"text": "...", "translation": "...", "reason": "..."}')
    prompt = (
        "You are an OCR+translation engine.\n"
        "1) Extract all readable text from the image.\n"
        f"2) Translate it to {target_lang}.\n"
        f"Return ONLY compact JSON matching schema: {schema}. No prose.\n"
        "If no text, return {\"text\":\"\",\"translation\":\"\",\"reason\":\"no_text\"}."
    )
    mime = _detect_image_mime(image_bytes)

    try:
        import aiohttp
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        payload = {
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {"inline_data": {
                        "mime_type": mime,
                        "data": base64.b64encode(image_bytes).decode("utf-8"),
                    }},
                ],
            }],
            "generationConfig": {"temperature": 0.2},
        }
        timeout_s = float(_env("TRANSLATE_VISION_TIMEOUT_SEC", "18"))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as session:
            async with session.post(url, json=payload) as resp:
                data = await resp.json(content_type=None)

        candidates = data.get("candidates") or []
        parts = []
        if candidates:
            parts = (candidates[0].get("content") or {}).get("parts") or []
        out = ""
        for p in parts:
            if isinstance(p, dict) and "text" in p:
                out += str(p["text"])
        out = _clean_output(out)
        try:
            j = json.loads(out)
        except Exception:
            return True, out, out, "non_json_output"
        detected = str(j.get("text", "") or "")
        translated = str(j.get("translation", "") or "")
        reason = str(j.get("reason", "") or "ok")
        return True, detected, translated, reason
    except Exception as e:
        return False, "", "", f"vision_failed:{e!r}"
class TranslateCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_call: Dict[int, float] = {}
        self._registered = False
        self._register_lock = asyncio.Lock()
        self._target_overrides: Dict[int, str] = {}

    def _cooldown_ok(self, user_id: int) -> Tuple[bool, float]:
        cd = _as_float("TRANSLATE_COOLDOWN_SEC", 5.0)
        now = asyncio.get_event_loop().time()
        last = self._last_call.get(user_id, 0.0)
        if now - last < cd:
            return False, cd - (now - last)
        self._last_call[user_id] = now
        return True, 0.0

    async def _ensure_registered(self):
        if not _as_bool("TRANSLATE_ENABLE", True):
            return
        async with self._register_lock:
            if self._registered:
                return
            await self.bot.wait_until_ready()

            gids = _translate_guild_ids()
            if not gids:
                gids = [g.id for g in getattr(self.bot, "guilds", [])]

            ctx_name = _env("TRANSLATE_CTX_NAME", "Translate (Nixe)").strip() or "Translate (Nixe)"
            rev_ctx_name = _env("REVERSE_IMAGE_CTX_NAME", "Reverse image (Nixe)").strip() or "Reverse image (Nixe)"
            su_ctx_name = _env("TRANSLATE_SUNDA_CTX_NAME", "").strip()
            jw_ctx_name = _env("TRANSLATE_JAWA_CTX_NAME", "").strip()
            ar_ctx_name = _env("TRANSLATE_AR_CTX_NAME", "").strip()

            # Force-remove legacy /translate slash if present (global cached).
            if _as_bool("TRANSLATE_FORCE_REMOVE_SLASH", True):
                try:
                    self.bot.tree.remove_command("translate", type=discord.AppCommandType.chat_input)
                    log.info("[translate] forced remove of chat_input /translate from local tree")
                except Exception as e:
                    log.debug("[translate] force remove slash skipped: %r", e)


            # cleanup translate* commands from this bot
            try:
                for cmd in list(self.bot.tree.get_commands()):
                    if cmd.name.lower().startswith("translate"):
                        try:
                            self.bot.tree.remove_command(cmd.name, type=cmd.type)
                        except Exception:
                            pass
            except Exception:
                pass

            # add commands per guild
            for gid in gids:
                gobj = discord.Object(id=gid)
                try:
                    # remove any per-guild leftovers
                    for cmd in list(self.bot.tree.get_commands(guild=gobj)):
                        if cmd.name.lower().startswith("translate"):
                            try:
                                self.bot.tree.remove_command(cmd.name, type=cmd.type, guild=gobj)
                            except Exception:
                                pass
                except Exception:
                    pass

                try:
                    self.bot.tree.add_command(
                        app_commands.ContextMenu(name=ctx_name, callback=self.translate_message_ctx),
                        guild=gobj,
                    )
                except Exception:
                    pass

                if su_ctx_name:
                    try:
                        self.bot.tree.add_command(
                            app_commands.ContextMenu(name=su_ctx_name, callback=self.translate_message_ctx_sunda),
                            guild=gobj,
                        )
                    except Exception:
                        pass

                if jw_ctx_name:
                    try:
                        self.bot.tree.add_command(
                            app_commands.ContextMenu(name=jw_ctx_name, callback=self.translate_message_ctx_jawa),
                            guild=gobj,
                        )
                    except Exception:
                        pass

                if ar_ctx_name:
                    try:
                        self.bot.tree.add_command(
                            app_commands.ContextMenu(name=ar_ctx_name, callback=self.translate_message_ctx_arabic),
                            guild=gobj,
                        )
                    except Exception:
                        pass

                if _as_bool("REVERSE_IMAGE_ENABLE", True):
                    try:
                        self.bot.tree.add_command(
                            app_commands.ContextMenu(name=rev_ctx_name, callback=self.reverse_image_ctx),
                            guild=gobj,
                        )
                    except Exception:
                        pass

                if _as_bool("TRANSLATE_SLASH_ENABLE", False):
                    try:
                        self.bot.tree.add_command(self.translate_slash, guild=gobj)
                    except Exception:
                        pass

            # sync once global to flush legacy, then per guild
            do_global_sync = _as_bool("TRANSLATE_SYNC_ON_BOOT", True) or _as_bool("TRANSLATE_FORCE_REMOVE_SLASH", True)
            if do_global_sync:
                try:
                    await self.bot.tree.sync()
                except Exception as e:
                    log.warning("[translate] global sync failed: %r", e)
                for gid in gids:
                    try:
                        await self.bot.tree.sync(guild=discord.Object(id=gid))
                    except Exception as e:
                        log.warning("[translate] guild sync failed gid=%s: %r", gid, e)

            self._registered = True
            log.info("[translate] registered ctx+slash to gids=%s", gids)


    async def translate_message_ctx_sunda(self, interaction: discord.Interaction, message: discord.Message):
        """Context-menu: Translate specifically to Sundanese.
        Uses the same pipeline as translate_message_ctx but overrides the target language for this interaction.
        """
        key = int(getattr(interaction, "id", 0) or 0)
        try:
            override = _env("TRANSLATE_SUNDA_TARGET", "Sundanese").strip() or "Sundanese"
        except Exception:
            override = "Sundanese"
        self._target_overrides[key] = override
        await self.translate_message_ctx(interaction, message)

    async def translate_message_ctx_jawa(self, interaction: discord.Interaction, message: discord.Message):
        """Context-menu: Translate specifically to Javanese.
        Uses the same pipeline as translate_message_ctx but overrides the target language for this interaction.
        """
        key = int(getattr(interaction, "id", 0) or 0)
        try:
            override = _env("TRANSLATE_JAWA_TARGET", "Javanese").strip() or "Javanese"
        except Exception:
            override = "Javanese"
        self._target_overrides[key] = override
        await self.translate_message_ctx(interaction, message)

    async def translate_message_ctx_arabic(self, interaction: discord.Interaction, message: discord.Message):
        """Context-menu: Translate specifically to Arabic.
        Uses the same pipeline as translate_message_ctx but overrides the target language for this interaction.
        """
        key = int(getattr(interaction, "id", 0) or 0)
        try:
            override = _env("TRANSLATE_AR_TARGET", "Arabic").strip() or "Arabic"
        except Exception:
            override = "Arabic"
        self._target_overrides[key] = override
        await self.translate_message_ctx(interaction, message)

    async def reverse_image_ctx(self, interaction: discord.Interaction, message: discord.Message):
        """Message context-menu: Reverse image search for attachments / embed images."""
        if not _as_bool("REVERSE_IMAGE_ENABLE", True):
            await interaction.response.send_message("Reverse image search is disabled.", ephemeral=True)
            return

        ok_cd, wait_s = self._cooldown_ok(interaction.user.id)
        if not ok_cd:
            await interaction.response.send_message(f"Cooldown. Try again in {wait_s:.1f}s.", ephemeral=True)
            return

        ephemeral = _as_bool("REVERSE_IMAGE_EPHEMERAL", True)
        await interaction.response.defer(thinking=True, ephemeral=ephemeral)

        # Refetch full message so embeds/attachments lengkap
        try:
            if interaction.channel and hasattr(interaction.channel, "fetch_message"):
                message = await interaction.channel.fetch_message(message.id)
        except Exception:
            # best-effort only
            pass

        # Unwrap reply/forward wrappers if needed.
        src_msg = _pick_best_source_message(message)

        debug = False
        try:
            debug = _as_bool("REVERSE_IMAGE_DEBUG_LOG", False) or _as_bool("TRANSLATE_DEBUG_LOG", False)
        except Exception:
            debug = False

        if debug:
            try:
                log.info(
                    "[revimg] ctx invoke uid=%s mid=%s src=%s embeds=%s atts=%s snaps=%s",
                    getattr(interaction.user, "id", None),
                    getattr(message, "id", None),
                    type(src_msg).__name__,
                    len(getattr(src_msg, "embeds", None) or []),
                    len(getattr(src_msg, "attachments", None) or []),
                    len(getattr(message, "message_snapshots", None) or []),
                )
            except Exception:
                pass

        max_images = int(_as_float("REVERSE_IMAGE_MAX_IMAGES", 3.0))
        urls: List[str] = []

        # Kumpulkan URL dari attachment gambar
        atts = getattr(src_msg, "attachments", None) or []
        for att in atts:
            try:
                fn = (getattr(att, "filename", "") or "").lower()
                url = (getattr(att, "url", None) or getattr(att, "proxy_url", None) or "")
                ct = (getattr(att, "content_type", "") or "").lower()
                is_img = (
                    any(fn.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"))
                    or any(str(url).lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"))
                    or ("image/" in ct)
                )
                if not is_img:
                    continue
                if url:
                    urls.append(str(url))
            except Exception:
                continue

        # Tambah juga dari embed image/thumbnail (misal preview link)
        embed_urls = _extract_image_urls_from_embeds(list(getattr(src_msg, "embeds", None) or []))
        for u in embed_urls:
            urls.append(u)

        # de-duplicate sambil jaga urutan
        seen: set[str] = set()
        uniq_urls: List[str] = []
        for u in urls:
            if not u:
                continue
            if u in seen:
                continue
            seen.add(u)
            uniq_urls.append(u)

        if not uniq_urls:
            await interaction.followup.send(
                "Tidak ada gambar pada pesan ini untuk reverse image search.",
                ephemeral=ephemeral,
            )
            return

        uniq_urls = uniq_urls[:max_images]

        embed = discord.Embed(title="Reverse image search")
        embed.description = (
            "Klik salah satu link di bawah untuk melakukan reverse image search via browser.\n"
            "Nixe hanya membuat link; pencarian dilakukan di situs pihak ketiga (Google/Bing/Yandex/SauceNAO/IQDB)."
        )

        for idx, u in enumerate(uniq_urls, 1):
            try:
                q = urllib_parse.quote_plus(u)
            except Exception:
                q = u

            lines = [
                f"[Google Lens](https://lens.google.com/uploadbyurl?url={q})",
                f"[Bing Visual Search](https://www.bing.com/images/searchbyimage?cbir=sbi&imgurl={q})",
                f"[Yandex Images](https://yandex.com/images/search?rpt=imageview&url={q})",
                f"[SauceNAO](https://saucenao.com/search.php?url={q})",
                f"[IQDB](https://iqdb.org/?url={q})",
            ]
            val = "\n".join(lines)
            embed.add_field(name=f"Gambar #{idx}", value=(val[:1024] or "(empty)"), inline=False)

        try:
            embed.set_thumbnail(url=uniq_urls[0])
        except Exception:
            pass

        embed.set_footer(text="Reverse image search helper: Google Lens • Bing • Yandex • SauceNAO • IQDB")

        await interaction.followup.send(embed=embed, ephemeral=ephemeral)

    async def translate_message_ctx(self, interaction: discord.Interaction, message: discord.Message):
        if not _as_bool("TRANSLATE_ENABLE", True):
            await interaction.response.send_message("Translate is disabled.", ephemeral=True)
            return

        ok_cd, wait_s = self._cooldown_ok(interaction.user.id)
        if not ok_cd:
            await interaction.response.send_message(f"Cooldown. Try again in {wait_s:.1f}s.", ephemeral=True)
            return

        ephemeral = _as_bool("TRANSLATE_EPHEMERAL", False)
        await interaction.response.defer(thinking=True, ephemeral=ephemeral)

        # Context-menu provides a partial Message; refetch for full embeds/attachments.
        try:
            if interaction.channel and hasattr(interaction.channel, "fetch_message"):
                message = await interaction.channel.fetch_message(message.id)
        except Exception:
            # best-effort only
            pass

        # Unwrap reply/forward wrappers if needed.
        src_msg = _pick_best_source_message(message)

        override = self._target_overrides.pop(int(getattr(interaction, "id", 0) or 0), None)
        if override:
            target = override
        else:
            target = _env("TRANSLATE_TARGET_LANG", "id").strip() or "id"
        debug = _as_bool("TRANSLATE_DEBUG_LOG", False)

        try:
            log.info(
                "[translate] ctx invoke uid=%s mid=%s src=%s rawlen=%s embeds=%s atts=%s snaps=%s target=%s",
                getattr(interaction.user, "id", None),
                getattr(message, "id", None),
                type(src_msg).__name__,
                len((getattr(src_msg, "content", "") or "")),
                len(getattr(src_msg, "embeds", None) or []),
                len(getattr(src_msg, "attachments", None) or []),
                len(getattr(message, "message_snapshots", None) or []),
                target,
            )
        except Exception:
            pass

        # -------------------------
        # 1) Collect base text (chat / embed text)

        # 1) Collect base text (chat / embed text)
        # -------------------------
        raw_text = (getattr(src_msg, "content", "") or "").strip()
        text_for_chat = raw_text

        embeds = list(getattr(src_msg, "embeds", None) or [])
        if embeds:
            emb_text = _extract_text_from_embeds(embeds)
            if emb_text:
                if not text_for_chat or _looks_like_only_urls(text_for_chat):
                    # kalau chat kosong / cuma URL, pakai teks embed saja
                    text_for_chat = emb_text
                else:
                    # kalau dua-duanya ada teks, gabungkan supaya info embed juga ikut diterjemahkan
                    text_for_chat = f"{text_for_chat}\n\n{emb_text}"

        text_for_chat = (text_for_chat or "").strip()
        # -------------------------
        # 2) Collect images (attachments + embed images)
        # -------------------------
        try:
            max_images = int(float(_env("TRANSLATE_MAX_IMAGES", "3")))
        except Exception:
            max_images = 3
        if max_images < 0:
            max_images = 0

        image_entries = []  # List[bytes]
        if max_images > 0:
            # 2a) attachments
            try:
                for att in (getattr(src_msg, "attachments", None) or []):
                    if len(image_entries) >= max_images:
                        break
                    fn = (getattr(att, "filename", "") or "").lower()
                    url = (getattr(att, "url", None) or getattr(att, "proxy_url", None) or "")
                    ct = (getattr(att, "content_type", "") or "").lower()
                    is_img = (
                        any(fn.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"))
                        or any(str(url).lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"))
                        or ("image/" in ct)
                    )
                    if not is_img:
                        continue
                    b = None
                    try:
                        read_fn = getattr(att, "read", None)
                        if callable(read_fn):
                            b = await read_fn()
                    except Exception:
                        b = None
                    if not b and url:
                        try:
                            b = await _fetch_image_bytes(str(url))
                        except Exception:
                            b = None
                    if b:
                        image_entries.append(b)
            except Exception:
                if debug:
                    log.exception("[translate] error while scanning attachments for images")

            # 2b) embed images (link previews / bot embeds)
            try:
                if len(image_entries) < max_images:
                    embeds2 = list(getattr(src_msg, "embeds", None) or [])
                    for u in _extract_image_urls_from_embeds(embeds2):
                        if len(image_entries) >= max_images:
                            break
                        try:
                            b = await _fetch_image_bytes(u)
                        except Exception:
                            b = None
                        if b:
                            image_entries.append(b)
            except Exception:
                if debug:
                    log.exception("[translate] error while scanning embeds for images")

        # Jika tidak ada teks chat/embed dan tidak ada gambar sama sekali -> langsung beri pesan kosong.
        if not text_for_chat and not image_entries:
            await interaction.followup.send("Tidak ada teks yang bisa diterjemahkan dari pesan ini.", ephemeral=ephemeral)
            return

        # -------------------------
        # 3) Bangun embed gabungan (gambar dulu, lalu chat)
        # -------------------------
        # Resolve language profile for display label / multi-style heuristics.
        prof = resolve_lang(target)
        if prof is not None:
            target_display = prof.display
            target_code = prof.code.lower()
        else:
            target_display = str(target)
            target_code = str(target or "").strip().lower()
        embed = discord.Embed(title="Translation")

        # 3a) Proses gambar-gambar (prioritas)
        image_any_ok = False
        for idx, img_bytes in enumerate(image_entries, 1):
            ok_img, detected, translated_img, reason = await _translate_image_gemini(img_bytes, target)
            field_name = f"🖼 Gambar #{idx}"
            if not ok_img:
                # Gagal untuk gambar ini saja; lanjut ke gambar berikutnya / chat.
                embed.add_field(
                    name=field_name,
                    value=(f"Gagal menerjemahkan gambar ini: {reason}"[:1024] or "(error)"),
                    inline=False,
                )
                continue

            if (detected or "").strip():
                # tampilkan teks asli + terjemahan
                value_lines = []
                value_lines.append("**Detected text:**")
                value_lines.append((detected or "(empty)")[:600])
                value_lines.append("")
                value_lines.append(f"**Translated → {target_display}:**")
                value_lines.append((translated_img or "(empty)")[:600])
                val = "\n".join(value_lines)
            else:
                # tidak ada teks terbaca
                if (translated_img or "").strip():
                    # kadang model hanya mengembalikan terjemahan
                    val = f"**Translated → {target_display}:**\n{(translated_img or '(empty)')[:1024]}"
                else:
                    val = "_Tidak ada teks terbaca di gambar ini._"

            embed.add_field(name=field_name, value=(val[:1024] or "(empty)"), inline=False)
            image_any_ok = True



        # 3b) Proses chat user (jika ada text_for_chat)
        provider = _pick_provider()
        translated_chat = ""
        chat_val: str | None = None

                # Mode khusus: target JA/KR/ZH dengan dua gaya + romaji/pinyin
        tgt_lower = target_code
        is_ja_target = tgt_lower.startswith("ja")
        is_ko_target = tgt_lower.startswith("ko")
        is_zh_target = tgt_lower.startswith("zh")

        ja_dual_enable = is_ja_target and _as_bool("TRANSLATE_JA_DUAL_ENABLE", True)
        ko_dual_enable = is_ko_target and _as_bool("TRANSLATE_KO_DUAL_ENABLE", True)
        zh_dual_enable = is_zh_target and _as_bool("TRANSLATE_ZH_DUAL_ENABLE", True)
        ja_romaji_enable = _as_bool("TRANSLATE_JA_ROMAJI_ENABLE", True)

        dual_kind: str | None = None
        dual_formal = ""
        dual_casual = ""
        dual_romaji = ""

        if text_for_chat:
            # chunking seperti sebelumnya
            try:
                try:
                    max_chars = int(_as_float("TRANSLATE_MAX_CHARS", 1800))
                except Exception:
                    max_chars = 1800
                chunks = _chunk_text(text_for_chat, max_chars)

                if ja_dual_enable or ko_dual_enable or zh_dual_enable:
                    formal_parts: List[str] = []
                    casual_parts: List[str] = []
                    romaji_parts: List[str] = []
                    for ch in chunks:
                        if ja_dual_enable:
                            ok_multi, res = await _gemini_translate_text_ja_multi(ch)
                        elif ko_dual_enable:
                            ok_multi, res = await _gemini_translate_text_ko_multi(ch)
                        elif zh_dual_enable:
                            ok_multi, res = await _gemini_translate_text_zh_multi(ch)
                        else:
                            ok_multi, res = False, {"reason": "invalid_dual_state"}

                        if not ok_multi:
                            if debug:
                                log.warning(
                                    "[translate] multi-style failed; fallback to single translation: %s",
                                    res.get("reason"),
                                )
                            # fallback: single-mode translate seluruh teks supaya hasil tetap ada
                            ok_single, out_single = await _gemini_translate_text(text_for_chat, target)
                            if not ok_single:
                                await interaction.followup.send(out_single, ephemeral=ephemeral)
                                return
                            translated_chat = out_single.strip()
                            ja_dual_enable = ko_dual_enable = zh_dual_enable = False
                            dual_kind = None
                            dual_formal = dual_casual = dual_romaji = ""
                            break

                        formal_parts.append(res.get("formal", ""))
                        casual_parts.append(res.get("casual", ""))
                        rom = res.get("romaji", "")
                        if rom:
                            romaji_parts.append(rom)

                    if ja_dual_enable or ko_dual_enable or zh_dual_enable:
                        if ja_dual_enable:
                            dual_kind = "ja"
                        elif ko_dual_enable:
                            dual_kind = "ko"
                        else:
                            dual_kind = "zh"
                        dual_formal = "\n".join(p for p in formal_parts if p).strip()
                        dual_casual = "\n".join(p for p in casual_parts if p).strip()
                        if romaji_parts:
                            dual_romaji = "\n".join(p for p in romaji_parts if p).strip()

                if not (ja_dual_enable or ko_dual_enable or zh_dual_enable):
                    # mode lama: satu hasil terjemahan saja
                    out_parts = []
                    for ch in chunks:
                        # provider untuk translate dikunci ke Gemini; Groq hanya untuk phishing.
                        ok, out = await _gemini_translate_text(ch, target)
                        if not ok:
                            await interaction.followup.send(out, ephemeral=ephemeral)
                            return
                        out_parts.append(out)
                    translated_chat = "\n".join(out_parts).strip()
            except Exception as e:
                if debug:
                    log.exception("[translate] chat translation failed: %r", e)
                translated_chat = ""
                dual_kind = None
                dual_formal = dual_casual = dual_romaji = ""

            # Susun field chat user
            src_preview = text_for_chat[:600]

            if dual_kind and (dual_formal or dual_casual or dual_romaji):
                # Untuk JA/KR/ZH, tampilkan 2 gaya + romanisasi sebagai field terpisah tanpa blok Source
                if dual_kind == "ja":
                    lang_label = "JA"
                    romaji_label = "🔤 Romaji"
                elif dual_kind == "ko":
                    lang_label = "KO"
                    romaji_label = "🔤 Romanization"
                else:
                    lang_label = "ZH"
                    romaji_label = "🔤 Pinyin"

                if dual_formal:
                    embed.add_field(
                        name=f"💬 {lang_label} (formal / polite)",
                        value=(dual_formal[:1024] or "(empty)"),
                        inline=False,
                    )
                if dual_casual:
                    embed.add_field(
                        name=f"💬 {lang_label} (casual / daily chat)",
                        value=(dual_casual[:1024] or "(empty)"),
                        inline=False,
                    )
                if dual_romaji:
                    embed.add_field(
                        name=romaji_label,
                        value=(dual_romaji[:1024] or "(empty)"),
                        inline=False,
                    )
                chat_val = None  # jangan buat field gabungan lagi
            else:
                if translated_chat and translated_chat.strip() != text_for_chat.strip():
                    # ada hasil terjemahan berbeda
                    value_lines = []
                    value_lines.append("**Source:**")
                    value_lines.append(src_preview)
                    value_lines.append("")
                    value_lines.append(f"**Translated → {target_display}:**")
                    value_lines.append(translated_chat)
                    chat_val = "\n".join(value_lines)
                else:
                    # sama atau gagal terjemah; untuk kasus ini:
                    # - jika sudah ada hasil gambar dan target adalah id, kita tidak perlu
                    #   menampilkan blok Chat user lagi agar embed tetap ringkas.
                    if not (image_any_ok and target_code == "id"):
                        value_lines = []
                        value_lines.append("**Source:**")
                        value_lines.append(src_preview)
                        value_lines.append("")
                        value_lines.append(f"_Teks sudah dalam bahasa target ({target_display}) atau tidak perlu diterjemahkan._")
                        chat_val = "\n".join(value_lines)
        if chat_val is not None:
            embed.add_field(name="💬 Chat user", value=(chat_val[:1024] or "(empty)"), inline=False)
        # Kalau embed masih tanpa field (harusnya tidak terjadi), fallback pesan teks.
        if not embed.fields:
            await interaction.followup.send("Tidak ada teks yang bisa diterjemahkan dari pesan ini.", ephemeral=ephemeral)
            return

        # Footer info provider untuk debug ringan
        footer_bits = [f"text={provider}", "image=gemini", f"target={target}"]
        embed.set_footer(text=" • ".join(footer_bits))

        await interaction.followup.send(embed=embed, ephemeral=ephemeral)

    @commands.Cog.listener()
    async def on_ready(self):
        # ensure commands registered after ready (Render-safe)
        if not self._registered:
            self.bot.loop.create_task(self._ensure_registered())

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        if _translate_guild_ids():
            return  # explicit list; don't auto-add
        self._registered = False
        self.bot.loop.create_task(self._ensure_registered())

async def setup(bot: commands.Bot):
    cog = TranslateCommands(bot)
    await bot.add_cog(cog)
    # register after ready
    bot.loop.create_task(cog._ensure_registered())