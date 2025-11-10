#!/usr/bin/env python3
"""
Smoketest (path-loader) v2 — ensures LP policy wraps the function.
Run: python scripts/smoketest_patch.py
"""
import os, sys, types, asyncio, json, importlib.util, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

def fail(msg):  print(f"[FAIL] {msg}"); sys.exit(1)
def ok(msg):    print(f"[OK] {msg}")

def ensure_files():
    needed = [
        ROOT/"nixe/cogs/a00z_lpg_no_timeout_overlay.py",
        ROOT/"nixe/helpers/overlay_utils/no_timeout_patch.py",
        ROOT/"nixe/helpers/overlay_utils/lp_policy_patch.py",
    ]
    for p in needed:
        if not p.is_file():
            fail(f"missing file: {p}")
    ok("overlay files exist")

def set_env():
    os.environ.setdefault("LPG_SHIELD_ENABLE", "0")
    os.environ.setdefault("LPG_BRIDGE_ALLOW_QUICK_FALLBACK", "0")
    os.environ.setdefault("LPG_DEFER_ON_TIMEOUT", "1")
    os.environ.setdefault("LPG_GUARD_LASTCHANCE_MS", "0")
    os.environ.setdefault("GEMINI_LUCKY_THRESHOLD", "0.92")

def import_by_path(mod_name: str, file_path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(mod_name, str(file_path))
    if spec is None or spec.loader is None:
        fail(f"cannot create spec for {mod_name} at {file_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod

def import_overlays_path():
    ntp = import_by_path("no_timeout_patch", ROOT/"nixe/helpers/overlay_utils/no_timeout_patch.py")
    lpp = import_by_path("lp_policy_patch", ROOT/"nixe/helpers/overlay_utils/lp_policy_patch.py")
    return ntp.apply_all_patches, lpp.apply_policy_patch

# Global test payload to be returned by dummy classify
_TEST_PAYLOAD = None

async def _dummy_classify(_bytes: bytes):
    global _TEST_PAYLOAD
    payload = _TEST_PAYLOAD or {"is_lucky": False, "score": 0.0, "features": {}}
    return (True, payload.get("score", 0.99), "gemini:model", "classified", json.dumps(payload))

async def run_policy_case(bridge, payload, expect_exec: bool, label: str):
    global _TEST_PAYLOAD
    _TEST_PAYLOAD = payload
    # Call the (wrapped) function — wrapper should enforce policy
    res = await bridge.classify_lucky_pull_bytes(b"123")
    ok_flag = bool(res[0])
    print(f"[LP] {label}: {'EXEC' if ok_flag else 'NOEXEC'}  via={res[2]} reason={res[3]}")
    if ok_flag != expect_exec:
        fail(f"policy mismatch on {label}")

async def main():
    ensure_files()
    set_env()

    # Prepare leaf modules FIRST and install dummy classify BEFORE applying policy
    if "nixe.helpers.gemini_bridge" not in sys.modules:
        mb = types.ModuleType("nixe.helpers.gemini_bridge")
        sys.modules["nixe.helpers.gemini_bridge"] = mb
    bridge = sys.modules["nixe.helpers.gemini_bridge"]
    setattr(bridge, "classify_lucky_pull_bytes", _dummy_classify)

    if "nixe.cogs.a00_lpg_thread_bridge_guard" not in sys.modules:
        gm = types.ModuleType("nixe.cogs.a00_lpg_thread_bridge_guard")
        sys.modules["nixe.cogs.a00_lpg_thread_bridge_guard"] = gm
        class DummyGuard:
            timeout = 0.1
            async def classify_lucky_pull_bytes(self, b: bytes):
                await asyncio.sleep(1.0)
                return (False, 0.0, "none", "should_not_happen")
            async def _classify(self, b: bytes):
                return await asyncio.wait_for(self.classify_lucky_pull_bytes(b), timeout=self.timeout)
        gm.DummyGuard = DummyGuard
    guard_mod = sys.modules["nixe.cogs.a00_lpg_thread_bridge_guard"]

    # Now import overlays and apply patches (they will wrap our dummy)
    apply_all_patches, apply_policy_patch = import_overlays_path()
    apply_all_patches()
    apply_policy_patch()
    ok("patches applied")

    # Payloads
    inv = {"is_lucky": True, "score": 0.99, "features":{"has_10_pull_grid": False, "has_result_text": False, "rarity_gold_5star_present": False, "is_inventory_or_loadout_ui": True, "is_shop_or_guide_card": False, "single_item_or_upgrade_ui": False, "dominant_purple_but_no_other_signals": False}}
    guide = {"is_lucky": True, "score": 0.99, "features":{"has_10_pull_grid": False, "has_result_text": True,  "rarity_gold_5star_present": False, "is_inventory_or_loadout_ui": False,"is_shop_or_guide_card": True,  "single_item_or_upgrade_ui": False, "dominant_purple_but_no_other_signals": False}}
    purple= {"is_lucky": True, "score": 0.99, "features":{"has_10_pull_grid": False, "has_result_text": False, "rarity_gold_5star_present": False, "is_inventory_or_loadout_ui": False,"is_shop_or_guide_card": False, "single_item_or_upgrade_ui": False, "dominant_purple_but_no_other_signals": True}}
    okres = {"is_lucky": True, "score": 0.95, "features":{"has_10_pull_grid": True,  "has_result_text": True,  "rarity_gold_5star_present": False,"is_inventory_or_loadout_ui": False,"is_shop_or_guide_card": False,"single_item_or_upgrade_ui": False,"dominant_purple_but_no_other_signals": False}}

    await run_policy_case(bridge, inv,    False, "inventory_veto")
    await run_policy_case(bridge, guide,  False, "guide_veto")
    await run_policy_case(bridge, purple, False, "purple_only_veto")
    await run_policy_case(bridge, okres,   True, "true_result")

    if hasattr(guard_mod, "DummyGuard"):
        g = guard_mod.DummyGuard()
        ok2, score2, via, reason = await g._classify(b"xxx")
        print(f"[TIMEOUT] via={via} reason={reason}")
        if not isinstance(reason, str) or not reason.startswith("deferred"):
            fail("no-timeout patch failed (expect 'deferred_noexec')")

    ok("all smoketests passed")

if __name__ == "__main__":
    asyncio.run(main())
