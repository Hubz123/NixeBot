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
"""

from __future__ import annotations

import os, json, logging, re, asyncio, base64
from typing import Optional, Tuple, List, Dict, Any

import discord
import aiohttp
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

    # Adjust style hints based on target language. For Japanese/Korean/Chinese we
    # explicitly ask for polite, grammatically correct written style so the
    # output does not read as weird slang.
    lang = (target_lang or "").lower()
    style_note = ""
    if lang in ("ja", "jp"):
        style_note = (
            " Use natural, polite Japanese (です・ます調) appropriate for general written messages. "
            "Avoid overly stiff keigo and avoid slang unless it is clearly present in the source."
        )
    elif lang in ("ko", "kr"):
        style_note = (
            " Use natural, polite Korean (해요체, '-요' form) appropriate for general conversation. "
            "Avoid rude or aggressive slang unless it is clearly present in the source."
        )
    elif lang in ("zh", "zh-cn", "zh-hans", "zh-hant", "cn", "chs", "cht"):
        style_note = (
            " Use natural, standard Simplified Chinese suitable for a wide audience. "
            "Avoid archaic or excessively literary style unless the source is clearly written that way."
        )
    elif lang in ("en",):
        style_note = " Use natural, fluent English."

    base_default = (
        f"You are a translation engine. Translate the user's text into {target_lang}. "
        "Do NOT leave any part in the source language except proper nouns, usernames, or URLs. "
        f"If the text is already in {target_lang}, return it unchanged. "
        + style_note
        + f" Return ONLY compact JSON matching this schema: {schema}. No prose."
    )
    base_sys = _env("TRANSLATE_SYS_MSG", base_default)

    strict_default = (
        f"STRICT MODE. Translate ALL user text into {target_lang}. "
        "No source-language remnants except proper nouns/usernames/URLs. "
        + style_note
        + f" Return ONLY compact JSON matching this schema: {schema}. No prose."
    )
    strict_sys = _env("TRANSLATE_SYS_MSG_STRICT", strict_default)
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

    @app_commands.command(name="translate", description="Translate free text with Nixe (Gemini)")
    @app_commands.describe(
        target="Target language: id/en/ja/ko/zh",
        text="Text to translate",
    )
    async def translate_slash(self, interaction: discord.Interaction, target: str, text: str):
        """
        Slash command: translate arbitrary text to the given language.

        This does NOT inspect message embeds/images. For full message+image
        translation continue to use the message context-menu.
        """
        if not _as_bool("TRANSLATE_ENABLE", True):
            await interaction.response.send_message("Translate is disabled.", ephemeral=True)
            return

        ok_cd, wait_s = self._cooldown_ok(interaction.user.id)
        if not ok_cd:
            await interaction.response.send_message(f"Cooldown. Try again in {wait_s:.1f}s.", ephemeral=True)
            return

        ephemeral = _as_bool("TRANSLATE_EPHEMERAL", False)
        await interaction.response.defer(thinking=True, ephemeral=ephemeral)

        raw = (target or "").strip().lower()

        # Normalise target language and map common aliases.
        if raw in ("id", "ind", "indo", "indonesia", "indonesian"):
            tgt = "id"
            tgt_label = "ID"
        elif raw in ("en", "eng", "english"):
            tgt = "en"
            tgt_label = "EN"
        elif raw in ("ja", "jp", "jpn", "japanese", "nihon", "nihongo"):
            tgt = "ja"
            tgt_label = "JA"
        elif raw in ("ko", "kr", "kor", "korean", "hangul", "hangeul"):
            tgt = "ko"
            tgt_label = "KO"
        elif raw in ("zh", "zh-cn", "zh-hans", "zh-hant", "cn", "chs", "cht", "chinese", "mandarin"):
            tgt = "zh"
            tgt_label = "ZH"
        else:
            await interaction.followup.send(
                "Bahasa tujuan tidak dikenal. Gunakan salah satu: id, en, ja, ko, zh.",
                ephemeral=True,
            )
            return

        text = (text or "").strip()
        if not text:
            await interaction.followup.send("Tidak ada teks untuk diterjemahkan.", ephemeral=True)
            return

        ok, out = await _gemini_translate_text(text, tgt)
        if not ok:
            await interaction.followup.send(f"Gagal menerjemahkan: {out}", ephemeral=True)
            return

        out = (out or "").strip()
        if not out:
            await interaction.followup.send("Model tidak mengembalikan hasil terjemahan.", ephemeral=True)
            return

        embed = discord.Embed(title=f"Translate → {tgt_label}")

        # Source preview
        src_preview = text[:1024]
        embed.add_field(
            name="Source",
            value=(src_preview or "(empty)"),
            inline=False,
        )

        # Paged translated text
        try:
            tr_chunks = _chunk_text(out, 1000)
        except Exception:
            tr_chunks = [out[:1000]]
        total = len(tr_chunks)
        for idx, chunk in enumerate(tr_chunks, 1):
            fname = f"Translated → {tgt_label}"
            if total > 1:
                fname = f"{fname} ({idx}/{total})"
            embed.add_field(
                name=fname,
                value=(chunk or "(empty)"),
                inline=False,
            )

        embed.set_footer(text=f"text=gemini • target={tgt}")
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Plain-text trigger: `nixe translate [text] ke <lang> <teks>`.

        Examples:
          - "nixe translate ke en aku mau tidur"
          - "nixe translate text ke jp aku suka kamu"
        """
        if message.author.bot:
            return
        if not _as_bool("TRANSLATE_ENABLE", True):
            return
        if not _as_bool("TRANSLATE_TEXT_ENABLE", True):
            # optional kill-switch; default is enabled (no config needed)
            return

        content = (getattr(message, "content", "") or "").strip()
        if not content:
            return

        # Match leading pattern: nixe translate [text|teks] ke <lang> <body>
        m = re.match(r"(?is)^(nixe\s+translate(?:\s+(?:text|teks))?\s+ke\s+)(\S+)\s+(.+)$", content)
        if not m:
            return

        lang_raw = (m.group(2) or "").strip().lower()
        body = (m.group(3) or "").strip()
        if not body:
            try:
                await message.channel.send("Tidak ada teks untuk diterjemahkan.", reference=message)
            except Exception:
                await message.channel.send("Tidak ada teks untuk diterjemahkan.")
            return

        ok_cd, wait_s = self._cooldown_ok(getattr(message.author, "id", 0))
        if not ok_cd:
            txt = f"Cooldown. Coba lagi dalam {wait_s:.1f} detik."
            try:
                await message.channel.send(txt, reference=message)
            except Exception:
                await message.channel.send(txt)
            return

        # Normalise target language and map common aliases.
        if lang_raw in ("id", "ind", "indo", "indonesia", "indonesian"):
            tgt = "id"
            tgt_label = "ID"
        elif lang_raw in ("en", "eng", "english"):
            tgt = "en"
            tgt_label = "EN"
        elif lang_raw in ("ja", "jp", "jpn", "japanese", "nihon", "nihongo"):
            tgt = "ja"
            tgt_label = "JA"
        elif lang_raw in ("ko", "kr", "kor", "korean", "hangul", "hangeul"):
            tgt = "ko"
            tgt_label = "KO"
        elif lang_raw in ("zh", "zh-cn", "zh-hans", "zh-hant", "cn", "chs", "cht", "chinese", "mandarin"):
            tgt = "zh"
            tgt_label = "ZH"
        else:
            msg_txt = "Bahasa tujuan tidak dikenal. Gunakan salah satu: id, en, ja, ko, zh."
            try:
                await message.channel.send(msg_txt, reference=message)
            except Exception:
                await message.channel.send(msg_txt)
            return

        ok, out = await _gemini_translate_text(body, tgt)
        if not ok:
            txt = f"Gagal menerjemahkan: {out}"
            try:
                await message.channel.send(txt, reference=message)
            except Exception:
                await message.channel.send(txt)
            return

        out = (out or "").strip()
        if not out:
            txt = "Model tidak mengembalikan hasil terjemahan."
            try:
                await message.channel.send(txt, reference=message)
            except Exception:
                await message.channel.send(txt)
            return

        embed = discord.Embed(title=f"Translate → {tgt_label}")

        # Source preview
        src_preview = body[:1024]
        embed.add_field(
            name="Source",
            value=(src_preview or "(empty)"),
            inline=False,
        )

        # Paged translated text
        try:
            tr_chunks = _chunk_text(out, 1000)
        except Exception:
            tr_chunks = [out[:1000]]
        total = len(tr_chunks)
        for idx, chunk in enumerate(tr_chunks, 1):
            fname = f"Translated → {tgt_label}"
            if total > 1:
                fname = f"{fname} ({idx}/{total})"
            embed.add_field(
                name=fname,
                value=(chunk or "(empty)"),
                inline=False,
            )

        embed.set_footer(text=f"text=gemini • target={tgt}")
        try:
            await message.channel.send(embed=embed, reference=message)
        except Exception:
            await message.channel.send(embed=embed)

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

            det_text = (detected or "").strip()
            tr_text = (translated_img or "").strip()

            if det_text:
                # Tampilkan teks asli di field terpisah (preview).
                det_val = f"**Detected text:**\n{det_text}"
                embed.add_field(
                    name=f"{field_name} — Source",
                    value=(det_val[:1024] or "(empty)"),
                    inline=False,
                )

            if tr_text:
                # Bagi terjemahan panjang menjadi beberapa halaman embed.
                try:
                    tr_chunks = _chunk_text(tr_text, 1000)
                except Exception:
                    tr_chunks = [tr_text[:1000]]
                total = len(tr_chunks)
                for page_idx, chunk in enumerate(tr_chunks, 1):
                    fname = f"{field_name} — Translated → {target}"
                    if total > 1:
                        fname = f"{fname} ({page_idx}/{total})"
                    embed.add_field(
                        name=fname,
                        value=(chunk or "(empty)"),
                        inline=False,
                    )
            elif not det_text:
                # tidak ada teks terbaca sama sekali
                embed.add_field(
                    name=field_name,
                    value="_Tidak ada teks terbaca di gambar ini._",
                    inline=False,
                )

            image_any_ok = image_any_ok or ok_img


        # 3b) Proses chat user (jika ada text_for_chat)
        provider = _pick_provider()
        translated_chat = ""
        if text_for_chat:
            # chunking teks panjang, lalu gabungkan hasil translate
            try:
                try:
                    max_chars = int(_as_float("TRANSLATE_MAX_CHARS", 1800))
                except Exception:
                    max_chars = 1800
                chunks = _chunk_text(text_for_chat, max_chars)
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

            # Susun field chat user
            src_preview = text_for_chat[:600]
            if translated_chat and translated_chat.strip() != text_for_chat.strip():
                # ada hasil terjemahan berbeda:
                # - selalu tampilkan source sebagai preview sendiri
                # - hasil terjemahan dipotong per-halaman embed berdasarkan panjang TERJEMAHAN,
                #   bukan dijumlah dengan panjang source.
                src_val = "**Source (preview):**\n" + src_preview
                embed.add_field(
                    name="💬 Chat user — Source",
                    value=(src_val[:1024] or "(empty)"),
                    inline=False,
                )

                try:
                    chat_chunks = _chunk_text(translated_chat, 1000)
                except Exception:
                    chat_chunks = [translated_chat[:1000]]
                total_pages = len(chat_chunks)
                for page_idx, chunk in enumerate(chat_chunks, 1):
                    fname = f"💬 Chat user — Translated → {target}"
                    if total_pages > 1:
                        fname = f"{fname} ({page_idx}/{total_pages})"
                    embed.add_field(
                        name=fname,
                        value=(chunk or "(empty)"),
                        inline=False,
                    )
            else:
                # sama atau gagal terjemah; untuk kasus ini:
                # - jika sudah ada hasil gambar dan target adalah id, kita tidak perlu
                #   menampilkan blok Chat user lagi agar embed tetap ringkas.
                if not (image_any_ok and str(target).lower() == "id"):
                    value_lines = []
                    value_lines.append("**Source:**")
                    value_lines.append(src_preview)
                    value_lines.append("")
                    value_lines.append(f"_Teks sudah dalam bahasa target ({target}) atau tidak perlu diterjemahkan._")
                    msg = "\n".join(value_lines)
                    embed.add_field(
                        name="💬 Chat user",
                        value=(msg[:1024] or "(empty)"),
                        inline=False,
                    )
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