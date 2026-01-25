"""sitecustomize.py

This file is auto-imported by Python at interpreter startup (when present on
sys.path). Because Render runs `python main.py` from the project root, the root
directory is on sys.path and this module is loaded automatically.

Purpose (Render Free 512MB):
- Merge runtime_env.json into os.environ early (best-effort).
- Reduce discord.py cache pressure (member/message caches) without editing main.py.
- Apply a conservative Pillow MAX_IMAGE_PIXELS to avoid large decode spikes.

This module is intentionally defensive: failures must never block startup.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _is_render() -> bool:
    # Render injects several RENDER_* vars. We keep this broad to tolerate changes.
    for k in ("RENDER", "RENDER_SERVICE_ID", "RENDER_INSTANCE_ID", "RENDER_EXTERNAL_URL"):
        if os.getenv(k):
            return True
    return False


def _merge_env_from_runtime_json() -> None:
    """Best-effort env merge, mirroring env-hybrid overlay behavior.

    We only set keys that are absent/empty in os.environ.
    """
    try:
        path = os.getenv("ENV_HYBRID_JSON_PATH", "nixe/config/runtime_env.json")
        p = Path(path)
        if not p.exists():
            return
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        for k, v in data.items():
            if v is None:
                continue
            s = str(v).strip()
            if not s or s.lower() in {"none", "null"}:
                continue
            if (os.getenv(k) or "").strip():
                continue
            os.environ[k] = s
    except Exception:
        return


def _apply_pillow_pixel_cap() -> None:
    try:
        from PIL import Image  # type: ignore

        # Default: 12MP; override via env if needed.
        cap = int(os.getenv("NIXE_MAX_IMAGE_PIXELS", "12000000") or "12000000")
        if cap > 0:
            Image.MAX_IMAGE_PIXELS = cap
    except Exception:
        return


def _patch_discord_bot_init() -> None:
    """Lower discord.py cache pressure on Render.

    This does NOT change intents; it only limits caching.
    """

    try:
        import discord  # type: ignore
        from discord.ext import commands  # type: ignore

        orig_init = commands.Bot.__init__

        def wrapped_init(self, *args, **kwargs):
            # Only apply on Render.
            if _is_render():
                try:
                    # Keep user-specified values if provided.
                    kwargs.setdefault("member_cache_flags", discord.MemberCacheFlags.none())
                    kwargs.setdefault("chunk_guilds_at_startup", False)
                    # Message cache is a frequent silent RSS contributor.
                    max_msgs = int(os.getenv("NIXE_MAX_MESSAGES", "200") or "200")
                    kwargs.setdefault("max_messages", max_msgs)
                except Exception:
                    pass
            return orig_init(self, *args, **kwargs)

        commands.Bot.__init__ = wrapped_init  # type: ignore[assignment]
    except Exception:
        return


def _main() -> None:
    # Load runtime env early so caps are available to imports.
    _merge_env_from_runtime_json()
    _apply_pillow_pixel_cap()
    _patch_discord_bot_init()


_main()
