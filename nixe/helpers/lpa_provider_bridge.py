# nixe/helpers/lpa_provider_bridge.py — image-first provider dispatch (NO env mutation)
# Purpose:
# - Provide a resilient, minimal dependency dispatcher for LPG classification.
# - NEVER uses GROQ_API_KEY (phishing-only). LPG must use GEMINI_* keys via gemini_bridge.
# - Optional google-generativeai path is only used if explicitly requested via provider order.
#
# Expected return shape:
#   (probability: float 0..1, via: str)
#
# Underlying providers may return:
#   (lucky: bool, score: float, via: str, reason: str)
#   (probability: float, via: str)
#   (bool, float)  -> treated as (lucky, score)

from __future__ import annotations

import asyncio
import importlib
import os
from typing import Any, Iterator, Tuple


def _try_import(path: str):
    try:
        return importlib.import_module(path)
    except Exception:
        return None


def _norm_prob(x: Any) -> float:
    try:
        v = float(x)
    except Exception:
        return 0.0
    if v != v:  # NaN
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def _call_to_prob(res: Any) -> Tuple[float | None, str]:
    """Convert various provider return shapes into (prob, status)."""
    if res is None:
        return None, "none"

    # tuple shapes
    if isinstance(res, tuple) and len(res) >= 2:
        a0, a1 = res[0], res[1]

        # (prob: float, via: str)
        if isinstance(a0, (int, float)) and isinstance(a1, str):
            return _norm_prob(a0), "ok"

        # (lucky: bool, score: float, via: str, reason: str)
        if isinstance(a0, bool) and isinstance(a1, (int, float)):
            lucky = bool(a0)
            score = _norm_prob(a1)
            return (score if lucky else 0.0), "ok"

        # (prob: float, anything)
        if isinstance(a0, (int, float)):
            return _norm_prob(a0), "ok"

    # bool alone
    if isinstance(res, bool):
        return (1.0 if res else 0.0), "ok"

    return None, "unknown_shape"


def _iter(order_csv: str) -> Iterator[tuple[str, Any]]:
    """Yield (provider_name, module_or_None) according to order."""
    order = [p.strip().lower() for p in (order_csv or "").split(",") if p.strip()]
    if not order:
        order = ["gemini", "groq"]

    # IMPORTANT:
    # - In this codebase, LPG uses GEMINI_* keys (Groq API keys).
    # - Therefore, both 'gemini' and 'groq' map to nixe.helpers.gemini_bridge (Groq LPG path).
    # - google-generativeai path is only used when explicitly requested as 'gemini_rules'.
    mapping = {
        "gemini": "nixe.helpers.gemini_bridge",
        "groq": "nixe.helpers.gemini_bridge",
        "gemini_rules": "nixe.helpers.gemini_bridge_lucky_rules",
        "rules": "nixe.helpers.gemini_bridge_lucky_rules",
    }

    for name in order:
        mod_path = mapping.get(name)
        if not mod_path:
            yield name, None
            continue
        yield name, _try_import(mod_path)


async def _maybe_await(x: Any):
    if asyncio.iscoroutine(x) or asyncio.isfuture(x):
        return await x
    return x


async def classify_with_image_bytes(img_bytes: bytes, *, order_csv: str | None = None) -> Tuple[float, str]:
    """Try providers in order until one returns a usable probability."""
    order = order_csv or os.getenv(
        "LPG_IMAGE_PROVIDER_ORDER",
        os.getenv("LPG_PROVIDER_ORDER", "gemini,groq"),
    )

    last = "provider_unavailable"
    for name, mod in _iter(order):
        if not mod:
            last = f"{name}:unavailable"
            continue

        # Try common LPG entrypoints
        for fn_name in ("classify_lucky_pull_bytes", "classify_lucky_pull_bytes_suspicious"):
            fn = getattr(mod, fn_name, None)
            if not fn:
                continue

            try:
                res = await _maybe_await(fn(img_bytes))
            except Exception as e:
                last = f"{name}:err:{type(e).__name__}"
                continue

            prob, status = _call_to_prob(res)
            if isinstance(prob, float):
                return prob, f"{name}:{fn_name}"
            last = f"{name}:{status}"

    return 0.0, last


async def classify_with_text(text: str, *, order_csv: str | None = None) -> Tuple[float, str]:
    """Text-only fallback (rare)."""
    order = order_csv or os.getenv("LPG_PROVIDER_ORDER", "gemini,groq")
    last = "provider_unavailable"

    for name, mod in _iter(order):
        if not mod:
            last = f"{name}:unavailable"
            continue

        for fn_name in ("classify_lucky_pull", "classify_lucky_pull_text"):
            fn = getattr(mod, fn_name, None)
            if not fn:
                continue

            try:
                res = await _maybe_await(fn(text))
            except Exception as e:
                last = f"{name}:err:{type(e).__name__}"
                continue

            prob, status = _call_to_prob(res)
            if isinstance(prob, float):
                return prob, f"{name}:{fn_name}"
            last = f"{name}:{status}"

    return 0.0, last


def classify(text: str, order_csv: str | None = None):
    """Convenience wrapper for scripts: returns (prob, via). If called inside an event loop, returns awaitable."""
    try:
        asyncio.get_running_loop()
        return classify_with_text(text, order_csv=order_csv)
    except RuntimeError:
        return asyncio.run(classify_with_text(text, order_csv=order_csv))


def classify_image(img_bytes: bytes, order_csv: str | None = None):
    """Convenience wrapper for scripts: returns (prob, via). If called inside an event loop, returns awaitable."""
    try:
        asyncio.get_running_loop()
        return classify_with_image_bytes(img_bytes, order_csv=order_csv)
    except RuntimeError:
        return asyncio.run(classify_with_image_bytes(img_bytes, order_csv=order_csv))
