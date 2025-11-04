# -*- coding: utf-8 -*-
import os
import logging
from typing import Set

try:
    import discord
except Exception:  # allow smoke to run without discord.py
    class _Dummy: ...
    class Message: ...
    class abc:
        class GuildChannel: ...
    discord = _Dummy()
    discord.Message = Message
    discord.abc = abc  # type: ignore

# Persona helpers
try:
    from nixe.helpers.persona_loader import load_persona, pick_line
except Exception:  # fallback for smoke
    def load_persona():
        return ("yandere", {"yandere":{"soft":["..."],"agro":["..."],"sharp":["..."]}}, None)
    def pick_line(data, mode, tone, **kwargs):
        return (data.get(mode, {}) or {}).get(tone, ["..."])[0]

log = logging.getLogger(__name__)

# --- HYBRID JSON RUNTIME CONFIG (ENV -> runtime_env.json fallback) ---
_CFG = None
_CFG_MTIME = None

def _runtime_path():
    import pathlib
    return pathlib.Path(__file__).resolve().parents[1] / "config" / "runtime_env.json"

def _load_cfg():
    global _CFG, _CFG_MTIME
    p = _runtime_path()
    try:
        mt = p.stat().st_mtime
    except Exception:
        mt = None
    if _CFG is not None and _CFG_MTIME == mt:
        return _CFG
    try:
        import json
        _CFG = json.loads(p.read_text(encoding="utf-8"))
        _CFG_MTIME = mt
    except Exception:
        _CFG = {}
        _CFG_MTIME = mt
    return _CFG

def _json_get(key: str):
    try:
        return (_load_cfg() or {}).get(key)
    except Exception:
        return None

def _env_str_any(*keys, default: str = "") -> str:
    # first non-empty (and not "0") from ENV then JSON
    for k in keys:
        v = os.getenv(k, None)
        if v is not None and str(v).strip() not in ("", "0"):
            return str(v).strip()
        j = _json_get(k)
        if j is not None and str(j).strip() not in ("", "0"):
            return str(j).strip()
    return default

def _env_int_any(*keys, default: int = 0) -> int:
    for k in keys:
        v = os.getenv(k, None)
        if v is not None and str(v).strip() not in ("", "0"):
            try:
                return int(str(v).strip())
            except Exception:
                pass
        j = _json_get(k)
        if j is not None and str(j).strip() not in ("", "0"):
            try:
                return int(str(j).strip())
            except Exception:
                pass
    return default

def _parse_id_list(s: str) -> Set[int]:
    out: Set[int] = set()
    for tok in str(s or "").split(","):
        tok = tok.strip().strip('"').strip("'")
        if not tok:
            continue
        try:
            out.add(int(tok))
        except Exception:
            pass
    return out

def _pick_tone(score: float, persona_tone: str) -> str:
    t = (persona_tone or "auto").lower().strip()
    if t in ("soft","agro","sharp"):
        return t
    try:
        sc = float(score)
    except Exception:
        sc = 0.0
    if sc >= 0.90:
        return "sharp"
    if sc >= 0.80:
        return "agro"
    return "soft"

def _resolve_reason() -> str:
    # default reason: "Tebaran Garam"
    return _env_str_any("LPG_PERSONA_REASON","LUCKYPULL_PERSONA_REASON","LPA_PERSONA_REASON", default="Tebaran Garam")

class LuckyPullGuard:
    def __init__(self, bot):
        self.bot = bot
        self.mention = True
        self.persona_mode = "yandere"
        self.persona_tone = os.getenv("LPG_PERSONA_TONE", "auto")

        # persona
        try:
            mode, data, _ = load_persona()
            self._persona_mode = mode or self.persona_mode
            self._persona_data = data or {}
        except Exception:
            self._persona_mode = self.persona_mode
            self._persona_data = {"yandere":{"soft":["..."],"agro":["..."],"sharp":["..."]}}

        # guard & redirect
        raw_guards = _env_str_any("LPA_GUARD_CHANNELS","LUCKYPULL_GUARD_CHANNELS","LPG_GUARD_CHANNELS","GUARD_CHANNELS", default="")
        self.guard_channels = _parse_id_list(raw_guards)
        self.redirect_channel_id = _env_int_any("LPA_REDIRECT_CHANNEL_ID","LUCKYPULL_REDIRECT_CHANNEL_ID","LPG_REDIRECT_CHANNEL_ID", default=0)

    def _is_guard_channel(self, channel: 'discord.abc.GuildChannel') -> bool:
        try:
            if not channel:
                return False
            cid = int(getattr(channel, "id", 0) or 0)
            if cid in self.guard_channels:
                return True
            pid = int(getattr(getattr(channel, "parent", None), "id", 0) or getattr(channel, "parent_id", 0) or 0)
            return pid in self.guard_channels
        except Exception:
            return False

    async def _persona_notify(self, message: 'discord.Message', score: float):
        tone = _pick_tone(score, self.persona_tone)
        if pick_line and self._persona_data:
            line = pick_line(self._persona_data, self._persona_mode or self.persona_mode, tone)
        else:
            line = "Konten dipindahkan ke channel yang benar."
        raw_line = line

        ch = getattr(message, "channel", None)
        ch_name = getattr(ch, "name", "channel")
        redirect_mention = f"<#{self.redirect_channel_id}>" if self.redirect_channel_id else f"#{ch_name}"
        user_mention = getattr(getattr(message,'author',None),'mention', '@user') if self.mention else str(getattr(message,'author','user'))
        author_name = str(getattr(message,'author','user'))

        # fill placeholders (single-mention policy)
        line_fmt = raw_line
        if "{user}" in line_fmt:
            line_fmt = line_fmt.replace("{user}", user_mention, 1)
            line_fmt = line_fmt.replace("{user}", author_name)
        line_fmt = line_fmt.replace("{user_name}", author_name)
        line_fmt = (line_fmt
                    .replace("{channel}", redirect_mention)
                    .replace("{channel_name}", f"#{ch_name}")
                    .replace("{reason}", _resolve_reason()))

        # prefix mention only if template doesn't already include user placeholder
        prefix = "" if ("{user}" in raw_line or "{user_name}" in raw_line) else f"{user_mention} "
        # conditional footer to avoid double channel mention
        footer = ""
        if ("{channel}" not in raw_line) and ("{channel_name}" not in raw_line) and (redirect_mention not in line_fmt):
            footer = "\n→ silakan post Lucky Pull di " + redirect_mention + "."
        text = f"{prefix}{line_fmt}{footer}"
        try:
            await message.channel.send(text, reference=message, mention_author=self.mention)  # type: ignore
        except Exception:
            try:
                await message.channel.send(text)  # type: ignore
            except Exception:
                pass

# optional setup() for discord.ext
async def setup(bot):
    try:
        await bot.add_cog(LuckyPullGuard(bot))
    except Exception:
        pass
