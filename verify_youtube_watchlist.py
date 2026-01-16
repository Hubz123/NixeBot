#!/usr/bin/env python3
"""verify_youtube_watchlist.py

Verifier watchlist YouTube (WuWa) untuk proyek NixeBot.

Tujuan utama verifier ini:
- *STRUCT* mode (default): cepat, tidak butuh network. Memvalidasi format JSON, field wajib, URL pattern, dan duplikasi.
- *NETWORK* mode: validasi URL channel 1-per-1 memakai yt-dlp (network bound). Cocok untuk audit manual, bukan default smoketest.

Catatan penting:
- `tools/smoketest_super.py` memanggil verifier ini di STRUCT mode agar tidak timeouts.
- NETWORK mode tetap tersedia via `--mode network` untuk cek URL real.

Cara pakai:
  # cepat (tanpa network):
  python verify_youtube_watchlist.py

  # path eksplisit:
  python verify_youtube_watchlist.py "./data/youtube_wuwa_watchlist.json"

  # audit network (boleh lama):
  python -m pip install -U yt-dlp
  python verify_youtube_watchlist.py --mode network --sleep 0.2 --limit 10 --timeout 20
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional


def _force_utf8_stdio() -> None:
    """Force UTF-8 stdio to avoid UnicodeEncodeError on Windows pipes."""
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


DEFAULT_TIMEOUT_SEC = 60

# Accept common YouTube channel/handle URL patterns.
_YT_URL_RE = re.compile(
    r"^https?://(www\.)?youtube\.com/(?:@[^/\s]+|c/[^/\s]+|channel/[^/\s]+|user/[^/\s]+)(?:/.*)?$",
    re.IGNORECASE,
)


def _pick(d: dict[str, Any], keys: list[str]) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def run_yt_dlp(url: str, timeout_sec: int = DEFAULT_TIMEOUT_SEC) -> tuple[str, str, str]:
    """Return (uploader, channel_id, channel_url) using yt-dlp without downloading/crawling videos."""
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--skip-download",
        "--no-warnings",
        "--flat-playlist",
        "--playlist-end",
        "1",
        "--dump-single-json",
        url,
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timeout after {timeout_sec}s")

    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()
        raise RuntimeError(err if err else f"yt-dlp failed (code={p.returncode})")

    try:
        info = json.loads(p.stdout)
    except Exception as e:
        raise RuntimeError(f"failed to parse yt-dlp json: {e}")

    if not isinstance(info, dict):
        raise RuntimeError("unexpected yt-dlp output type")

    uploader = _pick(info, ["uploader", "uploader_id", "channel", "channel_uploader"])
    channel_id = _pick(info, ["channel_id", "channelid", "uploader_id"])
    channel_url = _pick(info, ["channel_url", "uploader_url", "webpage_url", "original_url"])

    if channel_url and channel_url.startswith("@"):  # rare
        channel_url = f"https://www.youtube.com/{channel_url}"

    return uploader, channel_id, channel_url


def resolve_json_path(maybe_path: Optional[str]) -> Path:
    script_dir = Path(__file__).resolve().parent

    if maybe_path:
        p = Path(maybe_path)
        if p.exists():
            return p.resolve()
        p2 = (script_dir / maybe_path).resolve()
        if p2.exists():
            return p2
        raise FileNotFoundError(f"JSON not found: {maybe_path}")

    candidates = [
        script_dir / "youtube_wuwa_watchlist.json",
        script_dir / "data" / "youtube_wuwa_watchlist.json",
        script_dir / "DATA" / "youtube_wuwa_watchlist.json",
        script_dir / "NIXE" / "DATA" / "youtube_wuwa_watchlist.json",
        script_dir / "nixe" / "DATA" / "youtube_wuwa_watchlist.json",
    ]
    for c in candidates:
        if c.exists():
            return c.resolve()

    found: list[Path] = []
    for pattern in ("youtube_wuwa_watchlist.json", "youtube_wuwa_watchlist*.json"):
        for p in script_dir.rglob(pattern):
            try:
                rel = p.relative_to(script_dir)
            except ValueError:
                continue
            if len(rel.parts) <= 5:
                found.append(p.resolve())
        if found:
            break

    found = sorted(set(found), key=lambda x: str(x).lower())

    if len(found) == 1:
        return found[0]

    msg = "Tidak menemukan file watchlist secara unik.\n"
    msg += f"Folder script: {script_dir}\n"
    if not found:
        local_json = sorted(script_dir.glob("*.json"))
        if local_json:
            msg += "File .json di folder script:\n" + "\n".join(f"- {p.name}" for p in local_json)
        else:
            msg += "Tidak ada file .json di folder script.\n"
        msg += "\nLetakkan youtube_wuwa_watchlist.json di ./data/ atau ./DATA/, atau jalankan dengan path eksplisit."
        raise FileNotFoundError(msg)

    msg += "Ketemu beberapa kandidat (pilih salah satu path ini sebagai argumen):\n"
    for p in found:
        msg += f"- {p}\n"
    raise FileNotFoundError(msg)


def _load_targets(json_path: Path) -> list[dict[str, Any]]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    targets = data.get("targets", [])
    if not isinstance(targets, list):
        raise ValueError("Format JSON salah: 'targets' harus list")
    out: list[dict[str, Any]] = []
    for i, t in enumerate(targets, start=1):
        if isinstance(t, dict):
            out.append(t)
        else:
            raise ValueError(f"Format JSON salah: targets[{i}] bukan object")
    return out


def _struct_check(targets: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """Return (bad_count, issues)."""
    issues: list[str] = []
    bad = 0

    seen_ids: set[str] = set()
    seen_urls: set[str] = set()

    for idx, t in enumerate(targets, start=1):
        name = str(t.get("name") or "").strip()
        url = str(t.get("url") or "").strip()
        handle = str(t.get("handle") or "").strip()
        channel_id = str(t.get("channel_id") or "").strip()

        if not url:
            bad += 1
            issues.append(f"BAD {idx:02d}: missing url (name={name})")
            continue

        # Normalize: allow '@handle' bare.
        if url.startswith("@"):  # allow shorthand
            url = f"https://www.youtube.com/{url}"

        if not _YT_URL_RE.match(url):
            bad += 1
            issues.append(f"BAD {idx:02d}: invalid youtube url: {url} (name={name})")
            continue

        # Dedupe identifier
        ident = channel_id or handle or url
        if ident in seen_ids:
            bad += 1
            issues.append(f"BAD {idx:02d}: duplicate identifier: {ident} (name={name})")
        else:
            seen_ids.add(ident)

        # Dedupe url string
        if url in seen_urls:
            bad += 1
            issues.append(f"BAD {idx:02d}: duplicate url: {url} (name={name})")
        else:
            seen_urls.add(url)

        if not name:
            # Not fatal, but warn-like.
            issues.append(f"WARN {idx:02d}: empty name for url={url}")

    return bad, issues


def main() -> None:
    _force_utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path", nargs="?", default=None, help="Path ke youtube_wuwa_watchlist.json (opsional)")
    ap.add_argument("--mode", choices=["struct", "network"], default="struct", help="struct=cepat tanpa network; network=yt-dlp per channel")

    # NETWORK mode args
    ap.add_argument("--sleep", type=float, default=0.2, help="Delay antar channel (detik) [network]")
    ap.add_argument("--start", type=int, default=1, help="Mulai dari index ke-N (1-based) [network]")
    ap.add_argument("--limit", type=int, default=0, help="Batasi jumlah cek (0=semua) [network/struct]")
    ap.add_argument("--timeout", type=int, default=20, help="Timeout per channel (detik) [network]")

    args = ap.parse_args()
    json_path = resolve_json_path(args.json_path)

    targets = _load_targets(json_path)

    # Apply limit for both modes
    start = 1
    end = len(targets)
    if args.limit and int(args.limit) > 0:
        end = min(end, int(args.limit))
    targets_limited = targets[start - 1 : end]

    print(f"JSON: {json_path}")
    print(f"Targets: {len(targets)} | mode={args.mode} | limit={len(targets_limited)}\n")

    if args.mode == "struct":
        bad, issues = _struct_check(targets_limited)
        for line in issues:
            print(line)
        if bad > 0:
            print(f"\nFAIL: {bad} structural issue(s) found")
            sys.exit(2)
        print("\nPASS: structural checks OK")
        return

    # NETWORK mode
    if importlib.util.find_spec("yt_dlp") is None:
        print("ERROR: yt_dlp is not installed in this Python environment.")
        print("Install: python -m pip install -U yt-dlp")
        sys.exit(2)

    start_net = max(1, int(args.start))
    end_net = len(targets)
    if int(args.limit) and int(args.limit) > 0:
        end_net = min(end_net, start_net + int(args.limit) - 1)

    print(f"NETWORK checking {start_net}..{end_net} | sleep={float(args.sleep)}s | timeout={int(args.timeout)}s\n")

    bad_count = 0
    for idx in range(start_net, end_net + 1):
        t = targets[idx - 1]
        name = str(t.get("name") or "")
        url = str(t.get("url") or "")
        try:
            uploader, channel_id, channel_url = run_yt_dlp(url, timeout_sec=int(args.timeout))
            print(f"OK  {idx:02d}  {name}  ->  {uploader}  {channel_id}  {channel_url}")
        except Exception as e:
            print(f"BAD {idx:02d}  {name}  url={url}  err={e}")
            bad_count += 1
        time.sleep(float(args.sleep))

    if bad_count > 0:
        print(f"\nFAIL: {bad_count} channel(s) failed network verification")
        sys.exit(2)

    print("\nPASS: all checked channels verified (network)")


if __name__ == "__main__":
    main()
