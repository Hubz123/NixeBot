
from __future__ import annotations
import os, logging, asyncio, inspect, io

from discord.ext import commands

log = logging.getLogger(__name__)

class LPGImageCompactOverlay(commands.Cog):
    """
    Optional image compaction for Render free: convert big PNG to JPEG and downscale,
    before sending to Gemini. If Pillow not available, no-op.
    Env:
      LPG_IMG_MAX_SIDE=1280
      LPG_IMG_JPEG_QUALITY=85
      LPG_IMG_COMPACT_THRESHOLD=900000   # bytes; only compact if above
    """
    def __init__(self, bot):
        self.bot = bot
        self.max_side = int(os.getenv('LPG_IMG_MAX_SIDE', '1280'))
        self.jpeg_q = int(os.getenv('LPG_IMG_JPEG_QUALITY', '85'))
        self.threshold = int(os.getenv('LPG_IMG_COMPACT_THRESHOLD', '900000'))
        try:
            import nixe.helpers.gemini_bridge as gb
            if hasattr(gb, 'classify_lucky_pull_bytes') and inspect.iscoroutinefunction(gb.classify_lucky_pull_bytes):
                self._patch_pre(gb)
                log.warning('[lpg-img-compact] enabled max_side=%s quality=%s threshold=%s',
                            self.max_side, self.jpeg_q, self.threshold)
            else:
                log.warning('[lpg-img-compact] target function not found; no patch applied')
        except Exception as e:
            log.error('[lpg-img-compact] patch failed: %s', e)

    def _compact(self, b: bytes) -> bytes:
        if len(b) < self.threshold:
            return b
        try:
            from PIL import Image
            import numpy as np  # not strictly required, Pillow alone is enough
        except Exception:
            # Pillow not available: keep original
            return b
        try:
            im = Image.open(io.BytesIO(b)).convert('RGB')
            w, h = im.size
            scale = min(1.0, float(self.max_side) / max(w, h))
            if scale < 1.0:
                new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
                im = im.resize(new_size)
            out = io.BytesIO()
            im.save(out, format='JPEG', quality=self.jpeg_q, optimize=True)
            out.seek(0)
            nb = out.read()
            # Only use if smaller
            if len(nb) < len(b):
                return nb
            return b
        except Exception:
            return b

    def _patch_pre(self, gb):
        orig = gb.classify_lucky_pull_bytes
        compact = self._compact

        async def wrapped(*args, **kwargs):
            # Expect signature (image_bytes, *)
            if args:
                new0 = compact(args[0]) if isinstance(args[0], (bytes, bytearray)) else args[0]
                args = (new0,) + tuple(args[1:])
            return await orig(*args, **kwargs)

        gb.classify_lucky_pull_bytes = wrapped

async def setup(bot):
    await bot.add_cog(LPGImageCompactOverlay(bot))
