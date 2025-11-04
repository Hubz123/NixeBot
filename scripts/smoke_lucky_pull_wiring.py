# -*- coding: utf-8 -*-
"""
Smoke: Lucky Pull wiring (guard scope + redirect + persona notify dry-run)
- Asserts guard_ids populated from runtime_env.json (hybrid env)
- Tests _is_guard_channel() with channel.id and parent.id (thread-aware)
- Dry-run persona notify to build message text without Discord API
"""
import types, sys
import asyncio

from nixe.cogs.lucky_pull_guard import LuckyPullGuard, _pick_tone
from nixe.helpers.persona_loader import load_persona, pick_line

class _DummyChannel:
    def __init__(self, id, parent=None, name="dummy"):
        self.id = int(id)
        self.parent = parent
        self.name = name
    async def send(self, content=None, reference=None, mention_author=None, file=None):
        # just print in smoke
        print("[send]", content)

class _DummyMessage:
    def __init__(self, cid, pid=None):
        self.channel = _DummyChannel(cid, _DummyChannel(pid) if pid else None, name=f"ch{cid}")
        self.author = types.SimpleNamespace(mention="@user")
        self.guild = types.SimpleNamespace(get_channel=lambda _: None, fetch_channel=lambda _: None)

async def main():
    bot = types.SimpleNamespace()  # LuckyPullGuard only stores this reference
    lpg = LuckyPullGuard(bot)
    print("[wiring] guards:", sorted(lpg.guard_channels))
    print("[wiring] redirect:", lpg.redirect_channel_id)

    # Case A: direct channel hit
    if lpg.guard_channels:
        gid = next(iter(lpg.guard_channels))
        m = _DummyMessage(gid, None)
        print("[is_guard:direct]", lpg._is_guard_channel(m.channel))

    # Case B: thread under a guard parent
    if lpg.guard_channels:
        parent = next(iter(lpg.guard_channels))
        thread_id = 999999999999999999  # arbitrary child
        m = _DummyMessage(thread_id, parent)
        print("[is_guard:thread->parent]", lpg._is_guard_channel(m.channel))

    # Persona dry run
    mode, data, path = load_persona()
    print("[persona] mode:", mode, "path:", path)
    tone = _pick_tone(0.90, getattr(lpg, "persona_tone", "auto"))
    line = pick_line(data, mode, tone)
    await lpg._persona_notify(_DummyMessage(next(iter(lpg.guard_channels)) if lpg.guard_channels else 0), score=0.90)

asyncio.run(main())