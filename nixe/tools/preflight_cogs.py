# -*- coding: utf-8 -*-
"""Preflight checker: validates that all cogs under nixe.cogs are loadable as extensions.

Usage:
  python -m nixe.tools.preflight_cogs

Exits with code 0 if OK, non-zero otherwise.
"""
from __future__ import annotations
import importlib, pkgutil, sys, traceback

def iter_modules(package_root: str):
    pkg = importlib.import_module(package_root)
    for mod in pkgutil.iter_modules(pkg.__path__, package_root + "."):
        name = getattr(mod, "name", "")
        leaf = name.rsplit(".", 1)[-1]
        if not name or leaf.startswith("_"):
            continue
        yield name

def main() -> int:
    pkg_root = "nixe.cogs"
    bad = []
    for name in sorted(iter_modules(pkg_root)):
        try:
            m = importlib.import_module(name)
        except Exception as e:
            bad.append((name, f"import failed: {e!r}"))
            continue
        setup = getattr(m, "setup", None)
        if setup is None or not callable(setup):
            bad.append((name, "missing setup()"))
    if bad:
        print("Preflight FAILED. Issues:")
        for name, reason in bad:
            print(f" - {name}: {reason}")
        return 2
    print("Preflight OK: all nixe.cogs modules have setup().")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
