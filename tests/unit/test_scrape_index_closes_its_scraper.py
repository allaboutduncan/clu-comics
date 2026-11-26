"""The scrape index closes the cloudscraper session it opens for each page.

``_scrape_url_to_index`` builds one session per page and used to drop it on
return. A dropped cloudscraper session is not reclaimed, even by
``gc.collect()``: it keeps its socket and about a megabyte for the life of the
process (measured: 40 dropped after one request each left 40 connections open
and +41 MB; 40 closed left none and +3 MB). The function runs once per page
from three thread pools, and the nightly sweep calls it for every series, so a
reported worker went from 32 to 446 open fds across one sweep and grew about
1.3 GB a day (#842).
"""
import ast
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from models import getcomics

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _response(status, text="<html></html>"):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.headers = {}
    return resp


@pytest.fixture
def session(db_connection):
    getcomics._ensure_urls_table()
    made = MagicMock()
    with patch.object(getcomics.cloudscraper, "create_scraper", return_value=made):
        yield made


class TestScraperIsClosedOnEveryPath:
    @pytest.mark.parametrize("status", [200, 304, 404, 503])
    def test_closed_whatever_the_page_answers(self, session, status):
        session.get.return_value = _response(status)
        getcomics._scrape_url_to_index("https://getcomics.org/dc/x-1/")
        session.close.assert_called_once()

    def test_closed_when_the_fetch_raises(self, session):
        session.get.side_effect = ConnectionError("reset")
        assert getcomics._scrape_url_to_index("https://getcomics.org/dc/x-1/") == []
        session.close.assert_called_once()

    def test_a_failing_close_does_not_lose_the_result(self, session):
        session.get.return_value = _response(304)
        session.close.side_effect = RuntimeError("already closed")
        assert getcomics._scrape_url_to_index("https://getcomics.org/dc/x-1/") is None


def test_no_other_function_builds_an_unowned_scraper():
    """A new per-call ``create_scraper()`` in this module is the same leak
    again. Only the shared-session factory and the indexer (which closes its
    own, above) may build one."""
    tree = ast.parse((ROOT / "models" / "getcomics.py").read_text(encoding="utf-8"))
    builders = set()
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "create_scraper"):
                builders.add(func.name)
    assert builders == {"_make_scraper", "_scrape_url_to_index"}
