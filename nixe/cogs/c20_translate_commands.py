"""
c20_translate_commands.py

Add-only cog:
- Adds a Message Context Menu command "Translate" and a slash command /translate.
- Uses separate API keys so it does NOT touch existing LPG Gemini or Phish Groq keys.

Secrets (put in .env only):
  TRANSLATE_GEMINI_API_KEY=...
  TRANSLATE_GROQ_API_KEY=...

Optional configs (runtime_env.json or env):
  TRANSLATE_PROVIDER=gemini|groq   (default: gemini if key present else groq)
  TRANSLATE_TARGET_LANG=id        (default: id)
  TRANSLATE_GEMINI_MODEL=gemini-2.5-flash-lite
  TRANSLATE_GROQ_MODEL=llama-3.1-8b-instant
  TRANSLATE_MAX_CHARS=1800
  TRANSLATE_COOLDOWN_SEC=5
  TRANSLATE_SYNC_ON_BOOT=0        (set 1 once if you need to sync app commands)
  TRANSLATE_ALLOW_FALLBACK=0      (set 1 to allow fallback to GEMINI_API_KEY / GROQ_API_KEY)
"""

from __future__ import annotations

import os, json, logging, re, asyncio
from typing import Optional, Tuple, List, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

def _env(k: str, default: str = "") -> str:
    v = os.getenv(k)
    return v if v is not None and v != "" else default

def _as_int(k: str, default: int) -> int:
    try:
        return int(float(_env(k, str(default))))
    except Exception:
        return default


def _as_bool(k: str, default: bool=False) -> bool:
    v = _env(k, str(int(default))).strip().lower()
    if v in ('1','true','yes','on','enable','enabled'):
        return True
    if v in ('0','false','no','off','disable','disabled'):
        return False
    return default

def _translate_ephemeral() -> bool:
    """Whether to send translate responses ephemeral; default False (public)."""
    return _as_bool('TRANSLATE_EPHEMERAL', False)


def _translate_guild_ids() -> list[int]:
    """Parse guild IDs for fast per-guild sync (optional).

    Set TRANSLATE_GUILD_ID to a comma/space separated list of guild IDs to sync
    commands immediately into those guilds.
    """
    raw = _env('TRANSLATE_GUILD_ID', '').strip()
    if not raw:
        return [761163966030151701]
    parts = re.split(r'[ ,;]+', raw)
    gids: list[int] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        try:
            gids.append(int(p))
        except Exception:
            continue
    return gids

def _pretty_provider(tag: str) -> str:
    """Render provider tag like 'gemini' or 'groq' into a nice label."""
    t = (tag or '').lower()
    if 'gemini' in t:
        return 'Gemini'
    if 'groq' in t:
        return 'Groq'
    return tag or 'unknown'


def _split_chunks(text: str, max_chars: int) -> List[str]:
    """Split text into <=max_chars chunks, preserving paragraphs when possible."""
    text=(text or "").strip()
    if not text or max_chars<=0:
        return []
    if len(text)<=max_chars:
        return [text]
    paras=text.split("\n\n")
    chunks: List[str]=[]
    buf=""
    for p in paras:
        p=p.strip()
        if not p: 
            continue
        cand=(buf+"\n\n"+p) if buf else p
        if len(cand)<=max_chars:
            buf=cand
            continue
        if buf:
            chunks.append(buf); buf=""
        if len(p)>max_chars:
            for k in range(0,len(p),max_chars):
                chunks.append(p[k:k+max_chars])
        else:
            buf=p
    if buf: chunks.append(buf)
    return chunks


# JSON schema for image OCR+translation output (Gemini Vision).
IMAGE_TRANSLATE_SCHEMA = {
    "ok": True,
    "text": "<detected text from image>",
    "translation": "<translated text>",
    "source_lang": "<auto-detected source lang>",
    "target_lang": "<target>",
    "reason": "<short reason or notes>"
}

