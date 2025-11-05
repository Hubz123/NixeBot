
from __future__ import annotations
import os, io, asyncio, logging, collections, contextlib
import discord
from discord.ext import commands
from nixe.helpers.lp_gemini_helper import is_gemini_enabled
from nixe.helpers.lp_gemini_async import is_lucky_pull_async
from nixe.helpers.persona_gate import should_run_persona

log = logging.getLogger("nixe.cogs.lucky_pull_auto")

def _parse_id_list(v: str) -> list[int]:
    if not v: return []
    s = v.strip()
    if s.startswith("[") and s.endswith("]"):
        import json as _json
        try:
            arr = _json.loads(s)
            return [int(x) for x in arr if str(x).isdigit()]
        except Exception:
            pass
    return [int(x) for x in s.replace(" ","").split(",") if x.strip().isdigit()]

class LuckyPullAuto(commands.Cog):
    """High-throughput Lucky Pull guard with asyncio.Queue + workers"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()
        self.thr = float(os.getenv("GEMINI_LUCKY_THRESHOLD","0.85"))
        self.redirect_id = int(os.getenv("LUCKYPULL_REDIRECT_CHANNEL_ID","0") or "0")
        self.guard_ids = set(_parse_id_list(os.getenv("LUCKYPULL_GUARD_CHANNELS","") or os.getenv("LPG_GUARD_CHANNELS","")))
        self.delete_on_guard = True
        self.strict_on_guard = os.getenv("LPG_STRICT_ON_GUARD","1") == "1"
        # throughput
        self._workers_n = int(os.getenv("LPG_CONCURRENCY","2"))
        self._timeout_sec = float(os.getenv("LUCKYPULL_TIMEOUT_SEC","5"))
        self._queue_max = int(os.getenv("LPG_QUEUE_MAX","32"))
        self._queue: asyncio.Queue[discord.Message] = asyncio.Queue(maxsize=self._queue_max)
        self._workers: list[asyncio.Task] = []
        self._recent = collections.deque(maxlen=256)
        self.bot.loop.create_task(self._bootstrap())

    async def _bootstrap(self):
        await asyncio.sleep(0.2)
        for i in range(max(1, self._workers_n)):
            t = self.bot.loop.create_task(self._worker(i))
            self._workers.append(t)
        log.warning("[lpa] started %d workers | thr=%.2f timeout=%.1fs queue_max=%d",
                    len(self._workers), self.thr, self._timeout_sec, self._queue_max)

    def cog_unload(self):
        for t in self._workers:
            t.cancel()

    def _is_guard_channel(self, ch: discord.abc.GuildChannel) -> bool:
        return ch and int(getattr(ch,"id",0)) in self.guard_ids

    async def _maybe_redirect(self, message: discord.Message):
        if not self.redirect_id:
            return
        try:
            ch = message.guild.get_channel(self.redirect_id) or await self.bot.fetch_channel(self.redirect_id)
            mention = ch.mention if ch else f"<#{self.redirect_id}>"
            await message.channel.send(
                f"{message.author.mention}, silakan post Lucky Pull di {mention}.",
                delete_after=10
            )
        except Exception as e:
            log.debug("[lpa] redirect failed: %r", e)

    async def _classify_lucky_bytes(self, img: bytes) -> tuple[bool,float,str]:
        try:
            ok, score, reason = await asyncio.wait_for(
                is_lucky_pull_async(img, threshold=self.thr),
                timeout=self._timeout_sec
            )
            return bool(ok), float(score), str(reason)
        except Exception as e:
            return False, 0.0, f"error:{e}"

    async def _handle_single(self, message: discord.Message):
        if not message.attachments:
            return
        try:
            data = await message.attachments[0].read()
        except Exception as e:
            log.debug("[lpa] read attach failed: %r", e)
            return
        ok, score, _ = await self._classify_lucky_bytes(data)
        if not ok:
            return
        ctx = {"kind": "lucky", "ok": ok, "score": score, "provider": "gemini"}
        persona_ok, _ = should_run_persona(ctx)
        try:
            if self.delete_on_guard:
                await message.delete()
        except Exception as e:
            log.debug("[lpa] delete failed: %r", e)
        await self._maybe_redirect(message)
        if persona_ok:
            with contextlib.suppress(Exception):
                await message.channel.send(
                    f"{message.author.mention}, kontenmu melenceng dari tema. sudah dihapus. gunakan kanal yang tepat. (alasan: Lucky Pull)",
                    delete_after=10
                )

    async def _worker(self, idx: int):
        while True:
            msg = await self._queue.get()
            try:
                await self._handle_single(msg)
            except Exception as e:
                log.warning("[lpa] worker-%d error: %r", idx, e)
            finally:
                self._queue.task_done()

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if message.author.bot or not isinstance(message.channel, discord.TextChannel):
            return
        if not is_gemini_enabled():
            return
        if not self._is_guard_channel(message.channel):
            return
        if message.id in self._recent:
            return
        self._recent.append(message.id)
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            log.warning("[lpa] queue full (size=%d), dropping message %d", self._queue.qsize(), message.id)
