
# Lucky Pull "online" smoke that posts to a channel or thread via Discord REST,
# deletes the smoke message after TTL, and (optionally) posts a persona line
# using the same yandere.json the bot uses. No changes to your cogs are required.
#
# Fixes:
# - Define _post_message_text (NameError in prior version)
# - Correct deletion when posting inside a thread (use thread_id as channel for DELETE)
# - Persona send option wired to yandere.json with randomizable pick
#
# Usage (Windows example):
#   python scripts/smoke_lpg_online.py --chan-id 8865... --thread-id 1429... --ttl 5 --persona-send
#
import argparse
import json
import os
import time
from typing import Optional, Tuple, Dict, Any

import requests

# Local helper (no external deps)
from scripts.smoke_utils import load_env_hybrid, clamp

DISCORD_API = "https://discord.com/api/v10"


def _headers() -> Dict[str, str]:
    token = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("Missing DISCORD_TOKEN (or DISCORD_BOT_TOKEN). Put it in .env (secrets only).")
    return {
        "Authorization": f"Bot {token}",
        "User-Agent": "nixe-smoke/online (+LuckyPull)",
    }


def _post_message_text(channel_or_thread_id: str, content: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """Post a plain text message. Returns (ok, channel_id_used, message_id)."""
    url = f"{DISCORD_API}/channels/{channel_or_thread_id}/messages"
    try:
        r = requests.post(url, headers=_headers(), json={"content": content})
        if r.status_code in (200, 201):
            data = r.json()
            return True, data["channel_id"], data["id"]
        return False, None, None
    except Exception:
        return False, None, None


def _delete_message(channel_or_thread_id: str, message_id: str) -> bool:
    url = f"{DISCORD_API}/channels/{channel_or_thread_id}/messages/{message_id}"
    try:
        r = requests.delete(url, headers=_headers())
        return r.status_code in (200, 202, 204)
    except Exception:
        return False


def _post_embed(channel_or_thread_id: str, title: str, fields: Dict[str, str]) -> Tuple[bool, Optional[str], Optional[str]]:
    embed = {
        "title": title,
        "type": "rich",
        "fields": [{"name": k, "value": v, "inline": False} for k, v in fields.items()],
    }
    url = f"{DISCORD_API}/channels/{channel_or_thread_id}/messages"
    payload = {"embeds": [embed]}
    try:
        r = requests.post(url, headers=_headers(), json=payload)
        if r.status_code in (200, 201):
            data = r.json()
            return True, data["channel_id"], data["id"]
        return False, None, None
    except Exception:
        return False, None, None


def _load_persona_line(persona_path: str, allow_random: bool, preview_only: bool = False) -> str:
    """
    Tries to import persona loader from nixe.helpers if available;
    otherwise falls back to a minimal JSON reader.
    """
    # Try native loader first
    try:
        from nixe.helpers.persona_loader import load_persona, pick_line  # type: ignore
        data = load_persona(persona_path)
        line = pick_line(data, randomize=allow_random, preview=preview_only)
        return str(line)
    except Exception:
        # Fallback: expect {"lines": ["...","..."]} or a simple list ["..."]
        try:
            with open(persona_path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            if isinstance(raw, dict) and "lines" in raw and isinstance(raw["lines"], list) and raw["lines"]:
                lines = raw["lines"]
            elif isinstance(raw, list) and raw:
                lines = raw
            else:
                lines = ["(persona preview gagal dimuat)"]
            if allow_random:
                import random
                return random.choice(lines)
            return lines[0]
        except Exception:
            return "(persona preview gagal dimuat)"


def resolve_target_id(chan_id: Optional[str], thread_id: Optional[str]) -> str:
    """Use thread_id when provided; else use chan_id."""
    if thread_id:
        return thread_id
    if not chan_id:
        raise SystemExit("You must supply --chan-id or --thread-id.")
    return chan_id


def main():
    ap = argparse.ArgumentParser(description="Lucky Pull pipeline smoke test via REST")
    ap.add_argument("--chan-id", dest="chan_id", help="Channel ID to post into (parent channel).")
    ap.add_argument("--thread-id", dest="thread_id", help="Thread ID to post into (when provided, overrides chan-id).")
    ap.add_argument("--log-chan-id", dest="log_chan_id", help="Optional: channel for log echoes.")
    ap.add_argument("--ttl", type=int, default=5, help="Seconds before smoke message auto-deletes (default: 5).")
    ap.add_argument("--persona-file", default="nixe/config/yandere.json", help="Path to persona json file.")
    ap.add_argument("--persona-random", action="store_true", help="Randomize persona line selection.")
    ap.add_argument("--persona-send", action="store_true", help="Also send persona line to the target channel/thread.")
    ap.add_argument("--dotenv", dest="dotenv_path", help="Path to .env for secrets (optional).")
    ap.add_argument("--runtime-json", dest="runtime_json", default="nixe\\config\\runtime_env.json",
                    help="Path to runtime_env.json for summary (optional).")

    args = ap.parse_args()

    # Load hybrid env for secrets (DISCORD_TOKEN, etc.).
    summary = load_env_hybrid(args.dotenv_path, args.runtime_json)
    # Print a compact summary into the target channel/thread (as embed).
    fields = {
        "Lucky Pull pipeline smoke test via REST": "",
        "Channel test": "—",
        "Thread test": "—",
        "Persona file": args.persona_file,
        "Persona random": "ON" if args.persona_random else "OFF",
        "Persona preview": "—",
        "Env": summary["runtime_env_json_path"],
        "TTL": f"tests={args.ttl}s | log=0s",
        "ENV HYBRID": "runtime_env.json + .env (secrets only)",
    }

    target_id = resolve_target_id(args.chan_id, args.thread_id)

    # Channel test (post short text)
    ok, used_ch, msg_id = _post_message_text(target_id, "SMOKE LPG (online) — Result")
    if ok:
        fields["Channel test"] = f"OK (message_id `{msg_id}`)"
    else:
        fields["Channel test"] = "ERR failed to post"

    # Thread test (explicit): if posting to a thread id, still show a separate status to mimic your screenshot
    if args.thread_id:
        ok_t, _, msg_t = _post_message_text(args.thread_id, "_thread ping_")
        fields["Thread test"] = f"OK (message_id `{msg_t}`)" if ok_t else "ERR failed to post in thread"
        # ensure quick delete for the ping
        if ok_t:
            time.sleep(clamp(1, 1, 3))
            _delete_message(args.thread_id, msg_t)
    else:
        # Emulate previous error message for missing helper in older builds
        fields["Thread test"] = "—"

    # Persona preview (load from yandere.json)
    try:
        line = _load_persona_line(args.persona_file, allow_random=args.persona_random, preview_only=True)
        fields["Persona preview"] = str(line)[:2000]
    except Exception as e:
        fields["Persona preview"] = f"ERR {e!r}"

    # Providers/Guards line (best-effort: show from runtime json if present)
    try:
        if os.path.exists(args.runtime_json):
            with open(args.runtime_json, 'r', encoding='utf-8') as f:
                rj = json.load(f)
            # try a few likely keys
            guards = rj.get("LUCKYPULL_GUARD_CHANNELS") or rj.get("LPG_GUARD_CHANNELS") or []
            providers = (rj.get("GEMINI_MODEL") or "gemini")  # simplified preview
            if guards:
                fields["Providers   Guards"] = f"{providers}\n`" + ",".join(str(x) for x in guards) + "`"
            else:
                fields["Providers   Guards"] = f"{providers}"
    except Exception:
        pass

    # Post the embed summary to target
    ok_e, ch_e, msg_e = _post_embed(target_id, "SMOKE LPG (online) — Result", fields)

    # Optionally send persona line to show in channel/thread (wiring-like behavior)
    if args.persona_send:
        persona_line = _load_persona_line(args.persona_file, allow_random=args.persona_random, preview_only=False)
        _post_message_text(target_id, persona_line)

    # Auto-delete after TTL (if we posted the embed successfully).
    if ok_e and msg_e:
        time.sleep(clamp(args.ttl, 0, 3600))
        _delete_message(target_id, msg_e)

    # Also delete the first "SMOKE LPG (online) — Result" header if we created it
    if ok and msg_id:
        _delete_message(target_id, msg_id)

    # Optional echo to log channel
    if args.log_chan_id:
        _post_message_text(args.log_chan_id, f"[smoke] posted to <#{target_id}>; ttl={args.ttl}s; persona_send={'1' if args.persona_send else '0'}")


if __name__ == "__main__":
    main()
