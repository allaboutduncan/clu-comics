"""core.static_assets.asset_version -- the content-hash token behind asset().

A token that changes without the bytes changing re-downloads an asset for
nothing; one that does not change with the bytes serves stale JS for a year
(the versioned URL is immutable). Both directions are pinned here.
"""
import builtins
import os
import re

import pytest

from core import static_assets
from core.static_assets import asset_version


@pytest.fixture(autouse=True)
def _fresh_memo():
    static_assets._cache.clear()
    yield
    static_assets._cache.clear()


@pytest.fixture
def static(tmp_path):
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "app.js").write_text("console.log(1);\n")
    return tmp_path


def test_same_bytes_same_token(static):
    first = asset_version(str(static), "js/app.js")
    assert first == asset_version(str(static), "js/app.js")
    assert re.fullmatch(r"[0-9a-f]{10}", first)


def test_new_bytes_new_token(static):
    path = static / "js" / "app.js"
    before = asset_version(str(static), "js/app.js")
    path.write_text("console.log(2);\n")
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert asset_version(str(static), "js/app.js") != before


def test_touching_without_changing_bytes_keeps_the_token(static):
    path = static / "js" / "app.js"
    before = asset_version(str(static), "js/app.js")
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert asset_version(str(static), "js/app.js") == before


def test_missing_file_is_none(static):
    assert asset_version(str(static), "js/nope.js") is None


def test_escaping_the_folder_is_none(static):
    (static.parent / "outside.js").write_text("x")
    assert asset_version(str(static), "../outside.js") is None


def test_file_is_read_once_while_unchanged(static, monkeypatch):
    real_open = builtins.open
    reads = []

    def counting_open(file, *args, **kwargs):
        if str(file).endswith("app.js"):
            reads.append(file)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    for _ in range(5):
        asset_version(str(static), "js/app.js")
    assert len(reads) == 1
