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
    pv = _env("TRANSLATE_PROVIDER", "").strip().lower()
    if pv in ("gemini", "groq"):
        return pv
    if _pick_gemini_key():
        return "gemini"
    if _pick_groq_key():
        return "groq"
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
    sys_msg = _env(
        "TRANSLATE_SYS_MSG",
        f"You are a translation engine. Translate user text to {target_lang}. "
        f"Return ONLY compact JSON matching this schema: {schema}. No prose."
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": sys_msg + "\n\nTEXT:\n" + text}]}
        ],
        "generationConfig": {"temperature": 0.2},
    }

    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=_as_float("TRANSLATE_TIMEOUT_SEC", 12.0))
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json=payload) as resp:
                raw = await resp.text()
                if resp.status >= 400:
                    return False, f"Gemini error {resp.status}: {raw[:200]}"
                data = json.loads(raw)
                cand = (data.get("candidates") or [{}])[0]
                parts = ((cand.get("content") or {}).get("parts") or [])
                out = ""
                for p in parts:
                    if "text" in p:
                        out += p["text"]
                out = _clean_output(out)
                # accept JSON or plain text fallback
                try:
                    j = json.loads(out)
                    out2 = str(j.get("translation", "") or out)
                    return True, out2.strip() or "(empty)"
                except Exception:
                    return True, out or "(empty)"
    except Exception as e:
        return False, f"Gemini request failed: {e!r}"

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

                if _as_bool("TRANSLATE_SLASH_ENABLE", True):
                    try:
                        self.bot.tree.add_command(self.translate_slash, guild=gobj)
                    except Exception:
                        pass

            # sync once global to flush legacy, then per guild
            if _as_bool("TRANSLATE_SYNC_ON_BOOT", True):
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

        target = _env("TRANSLATE_TARGET_LANG", "id").strip() or "id"

        # image path
        image_bytes: Optional[bytes] = None
        if message.attachments:
            att = message.attachments[0]
            fn = (att.filename or "").lower()
            if any(fn.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif")):
                try:
                    image_bytes = await att.read()
                except Exception:
                    image_bytes = None

        if image_bytes:
            ok, detected, translated, reason = await _translate_image_gemini(image_bytes, target)
            if not ok:
                await interaction.followup.send(reason, ephemeral=ephemeral)
                return
            emb = discord.Embed(title="Translation (Image)")
            if detected:
                emb.add_field(name="Detected Text", value=detected[:1024], inline=False)
            emb.add_field(name=f"Translation → {target}", value=translated[:1024] or "(empty)", inline=False)
            emb.set_footer(text=f"Translated by Gemini • target={target} • {reason}")
            await interaction.followup.send(embed=emb, ephemeral=ephemeral)
            return

        # text / embeds
        raw_text = (message.content or "").strip()
        text = raw_text
        if message.embeds and (not raw_text or _looks_like_only_urls(raw_text)):
            emb_list = [e for e in message.embeds if isinstance(e, discord.Embed)]
            emb_text = _extract_text_from_embeds(emb_list)
            if emb_text:
                text = emb_text

        if not text:
            await interaction.followup.send("No text to translate.", ephemeral=ephemeral)
            return

        provider = _pick_provider()
        max_chars = int(_as_float("TRANSLATE_MAX_CHARS", 1800))
        chunks = _chunk_text(text, max_chars)

        for idx, ch in enumerate(chunks, 1):
            if provider == "groq":
                ok, out = await _groq_translate_text(ch, target)
            else:
                ok, out = await _gemini_translate_text(ch, target)

            if not ok:
                await interaction.followup.send(out, ephemeral=ephemeral)
                return

            emb = discord.Embed(title="Translation")
            emb.description = out[:4096]
            if len(chunks) > 1:
                emb.set_footer(text=f"Part {idx}/{len(chunks)} • Translated by {provider} • target={target}")
            else:
                emb.set_footer(text=f"Translated by {provider} • target={target}")
            await interaction.followup.send(embed=emb, ephemeral=ephemeral)

    @app_commands.command(name="translate", description="Translate text to target language")
    @app_commands.describe(text="Text to translate", target="Target language code, e.g. id, en, ja")
    async def translate_slash(self, interaction: discord.Interaction, text: str, target: Optional[str] = None):
        if not _as_bool("TRANSLATE_ENABLE", True):
            await interaction.response.send_message("Translate is disabled.", ephemeral=True)
            return

        ok_cd, wait_s = self._cooldown_ok(interaction.user.id)
        if not ok_cd:
            await interaction.response.send_message(f"Cooldown. Try again in {wait_s:.1f}s.", ephemeral=True)
            return

        ephemeral = _as_bool("TRANSLATE_EPHEMERAL", False)
        await interaction.response.defer(thinking=True, ephemeral=ephemeral)

        tgt = (target or _env("TRANSLATE_TARGET_LANG", "id")).strip() or "id"
        provider = _pick_provider()
        max_chars = int(_as_float("TRANSLATE_MAX_CHARS", 1800))
        chunks = _chunk_text(text, max_chars)

        for idx, ch in enumerate(chunks, 1):
            if provider == "groq":
                ok, out = await _groq_translate_text(ch, tgt)
            else:
                ok, out = await _gemini_translate_text(ch, tgt)

            if not ok:
                await interaction.followup.send(out, ephemeral=ephemeral)
                return

            emb = discord.Embed(title="Translation")
            emb.description = out[:4096]
            if len(chunks) > 1:
                emb.set_footer(text=f"Part {idx}/{len(chunks)} • Translated by {provider} • target={tgt}")
            else:
                emb.set_footer(text=f"Translated by {provider} • target={tgt}")
            await interaction.followup.send(embed=emb, ephemeral=ephemeral)


async def setup(bot: commands.Bot):
    cog = TranslateCommands(bot)
    await bot.add_cog(cog)
    # register after ready
    bot.loop.create_task(cog._ensure_registered())