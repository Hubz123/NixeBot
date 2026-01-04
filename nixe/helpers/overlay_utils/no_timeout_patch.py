import os, asyncio, logging, base64, io, inspect, hashlib, time
logger = logging.getLogger(__name__)

# Global concurrency + cache (friendly for free plan)
_GEM_RATE = int(os.getenv("LPG_GROQ_MAX_CONCURRENCY", "1"))  # serialize by default
_GEM_SEM = asyncio.Semaphore(max(1, _GEM_RATE))
_CACHE_TTL = int(os.getenv("LPG_CLASSIFY_CACHE_TTL_SEC", "30"))
_cache = {}  # key sha256 -> (ts, (ok, score, via, reason))

def _env_bool(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default) == "1"

def set_default_envs():
    defaults = {
        "LPG_SHIELD_ENABLE": "0",
        "LPG_BRIDGE_ALLOW_QUICK_FALLBACK": "0",
        "LPG_DEFER_ON_TIMEOUT": "1",
        "LPG_GUARD_LASTCHANCE_MS": "1800",
        "LPG_PROVIDER_PARALLEL": "0",
        "LPG_BURST_MODE": "stagger",
        "LPG_GROQ_MAX_CONCURRENCY": "1",
        "LPG_GEM_MAX_RPM": "4",
        "LPG_CLASSIFY_SOFT_TIMEOUT_MS": "6000",
        "LPG_TIMEOUT_SEC": "12",
        "LPG_IMG_MAX_DIM": "1024",
        "LPG_IMG_JPEG_Q": "85",
        "LPG_HTTP_TRIES": "3",
        "LPG_HTTP_PER_TRY_MS": "3000",
        "LPG_REQUIRE_CLASSIFY": "1",
        "LPG_ASSUME_LUCKY_ON_FALLBACK": "0",
        "LPG_FREE_PLAN": "1",
        "LPG_CLASSIFY_CACHE_TTL_SEC": "30",
        "LPG_BURST_TIMEOUT_MS": "1500",
    }
    for k, v in defaults.items():
        os.environ.setdefault(k, v)

def patch_shield_overlay():
    if _env_bool("LPG_SHIELD_ENABLE", "0"):
        logger.info("[nixe-patch] shield enabled by ENV; overlay leaves it intact.")
        return
    modules = [
        "nixe.cogs.a00_lpg_classify_timeout_shield_overlay",
        "nixe.cogs.a16_lpg_classify_timeout_shield_overlay",
    ]
    for modname in modules:
        try:
            mod = __import__(modname, fromlist=["*"])
        except Exception:
            continue
        target_names = ["classify_lucky_pull_bytes","classify_lucky_pull","_classify_lucky_pull_bytes"]
        impl = getattr(mod, "_impl", None)
        async def passthrough(*a, **kw):
            if callable(impl):
                return await impl(*a, **kw)
            return (False, 0.0, "none", "no_result")
        for name in target_names:
            if hasattr(mod, name):
                setattr(mod, name, passthrough)
                logger.info("[nixe-patch] shield overlay %s.%s -> passthrough", modname, name)

def _pil_available():
    try:
        import PIL  # noqa
        return True
    except Exception:
        return False

def _prep_image(image_bytes: bytes):
    if not _pil_available():
        return image_bytes, "image/png"
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        max_dim = int(os.getenv("LPG_IMG_MAX_DIM", "1024"))
        jpeg_q  = int(os.getenv("LPG_IMG_JPEG_Q", "85"))
        im.thumbnail((max_dim, max_dim))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=max(60, min(jpeg_q, 95)), optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return image_bytes, "image/png"

