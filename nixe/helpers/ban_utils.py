
from __future__ import annotations
import typing as _t

def emit_phish_detected(bot, message, result, evidence_urls: _t.Sequence[str] | None = None):
    payload = {
        "guild_id": getattr(message.guild, "id", None),
        "channel_id": getattr(message.channel, "id", None),
        "message_id": getattr(message, "id", None),
        "user_id": getattr(getattr(message, "author", None), "id", None),
        "score": float((result or {}).get("score", 0.0)) if isinstance(result, dict) else float(getattr(result, "score", 0.0)),
        "provider": str((result or {}).get("provider","")) if isinstance(result, dict) else str(getattr(result, "provider","")),
        "reason": str((result or {}).get("reason","")) if isinstance(result, dict) else str(getattr(result, "reason","")),
        "kind": str((result or {}).get("kind","")) if isinstance(result, dict) else str(getattr(result, "kind","")),
        "evidence": list(evidence_urls or []),
    }
    bot.dispatch("nixe_phish_detected", payload)
