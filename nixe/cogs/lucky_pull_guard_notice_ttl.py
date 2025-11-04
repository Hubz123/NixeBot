
# -*- coding: utf-8 -*-
import os, io, asyncio, logging
from typing import Optional
import discord
from discord.ext import commands

# NOTE: This file is a minimal add-on that enables auto-delete for persona notice
# via LUCKYPULL_NOTICE_TTL / LPG_NOTICE_TTL_SEC, while keeping your existing cog logic.
try:
    from nixe.cogs.lucky_pull_guard import LuckyPullGuard as _Base
    from nixe.cogs.lucky_pull_guard import _pick_tone, pick_line
    BASE_OK = True
except Exception:
    BASE_OK = False

if not BASE_OK:
    class _Base(commands.Cog):
        pass
    def _pick_tone(score: float, tone_env: str) -> str:
        tone_env = (tone_env or "auto").lower()
        if tone_env in ("soft","agro","sharp"): return tone_env
        if score >= 0.95: return "sharp"
        if score >= 0.85: return "agro"
        return "soft"
    def pick_line(*a, **k): return "Konten dipindahkan ke channel yang benar."

def _env_int_any(*keys, default=0):
    for k in keys:
        v = os.getenv(k, None)
        if v is None:
            continue
        try:
            return int(str(v).strip())
        except Exception:
            continue
    return default


# --- reason resolver helpers (ENV -> runtime_env.json fallback) ---
def _runtime_path():
    import pathlib
    return pathlib.Path(__file__).resolve().parents[1] / "config" / "runtime_env.json"
def _json_get(key):
    try:
        import json, pathlib
        p = _runtime_path()
        return json.loads(p.read_text(encoding="utf-8")).get(key)
    except Exception:
        return None
def _env_str_any(*keys, default=""):
    import os
    for k in keys:
        v = os.getenv(k, None)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
        j = _json_get(k)
        if j is not None and str(j).strip() != "":
            return str(j).strip()
    return default

class LuckyPullGuard(_Base):  # type: ignore[misc]
    async def _persona_notify(self, message: discord.Message, score: float):
        # Build the same message as base, then delete after TTL if configured
        tone = _pick_tone(score, getattr(self, "persona_tone", "auto"))
        if getattr(self, "_persona_data", None):
            line = pick_line(getattr(self, "_persona_data"), getattr(self, "_persona_mode", "yandere"), tone)
        else:
            line = "Konten dipindahkan ke channel yang benar."

        channel_mention = f"<#{getattr(self, 'redirect_channel_id', 0)}>" if getattr(self, 'redirect_channel_id', 0) else f"#{message.channel.name}"
        user_mention = message.author.mention if getattr(self, "mention", True) else str(message.author)
        line = (line.replace("{user}", user_mention)
                    .replace("{reason}", _env_str_any("LPG_PERSONA_REASON","LUCKYPULL_PERSONA_REASON","LPA_PERSONA_REASON", default="Tebaran Garam"))
                    .replace("{user_name}", str(message.author))
                    .replace("{channel}", channel_mention)
                    .replace("{channel_name}", f"#{message.channel.name}"))

        sent = None
        try:
            sent = await message.channel.send(line, reference=message, mention_author=getattr(self, "mention", True))
        except Exception:
            try:
                sent = await message.channel.send(line)
            except Exception:
                sent = None

        ttl = _env_int_any("LUCKYPULL_NOTICE_TTL", "LPG_NOTICE_TTL_SEC", default=0)
        if sent and ttl > 0:
            try:
                await sent.delete(delay=ttl)
            except Exception:
                pass
