# -*- coding: utf-8 -*-
from __future__ import annotations

"""ASGI entrypoint for the Flask dashboard.

This module is imported during our smoketest "import all modules" pass.
Some environments (local QA / CI / dry-run) may not have optional ASGI server
dependencies installed (e.g., `uvicorn`).

Policy:
- Importing this module must never fail.
- We only raise an error when the ASGI app is actually used by an ASGI server.
"""

from typing import Any, Callable, Awaitable

try:
    from uvicorn.middleware.wsgi import WSGIMiddleware  # type: ignore
except Exception as e:  # pragma: no cover
    WSGIMiddleware = None  # type: ignore
    _uvicorn_import_err: Exception | None = e
else:
    _uvicorn_import_err = None

try:
    # External Flask WSGI app exported by dashboard entrypoint.
    from app import app as _flask_app  # type: ignore
except Exception as e:  # pragma: no cover
    _flask_app = None
    _flask_import_err: Exception | None = e
else:
    _flask_import_err = None


def build_app() -> Any:
    if WSGIMiddleware is None:
        raise RuntimeError(
            "uvicorn is not installed. Install with `pip install uvicorn` to run ASGI server. "
            f"(import err: {_uvicorn_import_err})"
        )
    if _flask_app is None:
        raise RuntimeError(f"Failed to import Flask app: {_flask_import_err}")
    return WSGIMiddleware(_flask_app)


def _make_error_app(err: Exception) -> Callable[[dict, Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[Any]]], Awaitable[None]]:
    async def _app(scope, receive, send):
        if scope.get("type") != "http":
            return
        body = (f"ASGI app unavailable: {err}").encode("utf-8", errors="replace")
        headers = [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        await send({"type": "http.response.start", "status": 500, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    return _app


try:
    app = build_app()
except Exception as e:  # keep import-safe for smoketests
    app = _make_error_app(e)
