#!/usr/bin/env python3

from __future__ import annotations

import os, sys, asyncio, importlib, inspect, traceback, argparse
from pathlib import Path

COGS_DIR = "nixe/cogs"
PACKAGE_PREFIX = "nixe.cogs"
EXCLUDE_NAMES = {"__init__.py"}

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

class _LoopProxy:
    """Loop proxy that exposes create_task but cancels tasks immediately in smoke mode."""
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
    def create_task(self, coro):
        try:
            # create and cancel immediately to avoid side-effects
            task = self._loop.create_task(coro)
            task.cancel()
            return task
        except Exception:
            # fallback: create a noop task
            async def _noop(): return None
            t = self._loop.create_task(_noop())
            return t

class DummyTree:
    async def sync(self, *_, **__):
        return []
    def add_command(self, *_args, **_kwargs):
        return None

class DummyBot:
    def __init__(self):
        self.loaded = []
        self.tree = DummyTree()
        self.all_commands = {}
        self.cogs = {}  # for code that checks 'if "ReadyShim" in bot.cogs'
        # Use a real loop, but proxy create_task to cancel quickly.
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        self.loop = _LoopProxy(loop)

    async def add_cog(self, cog, *_, **__):
        name = getattr(cog.__class__, "__name__", str(cog))
        self.loaded.append(name)
        self.cogs[name] = cog  # expose to code that inspects bot.cogs
        # Cancel any tasks created in __init__ like self._task = bot.loop.create_task(...)
        for attr in dir(cog):
            try:
                val = getattr(cog, attr)
            except Exception:
                continue
            if isinstance(val, asyncio.Task):
                try:
                    val.cancel()
                except Exception:
                    pass

    def add_view(self, view, *_, **__):
        return None

    async def load_extension(self, name: str):
        mod = importlib.import_module(name)
        fn = getattr(mod, "setup", None) or getattr(mod, "setup_old", None)
        if inspect.iscoroutinefunction(fn):
            await fn(self)
        elif callable(fn):
            fn(self)

    def get_cog(self, name: str):
        return self.cogs.get(name)

def find_cog_modules() -> list[tuple[str, Path]]:
    cdir = ROOT / COGS_DIR
    mods = []
    if not cdir.exists():
        return mods
    for p in cdir.glob("*.py"):
        if p.name in EXCLUDE_NAMES:
            continue
        modname = f"{PACKAGE_PREFIX}.{p.stem}"
        mods.append((modname, p))
    return sorted(mods, key=lambda x: x[0])

async def load_one(modname: str) -> tuple[bool, str]:
    try:
        mod = importlib.import_module(modname)
    except Exception:
        return False, f"import error\n{traceback.format_exc()}"

    setup_fn = getattr(mod, "setup", None) or getattr(mod, "setup_old", None)
    if setup_fn is None:
        return True, "no setup() — import ok"

    try:
        bot = DummyBot()
        # Ensure wait_until_ready exists for any tasks.before_loop
        if not hasattr(bot, "wait_until_ready"):
            async def _noop_wait():
                return None
            bot.wait_until_ready = _noop_wait

        if inspect.iscoroutinefunction(setup_fn):
            await setup_fn(bot)
        else:
            setup_fn(bot)
        return True, f"loaded (cogs: {', '.join(bot.loaded) or '—'})"
    except Exception:
        return False, f"setup error\n{traceback.format_exc()}"

async def main_async(only: str | None):
    mods = find_cog_modules()
    if only:
        mods = [(m, p) for (m, p) in mods if only in m or only in p.name]

    print("== Smoke: cogs load ==")
    failures = []
    for modname, path in mods:
        ok, info = await load_one(modname)
        label = f"{modname} ({path.name})"
        if ok:
            print(f"OK   : {label} :: {info}")
        else:
            print(f"FAIL : {label}\n{info}\n{'-'*60}")
            failures.append(label)

    if failures:
        print("\nFAILED COGS:")
        for f in failures:
            print("-", f)
        sys.exit(1)
    else:
        print("All cogs loaded OK")
        sys.exit(0)

def main():
    ap = argparse.ArgumentParser(description="Smoke test: load all Nixe Discord cogs safely (Leina-style output)")
    ap.add_argument("--only", help="filter nama modul/file (substring match)")
    ap.add_argument("--exclude", help="comma-separated filenames to exclude")
    args = ap.parse_args()

    if args.exclude:
        for name in args.exclude.split(","):
            name = name.strip()
            if name:
                EXCLUDE_NAMES.add(name)

    try:
        asyncio.run(main_async(args.only))
    except KeyboardInterrupt:
        sys.exit(130)

if __name__ == "__main__":
    main()