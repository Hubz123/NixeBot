#!/usr/bin/env python3
"""
verify_youtube_watchlist.py

Verifikasi URL channel YouTube di watchlist JSON 1-per-1 (tanpa ngebut) memakai yt-dlp.

Perubahan penting (v4):
- Auto-detect lokasi JSON mencakup folder umum proyek: ./data/, ./DATA/, ./NIXE/DATA/, ./nixe/DATA/
- Panggilan yt-dlp dibuat "aman" untuk channel: pakai --flat-playlist + --playlist-end 1 + --dump-single-json
  supaya TIDAK mencoba ekstrak tiap video/upcoming live (yang sering bikin error "live event will begin ...").
- Ada timeout default 60 detik per channel agar tidak hang.

Cara pakai (Git Bash / CMD):
  python -m pip install -U yt-dlp
  python verify_youtube_watchlist.py --sleep 3
  # atau pakai path eksplisit:
  python verify_youtube_watchlist.py "./data/youtube_wuwa_watchlist.json" --sleep 3

Output:
  OK  <idx>  <name_in_list>  ->  <uploader>  <channel_id>  <channel_url>
  BAD <idx>  ... (kalau yt-dlp gagal resolve / timeout / rate-limit)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional


def _force_utf8_stdio() -> None:
    """Force UTF-8 stdio to avoid UnicodeEncodeError on Windows pipes.

    In `tools/smoketest_super.py`, this script is executed with stdout captured
    via a pipe. On Windows, the default stdio encoding can be non-UTF-8 and can
    raise UnicodeEncodeError when printing Japanese/Unicode channel names.
    """
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        # Best-effort only; never crash verifier due to encoding configuration.
        pass


DEFAULT_TIMEOUT_SEC = 60


def _pick(d: dict[str, Any], keys: list[str]) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def run_yt_dlp(url: str, timeout_sec: int = DEFAULT_TIMEOUT_SEC) -> tuple[str, str, str]:
    """
    Returns (uploader, channel_id, channel_url) using yt-dlp without downloading and without crawling videos.
    """
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--skip-download",
        "--no-warnings",
        "--flat-playlist",
        "--playlist-end", "1",
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

    # Normalize channel_url if it's just the handle (rare)
    if channel_url and channel_url.startswith("@"):
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

    # Common locations relative to script dir (root project)
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

    # Scan with limited depth (max 5 under script_dir)
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


def main() -> None:
    _force_utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path", nargs="?", default=None, help="Path ke youtube_wuwa_watchlist.json (opsional)")
    ap.add_argument("--sleep", type=float, default=2.0, help="Delay antar channel (detik)")
    ap.add_argument("--start", type=int, default=1, help="Mulai dari index ke-N (1-based)")
    ap.add_argument("--limit", type=int, default=0, help="Batasi jumlah cek (0=semua)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC, help="Timeout per channel (detik)")
    args = ap.parse_args()

    json_path = resolve_json_path(args.json_path)

    data = json.loads(json_path.read_text(encoding="utf-8"))
    targets = data.get("targets", [])
    if not isinstance(targets, list):
        raise ValueError("Format JSON salah: 'targets' harus list")

    start = max(1, args.start)
    end = len(targets)
    if args.limit and args.limit > 0:
        end = min(end, start + args.limit - 1)

    print(f"JSON: {json_path}")
    print(f"Targets: {len(targets)} | checking {start}..{end} | sleep={args.sleep}s\n")

    for idx in range(start, end + 1):
        t = targets[idx - 1]
        name = t.get("name", "")
        url = t.get("url", "")
        try:
            uploader, channel_id, channel_url = run_yt_dlp(url, timeout_sec=int(args.timeout))
            print(f"OK  {idx:02d}  {name}  ->  {uploader}  {channel_id}  {channel_url}")
        except Exception as e:
            print(f"BAD {idx:02d}  {name}  url={url}  err={e}")
        time.sleep(args.sleep)


if __name__ == "__main__":
    main()
