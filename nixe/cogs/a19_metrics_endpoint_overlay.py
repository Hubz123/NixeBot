from __future__ import annotations

"""
[a19-metrics]
Expose /metrics JSON on existing aiohttp web app if present.
NOOP if web app unavailable.
"""

import logging
from typing import Any, Dict

from discord.ext import commands

log = logging.getLogger(__name__)

def collect_metrics(bot) -> Dict[str, Any]:
    m: Dict[str, Any] = {}
    try: m["cogs_loaded"] = len(bot.cogs)
    except Exception: pass
    try: m["guilds"] = len(bot.guilds)
    except Exception: pass
    return m

class MetricsOverlay(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._installed = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self._installed:
            return
        self._installed = True

        app = getattr(self.bot, "web_app", None) or getattr(self.bot, "app", None)
        if app is None:
            log.warning("[metrics] web app not found; NOOP")
            return

        try:
            from aiohttp import web
        except Exception as e:
            log.warning(f"[metrics] aiohttp missing: {e}")
            return

        async def metrics_handler(request):
            data = collect_metrics(self.bot)
            return web.json_response(data)

        try:
            app.router.add_get("/metrics", metrics_handler)
            log.warning("[metrics] /metrics endpoint installed")
        except Exception as e:
            log.warning(f"[metrics] failed to install route: {e}")

async def setup(bot):
    await bot.add_cog(MetricsOverlay(bot))
