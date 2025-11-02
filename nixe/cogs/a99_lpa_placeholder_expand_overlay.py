# -*- coding: utf-8 -*-
"""
a99_lpa_placeholder_expand_overlay
----------------------------------
Tujuan: pastikan placeholder `{user}`, `{channel}`, `{channel_name}`, `{parent}` selalu
terisi benar TANPA mengubah config / template (yandere.json tetap).
Caranya: monkey-patch LuckyPullAuto._pick_persona_line untuk melakukan ekspansi setelah
baris persona dipilih.

Load order: a99_*.py agar overlay ini dipanggil PALING AKHIR.
"""
from __future__ import annotations

import re
import logging

log = logging.getLogger(__name__)

# token '#ngobrol' / '<#...>' lama -> paksa jadi mention redirect
_FORCE_TOKEN = re.compile(r"(?:<#\\d+>|#\\s*[^\\s#]*ngobrol[^\\s#]*|\\bngobrol\\b)", re.IGNORECASE)

def _expand_placeholders(text: str, *, user_mention: str, chan_mention: str, chan_name: str, parent_mention: str) -> str:
    if not text:
        return text
    out = str(text)

    # Bentuk umum dengan {} / {{}} / <> / $USER
    patterns = [
        (re.compile(r"\\{\\{\\s*user\\s*\\}\\}|\\{\\s*user\\s*\\}|<\\s*user\\s*>|\\$user|\\$USER|\\{USER\\}", re.IGNORECASE), user_mention),
        (re.compile(r"\\{\\{\\s*channel\\s*\\}\\}|\\{\\s*channel\\s*\\}|<\\s*channel\\s*>|\\$channel|\\$CHANNEL|\\{CHANNEL\\}", re.IGNORECASE), chan_mention),
        (re.compile(r"\\{\\{\\s*channel_name\\s*\\}\\}|\\{\\s*channel_name\\s*\\}", re.IGNORECASE), chan_name),
        (re.compile(r"\\{\\{\\s*parent\\s*\\}\\}|\\{\\s*parent\\s*\\}|<\\s*parent\\s*>|\\{PARENT\\}", re.IGNORECASE), parent_mention),
    ]
    for pat, rep in patterns:
        out = pat.sub(rep, out)

    return out

async def setup(bot):
    try:
        from nixe.cogs import lucky_pull_auto as _lpa
    except Exception as e:
        log.warning("[lpa-expand] lucky_pull_auto not available: %r", e)
        return

    if not hasattr(_lpa, "LuckyPullAuto"):
        log.warning("[lpa-expand] LuckyPullAuto class not found; no patch applied")
        return

    orig = getattr(_lpa.LuckyPullAuto, "_pick_persona_line", None)
    if not callable(orig):
        log.warning("[lpa-expand] _pick_persona_line not found; no patch applied")
        return

    async def _patched(self, author, channel, redir_channel, redir_mention):
        # panggil original untuk ambil baris
        try:
            line = orig(self, author, channel, redir_channel, redir_mention)
        except TypeError:
            # kalau original asynchronous (unlikely), await
            line = await orig(self, author, channel, redir_channel, redir_mention)  # type: ignore

        # Resolve konteks mention
        try:
            user_mention = getattr(author, "mention", "@user")
        except Exception:
            user_mention = "@user"

        try:
            chan_mention = getattr(redir_channel, "mention", None) or getattr(channel, "mention", "#channel")
        except Exception:
            chan_mention = "#channel"

        try:
            chan_name = getattr(redir_channel, "name", None) or getattr(channel, "name", "channel")
        except Exception:
            chan_name = "channel"

        try:
            parent = getattr(redir_channel or channel, "parent", None)
            parent_mention = getattr(parent, "mention", chan_mention) if parent else chan_mention
        except Exception:
            parent_mention = chan_mention

        # 1) ekspansi placeholder
        line2 = _expand_placeholders(
            line,
            user_mention=user_mention,
            chan_mention=chan_mention,
            chan_name=chan_name,
            parent_mention=parent_mention,
        )

        # 2) paksa token '#ngobrol' / '<#...>' ke redirect mention
        line2 = _FORCE_TOKEN.sub(redir_mention or chan_mention, line2)

        return line2

    try:
        _lpa.LuckyPullAuto._pick_persona_line = _patched  # type: ignore[attr-defined]
        log.warning("[lpa-expand] Placeholder expander patch applied to LuckyPullAuto._pick_persona_line")
    except Exception as e:
        log.warning("[lpa-expand] Failed to patch _pick_persona_line: %r", e)
