# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import importlib
import logging
import os
import pkgutil
from typing import Iterable, List, Tuple

from nixe.helpers.bootstate import mark_cogs_loaded

LOGGER = logging.getLogger(__name__)

# Default "fail-closed" set: if any of these fail to load, we treat startup as unsafe.
DEFAULT_REQUIRED_COGS = (
    "nixe.cogs.a00_env_hybrid_overlay",
    "nixe.cogs.a16_phash_phish_guard_overlay",
    "nixe.cogs.phish_groq_guard",
)

# Modules to skip during autoload to avoid duplicates / legacy stubs.
SKIP_EXTENSIONS = {
    # Legacy stub that defines the same Cog name (LinkPhishGuard) and can break autoload.
    "nixe.cogs.link_phish_guard",
}


def _parse_required_from_env() -> Tuple[str, ...]:
    raw = (os.getenv("NIXE_REQUIRED_COGS") or "").strip()
    if not raw:
        return DEFAULT_REQUIRED_COGS
    out: List[str] = []
    for part in raw.split(","):
        name = part.strip()
        if not name:
            continue
        out.append(name)
    return tuple(out) if out else DEFAULT_REQUIRED_COGS

def _iter_cog_names(package_root: str) -> List[str]:
    """Return sorted module names under package_root."""
    pkg = importlib.import_module(package_root)
    names = []
    for mod in pkgutil.iter_modules(pkg.__path__, package_root + "."):
        name = getattr(mod, "name", "")
        leaf = name.rsplit(".", 1)[-1]
        if not name or leaf.startswith("_"):
            continue
        if name in SKIP_EXTENSIONS:
            continue
        names.append(name)
    names.sort()  # deterministic load order
    return names

async def _load_one(bot, name: str) -> None:
    await bot.load_extension(name)
    LOGGER.info("✅ Loaded cog: %s", name)

async def _load_all_impl(bot, package_root: str = "nixe.cogs") -> List[str]:
    required = set(_parse_required_from_env())

    try:
        names = _iter_cog_names(package_root)
    except Exception as e:
        LOGGER.error("cogs_loader: cannot enumerate %s: %r", package_root, e)
        raise

    loaded: List[str] = []
    errors: List[Tuple[str, str]] = []

    # Force env overlay first (critical: it merges runtime_env.json into os.environ).
    env_first = "nixe.cogs.a00_env_hybrid_overlay"
    if env_first in names:
        try:
            await _load_one(bot, env_first)
            loaded.append(env_first)
        except Exception as e:
            errors.append((env_first, repr(e)))
    # Load the rest (sorted)
    for name in names:
        if name == env_first:
            continue
        try:
            await _load_one(bot, name)
            loaded.append(name)
        except Exception as e:
            # Don't silently skip: log as ERROR so you can see failures in INFO-level deployments.
            msg = str(e)
            # Common benign case: duplicate Cog name from legacy stub modules.
            if ("already loaded" in msg) and ("Cog named" in msg or "LinkPhishGuard" in msg):
                LOGGER.warning("⚠️ Duplicate cog ignored for %s (%s)", name, msg)
                continue
            # Only print tracebacks for required cogs; otherwise keep logs clean.
            if name in required:
                LOGGER.error("❌ Failed to load %s: %r", name, e, exc_info=True)
            else:
                LOGGER.error("❌ Failed to load %s: %r", name, e)
            errors.append((name, repr(e)))

    # Fail-closed safety: required cogs must be loaded.
    missing = sorted([c for c in required if c not in loaded])
    if missing:
        # Include a compact error summary (without leaking secrets)
        summary = "; ".join([f"{n}={err}" for (n, err) in errors[:8]])
        raise RuntimeError(f"Unsafe startup: missing required cogs={missing}. First errors: {summary}")

    return loaded

def load_all(bot, package_root: str = "nixe.cogs"):
    """Compat entrypoint: works whether the caller awaits it or not."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return asyncio.create_task(_load_all_impl(bot, package_root))
    tmp = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(tmp)
        return tmp.run_until_complete(_load_all_impl(bot, package_root))
    finally:
        try:
            tmp.close()
        except Exception:
            pass

async def autoload_all(bot, package_root: str = "nixe.cogs") -> List[str]:
    return await _load_all_impl(bot, package_root)

# --- discord.py extension entrypoint ---
# Allows: await bot.load_extension("nixe.cogs_loader")
async def setup(bot):
    loaded = await _load_all_impl(bot, "nixe.cogs")
    try:
        mark_cogs_loaded()
    except Exception:
        pass
    LOGGER.info("cogs_loader_ext: loaded %d cogs (fail-closed enabled)", len(loaded))

def legacy_setup(bot):
    """Compat shim for older loaders."""
    try:
        res = load_all(bot, "nixe.cogs")
        if hasattr(res, "__await__"):
            # caller may await; we intentionally do not here
            pass
    except Exception:
        pass