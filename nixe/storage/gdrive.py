"""nixe.storage.gdrive

Minimal Google Drive v3 helper via raw HTTP (aiohttp).

Auth options (choose one):
  A) Inject access token directly:
     - GDRIVE_ACCESS_TOKEN=<OAuth2 access token with Drive scope>

  B) Auto-refresh (recommended for long-running bots):
     - GDRIVE_REFRESH_TOKEN=<OAuth2 refresh token>
     - GDRIVE_CLIENT_ID=<OAuth client id>
     - GDRIVE_CLIENT_SECRET=<OAuth client secret>

Folder ops:
  - You can locate files by name inside a folder (DICT_GDRIVE_FOLDER_ID) and optionally create them.

Notes:
- Service Account flow is not implemented (to avoid extra deps + RSA signing); use OAuth refresh flow instead.

Drive API reference: https://developers.google.com/drive/api/v3/reference/
"""

from __future__ import annotations

import os, json, time
from typing import Optional, Dict, Any, Tuple

import aiohttp

_TOKEN_CACHE: Tuple[str, float] = ("", 0.0)  # (token, expiry_ts)


def _env(k: str, default: str = "") -> str:
    return (os.getenv(k, default) or default).strip()


async def _refresh_access_token() -> Optional[Tuple[str, float]]:
    """Return (access_token, expiry_ts) or None."""
    refresh_token = _env("GDRIVE_REFRESH_TOKEN", "")
    client_id = _env("GDRIVE_CLIENT_ID", "")
    client_secret = _env("GDRIVE_CLIENT_SECRET", "")
    if not (refresh_token and client_id and client_secret):
        return None

    url = "https://oauth2.googleapis.com/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=data) as r:
            if r.status != 200:
                txt = await r.text()
                raise RuntimeError(f"OAuth refresh gagal ({r.status}): {txt[:500]}")
            js = await r.json()
            tok = (js.get("access_token") or "").strip()
            exp = int(js.get("expires_in") or 3600)
            if not tok:
                raise RuntimeError("OAuth refresh: access_token kosong")
            # refresh a bit early
            return tok, (time.time() + max(60, exp - 120))


async def _get_token() -> str:
    """Get a valid access token from env or refresh flow."""
    global _TOKEN_CACHE

    direct = _env("GDRIVE_ACCESS_TOKEN", "")
    if direct:
        return direct

    tok, exp_ts = _TOKEN_CACHE
    if tok and time.time() < exp_ts:
        return tok

    refreshed = await _refresh_access_token()
    if refreshed:
        _TOKEN_CACHE = refreshed
        return refreshed[0]

    raise RuntimeError(
        "Drive auth tidak tersedia. Set GDRIVE_ACCESS_TOKEN atau set GDRIVE_REFRESH_TOKEN + GDRIVE_CLIENT_ID + GDRIVE_CLIENT_SECRET."
    )


async def _api_headers() -> Dict[str, str]:
    tok = await _get_token()
    return {
        "Authorization": f"Bearer {tok}",
        "Accept": "application/json",
    }


def _escape_q(s: str) -> str:
    return (s or "").replace("'", r"\'")


async def fetch_meta(file_id: str) -> Dict[str, Any]:
    if not file_id:
        raise ValueError("file_id kosong")
    url = (
        "https://www.googleapis.com/drive/v3/files/"
        f"{file_id}?fields=id,name,mimeType,size,md5Checksum,modifiedTime"
    )
    headers = await _api_headers()
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url) as r:
            if r.status != 200:
                txt = await r.text()
                raise RuntimeError(f"Drive meta fetch gagal ({r.status}): {txt[:500]}")
            return await r.json()


async def download_to_path(file_id: str, out_path: str) -> None:
    if not file_id:
        raise ValueError("file_id kosong")
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    headers = await _api_headers()
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url) as r:
            if r.status != 200:
                txt = await r.text()
                raise RuntimeError(f"Drive download gagal ({r.status}): {txt[:500]}")
            with open(out_path, "wb") as f:
                async for chunk in r.content.iter_chunked(1024 * 1024):
                    if chunk:
                        f.write(chunk)


async def upload_bytes_overwrite(file_id: str, data: bytes, mime_type: str = "application/octet-stream") -> None:
    if not file_id:
        raise ValueError("file_id kosong")
    url = f"https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=media"
    headers = await _api_headers()
    headers = dict(headers)
    headers["Content-Type"] = mime_type
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.patch(url, data=data) as r:
            if r.status not in (200, 201):
                txt = await r.text()
                raise RuntimeError(f"Drive upload overwrite gagal ({r.status}): {txt[:500]}")


async def find_file_id_by_name(folder_id: str, name: str) -> Optional[str]:
    if not folder_id:
        raise ValueError("folder_id kosong")
    if not name:
        raise ValueError("name kosong")

    q = f"name='{_escape_q(name)}' and '{_escape_q(folder_id)}' in parents and trashed=false"
    url = "https://www.googleapis.com/drive/v3/files"
    params = {"q": q, "pageSize": 1, "fields": "files(id,name)"}

    headers = await _api_headers()
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, params=params) as r:
            if r.status != 200:
                txt = await r.text()
                raise RuntimeError(f"Drive list gagal ({r.status}): {txt[:500]}")
            data = await r.json()
            files = data.get("files") or []
            if not files:
                return None
            return (files[0].get("id") or "").strip() or None


async def create_file_in_folder(folder_id: str, name: str, mime_type: str = "application/octet-stream") -> str:
    if not folder_id:
        raise ValueError("folder_id kosong")
    if not name:
        raise ValueError("name kosong")

    url = "https://www.googleapis.com/drive/v3/files?fields=id"
    body = {"name": name, "parents": [folder_id], "mimeType": mime_type}

    headers = await _api_headers()
    headers = dict(headers)
    headers["Content-Type"] = "application/json"

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post(url, data=json.dumps(body).encode("utf-8")) as r:
            if r.status not in (200, 201):
                txt = await r.text()
                raise RuntimeError(f"Drive create gagal ({r.status}): {txt[:500]}")
            data = await r.json()
            fid = (data.get("id") or "").strip()
            if not fid:
                raise RuntimeError("Drive create: response tanpa id")
            return fid


async def ensure_file_id(folder_id: str, name: str, create: bool = False, mime_type: str = "application/octet-stream") -> Optional[str]:
    fid = await find_file_id_by_name(folder_id, name)
    if fid:
        return fid
    if not create:
        return None
    return await create_file_in_folder(folder_id, name, mime_type=mime_type)
