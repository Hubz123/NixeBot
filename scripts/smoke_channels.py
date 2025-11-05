# scripts/smoke_channels.py
"""
Smoke check for channel lists & IDs (RELAXED JSON SUPPORTED).
- Accepts JSON with // comments, /* */ comments, trailing commas, and non-ASCII keys.
- Validates list keys: duplicates, links/mentions, non-numeric, too-short.
- Validates single channel keys: numeric & length.
- Cross-check: redirect not inside guard lists, LOG channel consistency.
- Outputs sanitized CSV suggestions for list keys (deduped, normalized, sorted).

Usage:
  python scripts/smoke_channels.py [path/to/runtime_env.json] [--write-sanitized out.json]
"""
from __future__ import annotations

# scripts/smoke_channels.py

import os, sys, json, re

DEFAULT_ENV_PATHS = [
    os.environ.get("RUNTIME_ENV_PATH") or "nixe/config/runtime_env.json",
    "runtime_env.json",
]

LIST_KEYS = [
    "LPG_GUARD_CHANNELS",
    "LUCKYPULL_GUARD_CHANNELS",
    "PROTECT_CHANNEL_IDS",
    "FIRST_TOUCHDOWN_BYPASS_CHANNELS",
    "PHASH_MATCH_SKIP_CHANNELS",
    "MIRROR_CHANNELS",
]

SINGLE_KEYS = [
    "LOG_CHANNEL_ID",
    "NIXE_PHISH_LOG_CHAN_ID",
    "PHISH_LOG_CHAN_ID",
    "PHASH_DB_PARENT_CHANNEL_ID",
    "PHASH_DB_THREAD_ID",
    "LPG_REDIRECT_CHANNEL_ID",
    "LUCKYPULL_REDIRECT_CHANNEL_ID",
    "MIRROR_DEST_ID",
    "PHASH_DB_MESSAGE_ID"
]

def find_env_path(argv):
    for arg in argv[1:]:
        if arg.startswith("--"): continue
        if os.path.exists(arg):
            return arg
    for p in DEFAULT_ENV_PATHS:
        if p and os.path.exists(p):
            return p
    print("[FAIL] runtime_env.json not found. Provide a path.", file=sys.stderr)
    sys.exit(1)

