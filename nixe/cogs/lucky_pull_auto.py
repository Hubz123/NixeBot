
import os
import asyncio
import logging
import re
import json
import pathlib
import discord
from discord.ext import commands

_log = logging.getLogger(__name__)

# ---- Hybrid config (no format change) ----
_CFG = None
_CFG_MTIME = None
def _cfg_path():
    return pathlib.Path(__file__).resolve().parents[1] / "config" / "runtime_env.json"

def _load_cfg():
    global _CFG, _CFG_MTIME
    p = _cfg_path()
    try:
        mt = p.stat().st_mtime
    except Exception:
        mt = None
    if _CFG is None or mt != _CFG_MTIME:
        try:
            _CFG = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            _CFG = {}
        _CFG_MTIME = mt
    return _CFG

def _env(k: str, d: str|None=None) -> str:
    cfg = _load_cfg()
    v = cfg.get(k, None)
    if v is None or str(v) == "":
        v = os.getenv(k, None)
    return str(v) if v not in (None, "") else (d or "")

def _parse_id_list(val: str) -> set[int]:
    ids: set[int] = set()
    for tok in re.split(r"[\s,]+", str(val or "")):
        if not tok: continue
        try: ids.add(int(tok))
        except Exception: pass
    return ids

# ---- Gemini helper (no Groq here) ----
try:
    from nixe.helpers.lp_gemini_helper import is_lucky_pull, is_gemini_enabled
except Exception:
    def is_gemini_enabled() -> bool: return False
    def is_lucky_pull(image_bytes: bytes, threshold: float = 0.65): return (False, 0.0, "helper_missing")

class LuckyPullAuto(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = True
        self.thr = float(_env("GEMINI_LUCKY_THRESHOLD", "0.75"))
        raw = (_env("LPA_GUARD_CHANNELS","") or _env("LUCKYPULL_GUARD_CHANNELS","")
               or _env("LPG_GUARD_CHANNELS","") or _env("GUARD_CHANNELS",""))
        self.guard_channels = _parse_id_list(raw)

    # Parent-aware guard test
    def _is_guard_channel(self, channel):
        try:
            if not channel: return False
            cid = int(getattr(channel, "id", 0) or 0)
            if cid in self.guard_channels: return True
            pid = int(getattr(getattr(channel, "parent", None), "id", 0) or getattr(channel, "parent_id", 0) or 0)
            return pid in self.guard_channels
        except Exception:
            return False

    async def _first_image_bytes(self, message: discord.Message) -> bytes:
        try:
            for att in getattr(message, 'attachments', []) or []:
                name = (getattr(att, 'filename', '') or '').lower()
                ctype = getattr(att, 'content_type', '') or ''
                if ctype.startswith('image/') or name.endswith(('.png','.jpg','.jpeg','.webp','.gif')):
                    try: return await att.read()
                    except Exception: pass
        except Exception:
            pass
        return b''

    async def _handle_lucky_pull(self, message: discord.Message) -> bool:
        # Gemini-only classification
        if not is_gemini_enabled():
            return False
        img = await self._first_image_bytes(message)
        if not img or len(img) < 4096:
            return False
        ok, score, _ = is_lucky_pull(img, threshold=self.thr)
        if not ok:
            return False
        # delete offending message
        try: await message.delete()
        except Exception: pass
        # wait persona ready then notify
        cg = self.bot.get_cog('LuckyPullGuard')
        if not cg:
            for _ in range(10):  # ~3s
                await asyncio.sleep(0.3)
                cg = self.bot.get_cog('LuckyPullGuard')
                if cg: break
        if cg and hasattr(cg, '_persona_notify'):
            try:
                await cg._persona_notify(message, score)  # type: ignore
                return True
            except Exception:
                pass
        return False

    @commands.Cog.listener("on_message")
    async def on_message(self, m: discord.Message):
        if not self.enabled or getattr(m.author, "bot", False) or not getattr(m, "guild", None):
            return
        ch = getattr(m, "channel", None)
        if not self._is_guard_channel(ch):
            return
        # Gemini-only Lucky Pull
        try:
            handled = await self._handle_lucky_pull(m)
            if handled: return
        except Exception:
            pass
        # If you keep an 'on_message_inner' in overlays, still delegate
        if hasattr(self, 'on_message_inner'):
            try: await self.on_message_inner(m)  # type: ignore
            except Exception as e: _log.exception("[lpa] on_message_inner failed: %s", e)

    @commands.Cog.listener("on_ready")
    async def _on_ready(self):
        _log.info("[lpa] re-assert INFO after ready")

async def setup(bot: commands.Bot):
    await bot.add_cog(LuckyPullAuto(bot))
