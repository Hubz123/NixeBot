"""
c23_poetry_freeform_commands.py

Free-form "Nixe" chat commands (no /, no Apps, no &):
  - "nixe puisi ..."
  - "nixe pantun ..."
  - "nixe caption ..."

Design goal (per user):
- Only the leading command is fixed (nixe puisi|pantun|caption).
- Everything after it is free-form and passed to the LLM as the user's instruction.
- Embed output format mirrors Translate (Nixe) style: request + result + source (best-effort).

Secrets (.env only):
  POETRY_GROQ_API_KEY=...

Optional configs (runtime_env.json or env):
  POETRY_ENABLE=1
  POETRY_GROQ_MODEL=meta-llama/llama-4-scout-17b-16e-instruct
  POETRY_COOLDOWN_SEC=6
  POETRY_MAX_OUTPUT_CHARS=3000
"""

from __future__ import annotations

import os
import re
import json
import time
import logging
from typing import Any, Dict, Optional, Tuple, List

import discord
from discord.ext import commands

try:
    from groq import Groq  # type: ignore
except Exception:
    Groq = None

log = logging.getLogger(__name__)

# -------------------------
# helpers
# -------------------------

def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)

def _as_bool(key: str, default: bool = False) -> bool:
    v = _env(key, "1" if default else "0").strip().lower()
    return v in ("1", "true", "yes", "y", "on")

def _as_int(key: str, default: int = 0) -> int:
    try:
        return int(_env(key, str(default)))
    except Exception:
        return default

def _clean_output(s: str) -> str:
    s = (s or "").strip()
    # strip code fences if model adds them
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    return s.strip()

def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """
    Best-effort JSON extraction:
    - accept pure JSON
    - accept JSON inside fences
    - accept JSON embedded in prose (first {...} block)
    """
    t = (text or "").strip()
    if not t:
        return None

    # Strip fences
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()

    # Pure JSON
    try:
        if t.startswith("{") and t.endswith("}"):
            return json.loads(t)
    except Exception:
        pass

    # Find first object block
    m = re.search(r"\{[\s\S]*\}", t)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

def _groq_client() -> Optional[Any]:
    key = _env("POETRY_GROQ_API_KEY", "").strip()
    if not key or Groq is None:
        return None
    try:
        return Groq(api_key=key)
    except Exception:
        return None

def _split_for_embed(text: str, limit: int = 1024) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for line in text.splitlines():
        if not line:
            cand = (buf + "\n").strip() + "\n"
            if len((buf + "\n").strip()) <= limit:
                buf = (buf + "\n").strip()
            else:
                # force cut
                while buf:
                    out.append(buf[:limit])
                    buf = buf[limit:]
                buf = ""
            continue
        cand = (buf + ("\n" if buf else "") + line)
        if len(cand) <= limit:
            buf = cand
        else:
            if buf:
                out.append(buf[:limit])
                buf = ""
            # line may be huge
            while len(line) > limit:
                out.append(line[:limit])
                line = line[limit:]
            buf = line
    if buf:
        out.append(buf[:limit])
    return out

# -------------------------
# prompt builder
# -------------------------

_SCHEMA = {
    "mode": "puisi|pantun|caption",
    "title": "string",
    "output": "string (main result)",
    "alts": [
        {"label": "string (e.g., Romaji / Casual / Formal / Pinyin)", "text": "string"}
    ],
    "lang": "string (best-effort code/name, e.g. ja/ko/zh-cn/id/en)",
    "notes": "string (very short, optional)",
}

