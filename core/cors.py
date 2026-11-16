"""Cross-origin (CORS) policy for the Flask app.

The web UI is same-origin and needs no CORS. API v1 (bearer tokens) and OPDS
are used by non-browser clients, which do not enforce CORS either.

The one cross-origin caller is the browser extension (``extension/``): its
background service worker POSTs ``{"link": ...}`` to ``/download``. When the
user grants the optional host permission for the CLU origin (options.js asks
for it) the browser skips CORS entirely; when they decline, the request
carries an ``Origin`` of ``chrome-extension://<id>`` (Chrome, Edge),
``moz-extension://<uuid>`` (Firefox, a random per-install id) or
``safari-web-extension://<id>``, and needs the server to allow it.

So CORS is granted for ``/download`` only, and only to extension origins.
Every other route sends no ``Access-Control-Allow-Origin`` header.

``api.py`` applies this through :func:`configure_cors`; it lives here so the
tests can apply the same policy to their own app (api.py cannot be imported
in tests).
"""
import re

from flask_cors import CORS

# Paths are matched with ``re.match`` (anchored at the start only), so the
# pattern is anchored at the end too: /download_status etc. stay excluded.
CORS_DOWNLOAD_PATH = re.compile(r"^/download\Z")

# Browser-extension origins. The id is a single host-like token.
EXTENSION_ORIGIN = re.compile(
    r"^(?:chrome|moz|safari-web)-extension://[A-Za-z0-9._-]+\Z",
    re.IGNORECASE,
)

CORS_RESOURCES = {
    CORS_DOWNLOAD_PATH: {
        "origins": [EXTENSION_ORIGIN],
        "methods": ["POST", "OPTIONS"],
    },
}


def configure_cors(app):
    """Apply CLU's CORS policy to *app*."""
    return CORS(app, resources=CORS_RESOURCES)
