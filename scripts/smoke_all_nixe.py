# scripts/smoke_all_nixe.py
"""
NixeBot — SMOKE ALL (channels + whitelist + configs + files)
v4:
- In --strict, list "diff" (order/format mismatch vs Suggest) => FAIL
- --fix-inplace / --fix-to <path> : rewrite list keys to sanitized Suggest CSV
- PASS/WARN/FAIL printed; relaxed JSON loader

Usage:
  python scripts/smoke_all_nixe.py [path/to/runtime_env.json]
    [--strict] [--no-print-pass]
    [--write-sanitized out.json] [--json out_report.json]
    [--fix-inplace] [--fix-to fixed.json]
"""
from __future__ import annotations

# scripts/smoke_all_nixe.py

import os, sys, json, re

# ---------- helpers: relaxed json loader ----------

def strip_json_comments(text: str) -> str:
    out = []; i=0; n=len(text); in_str=False; esc=False
    while i<n:
        ch=text[i]
        if in_str:
            out.append(ch)
            if esc: esc=False
            elif ch=="\\": esc=True
            elif ch=='"': in_str=False
            i+=1; continue
        if ch=='"':
            in_str=True; out.append(ch); i+=1; continue
        if ch=='/' and i+1<n and text[i+1]=='/':
            i+=2
            while i<n and text[i] not in "\r\n": i+=1
            continue
        if ch=='/' and i+1<n and text[i+1]=='*':
            i+=2
            while i+1<n and not (text[i]=='*' and text[i+1]=='/'): i+=1
            i+=2; continue
        out.append(ch); i+=1
    return "".join(out)

def remove_trailing_commas(text: str) -> str:
    pat=re.compile(r',\s*([}\]])')
    cur=text
    for _ in range(10):
        new=pat.sub(r'\1', cur)
        if new==cur: break
        cur=new
    return cur

def load_env_relaxed(path: str) -> dict:
    raw=open(path, "r", encoding="utf-8").read()
    txt=strip_json_comments(raw)
    txt=remove_trailing_commas(txt)
    return json.loads(txt)

def find_env_path(args):
    for a in args[1:]:
        if a.startswith("--"): continue
        if os.path.exists(a): return a
    for p in (os.environ.get("RUNTIME_ENV_PATH") or "", "nixe/config/runtime_env.json", "runtime_env.json"):
        if p and os.path.exists(p): return p
    print("[FAIL] runtime_env.json not found"); sys.exit(2)

# ---------- channels validation ----------

SINGLE_KEYS=[
    "LOG_CHANNEL_ID","NIXE_PHISH_LOG_CHAN_ID","PHISH_LOG_CHAN_ID",
    "PHASH_DB_PARENT_CHANNEL_ID","PHASH_DB_THREAD_ID","PHASH_DB_MESSAGE_ID",
    "LPG_REDIRECT_CHANNEL_ID","LUCKYPULL_REDIRECT_CHANNEL_ID",
    "MIRROR_DEST_ID",
]
LIST_KEYS=[
    "LPG_GUARD_CHANNELS","LUCKYPULL_GUARD_CHANNELS",
    "PROTECT_CHANNEL_IDS","FIRST_TOUCHDOWN_BYPASS_CHANNELS",
    "PHASH_MATCH_SKIP_CHANNELS","MIRROR_CHANNELS",
]

def parse_list(csv: str):
    return [t.strip() for t in str(csv or "").split(",") if str(t).strip()]

def normalize_token(tok: str):
    t=tok.strip()
    flags={"link":False,"mention":False,"non_digit":False,"too_short":False}
    if "http://" in t or "https://" in t or "/" in t: flags["link"]=True
    if t.startswith("<#") and t.endswith(">"):
        flags["mention"]=True; t=t[2:-1]
    if not t.isdigit(): flags["non_digit"]=True
    if t.isdigit() and len(t)<15: flags["too_short"]=True
    return t, flags

def sanitize_list(vals):
    seen=set(); dups=[]; flagged=[]; digits=[]
    for tok in vals:
        norm, fl = normalize_token(tok)
        if norm in seen: dups.append(norm)
        else: seen.add(norm)
        if any(fl.values()) or not norm.isdigit(): flagged.append((tok, fl))
        if norm.isdigit(): digits.append(norm)
    uniq = sorted(set(digits), key=int)
    return uniq, sorted(set(dups), key=lambda x:int(x) if x.isdigit() else 0), flagged

def as_bool(s, default=False):
    if s is None: return default
    return str(s).strip().lower() in ("1","true","yes","on","y")
def as_int(s, default=0):
    try: return int(str(s).strip())
    except: return default
def file_exists(p): return os.path.exists(p)

