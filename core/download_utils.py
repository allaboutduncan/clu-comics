"""Small, dependency-light helpers for the download pipeline.

Kept separate from ``api.py`` so the pure logic can be imported and tested
without triggering api.py's import-time side effects (worker threads, DB
connection, cloudscraper).
"""


def is_cloudflare_challenge(response) -> bool:
    """Detect a Cloudflare managed / JS "Just a moment..." challenge response.

    These challenges cannot be solved by any automated HTTP client (requests,
    cloudscraper, curl_cffi) or even headless/scripted browsers — only a real,
    manually-driven browser passes them. When we see one there is no point
    retrying; the caller surfaces a clear "download manually" error instead.

    Accepts anything with ``.headers`` (mapping) and ``.content`` (bytes),
    e.g. a ``requests.Response``.
    """
    try:
        headers = response.headers
        # Most reliable signal: Cloudflare stamps this on challenge responses.
        if 'challenge' in headers.get('cf-mitigated', '').lower():
            return True
        if 'cloudflare' not in headers.get('Server', '').lower():
            return False
        if 'text/html' not in headers.get('Content-Type', '').lower():
            return False
        # Fall back to sniffing the (small) challenge page body for markers.
        snippet = response.content[:4096].lower()
        return (b'just a moment' in snippet
                or b'__cf_chl' in snippet
                or b'challenge-platform' in snippet)
    except Exception:
        return False
