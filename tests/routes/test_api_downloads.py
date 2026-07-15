"""Tests for the download queue's provider selection, labelling and routing.

api.py itself is deliberately not imported here: at import time it builds a
Flask app, registers SIGINT/SIGTERM handlers, creates the download directory
and starts three worker threads (see the note in tests/routes/conftest.py).
The decision logic api.py depends on therefore lives in models/getcomics.py,
which is side-effect free and covered directly below.

Regression context: getcomics fronts every provider's button with an
indistinguishable ``getcomics.org/dls/<token>`` redirector, so a download's
provider cannot be inferred from its URL before resolution. Downloads with a
Pixeldrain link were being reported (and downloaded) as GetComics.
"""
import pytest

from models.getcomics import (
    PROVIDER_LABELS,
    is_unresolved_gc_redirect,
    provider_label,
    select_download_url,
)


class TestProviderPrioritySelection:

    def test_pixeldrain_beats_getcomics_when_configured_first(self):
        links = {
            "pixeldrain": "https://getcomics.org/dls/PDTOKEN",
            "download_now": "https://getcomics.org/dls/MAINTOKEN",
            "mega": None,
        }

        (provider, url), fallbacks = select_download_url(links, "pixeldrain,download_now,mega")

        assert provider == "pixeldrain"
        assert url == "https://getcomics.org/dls/PDTOKEN"
        # The runner-up stays available for failover, in priority order.
        assert fallbacks == [("download_now", "https://getcomics.org/dls/MAINTOKEN")]

    def test_getcomics_used_only_when_pixeldrain_absent(self):
        links = {"pixeldrain": None, "download_now": "https://getcomics.org/dls/MAINTOKEN", "mega": None}

        (provider, _url), _fallbacks = select_download_url(links, "pixeldrain,download_now,mega")

        assert provider == "download_now"


class TestProviderLabelling:
    """The label must reflect the provider priority chose, not a URL guess."""

    def test_dls_wrapped_pixeldrain_not_reported_as_getcomics(self):
        # The exact regression: before resolution this URL is indistinguishable
        # from the main-server link, and sniffing it yields "getcomics".
        label = provider_label("pixeldrain", "https://getcomics.org/dls/PDTOKEN")
        assert label == "pixeldrain"

    def test_resolved_pixeldrain_reported_as_pixeldrain(self):
        assert provider_label("pixeldrain", "https://pixeldrain.com/u/8uSDFbt2") == "pixeldrain"

    def test_download_now_key_maps_to_getcomics_label(self):
        # The config/priority key and the UI label differ by name.
        assert PROVIDER_LABELS["download_now"] == "getcomics"
        assert provider_label("download_now", "https://fs3.comicfiles.ru/x.cbz") == "getcomics"

    def test_every_priority_key_has_a_status_ui_label(self):
        # Guards against a new provider key that renders as a blank cell.
        for key in ("pixeldrain", "download_now", "mega"):
            assert PROVIDER_LABELS.get(key)

    @pytest.mark.parametrize("url,expected", [
        ("https://pixeldrain.com/u/abc", "pixeldrain"),
        ("https://mega.nz/file/abc", "mega"),
        ("https://comicbookplus.com/?dlid=1", "comicbookplus"),
        ("https://fs3.comicfiles.ru/x.cbz", "getcomics"),
    ])
    def test_keyless_downloads_fall_back_to_url(self, url, expected):
        # External/browser-extension downloads never ran priority selection.
        assert provider_label(None, url) == expected


class TestUnresolvedRedirectHandling:
    """An unresolved /dls/ link must fail over, not download as GetComics."""

    def test_unresolved_redirect_is_flagged(self):
        assert is_unresolved_gc_redirect("https://getcomics.org/dls/PDTOKEN==:abc==")

    def test_resolved_provider_url_is_not_flagged(self):
        assert not is_unresolved_gc_redirect("https://pixeldrain.com/u/8uSDFbt2")
        assert not is_unresolved_gc_redirect("https://fs3.comicfiles.ru/x.cbz")

    def test_post_page_is_not_flagged(self):
        # page_url is passed through as the Cloudflare escape hatch and must
        # not be mistaken for an unresolved download redirect.
        assert not is_unresolved_gc_redirect("https://getcomics.org/dc/nightwing-140-2026/")