class Report:
    def __init__(self, print_pass=True):
        self.pass_cnt=0; self.warn_cnt=0; self.fail_cnt=0; self.logs=[]; self.print_pass=print_pass
    def _log(self, lvl, msg):
        if lvl=="PASS": self.pass_cnt+=1
        elif lvl=="WARN": self.warn_cnt+=1
        else: self.fail_cnt+=1
        if self.print_pass or lvl!="PASS":
            self.logs.append(f"[{lvl}] {msg}")
    def PASS(self, msg): self._log("PASS", msg)
    def WARN(self, msg): self._log("WARN", msg)
    def FAIL(self, msg): self._log("FAIL", msg)
    def dump(self):
        if self.logs: print("\n".join(self.logs))
    def exit_code(self, strict=False):
        if self.fail_cnt>0: return 2
        if strict and self.warn_cnt>0: return 2
        return 0 if self.warn_cnt==0 else 1

def run(env_path: str, strict=False, print_pass=True, do_fix_inplace=False, fix_to_path=None):
    rep=Report(print_pass=print_pass)
    env=load_env_relaxed(env_path)
    root=os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    print(f"[SMOKE-ALL] env: {env_path}")
    print(f"[SMOKE-ALL] root: {root}\n")

    # Single keys
    print("== Single channel keys ==")
    for k in SINGLE_KEYS:
        v=env.get(k)
        if v is None: rep.WARN(f"{k}: missing"); continue
        norm, fl=normalize_token(str(v))
        if fl["link"] or fl["mention"] or fl["non_digit"] or fl["too_short"]:
            probs=",".join([n for n,b in fl.items() if b])
            rep.FAIL(f"{k}: {v} -> {probs if probs else 'invalid'}")
        else:
            rep.PASS(f"{k}: {v}")
    print()

    # List keys
    print("== List channel keys ==")
    parsed={}; suggest_map={}; any_strict_fail=False
    for k in LIST_KEYS:
        raw_csv = env.get(k,"")
        vals = parse_list(raw_csv)
        uniq, dups, flagged = sanitize_list(vals)
        parsed[k]=set(uniq)
        suggest_csv=",".join(uniq) if uniq else ""
        suggest_map[k]=suggest_csv
        diff = (suggest_csv != ",".join(vals))
        # Decide status
        if dups or flagged:
            rep.FAIL(f"{k}: count={len(vals)} uniq={len(uniq)} dups={len(dups)} flagged={len(flagged)}")
        elif strict and diff:
            any_strict_fail=True
            rep.FAIL(f"{k}: order/format differs from Suggest (diff=True)")
        else:
            if diff:
                rep.WARN(f"{k}: order/format differs (non-strict)")
            else:
                rep.PASS(f"{k}: OK")
        print(f"  - Suggest: {suggest_csv if suggest_csv else '(empty)'}")
    print()

    # Cross checks
    print("== Cross checks ==")
    redir=(env.get("LPG_REDIRECT_CHANNEL_ID") or env.get("LUCKYPULL_REDIRECT_CHANNEL_ID") or "").strip()
    guard=parsed.get("LPG_GUARD_CHANNELS", set()) | parsed.get("LUCKYPULL_GUARD_CHANNELS", set())
    rn,_=normalize_token(redir)
    if rn and rn in guard: rep.WARN(f"Redirect {redir} exists in guard list")
    else: rep.PASS("Redirect not in guards")
    logs=[env.get("LOG_CHANNEL_ID",""), env.get("NIXE_PHISH_LOG_CHAN_ID",""), env.get("PHISH_LOG_CHAN_ID","")]
    norm=[normalize_token(str(x))[0] for x in logs if x]
    if len(set(norm))>1: rep.WARN(f"LOG channels inconsistent: {logs}")
    else: rep.PASS("LOG channels consistent")
    print()

    # Whitelist
    print("== LPG whitelist ==")
    neg_file=env.get("LPG_NEG_FILE","data/lpg_negative_hashes.txt")
    neg_path=os.path.join(root, neg_file)
    if file_exists(neg_path):
        try: lines=sum(1 for _ in open(neg_path,"r",encoding="utf-8"))
        except: lines=-1
        rep.PASS(f"NEG file: {neg_file} lines={lines if lines>=0 else '?'}")
    else:
        rep.WARN(f"NEG file missing: {neg_file}")
    th_parent=env.get("LPG_NEG_PARENT_CHANNEL_ID")
    if th_parent: rep.PASS(f"Whitelist parent channel set: {th_parent}")
    else: rep.WARN("LPG_NEG_PARENT_CHANNEL_ID not set")
    print()

    # Lucky Pull (Gemini)
    print("== Lucky Pull (Gemini) ==")
    thr=float(str(env.get("GEMINI_LUCKY_THRESHOLD","0.81")))
    if 0.6<=thr<=0.95: rep.PASS(f"GEMINI_LUCKY_THRESHOLD={thr}")
    else: rep.WARN(f"GEMINI_LUCKY_THRESHOLD unusual: {thr}")
    rpm=as_int(env.get("LPG_GEM_MAX_RPM","6"),6)
    conc=as_int(env.get("LPG_GEM_MAX_CONCURRENCY","1"),1)
    if rpm<=10 and conc<=2: rep.PASS(f"Gemini rate: rpm={rpm} conc={conc}")
    else: rep.WARN(f"Gemini rate HIGH: rpm={rpm} conc={conc}")
    if guard: rep.PASS("Guard channels set")
    else: rep.FAIL("Guard channels empty")
    if rn: rep.PASS(f"Redirect set: {redir}")
    else: rep.FAIL("Redirect not set")
    print()

    # Phishing (NO Gemini)
    print("== Phishing (NO Gemini) ==")
    flags=[
        ("PHISH_GEMINI_ENABLE", env.get("PHISH_GEMINI_ENABLE","0")),
        ("IMAGE_PHISH_GEMINI_ENABLE", env.get("IMAGE_PHISH_GEMINI_ENABLE","0")),
        ("SUS_ATTACH_GEMINI_ENABLE", env.get("SUS_ATTACH_GEMINI_ENABLE","0")),
        ("SUS_ATTACH_USE_GEMINI", env.get("SUS_ATTACH_USE_GEMINI","0")),
        ("SUS_ATTACH_ALWAYS_GEM", env.get("SUS_ATTACH_ALWAYS_GEM","0")),
    ]
    bad=[k for k,v in flags if as_bool(v, False)]
    if bad: rep.FAIL("These must be OFF: "+", ".join(bad))
    else: rep.PASS("All phishing-Gemini flags are OFF")
    print()

    # Files
    print("== Files ==")
    files=[
        "nixe/cogs/lucky_pull_guard.py",
        "nixe/cogs/lpg_whitelist_thread_manager.py",
        "nixe/cogs/lpg_whitelist_ingestor.py",
        "nixe/helpers/hash_utils.py",
    ]
    for p in files:
        if file_exists(os.path.join(root,p)): rep.PASS(f"{p}")
        else: rep.WARN(f"missing: {p}")
    print()

    # Optional fix
    if do_fix_inplace or fix_to_path:
        fixed = load_env_relaxed(env_path)
        for k in LIST_KEYS:
            fixed[k] = suggest_map.get(k, "")
        target = env_path if do_fix_inplace else fix_to_path
        with open(target, "w", encoding="utf-8") as f:
            json.dump(fixed, f, ensure_ascii=False, indent=2)
        print(f"[WRITE] fixed lists -> {target}")

    # Summary
    rep.dump()
    print("\n== SUMMARY ==")
    print(f"PASS={rep.pass_cnt} WARN={rep.warn_cnt} FAIL={rep.fail_cnt}")
    return rep

