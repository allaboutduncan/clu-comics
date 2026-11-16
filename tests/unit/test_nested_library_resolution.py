"""A path belongs to the most specific library that contains it.

Libraries can nest (a library root inside another). Every place that maps a
path to "its" library must pick the longest containing root on a separator
boundary, whatever order the libraries are listed in; taking the first match
let an outer library listed first shadow every grant on the inner one.
"""
import os
from unittest.mock import patch

import pytest

from core.auth import _containing_library, _level_for

OUTER = os.path.normpath("/lib")
INNER = os.path.normpath("/lib/Inner")
OTHER = os.path.normpath("/lib/Inner-Two")

OUTER_LIB = {"id": 1, "path": "/lib"}
INNER_LIB = {"id": 2, "path": "/lib/Inner"}


def _p(*parts):
    return os.path.normpath(os.path.join(*parts))


ORDERS = [
    pytest.param([OUTER_LIB, INNER_LIB], id="outer-listed-first"),
    pytest.param([INNER_LIB, OUTER_LIB], id="inner-listed-first"),
]


class TestContainingLibrary:
    @pytest.mark.parametrize("libraries", ORDERS)
    def test_inner_path_maps_to_inner_library(self, libraries):
        assert _containing_library(_p(INNER, "X", "a.cbz"), libraries) == (INNER, 2)

    @pytest.mark.parametrize("libraries", ORDERS)
    def test_inner_root_itself_maps_to_inner(self, libraries):
        assert _containing_library(INNER, libraries) == (INNER, 2)

    @pytest.mark.parametrize("libraries", ORDERS)
    def test_outer_path_maps_to_outer_library(self, libraries):
        assert _containing_library(_p(OUTER, "Other", "a.cbz"), libraries) == (OUTER, 1)

    @pytest.mark.parametrize("libraries", ORDERS)
    def test_separator_bounded(self, libraries):
        # /lib/Inner-Two is not inside /lib/Inner: it is the outer library's.
        assert _containing_library(_p(OTHER, "a.cbz"), libraries) == (OUTER, 1)

    def test_outside_every_library(self):
        assert _containing_library(os.path.normpath("/elsewhere/a.cbz"),
                                   [OUTER_LIB, INNER_LIB]) is None


class TestLevelForNestedLibraries:
    @pytest.mark.parametrize("libraries", ORDERS)
    def test_grant_on_inner_library_is_honoured(self, libraries):
        # The user is granted only the inner library, with a folder grant in it.
        grants = [_p(INNER, "Marvel")]
        assert _level_for(_p(INNER, "Marvel", "x.cbz"), libraries, {2}, grants) == "full"
        assert _level_for(INNER, libraries, {2}, grants) == "traverse"

    @pytest.mark.parametrize("libraries", ORDERS)
    def test_inner_sibling_not_granted(self, libraries):
        grants = [_p(INNER, "Marvel")]
        assert _level_for(_p(INNER, "DC", "x.cbz"), libraries, {2}, grants) == "none"

    @pytest.mark.parametrize("libraries", ORDERS)
    def test_outer_grant_does_not_open_inner_library(self, libraries):
        # A whole-outer-library grant: the inner library is its own library and
        # needs its own grant.
        grants = [OUTER]
        assert _level_for(_p(OUTER, "Other", "x.cbz"), libraries, {1}, grants) == "full"
        assert _level_for(_p(INNER, "Marvel", "x.cbz"), libraries, {1}, grants) == "none"

    @pytest.mark.parametrize("libraries", ORDERS)
    def test_inner_library_not_granted_is_denied(self, libraries):
        # Granted the outer library and a folder inside the inner one's path:
        # the inner library itself is not granted, so the path is denied.
        grants = [_p(INNER, "Marvel")]
        assert _level_for(_p(INNER, "Marvel", "x.cbz"), libraries, {1}, grants) == "none"


class TestAccessibleFolderPrefixesNested:
    @pytest.mark.parametrize("libraries", ORDERS)
    def test_grant_counted_against_its_own_library(self, libraries):
        from core import auth

        grants = [_p(INNER, "Marvel"), _p(OUTER, "Other")]
        user = {"id": 7, "role": "reader"}
        with patch.object(auth, "is_login_required", return_value=True), \
                patch.object(auth, "_load_scope", return_value=(libraries, {2}, grants)):
            assert auth.accessible_folder_prefixes(user) == [_p(INNER, "Marvel")]


class TestHelpersLibraryNested:
    @pytest.mark.parametrize("libraries", ORDERS)
    def test_get_library_for_path_picks_inner(self, libraries):
        from helpers.library import get_library_for_path

        with patch("core.database.get_libraries", return_value=libraries):
            assert get_library_for_path(_p(INNER, "a.cbz"))["id"] == 2
            assert get_library_for_path(_p(OUTER, "a.cbz"))["id"] == 1
            assert get_library_for_path(_p(OTHER, "a.cbz"))["id"] == 1

    @pytest.mark.parametrize("roots", [
        pytest.param(["/lib", "/lib/Inner"], id="outer-first"),
        pytest.param(["/lib/Inner", "/lib"], id="inner-first"),
    ])
    def test_containing_root_picks_inner(self, roots):
        from helpers.library import _containing_root

        assert _containing_root(_p(INNER, "a.cbz"), roots) == INNER
        assert _containing_root(_p(OTHER, "a.cbz"), roots) == OUTER

    @pytest.mark.parametrize("order", ["outer-first", "inner-first"])
    def test_library_root_for_picks_inner(self, tmp_path, order):
        from helpers.library import library_root_for

        outer = tmp_path / "lib"
        inner = outer / "Inner"
        (inner / "X").mkdir(parents=True)
        roots = [str(outer), str(inner)]
        if order == "inner-first":
            roots.reverse()
        with patch("helpers.library.get_library_roots", return_value=roots):
            assert library_root_for(str(inner / "X")) == str(inner)
            assert library_root_for(str(outer)) == str(outer)
