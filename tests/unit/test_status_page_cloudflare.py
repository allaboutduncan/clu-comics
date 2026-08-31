"""The /status page has to say *why* a Cloudflare-blocked download failed.

A managed Cloudflare challenge is the one failure CLU cannot retry its way out
of (`should_auto_retry` refuses as soon as `manual_url` is set), so the row is
terminal and the user has to fetch the file in a browser themselves. If the
page only ever showed "error" there would be nothing on screen telling them
that, nor anything to click.

These are text assertions rather than route tests because the rendering lives
in status.html's own JavaScript — there is no Python to exercise. They guard
the two things that must not quietly disappear: the manual_url branch, and the
http(s)-only guard on the URL that ends up in an anchor's href.
"""

from pathlib import Path

import pytest

STATUS_HTML = Path(__file__).resolve().parents[2] / "templates" / "status.html"


@pytest.fixture(scope="module")
def status_source():
    return STATUS_HTML.read_text(encoding="utf-8")


class TestCloudflareRow:
    def test_reads_manual_url_from_the_progress_entry(self, status_source):
        """api.py records the link on the progress dict and
        build_status_snapshot passes it straight through, so the field name is
        a contract between the two."""
        assert "details.manual_url" in status_source

    def test_status_cell_names_cloudflare(self, status_source):
        assert "Blocked by Cloudflare" in status_source

    def test_offers_a_manual_download_link(self, status_source):
        assert "manualDownloadLink" in status_source
        assert "Download manually" in status_source

    def test_link_opens_in_a_new_tab_without_opener(self, status_source):
        assert "a.target = '_blank'" in status_source
        assert "a.rel = 'noopener'" in status_source

    def test_only_http_urls_are_turned_into_links(self, status_source):
        """The value lands in an href, so a javascript:/data: URL would be an
        injection vector if manual_url ever came from somewhere less trusted."""
        assert r"/^https?:\/\//i" in status_source

    def test_href_is_assigned_not_interpolated(self, status_source):
        """Building the anchor with innerHTML would put the URL inside markup;
        assigning the property keeps it data."""
        assert "a.href = url;" in status_source
