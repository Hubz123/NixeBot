# -*- coding: utf-8 -*-
"""
Smoke: ALL-IN-ONE — Persona + Lucky Pull Wiring
Run dari root project atau dari folder scripts:
    python scripts/smoke_guard_persona_all.py
atau:
    python -m scripts.smoke_guard_persona_all
"""
# Bootstrap agar 'nixe' bisa diimport walau dijalankan dari scripts/
try:
    from scripts._bootstrap_import import whereis  # noqa: F401
except Exception:
    import sys, os
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[1]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

import asyncio, types

# Persona
from nixe.helpers.persona_loader import load_persona, pick_line

# Lucky Pull
from nixe.cogs.lucky_pull_guard import LuckyPullGuard, _pick_tone

class _DummyChannel:
    def __init__(self, id, parent=None, name="dummy"):
        self.id = int(id)
        self.parent = parent
        self.name = name
    async def send(self, content=None, reference=None, mention_author=None, file=None):
        print("[send]", content)

class _DummyMessage:
    def __init__(self, cid, pid=None):
        self.channel = _DummyChannel(cid, _DummyChannel(pid) if pid else None, name=f"ch{cid}")
        self.author = types.SimpleNamespace(mention="@user")
        self.guild = types.SimpleNamespace(get_channel=lambda _: None, fetch_channel=lambda *_: None)

async def _persona_section():
    print("=== PERSONA CHECK ===")
    mode, data, path = load_persona()
    print(f"[persona] mode={mode} path={path}")
    for tone in ("soft","agro","sharp"):
        try:
            line = pick_line(data, mode, tone, user="@user", channel="#guard", reason="lucky_pull")
        except TypeError:
            line = pick_line(data, mode, tone)
        print(f"[persona:{tone}] {line}")

async def _wiring_section():
    print("\n=== LUCKY PULL WIRING ===")
    bot = types.SimpleNamespace()
    lpg = LuckyPullGuard(bot)
    guards = sorted(lpg.guard_channels)
    print("[wiring] guards:", guards)
    print("[wiring] redirect:", lpg.redirect_channel_id)

    if guards:
        # direct channel hit
        cid = guards[0]
        msg = _DummyMessage(cid, None)
        print("[is_guard:direct]", lpg._is_guard_channel(msg.channel))

        # thread under guard parent
        tid = 999999999999999999
        msg2 = _DummyMessage(tid, cid)
        print("[is_guard:thread->parent]", lpg._is_guard_channel(msg2.channel))

        # persona notify dry-run
        tone = _pick_tone(0.90, getattr(lpg, "persona_tone", "auto"))
        try:
            await lpg._persona_notify(_DummyMessage(cid), score=0.90)
        except Exception as e:
            print("[persona_notify:error]", e)
    else:
        print("[wiring] guard list is empty — check runtime_env.json or ENV variables.")

async def main():
    await _persona_section()
    await _wiring_section()

if __name__ == "__main__":
    asyncio.run(main())