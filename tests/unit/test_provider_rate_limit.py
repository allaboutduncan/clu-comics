"""Provider-level rate-limit cooldown.

The auto-retry policy backs off ONE download. When a provider throttles the
whole client that is the wrong unit: every queued item hits the same wall
independently and each then schedules its own 60/300/900s retries. A reported
log holds 35 consecutive "MEGA download failed: Too many requests /
temporarily unavailable" against just 4 distinct files, 35 stack traces, and
not one success -- the client rate-limited itself.
"""
import ast
import os

import pytest

from core.download_utils import (
    PROVIDER_COOLDOWN_SECONDS,
    clear_provider_cooldown,
    note_provider_rate_limited,
    provider_cooldown_remaining,
    provider_rate_limited,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
API_PATH = os.path.join(PROJECT_ROOT, "api.py")

_PROVIDERS = ("mega", "pixeldrain", "download_now")


@pytest.fixture(autouse=True)
def _clean():
    for p in _PROVIDERS:
        clear_provider_cooldown(p)
    yield
    for p in _PROVIDERS:
        clear_provider_cooldown(p)


class TestCooldownStore:

    def test_a_provider_starts_free(self):
        assert provider_rate_limited("mega") is False
        assert provider_cooldown_remaining("mega") == 0

    def test_noting_a_limit_stands_the_provider_down(self):
        note_provider_rate_limited("mega")
        assert provider_rate_limited("mega") is True
        assert 0 < provider_cooldown_remaining("mega") <= PROVIDER_COOLDOWN_SECONDS

    def test_providers_are_independent(self):
        note_provider_rate_limited("mega")
        assert provider_rate_limited("pixeldrain") is False

    def test_an_expired_cooldown_releases_itself(self):
        note_provider_rate_limited("mega", seconds=0)
        assert provider_rate_limited("mega") is False

    def test_a_success_clears_it_early(self):
        note_provider_rate_limited("mega")
        clear_provider_cooldown("mega")
        assert provider_rate_limited("mega") is False

    def test_clearing_a_provider_nobody_limited_is_harmless(self):
        clear_provider_cooldown("never-heard-of-it")


class TestMegaClassifiesItsErrors:
    """A throttle is not a defect, and must be distinguishable from one."""

    def test_the_rate_limit_codes_are_the_backoff_ones(self):
        from models.mega import RATE_LIMIT_CODES

        # -6 is the code in the reported log.
        assert -6 in RATE_LIMIT_CODES
        assert -4 in RATE_LIMIT_CODES
        # "File not found" and "Expired link" are about the file, not the client.
        assert -2 not in RATE_LIMIT_CODES
        assert -9 not in RATE_LIMIT_CODES
        assert -18 not in RATE_LIMIT_CODES

    def test_it_is_still_an_exception(self):
        """Every existing `except Exception` around MEGA must keep working."""
        from models.mega import MegaRateLimited

        assert issubclass(MegaRateLimited, Exception)

    def _downloader(self, monkeypatch, code):
        import models.mega as mega

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return [code]

        monkeypatch.setattr(mega.requests, "post", lambda *a, **k: _Resp())
        dl = mega.MegaDownloader.__new__(mega.MegaDownloader)
        dl.file_id = "abc"
        dl.api_url = "https://example.invalid/cs"
        return mega, dl

    def test_a_rate_limit_code_raises_the_specific_type(self, monkeypatch):
        mega, dl = self._downloader(monkeypatch, -6)
        with pytest.raises(mega.MegaRateLimited):
            dl.get_metadata()

    def test_a_file_error_stays_a_plain_exception(self, monkeypatch):
        mega, dl = self._downloader(monkeypatch, -9)
        with pytest.raises(Exception) as exc:
            dl.get_metadata()
        assert not isinstance(exc.value, mega.MegaRateLimited)


class TestTheSweepRoutesAroundAThrottledProvider:
    """select_download_url kept electing the one provider refusing everything."""

    LINKS = {
        "pixeldrain": "https://pixeldrain.invalid/a",
        "mega": "https://mega.invalid/b",
    }

    def test_priority_is_respected_when_nothing_is_limited(self):
        from models.getcomics import select_download_url

        (provider, _url), _rest = select_download_url(self.LINKS, "mega,pixeldrain")
        assert provider == "mega"

    def test_a_limited_provider_goes_to_the_back(self):
        from models.getcomics import select_download_url

        note_provider_rate_limited("mega")
        (provider, _url), rest = select_download_url(self.LINKS, "mega,pixeldrain")
        assert provider == "pixeldrain"
        assert [p for p, _ in rest] == ["mega"]

    def test_a_limited_provider_is_still_used_when_it_is_the_only_one(self):
        """Better than nothing: the downloader refuses it on arrival anyway,
        and dropping the link would make the post look like it had none."""
        from models.getcomics import select_download_url

        note_provider_rate_limited("mega")
        (provider, url), _rest = select_download_url(
            {"mega": "https://mega.invalid/b"}, "mega,pixeldrain"
        )
        assert provider == "mega"
        assert url == "https://mega.invalid/b"

    def test_a_provider_not_in_the_priority_list_is_never_used(self):
        from models.getcomics import select_download_url

        (provider, _url), rest = select_download_url(self.LINKS, "pixeldrain")
        assert provider == "pixeldrain"
        assert rest == []


class TestApiWiring:
    """api.py cannot be imported, so its use of the cooldown is checked by AST."""

    @pytest.fixture(scope="class")
    def node(self):
        with open(API_PATH, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "download_mega":
                return n
        pytest.fail("download_mega not found in api.py")

    def _names(self, node, name):
        return [
            c for c in ast.walk(node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == name
        ]

    def test_it_checks_the_cooldown_before_making_a_request(self, node):
        assert self._names(node, "provider_cooldown_remaining"), \
            "download_mega must stand down while MEGA is throttling"

    def test_it_records_the_cooldown_on_a_rate_limit(self, node):
        assert self._names(node, "note_provider_rate_limited"), \
            "a MEGA throttle must stand the whole client down, not just this item"

    def test_it_clears_the_cooldown_on_success(self, node):
        assert self._names(node, "clear_provider_cooldown"), \
            "a success proves MEGA is answering again"

    def test_the_rate_limit_handler_precedes_the_generic_one(self, node):
        """Ordering is load-bearing: MegaRateLimited is an Exception, so a
        generic handler placed first would swallow it -- and with it the
        cooldown and the "one line, no traceback" behaviour."""
        for t in [n for n in ast.walk(node) if isinstance(n, ast.Try)]:
            names = [
                h.type.id if isinstance(h.type, ast.Name) else None
                for h in t.handlers
            ]
            if "MegaRateLimited" in names and "Exception" in names:
                assert names.index("MegaRateLimited") < names.index("Exception")
                return
        pytest.fail("no try block handles MegaRateLimited alongside Exception")
