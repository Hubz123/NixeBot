#!/usr/bin/env python3
"""SmokeTest: LPG Groq/Gemini-Bridge (NixeBot)

Goals:
- Verify env keys & model list are detected (masked output).
- Perform a real classify call against Groq OpenAI-compatible endpoint via nixe.helpers.gemini_bridge.
- Fail hard on timeout / error / missing config (non-zero exit code).

Usage:
  python smoketest_lpg_bridge.py --image "C:\\path\\img.png" --print-config --repeat 2

Exit codes:
  0  = PASS
  10 = bad args / missing file
  20 = config missing (no key/model)
  30 = runtime call failed (timeout/error/exception)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

# Ensure repo root (script dir) is on sys.path so "import nixe" works.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

def _mask(s: str) -> str:
    s = str(s or "")
    if len(s) <= 6:
        return "***"
    return s[:3] + "_***" + s[-3:]

def _die(code: int, msg: str) -> None:
    print(f"FAIL: {msg}")
    raise SystemExit(code)

def _print_config(gb) -> None:
    keys = []
    try:
        keys = list(gb._env_keys_list())  # type: ignore[attr-defined]
    except Exception:
        keys = []
    keys_masked = ", ".join(_mask(k) for k in keys) if keys else "(none)"

    models = []
    try:
        models = list(gb._env_models_list())  # type: ignore[attr-defined]
    except Exception:
        models = []
    models_preview = ", ".join(models[:3]) if models else "(none)"

    try:
        per_t = float(gb._pick_timeout_sec(12.0))  # type: ignore[attr-defined]
    except Exception:
        per_t = 0.0
    try:
        tot_t = float(gb._pick_total_budget_sec(22.0))  # type: ignore[attr-defined]
    except Exception:
        tot_t = 0.0

    has_raw = bool(getattr(gb, "classify_lucky_pull_bytes_raw", None))
    print("SMOKETEST: LPG Groq/Gemini-Bridge")
    print("=" * 66)
    print(f"has_raw_entrypoint: {has_raw}")
    print(f"keys_count: {len(keys)}")
    print(f"keys_masked_preview: {keys_masked}")
    print(f"models_count: {len(models)}")
    print(f"models_preview: {models_preview}")
    print(f"internal_per_attempt_timeout_sec: {per_t}")
    print(f"internal_total_budget_sec: {tot_t}")
    print(f"outer_LPG_TIMEOUT_SEC_env: {os.getenv('LPG_TIMEOUT_SEC','')}")
    print(f"outer_LUCKYPULL_GROQ_TOTAL_TIMEOUT_SEC_env: {os.getenv('LUCKYPULL_GROQ_TOTAL_TIMEOUT_SEC','')}")
    print("-")

async def _run_once(gb, img_bytes: bytes, idx: int) -> tuple[bool, float, str, str, float]:
    fn = getattr(gb, "classify_lucky_pull_bytes_raw", None) or getattr(gb, "classify_lucky_pull_bytes", None)
    if not fn:
        _die(30, "gemini_bridge missing classify entrypoint")
    t0 = time.perf_counter()
    try:
        lucky, score, via, reason = await fn(img_bytes)  # type: ignore[misc]
    except Exception as e:
        _die(30, f"exception calling classify: {type(e).__name__}: {e}")
    elapsed = time.perf_counter() - t0
    return bool(lucky), float(score or 0.0), str(via), str(reason), float(elapsed)

def _is_fatal(lucky: bool, score: float, via: str, reason: str) -> bool:
    r = (reason or "").lower()
    v = (via or "").lower()
    if r.startswith("error:") or r in {"timeout", "classify_timeout", "classify_exception"}:
        return True
    if v in {"timeout", "error"}:
        return True
    # Config missing should fail hard.
    if r in {"no_api_key", "no_model", "no_groq_model"}:
        return True
    return False

async def main_async(args: argparse.Namespace) -> int:
    try:
        from nixe.helpers import gemini_bridge as gb  # type: ignore
    except Exception as e:
        _die(10, f"cannot import nixe.helpers.gemini_bridge: {type(e).__name__}: {e}")

    if args.print_config:
        _print_config(gb)

    img_path = Path(args.image).expanduser()
    if not img_path.exists():
        _die(10, f"image not found: {img_path}")
    img_bytes = img_path.read_bytes()
    print(f"image: {img_path.name}  bytes={len(img_bytes)}")

    # Hard config checks BEFORE calling network.
    models = []
    try:
        models = list(gb._env_models_list())  # type: ignore[attr-defined]
    except Exception:
        models = []
    keys = []
    try:
        keys = list(gb._env_keys_list())  # type: ignore[attr-defined]
    except Exception:
        keys = []

    if not keys:
        _die(20, "no LPG API key found in env (GEMINI_API_KEY / GEMINI_API_KEY_B etc.)")
    if not models:
        _die(20, "no GROQ vision model configured (GROQ_MODEL_VISION / CANDIDATES / FALLBACKS)")

    repeat = int(args.repeat or 1)
    for i in range(1, repeat + 1):
        lucky, score, via, reason, elapsed = await _run_once(gb, img_bytes, i)
        print(f"RUN {i}: lucky={lucky} score={score:.3f} via={via} reason={reason} elapsed={elapsed:.2f}s")
        if (reason or '').startswith('error:AttributeError'):
            print('HINT: error:AttributeError often means httpx timeout exception type mismatch or a network exception masked by exception-type lookup. Ensure gemini_bridge.py is updated from this ZIP.')
        if _is_fatal(lucky, score, via, reason):
            _die(30, f"fatal classify result: via={via} reason={reason}")
        # Optional format assertion
        if args.assert_via and not str(via).startswith("gemini:"):
            _die(30, f"via format mismatch (expected prefix 'gemini:'): {via}")

    print("PASS: LPG classify completed without fatal errors.")
    return 0

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--image", required=True, help="Path to an image file")
    p.add_argument("--repeat", type=int, default=1, help="Number of runs")
    p.add_argument("--print-config", action="store_true", help="Print masked config and internal timeouts")
    p.add_argument("--assert-via", action="store_true", help="Fail if via doesn't start with 'gemini:'")
    return p.parse_args()

def main() -> None:
    args = parse_args()
    rc = asyncio.run(main_async(args))
    raise SystemExit(rc)

if __name__ == "__main__":
    main()