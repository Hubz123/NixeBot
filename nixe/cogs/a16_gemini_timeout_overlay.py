
from __future__ import annotations
import os, logging, asyncio, inspect, random
from discord.ext import commands

log = logging.getLogger(__name__)

class GeminiTimeoutOverlay(commands.Cog):
    """
    Render-free friendly: increase timeout, add jittered retry,
    and gate concurrency via semaphore inside gemini_bridge.
    Only touches LPG classify (Gemini).
    """
    def __init__(self, bot):
        self.bot = bot
        self.timeout_ms = int(os.getenv('GEMINI_TIMEOUT_MS', '9000'))   # 9s on render free
        self.retry = os.getenv('GEMINI_RETRY_ON_TIMEOUT', '1') in ('1','true','True','yes','on')
        self.jitter_ms = int(os.getenv('GEMINI_RETRY_JITTER_MS', '350'))  # small random backoff
        self.max_conc = int(os.getenv('GEMINI_CONCURRENCY', '2'))        # keep small on free tier

        try:
            import nixe.helpers.gemini_bridge as gb
            if hasattr(gb, 'classify_lucky_pull_bytes') and inspect.iscoroutinefunction(gb.classify_lucky_pull_bytes):
                self._patch_async(gb)
                log.warning('[gemini-timeout] patched classify_lucky_pull_bytes timeout_ms=%s retry=%s jitter=%sms conc=%s',
                            self.timeout_ms, self.retry, self.jitter_ms, self.max_conc)
            else:
                log.warning('[gemini-timeout] target function not found; no patch applied')
        except Exception as e:
            log.error('[gemini-timeout] patch failed: %s', e)

    def _patch_async(self, gb):
        orig = gb.classify_lucky_pull_bytes
        timeout = self.timeout_ms / 1000.0
        retry = self.retry
        jitter_ms = self.jitter_ms
        max_conc = self.max_conc

        # Global semaphore on module (single process)
        sem = asyncio.Semaphore(max(1, max_conc))

        async def wrapped(*args, **kwargs):
            async with sem:
                try:
                    return await asyncio.wait_for(orig(*args, **kwargs), timeout)
                except asyncio.TimeoutError:
                    if not retry:
                        return (False, 0.0, 'timeout', 'classify_timeout')
                    # jittered backoff
                    try:
                        await asyncio.sleep(random.uniform(0.001 * jitter_ms, 0.002 * jitter_ms))
                    except Exception:
                        pass
                    try:
                        return await asyncio.wait_for(orig(*args, **kwargs), timeout)
                    except asyncio.TimeoutError:
                        return (False, 0.0, 'timeout', 'classify_timeout')

        gb.classify_lucky_pull_bytes = wrapped

async def setup(bot):
    await bot.add_cog(GeminiTimeoutOverlay(bot))
