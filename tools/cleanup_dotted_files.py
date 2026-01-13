#!/usr/bin/env python3
"""Cleanup accidental dotted Python filenames that break import scanners.

This repo should not contain Python modules with dots in the filename, e.g.:
  nixe/config_ids.patch.append.py

Such files are not importable as packages and can cause smoketest import failures.

This script removes only known offenders and their __pycache__ artifacts.
"""

from __future__ import annotations

from pathlib import Path
import glob

REPO_ROOT = Path(__file__).resolve().parents[1]

KNOWN_BAD = [
    REPO_ROOT / "nixe" / "config_ids.patch.append.py",
]

def main() -> int:
    removed = 0
    for f in KNOWN_BAD:
        if f.exists():
            f.unlink()
            print(f"removed: {f}")
            removed += 1

    # remove compiled artifacts (best-effort)
    # NOTE: use glob patterns as strings; never do Path + "*.pyc" (TypeError).
    patterns = [
        str(REPO_ROOT / "nixe" / "__pycache__" / "config_ids.patch.append*.pyc"),
        str(REPO_ROOT / "nixe" / "**" / "__pycache__" / "config_ids.patch.append*.pyc"),
        str(REPO_ROOT / "nixe" / "**" / "config_ids.patch.append*.pyc"),
    ]
    for pat in patterns:
        for m in glob.glob(pat, recursive=True):
            try:
                Path(m).unlink()
                print(f"removed: {m}")
                removed += 1
            except Exception:
                pass

    if removed == 0:
        print("no known dotted files found")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
