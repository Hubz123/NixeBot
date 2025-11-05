
# --- bootstrap path for local runs ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.abspath(__file__))
_PROJ = _os.path.abspath(_os.path.join(_ROOT, ".."))
if _PROJ not in _sys.path:
    _sys.path.insert(0, _PROJ)
# -------------------------------------

# scripts/smoke_lucky_pull_fp.py
import os, sys
from nixe.helpers import lpa_heuristics as LPAH

def run():
    os.environ.setdefault("LPA_REQUIRE_KEYWORD_IF_MODEL_DOWN", "1")
    os.environ.setdefault("LPA_FALLBACK_SCORE", "0.0")
    text = "Only One\nMommy."
    score, kw, neg = LPAH.score_text_basic(text)
    print(f"[SMOKE] kw_hits={kw} neg_hits={neg} score={score:.3f}")
    assert kw == 0, "Should not detect gacha keywords"
    assert score < 0.5, "Heuristic score too high for non-gacha dialogue"
    print("== SUMMARY == OK (no false positive)")

if __name__ == "__main__":
    run()
