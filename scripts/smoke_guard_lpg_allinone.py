#!/usr/bin/env python3
from __future__ import annotations
import os, sys, argparse, json, binascii
from pathlib import Path

def _ensure_project_root():
    here = Path(__file__).resolve()
    cur = here.parent
    for _ in range(8):
        if (cur / "nixe").exists():
            sys.path.insert(0, str(cur))
            return str(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    return None
_project_root = _ensure_project_root()

from nixe.helpers.env_hybrid import load_hybrid
from nixe.helpers.persona_gate import should_run_persona

def _hex8(b: bytes) -> str:
    import binascii as _b
    try: return _b.hexlify(b[:8]).decode("ascii")
    except Exception: return ""

def _read_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as f: return f.read()
    except Exception:
        return b""

def _simulate_classify(img_path: str) -> dict:
    full = (img_path or "").lower()
    b = _read_bytes(img_path) if img_path else b""
    n = len(b); first8 = _hex8(b)
    groq_model = os.getenv("GROQ_MODEL","llama-3.1-8b-instant")
    gem_model  = os.getenv("GEMINI_MODEL","gemini-2.5-flash-lite")
    if n and n < 1024:
        return {"ok": False, "kind": "other", "score": 0.0,
                "provider": f"gemini:{gem_model}", "via":"sim_stub",
                "reason": f"image_too_small(len={n})", "len": n, "hex8": first8}
    if any(k in full for k in ["phish","phising","phishing","scam","withdraw","tebaran"]):
        return {"ok": True, "kind": "phish", "score": 0.97,
                "provider": f"groq:{groq_model}", "via":"sim_stub",
                "reason":"path_hint(phish)", "len": n, "hex8": first8}
    if any(k in full for k in ["lucky","gacha","pull"]):
        return {"ok": True, "kind": "lucky", "score": 0.95,
                "provider": f"gemini:{gem_model}", "via":"sim_stub",
                "reason":"path_hint(lucky)", "len": n, "hex8": first8}
    return {"ok": False, "kind": "other", "score": 0.05,
            "provider": f"gemini:{gem_model}", "via":"sim_stub",
            "reason":"neutral_fallback(not_gacha_like)", "len": n, "hex8": first8}

def _first_touchdown_cog_present() -> bool:
    try:
        # typical path
        return Path(_project_root or ".").joinpath("nixe/cogs/a00_phish_first_touchdown_autoban.py").exists()
    except Exception:
        return False

def _env_bool(key: str, default: str="0") -> bool:
    return os.getenv(key, default) == "1"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--img", default=None)
    args = ap.parse_args()

    status = load_hybrid()
    print("=== ENV HYBRID CHECK ===")
    print(json.dumps(status, indent=2))

    print("\n=== POLICY (effective) ===")
    print(f"[POLICY] PHISH uses provider: {os.getenv('PHISH_PROVIDER','groq')}")
    print(f"[POLICY] Persona only for: {os.getenv('LPG_PERSONA_ONLY_FOR','lucky')}")
    print(f"[POLICY] Persona allowed providers: {os.getenv('LPG_PERSONA_ALLOWED_PROVIDERS','gemini')}")

    if args.img:
        res = _simulate_classify(args.img)
        src_info = f"[SMOKE] src={os.path.basename(args.img)} len={res.get('len',0)} hex8={res.get('hex8','')} (ffd8=jpeg?={'ffd8' in res.get('hex8','')})"
        tag = "LP" if res["kind"]=="lucky" else "PHISH" if res["kind"]=="phish" else "OTHER"
        print("\n=== CLASSIFY (sim) ===")
        print(src_info)
        print(f"[{tag}] ok={res['ok']} score={res['score']:.2f} provider={res['provider']} via={res['via']} reason={res['reason']}")

        # Persona decision preview
        okp, why = should_run_persona({
            "kind": res["kind"], "provider": res["provider"],
            "is_phish": res["kind"]=="phish",
            "ok": res["ok"], "score": res["score"],
            "reason": "phish" if res["kind"]=="phish" else "lucky" if res["kind"]=="lucky" else "other",
        })
        print("\n=== PERSONA DECISION (from classify) ===")
        print(f"persona -> {okp} ({why})")

        # ---- DRY-RUN MODERATION DECISION ----
        print("\n=== DECISION (dry-run) ===")
        if res["kind"] == "phish":
            ft_present = _first_touchdown_cog_present()
            policy_autoban = any([
                _env_bool("BAN_ON_FIRST_PHISH","1"),
                _env_bool("PHISH_AUTOBAN","1"),
                _env_bool("FIRST_TOUCHDOWN_AUTOBAN_ENABLE","1"),
            ])
            delete_days = int(os.getenv("PHISH_DELETE_MESSAGE_DAYS","7") or "7")
            reason = os.getenv("BAN_REASON","HACK ACCOUNT")
            ttl = int(os.getenv("BAN_EMBED_TTL_SEC","15") or "15")
            action = "BAN" if (ft_present or policy_autoban) else "FLAG_ONLY"
            print(f"action={action} reason='{reason}' delete_message_days={delete_days} embed_ttl={ttl}s first_touchdown_cog={ft_present} policy_autoban={policy_autoban}")
        elif res["kind"] == "lucky":
            # Lucky Pull: delete only (if guard configured)
            guard_channels = os.getenv("LUCKYPULL_GUARD_CHANNELS") or os.getenv("LPG_GUARD_CHANNELS") or ""
            redirect = os.getenv("LUCKYPULL_REDIRECT_CHANNEL_ID") or os.getenv("LPG_REDIRECT_CHANNEL_ID") or ""
            action = "DELETE" if guard_channels else "NONE"
            print(f"action={action} guard_channels={guard_channels or '[]'} redirect={redirect or '-'}")
        else:
            print("action=NONE")

    print("\n=== SUMMARY ===")
    print("result=OK (dry-run; no Discord actions executed)")

if __name__ == "__main__":
    main()
