from __future__ import annotations
import asyncio, os
import discord
from discord.ext import commands
from nixe.helpers import lpg_memory as LPM

TITLE = "📌 Lucky Pull Memory Board"
COLOR = 0xB249F8

async def _ensure_board(chan: discord.TextChannel):
    pins = await chan.pins()
    for m in pins:
        if m.embeds and (m.embeds[0].title or "").startswith(TITLE):
            return m
    msg = await chan.send(embed=discord.Embed(title=TITLE, description="Preparing...", color=COLOR))
    try: await msg.pin()
    except Exception: pass
    return msg

def _render(items):
    total = len(items)
    desc = [f"Total patterns: **{total}**", ""]
    tail = items[-60:]
    if not tail:
        desc.append("_No entries yet._")
    else:
        desc.append("**Recent fingerprints:**")
        line = []
        for i, x in enumerate(tail, 1):
            line.append(f"`{x[:10]}…`")
            if i % 3 == 0:
                desc.append(" • " + " | ".join(line)); line = []
        if line: desc.append(" • " + " | ".join(line))
    return "\n".join(desc)

class LpgMemoryBoard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._task = None
        self._started = False
    @commands.Cog.listener()
    async def on_ready(self):
        if getattr(self, '_started', False):
            return
        self._started = True
        try:
            self._task = asyncio.create_task(self._boot())
        except Exception:
            self._task = None


    async def _boot(self):
        await self.bot.wait_until_ready()
        await LPM.load()
        chan_id = int(os.getenv("LOG_CHANNEL_ID") or os.getenv("NIXE_PHISH_LOG_CHAN_ID") or "1431178130155896882")
        chan = self.bot.get_channel(chan_id)
        if not isinstance(chan, discord.TextChannel):
            return
        self.msg = await _ensure_board(chan)
        await self.update()

    async def update(self):
        await LPM.load()
        e = discord.Embed(title=TITLE, description=_render(LPM.S.items), color=COLOR)
        try:
            await self.msg.edit(embed=e)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_lpg_memory_changed(self):
        try:
            await self.update()
        except Exception:
            pass

async def setup(bot):
    await bot.add_cog(LpgMemoryBoard(bot))
