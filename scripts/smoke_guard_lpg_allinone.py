
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
    try: return binascii.hexlify(b[:8]).decode("ascii")
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
        return Path(_project_root or ".").joinpath("nixe/cogs/a00_phish_first_touchdown_autoban.py").exists()
    except Exception:
        return False

def _env_bool(key: str, default: str="0") -> bool:
    return os.getenv(key, default) == "1"

def _parse_guard_channels() -> list[int]:
    raw = os.getenv("LUCKYPULL_GUARD_CHANNELS") or os.getenv("LPG_GUARD_CHANNELS") or ""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        import json as _json
        try:
            arr = _json.loads(raw)
            return [int(x) for x in arr if str(x).isdigit()]
        except Exception:
            pass
    return [int(x) for x in raw.replace(" ","").split(",") if x.strip().isdigit()]

def _burst_timeline(n: int, classify_sec: float, workers: int):
    free_at = [0.0]*max(1,workers)
    out = []
    for i in range(n):
        k = min(range(len(free_at)), key=lambda j: free_at[j])
        start = free_at[k]
        finish = start + classify_sec
        free_at[k] = finish
        out.append((i+1, start, finish))
    makespan = max((f for _,_,f in out), default=0.0)
    return out, makespan

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--img", default=None)
    ap.add_argument("--as-channel", type=int, default=None)
    ap.add_argument("--burst", type=int, default=0)
    ap.add_argument("--classify-sec", type=float, default=0.8)
    args = ap.parse_args()

    status = load_hybrid()
    print("=== ENV HYBRID CHECK ===")
    print(json.dumps(status, indent=2))

    print("\n=== POLICY (effective) ===")
    print(f"[POLICY] PHISH uses provider: {os.getenv('PHISH_PROVIDER','groq')}")
    print(f"[POLICY] Persona only for: {os.getenv('LPG_PERSONA_ONLY_FOR','lucky')}")
    print(f"[POLICY] Persona allowed providers: {os.getenv('LPG_PERSONA_ALLOWED_PROVIDERS','gemini')}")

    if args.as_channel:
        guards = _parse_guard_channels()
        print(f"\n[CHANNEL] simulate channel={args.as_channel} | guard_channels={guards} | in_guard={args.as_channel in guards}")

    if args.img:
        res = _simulate_classify(args.img)
        src_info = f"[SMOKE] src={os.path.basename(args.img)} len={res.get('len',0)} hex8={res.get('hex8','')} (ffd8=jpeg?={'ffd8' in res.get('hex8','')})"
        tag = "LP" if res["kind"]=="lucky" else "PHISH" if res["kind"]=="phish" else "OTHER"
        print("\n=== CLASSIFY (sim) ===")
        print(src_info)
        print(f"[{tag}] ok={res['ok']} score={res['score']:.2f} provider={res['provider']} via={res['via']} reason={res['reason']}")

        okp, why = should_run_persona({
            "kind": res["kind"], "provider": res["provider"],
            "is_phish": res["kind"]=="phish",
            "ok": res["ok"], "score": res["score"],
            "reason": "phish" if res["kind"]=="phish" else "lucky" if res["kind"]=="lucky" else "other",
        })
        print("\n=== PERSONA DECISION (from classify) ===")
        print(f"persona -> {okp} ({why})")

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
            guard_channels = _parse_guard_channels()
            redirect = os.getenv("LUCKYPULL_REDIRECT_CHANNEL_ID") or os.getenv("LPG_REDIRECT_CHANNEL_ID") or ""
            in_guard = (args.as_channel in guard_channels) if args.as_channel else bool(guard_channels)
            action = "DELETE" if in_guard else "NONE"
            print(f"action={action} guard_channels={guard_channels or '[]'} redirect={redirect or '-'} as_channel={args.as_channel} in_guard={in_guard}")
        else:
            print("action=NONE")

    if args.burst and args.burst > 0:
        workers = int(os.getenv("LPG_CONCURRENCY","2") or "2")
        t, makespan = _burst_timeline(args.burst, classify_sec=max(0.1, args.classify_sec), workers=workers)
        print("\n=== BURST SIMULATION (Lucky Pull) ===")
        print(f"jobs={args.burst} workers={workers} classify~{args.classify_sec:.2f}s => finishes~{makespan:.2f}s")
        for (j,st,ft) in t:
            print(f"  #{j:02d} start={st:5.2f}s finish={ft:5.2f}s")

    print("\n=== SUMMARY ===")
    print("result=OK (dry-run; no Discord actions executed)")

if __name__ == "__main__":
    main()
