"""helpers.paging.per_page_arg: URL, then cookie, then default."""

from helpers.paging import per_page_arg

SIZES = (24, 50, 100)
COOKIE = "clu_per_page_test"


def _pick(url=None, cookie=None):
    args = {} if url is None else {"per_page": url}
    cookies = {} if cookie is None else {COOKIE: cookie}
    return per_page_arg(args, cookies, COOKIE, SIZES, 50)


def test_url_wins_over_cookie():
    assert _pick("100", "24") == 100


def test_cookie_used_when_url_is_missing():
    assert _pick(None, "24") == 24


def test_junk_or_unoffered_url_falls_through_to_cookie():
    assert _pick("abc", "24") == 24
    assert _pick("7", "24") == 24


def test_default_when_neither_is_usable():
    assert _pick() == 50
    assert _pick("abc", "junk") == 50
    assert _pick(None, "7") == 50
