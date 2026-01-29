from __future__ import annotations

"""
[a19-env-validate]
Runtime environment validator on startup.
Only logs warnings; never crashes.
"""

import os
import logging
import re
from typing import List

from discord.ext import commands

log = logging.getLogger(__name__)

_ID_RE = re.compile(r".*_ID$|.*_CHAN_ID$|.*_CHANNEL_ID$|.*_THREAD_ID$|.*_GUILD_ID$", re.IGNORECASE)

# IDs that are valid but not purely numeric (e.g., Google Drive folder IDs / OAuth client IDs)
_NON_NUMERIC_ID_KEYS = {
    "DICT_GDRIVE_FOLDER_ID",
    "GDRIVE_CLIENT_ID",
}
_GDRIVE_FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
_GOOGLE_OAUTH_CLIENT_ID_RE = re.compile(r"^\d+-[a-z0-9]+\.apps\.googleusercontent\.com$")

def _is_section_key(k: str) -> bool:
    return k.strip().startswith("---") and k.strip().endswith("---")

def validate_env() -> List[str]:
    issues: List[str] = []
    env = dict(os.environ)

    for k, v in env.items():
        if _is_section_key(k):
            continue
        if k.upper().startswith("RENDER_"):
            continue
        if _ID_RE.match(k):
            sv = str(v).strip()
            ku = k.upper()
            if ku in _NON_NUMERIC_ID_KEYS:
                if ku == "DICT_GDRIVE_FOLDER_ID":
                    if not sv or not _GDRIVE_FOLDER_ID_RE.match(sv):
                        issues.append(f"{k} invalid_drive_folder_id='{sv}'")
                elif ku == "GDRIVE_CLIENT_ID":
                    if not sv or not _GOOGLE_OAUTH_CLIENT_ID_RE.match(sv):
                        issues.append(f"{k} invalid_google_oauth_client_id='{sv}'")
                continue
            if not sv.isdigit() or int(sv) <= 0:
                issues.append(f"{k} invalid_id='{sv}'")

    for k in ["LPG_THRESHOLD", "LPG_THR", "PHASH_THR", "PHISH_THRESHOLD"]:
        if k in env:
            try:
                f = float(str(env[k]))
                if not (0.0 <= f <= 1.0):
                    issues.append(f"{k} out_of_range={f}")
            except Exception:
                issues.append(f"{k} not_float='{env[k]}'")

    for k in ["LPG_TIMEOUT_SEC", "LUCKYPULL_TIMEOUT_SEC", "GEMINI_TIMEOUT_MS"]:
        if k in env:
            try:
                x = float(str(env[k]))
                if x <= 0:
                    issues.append(f"{k} non_positive={x}")
            except Exception:
                issues.append(f"{k} not_number='{env[k]}'")
    return issues

class RuntimeEnvValidator(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._done = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self._done:
            return
        self._done = True
        issues = validate_env()
        if not issues:
            log.info("[env-validate] OK (no issues found)")
            return
        for it in issues[:50]:
            log.warning(f"[env-validate] {it}")
        if len(issues) > 50:
            log.warning(f"[env-validate] ... {len(issues)-50} more issue(s)")

async def setup(bot: commands.Bot):
    await bot.add_cog(RuntimeEnvValidator(bot))
