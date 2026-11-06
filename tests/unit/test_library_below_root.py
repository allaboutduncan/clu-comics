"""helpers.library.is_below_library_root -- the auto-metadata depth gate.

It used to require two levels below the root (<root>/Publisher/Series), so on
a flat library (/data/Series (2025)/) the auto-fetchers skipped every file
after logging "Auto-fetching metadata for:" and nothing said why.
"""
import os
from unittest.mock import patch

import pytest

from helpers.library import is_below_library_root


def _p(*parts):
    return os.path.join(os.sep, *parts)


@pytest.fixture
def roots():
    def _set(*paths):
        return patch("helpers.library.get_library_roots", return_value=list(paths))
    return _set


class TestIsBelowLibraryRoot:

    def test_root_itself_is_refused(self, roots):
        with roots(_p("data")):
            assert not is_below_library_root(_p("data"))
            assert not is_below_library_root(_p("data") + os.sep)

    def test_flat_series_folder_is_accepted(self, roots):
        with roots(_p("data")):
            assert is_below_library_root(_p("data", "Absolute Green Lantern (2025)"))

    def test_publisher_series_layout_is_accepted(self, roots):
        with roots(_p("data")):
            assert is_below_library_root(_p("data", "DC Comics", "Batman (2016)"))

    def test_sibling_with_shared_prefix_is_outside(self, roots):
        with roots(_p("data")):
            assert not is_below_library_root(_p("data2", "Series"))

    def test_nested_library_measured_from_its_own_root(self, roots):
        with roots(_p("data"), _p("data", "manga")):
            assert not is_below_library_root(_p("data", "manga"))
            assert is_below_library_root(_p("data", "manga", "Berserk"))

    def test_path_outside_every_library(self, roots):
        with roots(_p("data"), _p("comics")):
            assert not is_below_library_root(_p("downloads", "processed", "X"))

    def test_empty_path(self, roots):
        with roots(_p("data")):
            assert not is_below_library_root("")
            assert not is_below_library_root(None)
