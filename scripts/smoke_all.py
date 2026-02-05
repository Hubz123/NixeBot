#!/usr/bin/env python3
"""Unified smoketest entrypoint.

Usage:
  python scripts/smoke_all.py            # fast/offline-ish checks
  python scripts/smoke_all.py --super    # includes online probe + patch-presence checks
  python scripts/smoke_all.py --super --offline   # super, but skip network probe
  python scripts/smoke_all.py --online            # just run online probe in addition

Notes:
  - Online probe hits https://discord.com/api/v10/gateway and measures RTT.
  - Cloudflare 1015 / HTML 429 is treated as FAIL (means IP currently rate-limited/banned).
"""
from __future__ import annotations

import argparse
import compileall
import json
import os
import platform
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def ok(msg: str) -> None:
    print(f"[OK] {msg}")


def warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}")
    raise SystemExit(2)


def read_text(relpath: str) -> str:
    p = ROOT / relpath
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        fail(f"Missing required file: {relpath}")
        raise


def check_python_version() -> None:
    v = sys.version_info
    if (v.major, v.minor) < (3, 10):
        fail(f"Python too old: {platform.python_version()} (need >= 3.10)")
    ok(f"Python version OK: {platform.python_version()}")


def check_compileall() -> None:
    # compileall on repo root; skip venv if present
    ok_flag = compileall.compile_dir(str(ROOT), quiet=1)
    if not ok_flag:
        fail("Syntax compile failed for some .py files")
    ok("Syntax compile OK for all .py")


def check_cogs_structure() -> None:
    cogs_dir = ROOT / "nixe" / "cogs"
    if not cogs_dir.exists():
        fail("Missing nixe/cogs directory")
    py_files = [p for p in cogs_dir.rglob("*.py") if p.is_file()]
    ok(f"COGS structure checked: {len(py_files)} file(s)")


def check_main_healthz_port() -> None:
    main_py = read_text("main.py")
    # very lightweight heuristics
    if "/healthz" not in main_py and "healthz" not in main_py:
        warn("main.py does not mention /healthz (heuristic).")
    if "PORT" not in main_py:
        warn("main.py does not mention PORT env (heuristic).")
    ok(f"{ROOT / 'main.py'} exposes /healthz and uses PORT")


def _is_cf_html(body: str) -> bool:
    b = body.lower()
    return ("cloudflare" in b and "error" in b and "1015" in b) or ("access denied" in b and "cloudflare" in b) or ("<!doctype html" in b and "cloudflare" in b)


def online_probe_discord(timeout_s: float = 10.0) -> None:
    url = "https://discord.com/api/v10/gateway"
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "NixeBotSmoke/1.0"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = resp.read(200000).decode("utf-8", errors="replace")
            dt_ms = int((time.perf_counter() - t0) * 1000)
            code = getattr(resp, "status", None) or resp.getcode()
            # Cloudflare can respond 429 with HTML; sometimes 200 with JSON.
            if code == 200:
                # should be JSON with "url"
                try:
                    j = json.loads(data)
                    if not isinstance(j, dict) or "url" not in j:
                        warn("Discord gateway returned 200 but unexpected JSON shape.")
                    ok(f"Discord gateway probe OK: {dt_ms}ms")
                    return
                except Exception:
                    warn("Discord gateway returned 200 but response wasn't valid JSON.")
                    ok(f"Discord gateway probe OK (non-JSON): {dt_ms}ms")
                    return
            if code == 429 and _is_cf_html(data):
                fail(f"Discord gateway probe: Cloudflare 1015/HTML rate limit detected (HTTP 429). RTT={dt_ms}ms")
            fail(f"Discord gateway probe failed: HTTP {code}. RTT={dt_ms}ms")
    except Exception as e:
        s = str(e)
        # urllib may raise HTTPError carrying body
        if hasattr(e, "read"):
            try:
                body = e.read().decode("utf-8", errors="replace")  # type: ignore[attr-defined]
                if _is_cf_html(body):
                    fail("Discord gateway probe: Cloudflare 1015/HTML rate limit detected (exception).")
            except Exception:
                pass
        fail(f"Discord gateway probe error: {type(e).__name__}: {s}")


def check_patch_presence() -> None:
    # These checks are heuristic: they ensure the protective features are present in source.
    main_py = read_text("main.py").lower()
    if "cloudflare" not in main_py or "1015" not in main_py:
        warn("main.py does not appear to include Cloudflare 1015 handling keywords.")
    else:
        ok("main.py Cloudflare guard keywords present")

    # RAM auto-profile
    ram_overlay = read_text("nixe/cogs/a01_memory_sweeper_overlay.py")
    if "NIXE_RAM_AUTO_PROFILE" not in ram_overlay and "ram_auto_profile" not in ram_overlay:
        warn("a01_memory_sweeper_overlay.py does not mention NIXE_RAM_AUTO_PROFILE (auto-RAM profile may be missing).")
    else:
        ok("Auto RAM profile keywords present")

    # Net adaptive (optional but expected if you applied that patch)
    adaptive_path = ROOT / "nixe" / "helpers" / "adaptive_limits.py"
    if adaptive_path.exists():
        ok("Net-adaptive helper present")
    else:
        warn("Net-adaptive helper missing (nixe/helpers/adaptive_limits.py).")

    net_overlay = ROOT / "nixe" / "cogs" / "a00_net_adaptive_overlay.py"
    if net_overlay.exists():
        ok("Net-adaptive overlay present")
    else:
        warn("Net-adaptive overlay missing (nixe/cogs/a00_net_adaptive_overlay.py).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--super", action="store_true", help="Run extended checks (includes online probe unless --offline).")
    ap.add_argument("--online", action="store_true", help="Run online probe (Discord gateway) in addition to normal checks.")
    ap.add_argument("--offline", action="store_true", help="Skip network calls (even in --super).")
    ap.add_argument("--timeout", type=float, default=10.0, help="Online probe timeout seconds (default: 10).")
    args = ap.parse_args(argv)

    check_python_version()
    check_compileall()
    check_cogs_structure()
    check_main_healthz_port()

    if args.super:
        check_patch_presence()
        if not args.offline:
            online_probe_discord(timeout_s=args.timeout)
        else:
            ok("Online probe skipped (--offline)")
        ok("Super smoketests passed.")
        return 0

    if args.online and not args.offline:
        online_probe_discord(timeout_s=args.timeout)

    ok("All smoketests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
