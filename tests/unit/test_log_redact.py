"""core.log_redact: credentials in URLs are masked before text is written down.

Key-bearing URLs are assembled from parts with an obviously fake value, so
nothing here looks like a real credential.
"""
import logging
import re
from unittest.mock import patch

import pytest

from core.log_redact import MASK, redact_url

FAKE = "fake-" + "k3y" + "-value"


def _url(base, **params):
    return base + "?" + "&".join(f"{k}={v}" for k, v in params.items())


class TestRedactUrl:

    @pytest.mark.parametrize("param", ["apikey", "api_key", "APIKEY", "r", "token",
                                       "passkey", "password", "key"])
    def test_credential_param_is_masked(self, param):
        text = _url("https://indexer.example/api", t="search", **{param: FAKE}, o="xml")
        out = redact_url(text)
        assert FAKE not in out
        assert f"{param}={MASK}" in out
        # The rest of the URL survives, so the line is still useful.
        assert "t=search" in out and "o=xml" in out

    def test_non_credential_params_are_kept(self):
        text = _url("https://indexer.example/api", t="caps", q="Batman 1", cat="7030")
        assert redact_url(text) == text

    def test_requests_http_error_message(self):
        url = _url("http://sab.local:8080/api", mode="queue", output="json", apikey=FAKE)
        msg = f"401 Client Error: Unauthorized for url: {url}"
        out = redact_url(msg)
        assert FAKE not in out
        assert out.startswith("401 Client Error: Unauthorized for url: http://sab.local:8080/api?")

    def test_urllib3_relative_url_in_exception(self):
        msg = ("HTTPConnectionPool(host='idx', port=80): Max retries exceeded with url: "
               f"/api?t=search&apikey={FAKE}&o=xml (Caused by NewConnectionError('x'))")
        out = redact_url(msg)
        assert FAKE not in out
        assert "(Caused by NewConnectionError('x'))" in out

    def test_exception_object(self):
        err = RuntimeError(_url("https://idx.example/getnzb", id="1", r=FAKE))
        assert FAKE not in redact_url(err)

    def test_userinfo_is_masked(self):
        text = "https://someone:" + FAKE + "@nzb.example/api"
        out = redact_url(text)
        assert FAKE not in out and "someone" not in out
        assert out == f"https://{MASK}@nzb.example/api"

    def test_percent_encoded_url_inside_a_url(self):
        inner = "https%3A%2F%2Fidx.example%2Fgetnzb%3Fid%3D1%26r%3D" + FAKE + "%26i%3D2"
        text = _url("http://sab.local/api", mode="addurl", name=inner, apikey=FAKE)
        out = redact_url(text)
        assert FAKE not in out
        assert "%26i%3D2" in out  # the parameter after the key survives

    def test_none_and_empty(self):
        assert redact_url(None) is None
        assert redact_url("") == ""

    def test_plain_text_untouched(self):
        text = "Usenet search Batman #1: tried ['Batman 001'] -> 3 unique result(s)"
        assert redact_url(text) == text

    def test_fragment_boundary(self):
        out = redact_url(_url("https://x.example/p", token=FAKE) + "#frag")
        assert out.endswith(f"token={MASK}#frag")


class TestLoggerFilter:
    """The app/monitor loggers scrub every record (the backstop for log sites
    CLU cannot edit, such as api.py)."""

    @pytest.mark.parametrize("name", ["app_logger", "monitor_logger"])
    def test_logger_has_the_filter(self, name):
        import core.app_logging as al
        logger = logging.getLogger(name)
        assert any(isinstance(f, al.UrlCredentialFilter) for f in logger.filters)

    def test_filter_rewrites_message_with_args(self):
        from core.app_logging import UrlCredentialFilter
        record = logging.LogRecord(
            "app_logger", logging.ERROR, __file__, 1,
            "Download failed for %s", (_url("https://m.example/f.cbz", token=FAKE),), None,
        )
        assert UrlCredentialFilter().filter(record) is True
        assert FAKE not in record.getMessage()

    def test_filter_never_raises(self):
        from core.app_logging import UrlCredentialFilter
        record = logging.LogRecord("app_logger", logging.INFO, __file__, 1,
                                   "%s %s", ("only-one",), None)
        assert UrlCredentialFilter().filter(record) is True

    def test_app_logger_output_is_scrubbed(self, caplog):
        from core.app_logging import app_logger
        with caplog.at_level(logging.INFO, logger="app_logger"):
            app_logger.error(f"fetch failed: {_url('https://idx.example/api', apikey=FAKE)}")
        assert FAKE not in caplog.text
        assert "apikey=" + MASK in caplog.text


