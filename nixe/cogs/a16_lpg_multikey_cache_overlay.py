import os, io, asyncio, logging
import discord
from discord.ext import commands

from nixe.helpers import lpg_cache
from nixe.helpers import sticky_board
from nixe.helpers.gemini_bridge import classify_lucky_pull_bytes

ENABLE = os.getenv("LPG_OVERLAY_ENABLE","1") == "1"
LUCKY_PULL_GUARD_CHANNELS = [int(x) for x in (os.getenv("LUCKY_PULL_GUARD_CHANNELS","").split(',') if os.getenv("LUCKY_PULL_GUARD_CHANNELS") else [])]
REDIRECT_CHANNEL_ID = int(os.getenv("LPG_REDIRECT_CHANNEL_ID","0") or "0")
DEL_THRESHOLD = float(os.getenv("LPG_DELETE_THRESHOLD","0.85"))
REDIR_THRESHOLD = float(os.getenv("LPG_REDIRECT_THRESHOLD","0.70"))
GEM_THRESHOLD = float(os.getenv("GEMINI_LUCKY_THRESHOLD","0.75"))

class LuckyPullMultiKeyOverlay(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.board = sticky_board.StickyBoard(bot, "LPG_CACHE_THREAD_ID", "[LPG-STICKY]", "Lucky Pull Cache")
        self._bg_task = self.bot.loop.create_task(self._periodic_board())

    async def cog_unload(self):
        try:
            self._bg_task.cancel()
        except Exception:
            pass

    async def _periodic_board(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                lines = lpg_cache.debug_snapshot(40)
                await self.board.update_lines(lines, footer="Cached last 24h; single sticky embed.")
            except Exception as e:
                logging.warning("[lpg_overlay] board update failed: %s", e)
            await asyncio.sleep(30)

    def _is_guarded_channel(self, ch_id: int) -> bool:
        return ch_id in LUCKY_PULL_GUARD_CHANNELS if LUCKY_PULL_GUARD_CHANNELS else False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not ENABLE: return
        if message.author.bot: return
        ch: discord.abc.MessageableChannel = message.channel
        if not hasattr(ch, "id"): return
        if not self._is_guarded_channel(ch.id): return
        if not message.attachments: return

        # Take first image-like attachment
        img_att = None
        for a in message.attachments:
            if (a.content_type or "").startswith("image"):
                img_att = a; break
        if not img_att: return

        try:
            b = await img_att.read()
        except Exception:
            return

        # CACHE check first
        hit = lpg_cache.get(b)
        if hit:
            score, provider = hit
            await self._handle_action(message, score, f"cache:{provider}")
            return

        # Gemini classify with failover
        try:
            res = await classify_lucky_pull_bytes(b, timeout_ms=int(os.getenv("GEMINI_TIMEOUT_MS","20000")))
        except Exception as e:
            logging.warning("[lpg] classify error: %s", e)
            return
        score = float(res.get("score", 0.0))
        provider = str(res.get("provider", "gemini"))
        lpg_cache.put(b, score, provider)
        await self._handle_action(message, score, provider)

    async def _handle_action(self, message: discord.Message, score: float, provider: str):
        if score >= DEL_THRESHOLD:
            # delete and optionally redirect warning/persona
            try:
                await message.delete()
            except Exception:
                pass
            if REDIRECT_CHANNEL_ID:
                try:
                    ch = self.bot.get_channel(REDIRECT_CHANNEL_ID) or await self.bot.fetch_channel(REDIRECT_CHANNEL_ID)
                    await ch.send(f"\u26a0\ufe0f Lucky Pull terdeteksi (score={score:.2f}, via {provider}). Post gambar seperti ini di channel khusus Lucky Pull ya.")
                except Exception:
                    pass
        elif score >= REDIR_THRESHOLD:
            if REDIRECT_CHANNEL_ID:
                try:
                    ch = self.bot.get_channel(REDIRECT_CHANNEL_ID) or await self.bot.fetch_channel(REDIRECT_CHANNEL_ID)
                    await ch.send(f"\ud83d\udc49 Kemungkinan Lucky Pull (score={score:.2f}, via {provider}). Silakan lanjut di channel khusus.")
                except Exception:
                    pass

async def setup(bot: commands.Bot):
    await bot.add_cog(LuckyPullMultiKeyOverlay(bot))