def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def _wrap_gemini_call_in_module(mod):
    candidates = ["call_gemini", "gemini_call", "call_gemini_generate_content"]
    for name in candidates:
        fn = getattr(mod, name, None)
        if fn and inspect.iscoroutinefunction(fn):
            async def wrapped(image_bytes: bytes, *args, **kwargs):
                ib, mime = _prep_image(image_bytes)
                b64 = base64.b64encode(ib).decode("ascii")
                kwargs.setdefault("inline_mime", mime)
                kwargs.setdefault("inline_b64", b64)

                key = _sha256(ib)
                now = time.time()
                if _CACHE_TTL > 0:
                    hit = _cache.get(key)
                    if hit and now - hit[0] <= _CACHE_TTL:
                        return hit[1]

                tries = max(1, int(os.getenv("LPG_HTTP_TRIES", "3")))
                last_err = None
                async with _GEM_SEM:  # serialize for free plan
                    for _ in range(tries):
                        try:
                            res = await fn(image_bytes, *args, **kwargs)
                            if _CACHE_TTL > 0:
                                _cache[key] = (time.time(), res)
                            return res
                        except Exception as e:
                            last_err = f"{type(e).__name__}:{str(e)[:200]}"
                            continue
                return (False, 0.0, "none", "no_result:" + (last_err or "err"))
            setattr(mod, name, wrapped)
            logger.info("[nixe-patch] wrapped gemini call: %s.%s", mod.__name__, name)
            return True
    return False

def patch_gemini_bridge():
    modules = ["nixe.helpers.gemini_bridge","nixe.helpers.lp_gemini_helper","nixe.helpers.gemini_lpg_bridge"]
    for modname in modules:
        try:
            mod = __import__(modname, fromlist=["*"])
        except Exception:
            continue
        try:
            if _wrap_gemini_call_in_module(mod):
                return
        except Exception as e:
            logger.warning("[nixe-patch] failed to wrap gemini call in %s: %s", modname, e)

def patch_guard_defer():
    modnames = ["nixe.cogs.a00_lpg_thread_bridge_guard","nixe.cogs.a00_lpg_guard"]
    for modname in modnames:
        try:
            mod = __import__(modname, fromlist=["*"])
        except Exception:
            continue
        import inspect
        for attr in dir(mod):
            obj = getattr(mod, attr, None)
            if not inspect.isclass(obj):
                continue
            if hasattr(obj, "_classify"):
                _orig = getattr(obj, "_classify")
                if not inspect.iscoroutinefunction(_orig):
                    continue
                def _make_wrapped(_orig_func):
                    async def _wrapped(self, *args, **kwargs):
                        try:
                            return await _orig_func(self, *args, **kwargs)
                        except asyncio.TimeoutError:
                            # Support both calling conventions:
                            #   _classify(image_bytes)
                            #   _classify(message, image_bytes=...)
                            image_bytes = kwargs.get('image_bytes')
                            if image_bytes is None:
                                for a in args:
                                    if isinstance(a, (bytes, bytearray)):
                                        image_bytes = bytes(a)
                                        break
                            last_ms = int(os.getenv('LPG_GUARD_LASTCHANCE_MS', '1800'))
                            if last_ms > 0 and image_bytes:
                                try:
                                    from nixe.helpers.gemini_lpg_burst import classify_lucky_pull_bytes_burst as _burst
                                    os.environ.setdefault('LPG_BURST_TIMEOUT_MS', os.getenv('LPG_BURST_TIMEOUT_MS', '1500'))
                                    ok, score, via, reason = await _burst(bytes(image_bytes))
                                    return (bool(ok), float(score), f"{via or 'gemini:lastchance'}", f"lastchance({reason})")
                                except Exception:
                                    pass
                            return (False, 0.0, 'pending', 'deferred_noexec')
                    return _wrapped
                _wrapped = _make_wrapped(_orig)
                setattr(obj, "_classify", _wrapped)
                logger.info("[nixe-patch] guard defer patch applied on %s.%s", modname, attr)

def apply_all_patches():
    try:
        set_default_envs()
    except Exception:
        pass
    try:
        patch_shield_overlay()
    except Exception as e:
        logger.warning("[nixe-patch] shield patch skipped: %s", e)
    try:
        patch_gemini_bridge()
    except Exception as e:
        logger.warning("[nixe-patch] lpg bridge patch skipped: %s", e)
    try:
        patch_guard_defer()
    except Exception as e:
        logger.warning("[nixe-patch] guard defer patch skipped: %s", e)
