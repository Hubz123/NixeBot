from __future__ import annotations

"""
[a19-negtext-reload]
Hot-reload for LPG negative-text files, plus optional SOFT list loading.

HARD:
  LPG_NEGATIVE_TEXT or LPG_NEGATIVE_TEXT_FILE (already used by n57)
SOFT (optional, no veto here):
  LPG_NEGATIVE_TEXT_SOFT or LPG_NEGATIVE_TEXT_SOFT_FILE

Command:
  /negtext_reload (admin only)

Overlay is additive; safe if guards don't expose negtext attrs.
"""

import os
import logging
from typing import List, Set

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

def _read_tokens_from_file(path: str) -> List[str]:
    out: List[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                out.append(s.lower())
    except FileNotFoundError:
        log.warning(f"[negtext-reload] file not found: {path}")
    except Exception as e:
        log.warning(f"[negtext-reload] failed reading {path}: {e}")
    return out

def _parse_env_list(raw: str) -> List[str]:
    raw = raw.strip()
    if not raw:
        return []
    try:
        if raw.startswith("["):
            import json
            return [str(x).lower() for x in json.loads(raw)]
    except Exception:
        pass
    return [s.strip().lower() for s in raw.split(",") if s.strip()]

def load_hard_tokens() -> List[str]:
    hard: List[str] = []
    hard.extend(_parse_env_list(os.getenv("LPG_NEGATIVE_TEXT", "")))
    fpath = os.getenv("LPG_NEGATIVE_TEXT_FILE", "").strip()
    if fpath:
        hard.extend(_read_tokens_from_file(fpath))
    seen: Set[str] = set()
    out: List[str] = []
    for t in hard:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out

def load_soft_tokens() -> List[str]:
    soft: List[str] = []
    soft.extend(_parse_env_list(os.getenv("LPG_NEGATIVE_TEXT_SOFT", "")))
    fpath = os.getenv("LPG_NEGATIVE_TEXT_SOFT_FILE", "").strip()
    if fpath:
        soft.extend(_read_tokens_from_file(fpath))
    seen: Set[str] = set()
    out: List[str] = []
    for t in soft:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out

class NegTextReloadCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.hard_tokens: List[str] = load_hard_tokens()
        self.soft_tokens: List[str] = load_soft_tokens()
        log.warning(f"[negtext-reload] loaded hard={len(self.hard_tokens)} soft={len(self.soft_tokens)}")

    def _apply_to_guards(self) -> int:
        applied = 0
        for cog in self.bot.cogs.values():
            for attr in ["neg_text", "neg_text_tokens", "negative_text_tokens", "hard_neg_text_tokens"]:
                if hasattr(cog, attr):
                    try:
                        setattr(cog, attr, list(self.hard_tokens))
                        applied += 1
                        break
                    except Exception:
                        pass
            for attr in ["soft_neg_text", "soft_neg_text_tokens", "negative_text_soft_tokens"]:
                if hasattr(cog, attr):
                    try:
                        setattr(cog, attr, list(self.soft_tokens))
                    except Exception:
                        pass
        return applied

    @commands.hybrid_command(
        name="negtext_reload",
        description="Reload LPG negative text tokens from file/env (admin only).",
        with_app_command=True,
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def negtext_reload(self, ctx: commands.Context):
        self.hard_tokens = load_hard_tokens()
        self.soft_tokens = load_soft_tokens()
        applied = self._apply_to_guards()

        emb = discord.Embed(title="NEG Text Reloaded", color=discord.Color.green())
        emb.add_field(name="Hard tokens", value=str(len(self.hard_tokens)), inline=True)
        emb.add_field(name="Soft tokens", value=str(len(self.soft_tokens)), inline=True)
        emb.add_field(name="Guards updated", value=str(applied), inline=True)
        await ctx.reply(embed=emb)

async def setup(bot: commands.Bot):
    await bot.add_cog(NegTextReloadCog(bot))
