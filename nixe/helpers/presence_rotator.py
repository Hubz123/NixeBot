from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import discord

# Default config path inside repo
_DEFAULT_CFG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "presence_rotator.json")


@dataclass(frozen=True)
class PresenceItem:
    activity_type: discord.ActivityType
    name: str
    duration_seconds: int


def _map_activity_type(t: str) -> discord.ActivityType:
    t = (t or "").strip().lower()
    m = {
        "playing": discord.ActivityType.playing,
        "watching": discord.ActivityType.watching,
        "listening": discord.ActivityType.listening,
        "competing": discord.ActivityType.competing,
        "streaming": discord.ActivityType.streaming,
    }
    if t not in m:
        raise ValueError(f"Unknown activity type: {t!r}")
    return m[t]


def _map_status(s: str) -> discord.Status:
    s = (s or "").strip().lower()
    m = {
        "online": discord.Status.online,
        "idle": discord.Status.idle,
        "dnd": discord.Status.dnd,
        "invisible": discord.Status.invisible,
        "offline": discord.Status.offline,
    }
    if s not in m:
        raise ValueError(f"Unknown default_status: {s!r}")
    return m[s]


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    cfg_path = path or os.getenv("NIXE_PRESENCE_ROTATOR_CONFIG") or _DEFAULT_CFG_PATH
    cfg_path = os.path.abspath(cfg_path)

    # Allow raw JSON via env, but prefer file path.
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)

    raw = os.getenv("NIXE_PRESENCE_ROTATOR_CONFIG_JSON")
    if raw:
        return json.loads(raw)

    # If path doesn't exist and no raw JSON, fail loudly.
    raise FileNotFoundError(f"Presence rotator config not found: {cfg_path}")


def _parse_schedule(cfg: Dict[str, Any]) -> List[PresenceItem]:
    sched = cfg.get("schedule")
    if not isinstance(sched, list) or not sched:
        raise ValueError("presence_rotator.json: 'schedule' must be a non-empty list")

    items: List[PresenceItem] = []
    for i, it in enumerate(sched):
        if not isinstance(it, dict):
            raise ValueError(f"presence_rotator.json: schedule[{i}] must be an object")
        typ = _map_activity_type(str(it.get("type", "playing")))
        name = str(it.get("name", "")).strip()
        if not name:
            raise ValueError(f"presence_rotator.json: schedule[{i}].name is empty")
        dur_min = it.get("duration_minutes")
        if not isinstance(dur_min, int) or dur_min <= 0:
            raise ValueError(f"presence_rotator.json: schedule[{i}].duration_minutes must be positive int")
        items.append(PresenceItem(activity_type=typ, name=name, duration_seconds=dur_min * 60))
    return items


class PresenceRotator:
    def __init__(self, bot: discord.Client, cfg_path: Optional[str] = None):
        self.bot = bot
        self.cfg_path = cfg_path
        self._task: Optional[asyncio.Task] = None
        self._stop_evt = asyncio.Event()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_evt.clear()
        self._task = asyncio.create_task(self._run(), name="presence-rotator")

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                # swallow to avoid killing shutdown
                pass

    async def _run(self) -> None:
        cfg = load_config(self.cfg_path)
        default_status = _map_status(str(cfg.get("default_status", "online")))
        schedule = _parse_schedule(cfg)

        idx = 0
        while not self._stop_evt.is_set():
            item = schedule[idx % len(schedule)]
            idx += 1

            try:
                activity = discord.Activity(type=item.activity_type, name=item.name)
                await self.bot.change_presence(status=default_status, activity=activity)
            except Exception:
                # Don't crash loop on transient Discord errors
                pass

            # Sleep in small chunks so stop() is responsive.
            remaining = item.duration_seconds
            while remaining > 0 and not self._stop_evt.is_set():
                step = min(30, remaining)
                await asyncio.sleep(step)
                remaining -= step


def ensure_rotator_started(bot: discord.Client) -> None:
    # Idempotent startup to avoid double-start on reconnect.
    if getattr(bot, "_nixe_presence_rotator", None) is None:
        setattr(bot, "_nixe_presence_rotator", PresenceRotator(bot))
    rot: PresenceRotator = getattr(bot, "_nixe_presence_rotator")
    rot.start()
