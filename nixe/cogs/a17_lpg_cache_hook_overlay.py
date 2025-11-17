# -*- coding: utf-8 -*-
"""
a17_lpg_cache_hook_overlay
--------------------------
Wraps Lucky Pull classifier to:
1) Check memory cache (sha1 exact, then aHash approx) before calling provider.
2) If provider called, persist result to cache thread and memory.

Env (optional):
- LPG_CACHE_ENABLE (default "1")
- LPG_CACHE_AHASH_MAXDIST (default "6")
- LPG_CACHE_ACCEPT_SIM_OK_MIN (default "0.90")   -> accept similar-ok without recheck
- LPG_CACHE_ACCEPT_SIM_NOK_RECHECK (default "1") -> if similar says not lucky, still recheck with provider
- LPG_CACHE_STORE_ERROR_RESULTS (default "0")    -> if "1", also store http_error/timeout/no_result results
"""
from __future__ import annotations
import os, asyncio, logging
from discord.ext import commands

log = logging.getLogger(__name__)


def _env(k: str, d: str = "") -> str:
    v = os.getenv(k)
    return str(v) if v is not None else d


class LPGCacheHook(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enable = _env("LPG_CACHE_ENABLE", "1") == "1"
        self.maxdist = int(_env("LPG_CACHE_AHASH_MAXDIST", "6") or "6")
        try:
            self.sim_ok_min = float(_env("LPG_CACHE_ACCEPT_SIM_OK_MIN", "0.90"))
        except Exception:
            self.sim_ok_min = 0.90
        self.sim_nok_recheck = _env("LPG_CACHE_ACCEPT_SIM_NOK_RECHECK", "1") == "1"
        # by default, we DO NOT store pure error/timeout/http_error/no_result results
        self.store_error_results = _env("LPG_CACHE_STORE_ERROR_RESULTS", "0") == "1"

        # capture current classifier
        try:
            import nixe.helpers.gemini_bridge as gb
            self._orig = gb.classify_lucky_pull_bytes
        except Exception as e:
            self._orig = None
            log.warning("[lpg-cache] cannot hook gemini_bridge: %r", e)
            return

        async def patched(image_bytes: bytes, *args, **kwargs):
            if not self.enable or not self._orig:
                return await self._orig(image_bytes, *args, **kwargs)

            # Try cache
            try:
                from nixe.helpers import lpg_cache_memory as cache
                ent = cache.get_exact(image_bytes)
                if ent:
                    return ent["ok"], ent["score"], "cache:sha1", "hit"
                sim = cache.get_similar(image_bytes, self.maxdist)
                if sim:
                    ent, dist = sim
                    if ent.get("ok") and float(ent.get("score", 0.0)) >= self.sim_ok_min:
                        return True, float(ent.get("score", 0.0)), "cache:ahash", f"dist={dist}"
                    # else -> fallthrough to provider
            except Exception:
                # cache failures must never break classify
                pass

            # Call provider (shield/burst pipeline under the hood)
            ok, score, via, reason = await self._orig(image_bytes, *args, **kwargs)

            # Persist to in-memory + thread, but avoid poisoning cache with pure errors
            try:
                from nixe.helpers import lpg_cache_memory as cache

                cacheable = True
                if not self.store_error_results:
                    rlow = str(reason or "").lower()
                    vlow = str(via or "").lower()
                    # anything that clearly looks like transport/timeout/shield issues is NOT cached
                    err_tokens = (
                        "http_error",
                        "no_result",
                        "shield_timeout",
                        "shield_error",
                        "classify_exception",
                        "request_timeout",
                        "burst_shape",
                    )
                    if any(tok in rlow for tok in err_tokens):
                        cacheable = False
                    if vlow.startswith("none") or vlow.startswith("timeout") or vlow.startswith("error"):
                        cacheable = False

                if cacheable:
                    entry = cache.put(image_bytes, ok, score, via, reason)
                    # fire-and-forget persist to cache thread
                    try:
                        from nixe.cogs.a17_lpg_cache_persistence_overlay import LPGCachePersistence  # noqa
                        cog = self.bot.get_cog("LPGCachePersistence")
                        if cog:
                            asyncio.create_task(cog.persist(entry))
                    except Exception:
                        pass
            except Exception:
                pass

            # If similar cache negative existed and recheck disabled, we could return cached negative,
            # but by default we trust provider result here.
            return ok, score, via, reason

        # inject
        try:
            import nixe.helpers.gemini_bridge as gb2
            gb2.classify_lucky_pull_bytes = patched  # type: ignore
            log.warning(
                "[lpg-cache] hook enabled (maxdist=%s, sim_ok_min=%.2f, store_error_results=%s)",
                self.maxdist,
                self.sim_ok_min,
                self.store_error_results,
            )
        except Exception as e:
            log.warning("[lpg-cache] hook inject failed: %r", e)


async def setup(bot: commands.Bot):
    await bot.add_cog(LPGCacheHook(bot))
