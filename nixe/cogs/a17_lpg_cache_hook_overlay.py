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
import os, asyncio, logging, hashlib
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
        # if True, cache is only used as fallback when provider errors/timeouts
        self.fallback_on_error_only = _env("LPG_CACHE_FALLBACK_ON_ERROR_ONLY", "1") == "1"

        # capture current classifier
        try:
            import nixe.helpers.gemini_bridge as gb
            self._orig = gb.classify_lucky_pull_bytes
            self._orig_raw = getattr(gb, 'classify_lucky_pull_bytes_raw', None)
        except Exception as e:
            self._orig = None
            self._orig_raw = None
            log.warning("[lpg-cache] cannot hook gemini_bridge: %r", e)
            return

        async def _patched_core(orig_func, image_bytes: bytes, *args, **kwargs):
            # HARD OVERRIDE: denylist must force NOT LUCKY before any provider/caches.
            # This must apply even when LPG_CACHE_ENABLE=0.
            sha1 = hashlib.sha1(image_bytes).hexdigest()
            try:
                from nixe.helpers import lpg_denylist
                if lpg_denylist.is_denied_sha1(sha1):
                    return False, 0.0, 'unlearn_deny', 'deny_sha1'
            except Exception:
                pass

            # If we cannot call the provider, fail closed (NOT LUCKY).
            if not orig_func:
                return False, 0.0, 'orig_missing', 'orig_missing'

            # If cache is disabled, just call provider (denylist already handled).
            if not self.enable:
                return await orig_func(image_bytes, *args, **kwargs)

            # Optional aHash deny (best effort; ignored if PIL missing).
            try:
                from nixe.helpers import lpg_denylist
                from nixe.helpers import lpg_cache_memory as _cache
                ah, _wh = _cache._to_ahash_bytes(image_bytes)  # type: ignore[attr-defined]
                if lpg_denylist.is_denied_ahash(str(ah)):
                    return False, 0.0, 'unlearn_deny', 'deny_ahash'
            except Exception:
                pass

            # Preload cache handles and potential hits; do NOT raise if cache is broken.
            exact_ent = None
            sim_ent = None
            sim_dist = None
            cache_hint = ""
            try:
                from nixe.helpers import lpg_cache_memory as cache
                exact_ent = cache.get_exact(image_bytes)
                # Legacy behaviour: allow exact hit to short-circuit if not limited to error-only.
                if exact_ent and not self.fallback_on_error_only:
                    cache_hint = "cache_hit_sha1"
                sim = cache.get_similar(image_bytes, self.maxdist)
                if sim:
                    sim_ent, sim_dist = sim
                    if (
                        not self.fallback_on_error_only
                        and sim_ent.get("ok")
                        and float(sim_ent.get("score", 0.0)) >= self.sim_ok_min
                    ):
                        # Similar-positive hit is recorded but does NOT short-circuit.
                        cache_hint = f"cache_hit_ahash:dist={sim_dist}"
            except Exception:
                # cache failures must never break classify
                exact_ent = None
                sim_ent = None
                sim_dist = None
                cache_hint = ""

            # Call provider (shield/burst pipeline under the hood)
            ok, score, via, reason = await orig_func(image_bytes, *args, **kwargs)
            try:
                if cache_hint and isinstance(reason, str) and reason:
                    reason = f"{reason};{cache_hint}"
            except Exception:
                pass

            # Decide whether provider result looks like a transport/timeout error.
            rlow = str(reason or "").lower()
            vlow = str(via or "").lower()
            err_tokens = (
                "http_error",
                "no_result",
                "shield_timeout",
                "shield_error",
                "classify_exception",
                "request_timeout",
                "burst_shape",
            )
            is_errorish = any(tok in rlow for tok in err_tokens) or vlow.startswith("none") or vlow.startswith("timeout") or vlow.startswith("error")

            # If provider errored and fallback-on-error-only is enabled, try cache as a rescue.
            if self.fallback_on_error_only and is_errorish:
                try:
                    from nixe.helpers import lpg_cache_memory as cache

                    # refresh hits if we did not compute them above (or cache module changed)
                    if exact_ent is None:
                        exact_ent = cache.get_exact(image_bytes)
                    if exact_ent and exact_ent.get("ok") and float(exact_ent.get("score", 0.0)) >= self.sim_ok_min:
                        return True, float(exact_ent.get("score", 0.0)), "cache:sha1-fallback", f"fallback({reason})"

                except Exception:
                    # If cache lookup fails here, fall back to original provider result.
                    pass

            # Persist to in-memory + thread, but avoid poisoning cache with pure errors
            try:
                from nixe.helpers import lpg_cache_memory as cache

                cacheable = True
                if not self.store_error_results:
                    # reuse same err_tokens / is_errorish logic
                    if is_errorish:
                        cacheable = False

                entry = None
                if cacheable:
                    entry = cache.put(image_bytes, ok, score, via, reason)
                    # fire-and-forget persist to cache thread
                    try:
                        from nixe.cogs.a17_lpg_cache_persistence_overlay import LPGCachePersistence  # noqa
                        cog = self.bot.get_cog("LPGCachePersistence")
                        if cog:
                            import asyncio as _asyncio
                            _asyncio.create_task(cog.persist(entry))
                    except Exception:
                        pass

                # Also remember strong LUCKY (for memory board / Upstash)
                try:
                    if entry and entry.get("ok") and float(entry.get("score", 0.0)) >= self.sim_ok_min:
                        from nixe.helpers import lpg_memory as LPM
                        # record ahash into memory and notify board
                        ah = str(entry.get("ahash") or "")
                        if ah:
                            LPM.remember(ah)
                            try:
                                # dispatch custom event; LpgMemoryBoard listens on it
                                self.bot.dispatch("lpg_memory_changed")
                            except Exception:
                                pass
                except Exception:
                    # memory failures must not affect classify
                    pass
            except Exception:
                pass

            return ok, score, via, reason

        async def patched(image_bytes: bytes, *args, **kwargs):
            return await _patched_core(self._orig, image_bytes, *args, **kwargs)

        async def patched_raw(image_bytes: bytes, *args, **kwargs):
            return await _patched_core(self._orig_raw, image_bytes, *args, **kwargs)

        # inject
        try:
            import nixe.helpers.gemini_bridge as gb2
            gb2.classify_lucky_pull_bytes = patched  # type: ignore
            if getattr(gb2, 'classify_lucky_pull_bytes_raw', None):
                gb2.classify_lucky_pull_bytes_raw = patched_raw  # type: ignore
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
