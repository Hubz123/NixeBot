"""
Hardened Lucky Pull Auto (NO-OP)
--------------------------------
- Prevents double persona by suppressing the generic Lucky Pull notice.
- If you ever want the generic notice back, set env LPG_ALLOW_GENERIC_NOTICE=1.
- No changes to runtime_env.json are required.
"""
import os
import logging
from discord.ext import commands

ALLOW_GENERIC = os.getenv("LPG_ALLOW_GENERIC_NOTICE", "0") == "1"
LOG = logging.getLogger("nixe.cogs.lucky_pull_auto")

class LuckyPullAuto(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener("on_message")
    async def on_message(self, message):
        # Always do nothing (NO-OP) unless explicitly allowed via env.
        if not ALLOW_GENERIC:
            return
        try:
            red = os.getenv('LPG_REDIRECT_CHANNEL_ID', os.getenv('LUCKYPULL_REDIRECT_CHANNEL_ID','0'))
            await message.channel.send(
                f"{message.author.mention}, *Lucky Pull* terdeteksi dan telah dihapus. "
                f"Silakan unggah di <#{red}>."
            )
        except Exception as e:
            LOG.warning("[lpa] failed to send generic notice: %r", e)

async def setup(bot: commands.Bot):
    # In your discord.py build, add_cog behaves like a coroutine.
    await bot.add_cog(LuckyPullAuto(bot))
