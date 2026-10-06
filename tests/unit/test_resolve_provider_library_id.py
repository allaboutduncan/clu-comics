"""helpers.library.resolve_provider_library_id -- which library's provider
settings apply to a metadata fetch that did not name one.

The collection grid's "Fetch Metadata" sent no library_id, so
/api/search-metadata fell back to a hardcoded order with Metron first and ran
Metron even when the library had it disabled and ranked third.
"""
import os
from unittest.mock import patch

from helpers.library import resolve_provider_library_id

LIB_ROOT = os.path.normpath("/data/comics")
LIBS = [{"id": 7, "path": LIB_ROOT}]
COMIC = os.path.join(LIB_ROOT, "Batman", "Batman 001 (2020).cbz")


@patch("core.database.get_library_providers")
@patch("core.database.get_libraries", return_value=LIBS)
class TestResolveProviderLibraryId:

    def test_explicit_id_wins(self, mock_libs, mock_providers):
        assert resolve_provider_library_id(3, COMIC) == 3
        mock_libs.assert_not_called()

    def test_path_inside_configured_library(self, mock_libs, mock_providers):
        mock_providers.return_value = [{"provider_type": "comicvine", "enabled": True}]
        assert resolve_provider_library_id(None, COMIC) == 7
        mock_providers.assert_called_once_with(7)

    def test_unconfigured_library_keeps_the_global_fallback(self, mock_libs, mock_providers):
        """No provider rows means get_library_providers returns [], which would
        make the cascade try nothing at all."""
        mock_providers.return_value = []
        assert resolve_provider_library_id(None, COMIC) is None

    def test_path_outside_every_library(self, mock_libs, mock_providers):
        assert resolve_provider_library_id(None, os.path.normpath("/downloads/x.cbz")) is None
        mock_providers.assert_not_called()

    def test_sibling_prefix_is_not_inside(self, mock_libs, mock_providers):
        mock_providers.return_value = [{"provider_type": "metron", "enabled": True}]
        other = os.path.normpath("/data/comics-old/x.cbz")
        assert resolve_provider_library_id(None, other) is None

    def test_no_path(self, mock_libs, mock_providers):
        assert resolve_provider_library_id(None, None) is None