def main(argv):
    strict="--strict" in argv
    print_pass = not ("--no-print-pass" in argv)
    json_out=None; target=None; do_fix_inplace="--fix-inplace" in argv
    if "--json" in argv:
        i=argv.index("--json")
        if i+1<len(argv): json_out=argv[i+1]
    if "--write-sanitized" in argv:
        i=argv.index("--write-sanitized")
        if i+1<len(argv): target=argv[i+1]
        else: print("[FAIL] --write-sanitized requires path"); sys.exit(2)
    fix_to_path=None
    if "--fix-to" in argv:
        i=argv.index("--fix-to")
        if i+1<len(argv): fix_to_path = argv[i+1]
        else: print("[FAIL] --fix-to requires path"); sys.exit(2)

    env_path=find_env_path(argv)
    rep=run(env_path, strict=strict, print_pass=print_pass, do_fix_inplace=do_fix_inplace, fix_to_path=fix_to_path)
    if target:
        raw=open(env_path,"r",encoding="utf-8").read()
        txt=strip_json_comments(raw); txt=remove_trailing_commas(txt)
        data=json.loads(txt)
        with open(target,"w",encoding="utf-8") as f: json.dump(data,f,ensure_ascii=False,indent=2)
        print(f"[WRITE] sanitized -> {target}")
    if json_out:
        with open(json_out,"w",encoding="utf-8") as f:
            json.dump({"pass":rep.pass_cnt,"warn":rep.warn_cnt,"fail":rep.fail_cnt,"logs":rep.logs},f,ensure_ascii=False,indent=2)
        print(f"[WRITE] report -> {json_out}")
    sys.exit(rep.exit_code(strict=strict))

if __name__=="__main__":
    main(sys.argv)