IMAGE_TRANSLATE_SYS_MSG = (
    "You are an OCR+translation engine. "
    "First read all visible text in the image accurately. "
    "Then translate it to the requested target language. "
    "Return ONLY compact JSON matching this schema: "
    + json.dumps(IMAGE_TRANSLATE_SCHEMA, ensure_ascii=False) + ". "
    "No markdown, no prose outside JSON."
)

def _as_float(k: str, default: float) -> float:
    try:
        return float(_env(k, str(default)))
    except Exception:
        return default

def _is_secret_key(k: str) -> bool:
    u = k.upper()
    return u.endswith("_TOKEN") or u.endswith("_API_KEY") or u.endswith("_SECRET")

def _pick_provider() -> str:
    p = _env("TRANSLATE_PROVIDER", "").lower().strip()
    if p in ("gemini", "groq"):
        return p
    # auto pick by available keys
    if _pick_gemini_key():
        return "gemini"
    if _pick_groq_key():
        return "groq"
    return "gemini"

def _pick_gemini_key() -> str:
    key = _env("TRANSLATE_GEMINI_API_KEY", "")
    if key:
        return key
    if _env("TRANSLATE_ALLOW_FALLBACK", "0") == "1":
        return _env("GEMINI_API_KEY", _env("GEMINI_API_KEY_B", _env("GEMINI_BACKUP_API_KEY", "")))
    return ""

def _pick_groq_key() -> str:
    key = _env("TRANSLATE_GROQ_API_KEY", "")
    if key:
        return key
    if _env("TRANSLATE_ALLOW_FALLBACK", "0") == "1":
        return _env("GROQ_API_KEY", "")
    return ""

def _clean_output(s: str) -> str:
    s = (s or "").strip()
    # strip code fences if present
    if s.startswith("```"):
        s = re.sub(r"^```\w*\n|```$", "", s, flags=re.S).strip()
    # strip surrounding quotes
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    return s

async def _translate_gemini(text: str, target_lang: str) -> Tuple[bool, str]:
    import aiohttp
    key = _pick_gemini_key()
    if not key:
        return False, "No TRANSLATE_GEMINI_API_KEY configured."
    model = _env("TRANSLATE_GEMINI_MODEL", _env("GEMINI_MODEL", "gemini-2.5-flash-lite"))
    prompt = (
        f"Translate the following text to {target_lang}. "
        "Return ONLY the translated text, no explanations, no quotes, no markdown.\n\n"
        f"TEXT:\n{text}"
    )
    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048}
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    timeout = aiohttp.ClientTimeout(total=_as_float("TRANSLATE_TIMEOUT_SEC", 12.0))
    try:
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
                return True, out or "(empty)"
    except Exception as e:
        return False, f"Gemini request failed: {e!r}"


async def _translate_image_gemini(image_bytes: bytes, target_lang: str) -> Tuple[bool, str, str, str]:
    """OCR + translate an image using Gemini Vision. Returns ok, detected_text, translated_text, reason."""
    try:
        from google import genai  # type: ignore
    except Exception as e:
        return False, "", "", f"gemini sdk missing: {e!r}"

    key = _env("TRANSLATE_GEMINI_API_KEY", "")
    if not key and _as_bool("TRANSLATE_ALLOW_FALLBACK", False):
        key = _env("GEMINI_API_KEY", "")
    if not key:
        return False, "", "", "missing TRANSLATE_GEMINI_API_KEY"

    model = _env("TRANSLATE_IMAGE_MODEL", _env("TRANSLATE_GEMINI_MODEL", "gemini-2.5-flash"))
    try:
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model=model,
            contents=[{
                "role": "user",
                "parts": [
                    {"text": IMAGE_TRANSLATE_SYS_MSG + f" Target language: {target_lang}."},
                    {"inline_data": {"mime_type": "image/png", "data": image_bytes}},
                ],
            }],
        )
        raw = _clean_output((resp.text or "").strip())
        try:
            data = json.loads(raw)
        except Exception:
            return True, raw, raw, "non_json_output"
        detected = str(data.get("text", "") or "")
        translated = str(data.get("translation", "") or "")
        reason = str(data.get("reason", "") or "ok")
        return True, detected, translated, reason
    except Exception as e:
        return False, "", "", f"gemini vision failed: {e!r}"

