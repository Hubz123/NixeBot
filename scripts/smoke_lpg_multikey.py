
# --- bootstrap path for local runs ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.abspath(__file__))
_PROJ = _os.path.abspath(_os.path.join(_ROOT, ".."))
if _PROJ not in _sys.path:
    _sys.path.insert(0, _PROJ)
# -------------------------------------

import asyncio, os
from nixe.helpers.gemini_bridge import classify_lucky_pull_bytes

SAMPLE = b'\xff\xd8\xff\xe0' + b'fakejpegdata'  # tiny placeholder

async def main():
    os.environ.setdefault("GEMINI_KEYS", '["KEY_A","KEY_B"]')
    os.environ.setdefault("GEMINI_MODELS", '["gemini-2.5-flash-lite","gemini-2.5-flash"]')
    os.environ.setdefault("GEMINI_COOLDOWN_SEC", "5")
    os.environ.setdefault("GEMINI_MAX_RETRIES", "2")
    os.environ.setdefault("GEMINI_MAX_CONCURRENT", "2")
    os.environ.setdefault("GEMINI_LUCKY_THRESHOLD", "0.75")
    # Dry run: will fail without google-generativeai, but ensures import path OK
    try:
        res = await classify_lucky_pull_bytes(SAMPLE, timeout_ms=1000)
        print("[SMOKE] result", res)
    except Exception as e:
        print("[SMOKE] expected error or success:", e)

if __name__ == "__main__":
    asyncio.run(main())
