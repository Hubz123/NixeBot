import os, json, logging, inspect
logger = logging.getLogger(__name__)

THR_DEFAULT = 0.92
def _thr():
    try:
        return float(os.getenv("GROQ_LUCKY_THRESHOLD", str(THR_DEFAULT)))
    except Exception:
        return THR_DEFAULT

def _apply_rules(payload):
    thr = _thr()
    try:
        ok = bool(payload.get("is_lucky"))
        score = float(payload.get("score", 0.0))
        f = payload.get("features", {}) or {}
    except Exception:
        return False, 0.0, "policy", "parse_error"

    signals = int(bool(f.get("has_10_pull_grid"))) + int(bool(f.get("has_result_text"))) + int(bool(f.get("rarity_gold_5star_present")))
    veto = any([
        f.get("is_inventory_or_loadout_ui"),
        f.get("is_shop_or_guide_card"),
        f.get("single_item_or_upgrade_ui"),
        f.get("dominant_purple_but_no_other_signals"),
    ])

    if score < thr:    return False, score, "policy", "below_threshold"
    if signals < 2:    return False, score, "policy", "insufficient_signals"
    if veto:           return False, score, "policy", "veto_context"
    if ok:             return True,  score, "policy", "gemini_confirmed"
    return False, score, "policy", "gemini_said_no"

def apply_policy_patch():
    targets = ["nixe.helpers.gemini_bridge","nixe.helpers.lp_gemini_helper","nixe.helpers.gemini_lpg_bridge"]
    for modname in targets:
        try:
            mod = __import__(modname, fromlist=["*"])
        except Exception:
            continue
        for fn_name in dir(mod):
            fn = getattr(mod, fn_name, None)
            if not callable(fn) or not inspect.iscoroutinefunction(fn):
                continue
            if "classify" in fn_name and "lucky" in fn_name:
                async def wrapped(*a, **kw):
                    res = await fn(*a, **kw)
                    try:
                        if isinstance(res, (list, tuple)) and len(res) >= 4:
                            ok, score, via, reason = res[:4]
                            payload = None
                            if len(res) >= 5 and isinstance(res[4], (str, bytes)):
                                txt = res[4].decode("utf-8") if isinstance(res[4], bytes) else res[4]
                                try:
                                    payload = json.loads(txt)
                                except Exception:
                                    payload = None
                            if payload is None:
                                return res
                            ok2, score2, via2, reason2 = _apply_rules(payload)
                            return (ok2, float(score2), f"{via}:{via2}", reason2)
                    except Exception as e:
                        logger.warning("[nixe-policy] wrapper error: %s", e)
                    return res
                setattr(mod, fn_name, wrapped)
                logger.info("[nixe-policy] wrapped (lpg/groq) %s.%s for LP policy", mod.__name__, fn_name)
