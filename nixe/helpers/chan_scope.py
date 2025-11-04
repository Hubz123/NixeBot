from __future__ import annotations

def parse_id_list(s: str):
    """Parse comma-separated ID strings into a set of ints (ignoring blanks)."""
    if not s:
        return set()
    out = set()
    for tok in str(s).split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.add(int(tok))
        except ValueError:
            # tolerate string IDs
            try:
                out.add(int(tok.strip('"')))
            except Exception:
                pass
    return out

def in_guard_scope(message, guard_ids: set[int]) -> bool:
    """Return True if message is posted in a channel or a thread whose parent matches guard_ids."""
    try:
        cid = getattr(message.channel, "id", None)
        if cid in guard_ids:
            return True
        parent_id = getattr(getattr(message.channel, "parent", None), "id", None)
        if parent_id in guard_ids:
            return True
        # Some discord libs expose parent_id directly
        parent_id2 = getattr(message.channel, "parent_id", None)
        if parent_id2 in guard_ids:
            return True
    except Exception:
        pass
    return False