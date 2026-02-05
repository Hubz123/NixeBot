#!/usr/bin/env python3
"""Compatibility wrapper.

This project uses scripts/smoke_all.py as the single entrypoint for both regular and super smoketests.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure repo root on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.smoke_all import main as smoke_all_main

if __name__ == "__main__":
    # Delegate to smoke_all in super mode.
    # Preserve user-provided args, but inject --super if not present.
    argv = sys.argv[1:]
    if "--super" not in argv:
        argv = ["--super"] + argv
    raise SystemExit(smoke_all_main(argv))
