"""Tests for Komga path mapping and filename extraction -- pure, no HTTP or DB."""
import pytest

from models.komga import (
    komga_file_name,
    map_komga_path,
    map_komga_path_multi,
)


# ---------------------------------------------------------------------------
# komga_file_name
# ---------------------------------------------------------------------------

class TestKomgaFileName:

    def test_admin_posix_path(self):
        """Admin users get the full server path; take the last segment."""
        assert komga_file_name("/comics/Marvel/Spider-Man 001.cbz") == "Spider-Man 001.cbz"

    def test_non_admin_bare_filename(self):
        """Non-admins get a bare filename already (BookDto.restrictUrl)."""
        assert komga_file_name("Spider-Man 001.cbz") == "Spider-Man 001.cbz"

    def test_windows_path(self):
        """A Komga host on Windows must not defeat a CLU running on POSIX."""
        assert komga_file_name("C:\\comics\\Marvel\\Spider-Man 001.cbz") == "Spider-Man 001.cbz"

    def test_empty_url(self):
        assert komga_file_name("") == ""

    def test_none_url(self):
        assert komga_file_name(None) == ""

    def test_spaces_preserved(self):
        """Komga does not URL-encode the path; spaces stay as-is."""
        assert komga_file_name("/comics/The Amazing Spider-Man 001.cbz") == \
            "The Amazing Spider-Man 001.cbz"


# ---------------------------------------------------------------------------
# map_komga_path
# ---------------------------------------------------------------------------

class TestMapKomgaPath:

    def test_simple_prefix_swap(self):
        assert map_komga_path("/comics/Marvel/X.cbz", "/comics", "/data") == "/data/Marvel/X.cbz"

    def test_trailing_slash_on_prefix(self):
        assert map_komga_path("/comics/Marvel/X.cbz", "/comics/", "/data/") == "/data/Marvel/X.cbz"

    def test_no_match_returns_input_unchanged(self):
        assert map_komga_path("/media/X.cbz", "/comics", "/data") == "/media/X.cbz"

    def test_empty_komga_prefix_returns_input(self):
        assert map_komga_path("/comics/X.cbz", "", "/data") == "/comics/X.cbz"

    def test_empty_clu_prefix_returns_input(self):
        assert map_komga_path("/comics/X.cbz", "/comics", "") == "/comics/X.cbz"

    def test_backslashes_normalized(self):
        assert map_komga_path("\\comics\\Marvel\\X.cbz", "/comics", "/data") == "/data/Marvel/X.cbz"

    def test_sibling_directory_must_not_match(self):
        """Regression: prefix '/comics' must not swallow '/comics-archive'.

        A bare startswith() rewrote this to '/data-archive/X.cbz' -- a path
        belonging to no library at all.
        """
        assert map_komga_path("/comics-archive/X.cbz", "/comics", "/data") == "/comics-archive/X.cbz"

    def test_exact_prefix_match(self):
        assert map_komga_path("/comics", "/comics", "/data") == "/data"


# ---------------------------------------------------------------------------
# map_komga_path_multi
# ---------------------------------------------------------------------------

class TestMapKomgaPathMulti:

    def test_first_matching_mapping_wins(self):
        mappings = [
            {"komga_prefix": "/manga", "clu_prefix": "/data/manga"},
            {"komga_prefix": "/comics", "clu_prefix": "/data/comics"},
        ]
        assert map_komga_path_multi("/comics/X.cbz", mappings) == "/data/comics/X.cbz"

    def test_longest_prefix_first_when_caller_sorts(self):
        """map_komga_path_multi relies on the CALLER sorting by prefix length
        descending (run_komga_sync does), so the nested mapping wins."""
        mappings = [
            {"komga_prefix": "/comics/marvel", "clu_prefix": "/data/marvel"},
            {"komga_prefix": "/comics", "clu_prefix": "/data/other"},
        ]
        assert map_komga_path_multi("/comics/marvel/X.cbz", mappings) == "/data/marvel/X.cbz"

    def test_no_mappings_returns_input(self):
        assert map_komga_path_multi("/comics/X.cbz", []) == "/comics/X.cbz"

    def test_no_matching_mapping_returns_input(self):
        mappings = [{"komga_prefix": "/manga", "clu_prefix": "/data/manga"}]
        assert map_komga_path_multi("/comics/X.cbz", mappings) == "/comics/X.cbz"
