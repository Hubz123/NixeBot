"""
a00_env_z_profile_minipc_overlay.py

Add-only overlay:
- If NIXE_RUNTIME_PROFILE=minipc (or RUNTIME_PROFILE=minipc), load
  nixe/config/runtime_env_minipc.json and export its non-secret keys to os.environ.
- Works even if env_hybrid already ran, by overwriting configs after that.
- Does NOT touch secrets (keys/tokens) and does NOT modify any files.
"""

import json
import os
from pathlib import Path
import logging

log = logging.getLogger(__name__)

PROFILE_ENV_KEYS = ("NIXE_RUNTIME_PROFILE", "RUNTIME_PROFILE")

def _is_secret_key(k: str) -> bool:
    u = k.upper()
    return u.endswith("_TOKEN") or u.endswith("_API_KEY") or u.endswith("_SECRET")

def _load_minipc_runtime() -> dict | None:
    # locate nixe/config/runtime_env_minipc.json relative to this file
    here = Path(__file__).resolve()
    cfg_path = here.parents[1] / "config" / "runtime_env_minipc.json"
    if not cfg_path.exists():
        log.warning("[env-profile] minipc runtime not found at %s; skip", cfg_path)
        return None
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.exception("[env-profile] failed to load minipc runtime: %r", e)
        return None

def _apply_runtime(data: dict) -> int:
    n = 0
    for k, v in data.items():
        # skip section headers and empty keys
        if not k or k.startswith("---"):
            continue
        if _is_secret_key(k):
            continue
        os.environ[k] = str(v)
        n += 1
    return n

async def setup(bot):
    profile = None
    for key in PROFILE_ENV_KEYS:
        val = os.getenv(key, "").strip().lower()
        if val:
            profile = val
            break
    if profile != "minipc":
        log.info("[env-profile] profile=%s (no override)", profile or "default")
        return

    data = _load_minipc_runtime()
    if not data:
        return
    n = _apply_runtime(data)
    log.warning("[env-profile] applied minipc runtime override; keys=%d", n)
