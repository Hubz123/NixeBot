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
from typing import Optional, Tuple

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

def _pretty_provider(tag: str) -> str:
    """Render provider tag like 'gemini' or 'groq' into a nice label."""
    t = (tag or '').lower()
    if 'gemini' in t:
        return 'Gemini'
    if 'groq' in t:
        return 'Groq'
    return tag or 'unknown'

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
        ok_cd, wait = self._cooldown_ok(interaction.user.id)
        if not ok_cd:
            await interaction.response.send_message(f"Cooldown. Try again in {wait:.1f}s.", ephemeral=True)
            return

        text = (message.content or "").strip()
        if not text:
            await interaction.response.send_message("No text found to translate in that message.", ephemeral=True)
            return

        max_chars = _as_int("TRANSLATE_MAX_CHARS", 1800)
        if len(text) > max_chars:
            text = text[:max_chars] + "…"

        target = _env("TRANSLATE_TARGET_LANG", "id")
        ok, out, prov = await self._do_translate(text, target)
        if not ok:
            await interaction.response.send_message(out, ephemeral=_translate_ephemeral())
            return

        embed = discord.Embed(title="Translation", description=out)
        embed.set_footer(text=f"Translated by {_pretty_provider(prov)} • target={target}")
        await interaction.response.send_message(embed=embed, ephemeral=_translate_ephemeral())
    @app_commands.describe(text="Text to translate", target_lang="Target language code (default from env)")
    async def translate_slash(self, interaction: discord.Interaction, text: str, target_lang: Optional[str] = None):
        if not _as_bool('TRANSLATE_ENABLE_SLASH', False):
            await interaction.response.send_message(
                "Slash /translate is disabled. Use Apps > Translate (message context menu).",
                ephemeral=True,
            )
            return

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

    # Register message context menu
    try:
        menu = app_commands.ContextMenu(
            name="Translate",
            callback=cog.translate_message_ctx,
        )
        if not any(cmd.name == "Translate" for cmd in bot.tree.get_commands(type=discord.AppCommandType.message)):
            bot.tree.add_command(menu)
    except Exception:
        log.debug("[translate] context menu registration skipped", exc_info=True)

    # Optional one-time sync if user enables it
    # Optional slash command registration (default OFF; prefer Apps > Translate)
    try:
        if _as_bool('TRANSLATE_ENABLE_SLASH', False):
            slash = app_commands.Command(
                name='translate',
                description='Translate a text using separate translate providers.',
                callback=cog.translate_slash,
            )
            if not any(cmd.name == 'translate' for cmd in bot.tree.get_commands(type=discord.AppCommandType.chat_input)):
                bot.tree.add_command(slash)
                log.warning('[translate] slash /translate registered')
    except Exception:
        log.debug('[translate] slash registration skipped', exc_info=True)

    try:
        if _env("TRANSLATE_SYNC_ON_BOOT", "0") == "1":
            await bot.tree.sync()
            log.warning("[translate] app commands synced on boot")
    except Exception:
        log.debug("[translate] tree.sync skipped", exc_info=True)