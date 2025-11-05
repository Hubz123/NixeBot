
from __future__ import annotations
import asyncio
from typing import Tuple
from .lp_gemini_helper import is_lucky_pull as _sync_is_lucky_pull

async def is_lucky_pull_async(image_bytes: bytes, threshold: float = 0.65, timeout: float = 5.0) -> Tuple[bool,float,str]:
    loop = asyncio.get_running_loop()
    def _call():
        try:
            ok,score,reason = _sync_is_lucky_pull(image_bytes, threshold=threshold)
            return bool(ok), float(score), str(reason)
        except Exception as e:
            return False, 0.0, f"error:{e}"
    return await loop.run_in_executor(None, _call)
