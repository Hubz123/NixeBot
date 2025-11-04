
import os, re
def _parse_id_list(val: str):
    ids = set()
    for tok in re.split(r"[, \s]+", val or ""):
        if not tok: continue
        try: ids.add(int(tok))
        except: pass
    return sorted(ids)

raw = (os.getenv("LPG_GUARD_CHANNELS") or os.getenv("LUCKYPULL_GUARD_CHANNELS") 
       or os.getenv("LPA_GUARD_CHANNELS") or os.getenv("GUARD_CHANNELS") or "")
print("RAW:", raw)
print("PARSED:", _parse_id_list(raw))
