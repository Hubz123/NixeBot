# -*- coding: utf-8 -*-
"""
a00_lpg_suppress_double_notice_overlay (fixed)
----------------------------------------------
Menghapus *notice generic* Lucky Pull yang dobel—termasuk bila kalimat yang sama
muncul dari persona—sehingga yang tersisa hanya pesan persona (yandere.json).

Aktif jika LPG_SUPPRESS_GENERIC_NOTICE=1 (default: 1).
Compat: discord.py 2.x (async setup).
"""
import os
import logging
from discord.ext import commands

PHRASES = (
    "kontenmu melenceng dari tema. sudah dihapus. gunakan kanal yang tepat.",
)

class LpgSuppressDoubleNotice(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.enabled = os.getenv("LPG_SUPPRESS_GENERIC_NOTICE", "1") == "1"
        self.log = logging.getLogger("nixe.cogs.a00_lpg_suppress_double_notice_overlay")
        if self.enabled:
            self.log.warning("[lpg-suppress] enabled (generic Lucky Pull notice will be removed)")

    @commands.Cog.listener()
    async def on_message(self, message):
        # Hanya kalau aktif
        if not self.enabled:
            return
        # Hanya pesan dari bot ini sendiri
        if not message or not getattr(message, "author", None):
            return
        if not self.bot.user or message.author.id != self.bot.user.id:
            return

        content = (message.content or "").lower().strip()
        # Deteksi frasa generic
        if any(phrase in content for phrase in PHRASES):
            # Safe channels: env set or parent as fallback
            safe_ids = set()
            val = os.getenv('LPG_SAFE_CHANNEL_IDS') or ''
            if val.strip():
                try:
                    safe_ids = {int(x) for x in val.split(',') if x.strip()}
                except Exception:
                    safe_ids = set()
            if not safe_ids:
                try:
                    pid = int(os.getenv('LPG_PARENT_CHANNEL_ID','0') or '0')
                    if pid > 0:
                        safe_ids.add(pid)
                except Exception:
                    pass
            try:
                if getattr(message.channel, 'id', 0) in safe_ids:
                    return
            except Exception:
                pass
            # If active persona is yandere, do NOT delete persona output
            if (os.getenv('PERSONA_NAME','').lower().find('yandere') >= 0) or (os.getenv('PERSONA_PROFILE','').lower().find('yandere') >= 0):
                return
            try:
                await message.delete()
                self.log.info("[lpg-suppress] deleted generic notice to avoid double message")
            except Exception as e:
                self.log.warning("[lpg-suppress] failed to delete generic notice: %r", e)

async def setup(bot):
    # discord.py 2.x requires async setup and awaiting add_cog
    await bot.add_cog(LpgSuppressDoubleNotice(bot))