async def _translate_groq(text: str, target_lang: str) -> Tuple[bool, str]:
    key = _pick_groq_key()
    if not key:
        return False, "No TRANSLATE_GROQ_API_KEY configured."
    model = _env("TRANSLATE_GROQ_MODEL", _env("GROQ_MODEL_TEXT", _env("GROQ_MODEL", "llama-3.1-8b-instant")))
    try:
        from groq import Groq
    except Exception:
        Groq = None
    if Groq is None:
        return False, "Groq SDK not available in this environment."
    try:
        client = Groq(api_key=key)
        sys_msg = (
            f"You are a translation engine. Translate user text to {target_lang}. "
            "Output ONLY the translation, no commentary."
        )
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": text},
            ],
            temperature=0.2,
            max_tokens=2000,
        )
        out = resp.choices[0].message.content if resp.choices else ""
        out = _clean_output(out)
        return True, out or "(empty)"
    except Exception as e:
        return False, f"Groq request failed: {e!r}"

class TranslateCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_call = {}  # user_id -> monotonic seconds

    def _cooldown_ok(self, user_id: int) -> Tuple[bool, float]:
        cd = _as_float("TRANSLATE_COOLDOWN_SEC", 5.0)
        now = asyncio.get_event_loop().time()
        last = self._last_call.get(user_id, 0.0)
        if now - last < cd:
            return False, cd - (now - last)
        self._last_call[user_id] = now
        return True, 0.0

    async def _do_translate(self, text: str, target_lang: str) -> Tuple[bool, str, str]:
        provider = _pick_provider()
        if provider == "groq":
            ok, out = await _translate_groq(text, target_lang)
            return ok, out, "groq"
        ok, out = await _translate_gemini(text, target_lang)
        return ok, out, "gemini"


