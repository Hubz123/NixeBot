from __future__ import annotations

"""
[a19-phish-img-compact]
Compact large images before Groq phishing vision classification.
Best-effort; NOOP if target not found or PIL unavailable.
"""

import os
import io
import logging

log = logging.getLogger(__name__)

def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except Exception:
        return default

def compact_image_bytes(data: bytes, max_side: int = 896, quality: int = 83) -> bytes:
    try:
        from PIL import Image
    except Exception:
        return data
    try:
        im = Image.open(io.BytesIO(data))
        w, h = im.size
        scale = min(1.0, float(max_side) / float(max(w, h)))
        if scale < 0.999:
            im = im.resize((int(w * scale), int(h * scale)))
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()
    except Exception:
        return data

def _patch_method(cls, name: str) -> bool:
    m = getattr(cls, name, None)
    if not callable(m) or getattr(m, "_nixe_phish_compact_patched", False):
        return False

    max_side = _env_int("PHISH_IMG_COMPACT_MAX_SIDE", 896)
    quality = _env_int("PHISH_IMG_COMPACT_QUALITY", 83)
    size_thr = _env_int("PHISH_IMG_COMPACT_BYTES_THRESHOLD", 900000)

    async def wrapped(self, img_bytes: bytes, *args, **kwargs):
        if img_bytes and len(img_bytes) >= size_thr:
            img_bytes = compact_image_bytes(img_bytes, max_side=max_side, quality=quality)
        return await m(self, img_bytes, *args, **kwargs)

    setattr(wrapped, "_nixe_phish_compact_patched", True)
    setattr(cls, name, wrapped)
    log.warning(f"[phish-img-compact] patched {cls.__name__}.{name}")
    return True

async def setup(bot):
    try:
        import nixe.cogs.phish_groq_guard as mod
    except Exception as e:
        log.warning(f"[phish-img-compact] import phish_groq_guard failed: {e}")
        return

    patched = False
    for cls_name in ["PhishGroqGuard", "GroqPhishGuard", "PhishGroqGuardCog"]:
        cls = getattr(mod, cls_name, None)
        if not cls:
            continue
        for meth in ["classify_image_bytes", "classify_bytes", "_classify_image_bytes"]:
            if _patch_method(cls, meth):
                patched = True
                break
        if patched:
            break
    if not patched:
        log.warning("[phish-img-compact] no target method found; NOOP")
