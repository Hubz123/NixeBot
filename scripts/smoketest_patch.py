#!/usr/bin/env python3
import os, sys, types, asyncio, json, importlib.util, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
def import_by_path(n,p):
    spec=importlib.util.spec_from_file_location(n,str(p)); m=importlib.util.module_from_spec(spec); sys.modules[n]=m; spec.loader.exec_module(m); return m
def ensure():
    for p in [ROOT/'nixe/cogs/a00z_lpg_no_timeout_overlay.py', ROOT/'nixe/helpers/overlay_utils/no_timeout_patch.py', ROOT/'nixe/helpers/overlay_utils/lp_policy_patch.py']:
        assert p.is_file(), f'missing {p}'
async def main():
    ensure()
    os.environ.setdefault('LPG_SHIELD_ENABLE','0'); os.environ.setdefault('LPG_BRIDGE_ALLOW_QUICK_FALLBACK','0')
    os.environ.setdefault('LPG_DEFER_ON_TIMEOUT','1'); os.environ.setdefault('LPG_GUARD_LASTCHANCE_MS','1800'); os.environ.setdefault('GEMINI_LUCKY_THRESHOLD','0.92')
    if 'nixe.helpers.gemini_bridge' not in sys.modules: sys.modules['nixe.helpers.gemini_bridge']=types.ModuleType('nixe.helpers.gemini_bridge')
    if 'nixe.cogs.a00_lpg_thread_bridge_guard' not in sys.modules:
        gm=types.ModuleType('nixe.cogs.a00_lpg_thread_bridge_guard'); sys.modules['nixe.cogs.a00_lpg_thread_bridge_guard']=gm
        class DummyGuard:
            timeout=0.1
            async def classify_lucky_pull_bytes(self,b): import asyncio; await asyncio.sleep(1.0); return (False,0.0,'none','never')
            async def _classify(self,b): import asyncio; return await asyncio.wait_for(self.classify_lucky_pull_bytes(b), timeout=self.timeout)
        gm.DummyGuard=DummyGuard
    ntp=import_by_path('no_timeout_patch',ROOT/'nixe/helpers/overlay_utils/no_timeout_patch.py'); lpp=import_by_path('lp_policy_patch',ROOT/'nixe/helpers/overlay_utils/lp_policy_patch.py')
    ntp.apply_all_patches(); lpp.apply_policy_patch(); print('[OK] patches applied')
    import nixe.helpers.gemini_bridge as bridge
    async def run(payload, expect, label):
        async def dummy(_): return (True, payload.get('score',0.99),'gemini:model','classified', json.dumps(payload))
        bridge.classify_lucky_pull_bytes=dummy; res=await bridge.classify_lucky_pull_bytes(b'1'); ok=bool(res[0]); print(f'[LP] {label}:', 'EXEC' if ok else 'NOEXEC', res[2], res[3]); assert ok==expect
    inv={'is_lucky':True,'score':0.95,'features':{'has_10_pull_grid':False,'has_result_text':False,'rarity_gold_5star_present':False,'is_inventory_or_loadout_ui':True,'is_shop_or_guide_card':False,'single_item_or_upgrade_ui':False,'dominant_purple_but_no_other_signals':False}}
    guide={'is_lucky':True,'score':0.95,'features':{'has_10_pull_grid':False,'has_result_text':True,'rarity_gold_5star_present':False,'is_inventory_or_loadout_ui':False,'is_shop_or_guide_card':True,'single_item_or_upgrade_ui':False,'dominant_purple_but_no_other_signals':False}}
    purple={'is_lucky':True,'score':0.95,'features':{'has_10_pull_grid':False,'has_result_text':False,'rarity_gold_5star_present':False,'is_inventory_or_loadout_ui':False,'is_shop_or_guide_card':False,'single_item_or_upgrade_ui':False,'dominant_purple_but_no_other_signals':True}}
    okres={'is_lucky':True,'score':0.95,'features':{'has_10_pull_grid':True,'has_result_text':True,'rarity_gold_5star_present':False,'is_inventory_or_loadout_ui':False,'is_shop_or_guide_card':False,'single_item_or_upgrade_ui':False,'dominant_purple_but_no_other_signals':False}}
    await run(inv,False,'inventory_veto'); await run(guide,False,'guide_veto'); await run(purple,False,'purple_only_veto'); await run(okres,True,'true_result')
    import nixe.cogs.a00_lpg_thread_bridge_guard as guard_mod; g=guard_mod.DummyGuard(); ok,score,via,reason=await g._classify(b'x'); print('[TIMEOUT]',via,reason); assert str(reason).startswith('deferred'); print('[OK] all smoketests passed')
if __name__=='__main__': import asyncio; asyncio.run(main())