async def translate_message_ctx(self, interaction: discord.Interaction, message: discord.Message):
    ok_cd, wait_s = self._cooldown_ok(interaction.user.id)
    if not ok_cd:
        await interaction.response.send_message(
            f"Cooldown. Try again in {wait_s:.1f}s.",
            ephemeral=True
        )
        return

    # Collect text candidates from content and embeds (Twitter/YouTube previews).
    text = (message.content or "").strip()
    if not text and message.embeds:
        emb_list = [e for e in message.embeds if isinstance(e, discord.Embed)]
        text = _extract_text_from_embeds(emb_list)

    # If still no text, but there is an image attachment, OCR+translate the image.
    image_bytes: Optional[bytes] = None
    if not text and message.attachments:
        for a in message.attachments:
            fn = (a.filename or "").lower()
            if any(fn.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif")):
                try:
                    b = await a.read()
                    if b:
                        image_bytes = b
                        break
                except Exception:
                    continue

    target = _env("TRANSLATE_TARGET_LANG", "id").strip()

    # Image path
    if image_bytes is not None and not text:
        await interaction.response.defer(ephemeral=_translate_ephemeral(), thinking=True)
        ok, detected, translated, reason = await _translate_image_gemini(image_bytes, target)
        if not ok:
            await interaction.followup.send(translated or reason, ephemeral=_translate_ephemeral())
            return

        desc = translated or "(empty)"
        embed = discord.Embed(title="Translation (Image)", description=desc)
        if detected:
            embed.add_field(name="Detected Text", value=detected[:1024], inline=False)
        embed.set_footer(text=f"Translated by Gemini • target={target}")
        await interaction.followup.send(embed=embed, ephemeral=_translate_ephemeral())
        return

    # Text path
    text = (text or "").strip()
    if not text:
        await interaction.response.send_message("No text found to translate in that message.", ephemeral=True)
        return

    max_chars = _as_int("TRANSLATE_MAX_CHARS", 1800)
    chunks = _split_chunks(text, max_chars)
    if not chunks:
        await interaction.response.send_message("Text is empty.", ephemeral=True)
        return

    if len(chunks) > 1:
        await interaction.response.defer(ephemeral=_translate_ephemeral(), thinking=True)
        total = len(chunks)
        for idx, ch in enumerate(chunks, start=1):
            ok, out, prov = await self._do_translate(ch, target)
            if not ok:
                await interaction.followup.send(out, ephemeral=_translate_ephemeral())
                return
            embed = discord.Embed(title=f"Translation ({idx}/{total})", description=out)
            embed.set_footer(text=f"Translated by {_pretty_provider(prov)} • target={target}")
            await interaction.followup.send(embed=embed, ephemeral=_translate_ephemeral())
        return

    ok, out, prov = await self._do_translate(chunks[0], target)
    if not ok:
        await interaction.response.send_message(out, ephemeral=_translate_ephemeral())
        return

    embed = discord.Embed(title="Translation", description=out)
    embed.set_footer(text=f"Translated by {_pretty_provider(prov)} • target={target}")
    await interaction.response.send_message(embed=embed, ephemeral=_translate_ephemeral())
    async def translate_slash(self, interaction: discord.Interaction, text: str, target_lang: Optional[str] = None):
        ok_cd, wait = self._cooldown_ok(interaction.user.id)
        if not ok_cd:
            await interaction.response.send_message(f"Cooldown. Try again in {wait:.1f}s.", ephemeral=True)
            return

        text = (text or "").strip()
        if not text:
            await interaction.response.send_message("Text is empty.", ephemeral=True)
            return

        max_chars = _as_int("TRANSLATE_MAX_CHARS", 1800)
        if len(text) > max_chars:
            text = text[:max_chars] + "…"

        target = (target_lang or _env("TRANSLATE_TARGET_LANG", "id")).strip()
        ok, out, prov = await self._do_translate(text, target)
        if not ok:
            await interaction.response.send_message(out, ephemeral=_translate_ephemeral())
            return
        embed = discord.Embed(title="Translation", description=out)
        embed.set_footer(text=f"Translated by {_pretty_provider(prov)} • target={target}")
        await interaction.response.send_message(embed=embed, ephemeral=_translate_ephemeral())

async def setup(bot: commands.Bot):
    # Allow runtime/env toggle. Default enabled.
    _en = _env('TRANSLATE_ENABLE', '1').strip().lower()
    if _en in ('0','false','no','off','disable','disabled'):
        log.warning('[translate] disabled via TRANSLATE_ENABLE=0')
        return

    cog = TranslateCommands(bot)
    await bot.add_cog(cog)

    added_any = False

    # Clean up legacy/duplicate message context menus from previous versions of THIS bot.
    try:
        for cmd in list(bot.tree.get_commands(type=discord.AppCommandType.message)):
            if (cmd.name or '').lower().startswith('translate'):
                bot.tree.remove_command(cmd.name, type=discord.AppCommandType.message)
                added_any = True
    except Exception:
        pass

    # Register message context menu as GUILD-ONLY to avoid global collisions.
    gids_now = _translate_guild_ids()
    if gids_now:
        for gid in gids_now:
            menu = app_commands.ContextMenu(
                name="Translate (Nixe)",
                callback=cog.translate_message_ctx,
            )
            bot.tree.add_command(menu, guild=discord.Object(id=gid))
        added_any = True
    else:
        log.warning("[translate] No guild IDs configured for Translate (Nixe); command will not be registered.")

    async def _sync_later():
        try:
            # First sync global tree (Translate is not added globally) to clear legacy global commands.
            await bot.tree.sync()
        except Exception as e:
            log.warning(f"[translate] global sync failed: {e!r}")
        # Then sync selected guild(s).
        for gid in gids_now or []:
            try:
                await bot.tree.sync(guild=discord.Object(id=gid))
            except Exception as e:
                log.warning(f"[translate] guild sync failed gid={gid}: {e!r}")
        total_cmds = len(bot.tree.get_commands(type=discord.AppCommandType.message))
        log.warning(f"[translate] app commands guild-synced into {len(gids_now or [])} guild(s) (total={total_cmds})")

    force_sync = bool(gids_now)
    if added_any or force_sync or _env("TRANSLATE_SYNC_ON_BOOT", "0") == "1":
        bot.loop.create_task(_sync_later())
