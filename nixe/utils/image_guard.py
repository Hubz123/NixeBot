from __future__ import annotations

import logging
import warnings

from PIL import Image, ImageFile

try:
    from PIL import DecompressionBombWarning  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    from PIL.Image import DecompressionBombWarning  # type: ignore[attr-defined]

log = logging.getLogger(__name__)

# Default safe cap for very large images. This is intentionally conservative
# for low-memory environments like Render free tier.
SAFE_MAX_PIXELS = 35_000_000


def enable_global_pillow_guard(max_pixels: int = SAFE_MAX_PIXELS) -> None:
    """
    Apply a global safety guard to Pillow so that extremely large images
    raise an exception instead of being fully decompressed.

    This protects the bot from out-of-memory crashes when users upload
    absurdly large screenshots (tens of millions of pixels) that would
    otherwise trigger Pillow's decompression bomb behaviour.
    """
    try:
        old = getattr(Image, "MAX_IMAGE_PIXELS", None)
        Image.MAX_IMAGE_PIXELS = max_pixels
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        warnings.simplefilter("error", DecompressionBombWarning)
        log.warning(
            "[image-guard] Pillow guard enabled; MAX_IMAGE_PIXELS=%s (was=%s)",
            max_pixels,
            old,
        )
    except Exception:
        log.exception("[image-guard] failed to apply Pillow guard")
