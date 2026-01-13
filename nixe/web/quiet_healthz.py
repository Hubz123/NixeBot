from __future__ import annotations

# This module is imported in some environments just to "touch" healthz settings.
# During smoketests (dry-run imports), optional constants must not hard-fail.
try:
    from ..config.self_learning_cfg import NIXE_HEALTHZ_PATH, NIXE_HEALTHZ_SILENCE  # noqa: F401
except Exception:
    NIXE_HEALTHZ_PATH = "/healthz"  # noqa: F401
    NIXE_HEALTHZ_SILENCE = 1        # noqa: F401