class TestSitesRedact:
    """The log/status sites that carried a key now go through the helper."""

    def test_sabnzbd_exception_is_redacted(self):
        import requests
        from models.download_clients import DownloadClientConfig
        from models.download_clients.sabnzbd_client import SABnzbdClient

        cfg = DownloadClientConfig(host="sab.local", port=8080, api_key=FAKE)
        client = SABnzbdClient(cfg)
        boom = requests.exceptions.HTTPError(
            "401 Client Error: Unauthorized for url: "
            + _url("http://sab.local:8080/api", mode="addurl", apikey=FAKE))
        with patch("requests.get", side_effect=boom), \
                patch("models.download_clients.sabnzbd_client.app_logger") as log:
            result = client.add_nzb("https://idx.example/getnzb", "Batman 001")
        assert not result.success
        assert FAKE not in result.error
        assert FAKE not in (client.last_error or "")
        assert all(FAKE not in str(c) for c in log.error.call_args_list)

    def test_newznab_search_exception_is_redacted(self):
        from models.indexers import IndexerConfig, get_indexer_impl
        from models.indexers.base import IndexerType

        cfg = IndexerConfig(name="idx", url="https://idx.example", api_key=FAKE)
        impl = get_indexer_impl(IndexerType.NEWZNAB, cfg)
        boom = ConnectionError("Max retries exceeded with url: "
                               + _url("/api", t="search", apikey=FAKE))
        with patch("requests.get", side_effect=boom):
            assert impl.search("Batman") == []
        assert FAKE not in impl.last_error

    def test_usenet_nzb_fetch_logs_redacted(self):
        from models import usenet
        url = _url("https://idx.example/getnzb", id="9", r=FAKE)
        with patch("requests.get", side_effect=OSError("boom " + url)), \
                patch.object(usenet, "app_logger") as log:
            assert usenet._fetch_nzb(url) is None
        logged = " ".join(str(c) for c in log.method_calls)
        assert FAKE not in logged
        assert re.search(r"r=\*\*\*", logged)

    def test_download_error_status_is_redacted(self):
        from core.download_utils import set_error_status
        progress = {"d1": {"status": "in_progress"}}
        set_error_status(progress, "d1", "403 for " + _url("https://m.example/f", token=FAKE))
        assert FAKE not in progress["d1"]["error"]

    def test_notification_body_is_redacted(self):
        from core.download_utils import download_notification_body
        body = download_notification_body(
            "Batman 001.cbz", error="failed " + _url("https://m.example/f", token=FAKE))
        assert FAKE not in body

    def test_debug_package_log_tail_is_redacted(self, tmp_path):
        import io
        import zipfile
        from core import debug_package

        log = tmp_path / "app.log"
        log.write_text("ERROR fetch " + _url("https://idx.example/api", apikey=FAKE) + "\n",
                       encoding="utf-8")
        with patch.object(debug_package, "APP_LOG", str(log)), \
                patch.object(debug_package, "MONITOR_LOG", str(log)), \
                patch.object(debug_package, "_db_settings_json", return_value="{}"), \
                patch.object(debug_package, "_download_runs_json", return_value="{}"), \
                patch.object(debug_package, "_preflight_json", return_value="{}"), \
                patch.object(debug_package, "_root_paths_json", return_value="{}"):
            data = debug_package.build_debug_package()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            text = zf.read("logs/app.log").decode("utf-8")
        assert FAKE not in text
        assert "apikey=" + MASK in text