_SYS = (
    "You are Nixe, a Discord assistant that writes creative text (poems, pantun, captions). "
    "The user will start the message with: 'nixe puisi' or 'nixe pantun' or 'nixe caption'. "
    "Everything AFTER that is free-form instruction; do not assume a rigid syntax. "
    "Follow the user's requested language (e.g. jp/ja/japanese, kr/ko/korean, cn/zh/mandarin, id/indonesia, en/english) "
    "and requested politeness/style (casual/polite/formal/keigo/banmal/haeyo) and romanization (romaji/romanize/pinyin) if mentioned. "
    "If the user asks for JP/KR/CN and asks for romanization, include it as an alt block. "
    "If the user asks for both formal and casual, provide both as separate alt blocks. "
    "Keep the output safe (no hate/harassment, no sexual explicit content, no instructions for wrongdoing). "
    "Return ONLY compact JSON that matches this schema exactly:\n"
    f"{json.dumps(_SCHEMA, ensure_ascii=False)}\n"
    "No prose, no markdown outside JSON."
)

def _make_user_prompt(mode: str, free_instruction: str) -> str:
    free_instruction = (free_instruction or "").strip()
    return (
        f"MODE: {mode}\n"
        "INSTRUCTION (free-form):\n"
        f"{free_instruction}\n"
    )

# -------------------------
# Cog
# -------------------------

class PoetryFreeformCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_by_user: Dict[int, float] = {}

    def _cooldown_ok(self, uid: int) -> Tuple[bool, int]:
        cd = _as_int("POETRY_COOLDOWN_SEC", 6)
        if cd <= 0:
            return True, 0
        now = time.time()
        last = self._last_by_user.get(uid, 0.0)
        if now - last < cd:
            return False, int(cd - (now - last))
        self._last_by_user[uid] = now
        return True, 0

    async def _call_groq(self, mode: str, free_instruction: str) -> Tuple[bool, str, str]:
        client = _groq_client()
        if client is None:
            return False, "", "missing POETRY_GROQ_API_KEY or groq lib not available"

        model = _env("POETRY_GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct").strip() or "meta-llama/llama-4-scout-17b-16e-instruct"
        max_chars = _as_int("POETRY_MAX_OUTPUT_CHARS", 3000)

        user_msg = _make_user_prompt(mode, free_instruction)

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYS},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.9,
            )
            raw = (resp.choices[0].message.content or "").strip()
            raw = _clean_output(raw)
            if not raw:
                return False, "", "empty_response"
            # limit payload length (defensive)
            if len(raw) > 20000:
                raw = raw[:20000]
            data = _extract_json(raw)
            if not data:
                # fallback: treat raw as output string
                out = raw[:max_chars]
                return True, out, f"groq:{model}"
            # compose unified output for embed building
            main_out = _clean_output(str(data.get("output", "")))
            title = _clean_output(str(data.get("title", ""))) or mode.title()
            lang = _clean_output(str(data.get("lang", "")))
            notes = _clean_output(str(data.get("notes", "")))

            alts = data.get("alts", []) if isinstance(data.get("alts", []), list) else []
            # Build a plain-text package for downstream embed fields.
            blocks = []
            if main_out:
                blocks.append(main_out)
            # We'll keep alt blocks separate for embed fields (not inline).
            packaged = json.dumps(
                {
                    "title": title,
                    "lang": lang,
                    "notes": notes,
                    "output": main_out,
                    "alts": alts,
                },
                ensure_ascii=False,
            )
            if len(packaged) > 20000:
                packaged = packaged[:20000]
            return True, packaged, f"groq:{model}"
        except Exception as e:
            return False, "", f"groq_error:{type(e).__name__}"

    def _build_embed(self, mode: str, request: str, packed: str, provider: str, author: discord.abc.User) -> discord.Embed:
        """
        Embed layout mirrors Translate (Nixe) style:
        - title
        - fields: Request, Result (+ optional alt fields)
        - footer: provider + mode
        """
        title = f"📝 {mode.title()} (Nixe)"
        embed = discord.Embed(title=title, description="", color=0x2F3136)

        # Request field
        req_chunks = _split_for_embed(request, 1024)
        if req_chunks:
            embed.add_field(name="📌 Permintaan", value=req_chunks[0], inline=False)
            for i, ch in enumerate(req_chunks[1:], start=2):
                embed.add_field(name=f"📌 Permintaan (lanjutan {i})", value=ch, inline=False)

        # Packed may be JSON or plain text
        data = _extract_json(packed)
        if data and "output" in data:
            out = _clean_output(str(data.get("output", "")))
            out_chunks = _split_for_embed(out, 1024)
            if out_chunks:
                embed.add_field(name="✨ Hasil", value=out_chunks[0], inline=False)
                for i, ch in enumerate(out_chunks[1:], start=2):
                    embed.add_field(name=f"✨ Hasil (lanjutan {i})", value=ch, inline=False)
            else:
                embed.add_field(name="✨ Hasil", value="(kosong)", inline=False)

            # Alt blocks
            alts = data.get("alts", [])
            if isinstance(alts, list):
                for alt in alts[:6]:  # cap to avoid spam
                    if not isinstance(alt, dict):
                        continue
                    label = _clean_output(str(alt.get("label", ""))) or "Varian"
                    txt = _clean_output(str(alt.get("text", "")))
                    if not txt:
                        continue
                    chunks = _split_for_embed(txt, 1024)
                    if not chunks:
                        continue
                    embed.add_field(name=f"🔁 {label}", value=chunks[0], inline=False)
                    for i, ch in enumerate(chunks[1:], start=2):
                        embed.add_field(name=f"🔁 {label} (lanjutan {i})", value=ch, inline=False)

            # Notes
            notes = _clean_output(str(data.get("notes", "")))
            if notes:
                embed.add_field(name="🧾 Catatan", value=notes[:1024], inline=False)

            lang = _clean_output(str(data.get("lang", "")))
            if lang:
                embed.add_field(name="🌐 Bahasa", value=lang[:1024], inline=False)
        else:
            # plain
            out_chunks = _split_for_embed(packed, 1024)
            if out_chunks:
                embed.add_field(name="✨ Hasil", value=out_chunks[0], inline=False)
                for i, ch in enumerate(out_chunks[1:], start=2):
                    embed.add_field(name=f"✨ Hasil (lanjutan {i})", value=ch, inline=False)
            else:
                embed.add_field(name="✨ Hasil", value="(kosong)", inline=False)

        embed.set_footer(text=f"{provider} • mode={mode}")
        try:
            embed.set_author(name=str(author), icon_url=getattr(author.display_avatar, "url", None))
        except Exception:
            pass
        return embed

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # avoid loops
        if message.author.bot:
            return
        if not message.guild:
            return
        if not _as_bool("POETRY_ENABLE", True):
            return

        content = (message.content or "").strip()
        if not content:
            return

        # Match: nixe (puisi|pantun|caption) <free...>
        m = re.match(r"(?is)^\s*nixe\s+(puisi|pantun|caption)\b(.*)$", content)
        if not m:
            return

        mode = (m.group(1) or "").strip().lower()
        free = (m.group(2) or "").lstrip()

        # Append remaining lines (if any) untouched
        lines = content.splitlines()
        if len(lines) > 1:
            # remove first line prefix portion already captured; keep rest as-is
            free = (free + "\n" + "\n".join(lines[1:])).strip()

        # If free is empty, try reply content
        if not free:
            ref = message.reference
            if ref and isinstance(ref.resolved, discord.Message):
                ref_msg = ref.resolved
                free = (ref_msg.content or "").strip()

        if not free:
            await message.channel.send(
                "Teks kosong. Tulis setelah command, atau reply pesan lalu ketik: `nixe puisi` / `nixe pantun` / `nixe caption`.",
                reference=message,
            )
            return

        # Cooldown (silent if hit)
        ok_cd, _wait = self._cooldown_ok(message.author.id)
        if not ok_cd:
            return

        ok, packed, provider = await self._call_groq(mode, free)
        if not ok:
            await message.channel.send(
                f"Gagal membuat {mode}. ({provider})",
                reference=message,
            )
            return

        emb = self._build_embed(mode, free, packed, provider, message.author)
        await message.channel.send(embed=emb, reference=message)


async def setup(bot: commands.Bot):
    await bot.add_cog(PoetryFreeformCommands(bot))
