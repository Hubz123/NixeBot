# nixe/helpers/gemini_http_cleanup.py
from __future__ import annotations
import atexit, asyncio
try:
    from nixe.helpers import gemini_lpg_burst as _g
except Exception:
    _g = None

def _finalize():
    if _g is None:
        return
    s = getattr(_g, "_SESSION", None)
    if s is None:
        return
    try:
        if not getattr(s, "closed", True):
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(s.close())
                else:
                    loop.run_until_complete(s.close())
            except Exception:
                pass
    finally:
        try: _g._SESSION = None
        except Exception: pass

atexit.register(_finalize)

async def close_now():
    if _g is None:
        return
    s = getattr(_g, "_SESSION", None)
    _g._SESSION = None
    if s and not getattr(s, "closed", True):
        try:
            await s.close()
        except Exception:
            pass