def strip_json_comments(text: str) -> str:
    # Remove // and /* */ outside of strings
    out = []
    i = 0
    n = len(text)
    in_str = False
    esc = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        # not in string
        if ch == '"':
            in_str = True
            out.append(ch); i += 1; continue
        if ch == "/" and i+1 < n and text[i+1] == "/":
            # line comment
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i+1 < n and text[i+1] == "*":
            # block comment
            i += 2
            while i+1 < n and not (text[i] == "*" and text[i+1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch); i += 1
    return "".join(out)

def remove_trailing_commas(text: str) -> str:
    # repeatedly remove ", }" or ", ]" patterns outside strings
    # simple regex pass is usually enough
    prev = None
    cur = text
    pattern = re.compile(r',\s*([}\]])')
    # avoid removing commas inside strings by a naive approach: rough but works after comment-strip
    # repeat until stable
    for _ in range(10):
        new = pattern.sub(r'\1', cur)
        if new == cur: break
        cur = new
    return cur

def load_env_relaxed(path: str) -> dict:
    raw = open(path, "r", encoding="utf-8").read()
    txt = strip_json_comments(raw)
    txt = remove_trailing_commas(txt)
    try:
        return json.loads(txt)
    except json.JSONDecodeError as e:
        # print context
        start = max(0, e.pos - 60); end = min(len(txt), e.pos + 60)
        snippet = txt[start:end].replace("\n", "\\n")
        print(f"[ERROR] JSON parse failed at pos {e.pos}: {e.msg}\n...{snippet}...", file=sys.stderr)
        raise

def parse_list(s: str):
    if not s: return []
    return [tok.strip() for tok in str(s).split(",") if tok.strip()]

def normalize_token(tok: str):
    t = tok.strip()
    flags = {"link": False, "mention": False, "non_digit": False, "too_short": False}
    if "http://" in t or "https://" in t or "/" in t:
        flags["link"] = True
    if t.startswith("<#") and t.endswith(">"):
        flags["mention"] = True
        t = t[2:-1]
    if not t.isdigit():
        flags["non_digit"] = True
    if t.isdigit() and len(t) < 15:
        flags["too_short"] = True
    return t, flags

def sanitize_list(vals):
    seen = set(); dups = []; flagged = []; normalized = []
    for tok in vals:
        norm, flags = normalize_token(tok)
        normalized.append(norm)
        if norm in seen:
            dups.append(norm)
        else:
            seen.add(norm)
        if any(flags.values()) or (not norm.isdigit()):
            flagged.append((tok, flags))
    digits = [x for x in seen if x.isdigit()]
    digits_sorted = sorted(digits, key=int)
    return digits_sorted, dups, flagged

def check_single(val: str):
    norm, flags = normalize_token(str(val))
    ok = norm.isdigit() and not flags["too_short"] and not flags["link"] and not flags["mention"]
    return ok, flags

def main(argv):
    out_path = None
    if "--write-sanitized" in argv:
        i = argv.index("--write-sanitized")
        if i+1 < len(argv):
            out_path = argv[i+1]
        else:
            print("[FAIL] --write-sanitized requires a path", file=sys.stderr); sys.exit(2)

    path = find_env_path(argv)
    env = load_env_relaxed(path)
    print(f"[SMOKE] env path: {path}")

    all_ok = True

    print("\n== Single channel keys ==")
    for k in SINGLE_KEYS:
        if k not in env:
            print(f"[WARN] {k}: missing"); all_ok = False; continue
        ok, flags = check_single(env[k])
        if ok: print(f"[PASS] {k}: {env[k]}")
        else:
            problems = [f for f,b in flags.items() if b]
            print(f"[FAIL] {k}: {env[k]}  -> {'/'.join(problems) if problems else 'invalid'}"); all_ok = False

    print("\n== List channel keys ==")
    suggestions = {}; parsed_lists = {}
    for k in LIST_KEYS:
        vals = parse_list(env.get(k, ""))
        uniq, dups, flagged = sanitize_list(vals)
        parsed_lists[k] = set(uniq)
        suggestions[k] = ",".join(uniq)
        status = "PASS" if (not flagged and not dups) else ("WARN" if uniq else "FAIL")
        print(f"[{status}] {k}: count={len(vals)} uniq={len(uniq)}")
        if dups:
            print(f"  - Duplicates: {sorted(set(dups), key=lambda x: int(x) if x.isdigit() else 0)}")
        if flagged:
            print("  - Flagged tokens:")
            for orig, fl in flagged:
                probs = [n for n,b in fl.items() if b]
                print(f"    • {orig}  -> {','.join(probs) if probs else 'invalid'}")
        print(f"  - Suggest: {suggestions[k] if suggestions[k] else '(empty)'}")

    print("\n== Cross checks ==")
    redirs = [env.get("LPG_REDIRECT_CHANNEL_ID",""), env.get("LUCKYPULL_REDIRECT_CHANNEL_ID","")]
    guards = parsed_lists.get("LPG_GUARD_CHANNELS", set()) | parsed_lists.get("LUCKYPULL_GUARD_CHANNELS", set())
    for r in redirs:
        nr, _ = normalize_token(str(r))
        if nr and nr in guards:
            print(f"[WARN] Redirect channel ({r}) exists in guard list -> remove from guards.")
    log_ids = [env.get("LOG_CHANNEL_ID",""), env.get("NIXE_PHISH_LOG_CHAN_ID",""), env.get("PHISH_LOG_CHAN_ID","")]
    log_ids_norm = [normalize_token(str(x))[0] for x in log_ids if x]
    if len(set(log_ids_norm)) > 1:
        print(f"[WARN] LOG channel IDs not consistent: {log_ids}")
    else:
        print("[PASS] LOG channels consistent")

    print("\n== SUMMARY ==")
    print("OK" if all_ok else "CHECK ABOVE")

    if out_path:
        # write sanitized (comments removed, trailing commas fixed)
        raw = open(path, "r", encoding="utf-8").read()
        txt = strip_json_comments(raw)
        txt = remove_trailing_commas(txt)
        data = json.loads(txt)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\n[WRITE] sanitized JSON -> {out_path}")

if __name__ == "__main__":
    main(sys.argv)