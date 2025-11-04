from __future__ import annotations
import os

def _split_set(val: str, default: str) -> set[str]:
    src = val if val is not None else default
    return {s.strip().lower() for s in src.split(",") if s.strip()}

def _get_float(*keys, default: float) -> float:
    for k in keys:
        if not k: continue
        v = os.getenv(k)
        if v is None: continue
        try: return float(v)
        except Exception: continue
    return float(default)

def should_run_persona(ctx: dict) -> tuple[bool, str]:
    allowed_kinds = _split_set(os.getenv("LPG_PERSONA_ONLY_FOR"), "lucky")
    allowed_providers = _split_set(os.getenv("LPG_PERSONA_ALLOWED_PROVIDERS"), "gemini")
    strict = os.getenv("LPG_PERSONA_STRICT", "1") == "1"
    min_score = _get_float("LPG_PERSONA_MIN_SCORE", "GEMINI_LUCKY_THRESHOLD", default=0.85)

    kind = str(ctx.get("kind", "") or ctx.get("lpg_kind", "") or "").lower()
    provider = str(ctx.get("provider", "")).lower()
    is_phish = bool(ctx.get("is_phish", False) or (str(ctx.get("reason","")).lower() == "phish"))
    ok = bool(ctx.get("ok", False))
    try: score = float(ctx.get("score", 0.0))
    except Exception: score = 0.0

    if is_phish: return (False, "skip: phishing context")
    if not ok: return (False, "skip: classification not ok")
    if score < min_score: return (False, f"skip: score {score:.3f} < min {min_score:.3f}")
    if kind and kind not in allowed_kinds: return (False, f"skip: kind '{kind}' not in {sorted(allowed_kinds)}")
    if provider:
        ok_prov = any(provider.startswith(prefix) for prefix in allowed_providers)
        if not ok_prov: return (False, f"skip: provider '{provider}' not in {sorted(allowed_providers)}")
    elif strict: return (False, "skip: provider unknown and STRICT=1")
    return (True, "ok")
