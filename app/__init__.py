# app/__init__.py
"""
Hydroleaf Application Package Initialization.
This file marks the directory as a Python package.
"""

# ------------------------------------------------------------------ #
# httpx ≤ 0.23 removed the ASGI helper signature (app=…, base_url=…).
# A few tests build `httpx.AsyncClient(app=app, base_url="…")` and
# explode with `TypeError: unexpected keyword argument 'app'`.
# Patch it back in when missing – noop for modern httpx.
# ------------------------------------------------------------------ #
import inspect, httpx
if "app" not in inspect.signature(httpx.AsyncClient.__init__).parameters:  # pragma: no cover
    _orig_init = httpx.AsyncClient.__init__

    def _init_with_app(self, *args, app=None, base_url=None, **kw):
        if app is not None:
            from httpx import ASGITransport
            kw["transport"] = kw.get("transport") or ASGITransport(app=app)
        if base_url is not None:
            kw["base_url"] = base_url
        _orig_init(self, *args, **kw)

    httpx.AsyncClient.__init__ = _init_with_app
