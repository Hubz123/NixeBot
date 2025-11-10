
import os, sys, types, asyncio, json

def setup_env():
    os.environ.setdefault("LPG_SHIELD_ENABLE", "0")
    os.environ.setdefault("LPG_BRIDGE_ALLOW_QUICK_FALLBACK", "0")
    os.environ.setdefault("LPG_DEFER_ON_TIMEOUT", "1")
    os.environ.setdefault("LPG_GUARD_LASTCHANCE_MS", "0")
    os.environ.setdefault("GEMINI_LUCKY_THRESHOLD", "0.92")

def prepare_modules():
    pkg = types.ModuleType("nixe")
    helpers = types.ModuleType("nixe.helpers")
    bridge = types.ModuleType("nixe.helpers.gemini_bridge")
    sys.modules["nixe"] = pkg
    sys.modules["nixe.helpers"] = helpers
    sys.modules["nixe.helpers.gemini_bridge"] = bridge

    guard_mod = types.ModuleType("nixe.cogs.a00_lpg_thread_bridge_guard")
    sys.modules["nixe.cogs.a00_lpg_thread_bridge_guard"] = guard_mod

    async def classify_lucky_pull_bytes_dummy(image_bytes: bytes):
        return (True, 0.99, "gemini:model", "classified", json.dumps({"is_lucky":True,"score":0.99,"features":{"has_10_pull_grid":True,"has_result_text":True,"rarity_gold_5star_present":True,"is_inventory_or_loadout_ui":False,"is_shop_or_guide_card":False,"single_item_or_upgrade_ui":False,"dominant_purple_but_no_other_signals":False}}))
    bridge.classify_lucky_pull_bytes = classify_lucky_pull_bytes_dummy

    class DummyGuard:
        timeout = 0.1
        async def classify_lucky_pull_bytes(self, b: bytes):
            await asyncio.sleep(1.0)
            return (False, 0.0, "none", "should_not_happen")
        async def _classify(self, b: bytes):
            return await asyncio.wait_for(self.classify_lucky_pull_bytes(b), timeout=self.timeout)

    guard_mod.DummyGuard = DummyGuard

def apply_patches():
    from nixe.helpers.overlay_utils.no_timeout_patch import apply_all_patches
    from nixe.helpers.overlay_utils.lp_policy_patch import apply_policy_patch
    apply_all_patches()
    apply_policy_patch()

def test_policy_and_timeout():
    setup_env()
    prepare_modules()
    apply_patches()

    import nixe.helpers.gemini_bridge as bridge
    import nixe.cogs.a00_lpg_thread_bridge_guard as guard_mod

    async def run_policy_case(payload, expect_exec: bool):
        async def fake(image_bytes: bytes):
            return (True, payload.get("score", 0.99), "gemini:model", "classified", json.dumps(payload))
        setattr(bridge, "classify_lucky_pull_bytes", fake)
        res = asyncio.get_event_loop().run_until_complete(bridge.classify_lucky_pull_bytes(b"123"))
        ok = bool(res[0])
        assert ok == expect_exec

    inv = {"is_lucky": True, "score": 0.99, "features":{"has_10_pull_grid": False, "has_result_text": False, "rarity_gold_5star_present": False, "is_inventory_or_loadout_ui": True, "is_shop_or_guide_card": False, "single_item_or_upgrade_ui": False, "dominant_purple_but_no_other_signals": False}}
    guide = {"is_lucky": True, "score": 0.99, "features":{"has_10_pull_grid": False, "has_result_text": True,  "rarity_gold_5star_present": False, "is_inventory_or_loadout_ui": False,"is_shop_or_guide_card": True,  "single_item_or_upgrade_ui": False, "dominant_purple_but_no_other_signals": False}}
    purple= {"is_lucky": True, "score": 0.99, "features":{"has_10_pull_grid": False, "has_result_text": False, "rarity_gold_5star_present": False, "is_inventory_or_loadout_ui": False,"is_shop_or_guide_card": False, "single_item_or_upgrade_ui": False, "dominant_purple_but_no_other_signals": True}}
    okres = {"is_lucky": True, "score": 0.95, "features":{"has_10_pull_grid": True,  "has_result_text": True,  "rarity_gold_5star_present": False,"is_inventory_or_loadout_ui": False,"is_shop_or_guide_card": False,"single_item_or_upgrade_ui": False,"dominant_purple_but_no_other_signals": False}}

    run_policy_case(inv,    False)
    run_policy_case(guide,  False)
    run_policy_case(purple, False)
    run_policy_case(okres,   True)

    g = guard_mod.DummyGuard()
    ok, score, via, reason = asyncio.get_event_loop().run_until_complete(g._classify(b'xxx'))
    assert reason.startswith("deferred")
