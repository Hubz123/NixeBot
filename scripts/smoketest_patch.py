#!/usr/bin/env python3
"""Patch smoke test (offline)

Validates:
- no_timeout_patch.apply_all_patches() loads and applies without crashing
- lp_policy_patch.apply_policy_patch() actually enforces veto rules
- guard defer patch converts asyncio.TimeoutError into deferred_noexec

This script is intended to be run from repo root:
  python scripts/smoketest_patch.py

It does NOT require Discord login.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def import_by_path(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def ensure_files() -> None:
    required = [
        ROOT / "nixe/helpers/overlay_utils/no_timeout_patch.py",
        ROOT / "nixe/helpers/overlay_utils/lp_policy_patch.py",
    ]
    for p in required:
        assert p.is_file(), f"missing {p}"


def _install_minimal_guard_stub() -> None:
    # Provide a tiny guard module for patch_guard_defer to hook.
    if "nixe.cogs.a00_lpg_thread_bridge_guard" in sys.modules:
        return

    gm = types.ModuleType("nixe.cogs.a00_lpg_thread_bridge_guard")
    sys.modules["nixe.cogs.a00_lpg_thread_bridge_guard"] = gm

    class DummyGuard:
        timeout = 0.1

        async def classify_lucky_pull_bytes(self, b: bytes):
            await asyncio.sleep(1.0)
            return (False, 0.0, "none", "never")

        async def _classify(self, b: bytes):
            return await asyncio.wait_for(self.classify_lucky_pull_bytes(b), timeout=self.timeout)

    gm.DummyGuard = DummyGuard


async def main() -> None:
    ensure_files()

    # Defaults used by patches.
    os.environ.setdefault("LPG_SHIELD_ENABLE", "0")
    os.environ.setdefault("LPG_BRIDGE_ALLOW_QUICK_FALLBACK", "0")
    os.environ.setdefault("LPG_DEFER_ON_TIMEOUT", "1")
    os.environ.setdefault("LPG_GUARD_LASTCHANCE_MS", "1800")
    os.environ.setdefault("GROQ_LUCKY_THRESHOLD", "0.92")

    # Provide a stub gemini_bridge module so this smoke test stays offline.
    bridge = sys.modules.get("nixe.helpers.gemini_bridge")
    if bridge is None:
        bridge = types.ModuleType("nixe.helpers.gemini_bridge")
        sys.modules["nixe.helpers.gemini_bridge"] = bridge

    current_payload: dict = {}

    async def dummy_classify(_image_bytes: bytes, *args, **kwargs):
        payload = dict(current_payload)
        score = float(payload.get("score", 0.99))
        return (True, score, "gemini:model", "classified", json.dumps(payload, ensure_ascii=False))

    # IMPORTANT: install dummy BEFORE applying policy patch so it wraps the dummy.
    bridge.classify_lucky_pull_bytes = dummy_classify

    _install_minimal_guard_stub()

    ntp = import_by_path("no_timeout_patch", ROOT / "nixe/helpers/overlay_utils/no_timeout_patch.py")
    lpp = import_by_path("lp_policy_patch", ROOT / "nixe/helpers/overlay_utils/lp_policy_patch.py")

    ntp.apply_all_patches()
    lpp.apply_policy_patch()
    print("[OK] patches applied")

    async def run_case(payload: dict, expect: bool, label: str):
        nonlocal current_payload
        current_payload = payload
        ok, score, via, reason = await bridge.classify_lucky_pull_bytes(b"1")
        print(f"[LP] {label}: ok={ok} score={score} via={via} reason={reason}")
        assert bool(ok) == bool(expect), f"{label} expected {expect} got {ok} (reason={reason})"

    inv = {
        "is_lucky": True,
        "score": 0.95,
        "features": {
            "has_10_pull_grid": False,
            "has_result_text": False,
            "rarity_gold_5star_present": False,
            "is_inventory_or_loadout_ui": True,
            "is_shop_or_guide_card": False,
            "single_item_or_upgrade_ui": False,
            "dominant_purple_but_no_other_signals": False,
        },
    }
    guide = {
        "is_lucky": True,
        "score": 0.95,
        "features": {
            "has_10_pull_grid": False,
            "has_result_text": True,
            "rarity_gold_5star_present": False,
            "is_inventory_or_loadout_ui": False,
            "is_shop_or_guide_card": True,
            "single_item_or_upgrade_ui": False,
            "dominant_purple_but_no_other_signals": False,
        },
    }
    purple = {
        "is_lucky": True,
        "score": 0.95,
        "features": {
            "has_10_pull_grid": False,
            "has_result_text": False,
            "rarity_gold_5star_present": False,
            "is_inventory_or_loadout_ui": False,
            "is_shop_or_guide_card": False,
            "single_item_or_upgrade_ui": False,
            "dominant_purple_but_no_other_signals": True,
        },
    }
    okres = {
        "is_lucky": True,
        "score": 0.95,
        "features": {
            "has_10_pull_grid": True,
            "has_result_text": True,
            "rarity_gold_5star_present": False,
            "is_inventory_or_loadout_ui": False,
            "is_shop_or_guide_card": False,
            "single_item_or_upgrade_ui": False,
            "dominant_purple_but_no_other_signals": False,
        },
    }

    await run_case(inv, False, "inventory_veto")
    await run_case(guide, False, "guide_veto")
    await run_case(purple, False, "purple_only_veto")
    await run_case(okres, True, "true_result")

    import nixe.cogs.a00_lpg_thread_bridge_guard as guard_mod

    g = guard_mod.DummyGuard()
    ok, score, via, reason = await g._classify(b"x")
    print("[TIMEOUT]", ok, score, via, reason)
    assert str(reason).startswith("deferred"), f"expected deferred*, got {reason!r}"
    print("[OK] all smoketests passed")


if __name__ == "__main__":
    asyncio.run(main())
