"""Tests for resolve_komga_book_path -- file index lookup injected, no DB."""
import pytest
from unittest.mock import patch

from models.komga import extract_book_info, resolve_komga_book_path


def _info(url="/comics/Marvel/Spider-Man 001.cbz", name="Spider-Man 001", page=0):
    """Build an info dict the way extract_book_info would, from a Komga book."""
    return extract_book_info({
        "id": "b1",
        "url": url,
        "name": name,
        "media": {"pagesCount": 24},
        "readProgress": {"page": page, "completed": False},
    })


def _lookup(*paths):
    """A find_file_index_paths_by_name stub returning fixed paths."""
    return lambda filename, **kw: list(paths)


MAPPINGS = [{"komga_prefix": "/comics", "clu_prefix": "/data"}]


# ---------------------------------------------------------------------------
# Mapping branch
# ---------------------------------------------------------------------------

class TestMappingBranch:

    def test_mapped_path_that_exists_wins(self):
        with patch("models.komga.os.path.exists", return_value=True):
            path, reason = resolve_komga_book_path(
                _info(), MAPPINGS, lookup=_lookup()
            )
        assert path == "/data/Marvel/Spider-Man 001.cbz"
        assert reason == "mapping"

    def test_mapped_path_that_does_not_exist_falls_back_to_file_index(self):
        with patch("models.komga.os.path.exists", return_value=False):
            path, reason = resolve_komga_book_path(
                _info(), MAPPINGS,
                lookup=_lookup("/data/Marvel/Spider-Man 001.cbz"),
            )
        assert path == "/data/Marvel/Spider-Man 001.cbz"
        assert reason == "file_index"

    def test_unmapped_path_that_exists_is_used(self):
        """Identical bind mounts in both containers need no prefixes at all."""
        with patch("models.komga.os.path.exists", return_value=True):
            path, reason = resolve_komga_book_path(
                _info(url="/data/Marvel/Spider-Man 001.cbz"), [], lookup=_lookup()
            )
        assert path == "/data/Marvel/Spider-Man 001.cbz"
        assert reason == "mapping"


# ---------------------------------------------------------------------------
# File index fallback -- the regression this whole fix is about
# ---------------------------------------------------------------------------

class TestFileIndexFallback:

    def test_looks_up_filename_with_extension_not_the_stem(self):
        """The bug: Komga's `name` is the stem ('Spider-Man 001') but
        file_index.name stores the extension too, so the lookup never matched.
        The filename must come from `url`, which always carries the extension.
        """
        seen = []

        def lookup(filename, **kw):
            seen.append(filename)
            return ["/data/Marvel/Spider-Man 001.cbz"]

        with patch("models.komga.os.path.exists", return_value=False):
            path, reason = resolve_komga_book_path(_info(), [], lookup=lookup)

        assert seen == ["Spider-Man 001.cbz"]
        assert seen != ["Spider-Man 001"]
        assert path == "/data/Marvel/Spider-Man 001.cbz"
        assert reason == "file_index"

    def test_non_admin_bare_url_skips_mapping_entirely(self):
        """Non-admins get a bare filename. There is nothing to map, and
        os.path.exists() would resolve it against the working directory.
        """
        with patch("models.komga.os.path.exists") as mock_exists:
            path, reason = resolve_komga_book_path(
                _info(url="Spider-Man 001.cbz"), MAPPINGS,
                lookup=_lookup("/data/Marvel/Spider-Man 001.cbz"),
            )
        mock_exists.assert_not_called()
        assert path == "/data/Marvel/Spider-Man 001.cbz"
        assert reason == "file_index"

    def test_no_exists_check_on_file_index_result(self):
        """A path out of CLU's own index is a CLU path by construction."""
        with patch("models.komga.os.path.exists", return_value=False):
            path, reason = resolve_komga_book_path(
                _info(url="Spider-Man 001.cbz"), [],
                lookup=_lookup("/data/Marvel/Spider-Man 001.cbz"),
            )
        assert path == "/data/Marvel/Spider-Man 001.cbz"
        assert reason == "file_index"

    def test_no_candidates_is_no_match(self):
        with patch("models.komga.os.path.exists", return_value=False):
            path, reason = resolve_komga_book_path(_info(), [], lookup=_lookup())
        assert path is None
        assert reason == "no_match"

    def test_empty_url_is_no_match(self):
        path, reason = resolve_komga_book_path(
            _info(url=""), MAPPINGS, lookup=_lookup("/data/X.cbz")
        )
        assert path is None
        assert reason == "no_match"


# ---------------------------------------------------------------------------
# Ambiguity -- never guess which comic to mark read
# ---------------------------------------------------------------------------

class TestAmbiguity:

    def test_two_candidates_without_parent_info_is_ambiguous(self):
        """Non-admin url has no directory, so nothing can disambiguate."""
        path, reason = resolve_komga_book_path(
            _info(url="Spider-Man 001.cbz"), [],
            lookup=_lookup(
                "/data/Marvel/Spider-Man 001.cbz",
                "/data/Backups/Spider-Man 001.cbz",
            ),
        )
        assert path is None
        assert reason == "ambiguous"

    def test_parent_directory_disambiguates(self):
        with patch("models.komga.os.path.exists", return_value=False):
            path, reason = resolve_komga_book_path(
                _info(url="/comics/Backups/Spider-Man 001.cbz"), [],
                lookup=_lookup(
                    "/data/Marvel/Spider-Man 001.cbz",
                    "/data/Backups/Spider-Man 001.cbz",
                ),
            )
        assert path == "/data/Backups/Spider-Man 001.cbz"
        assert reason == "file_index"

    def test_parent_matching_two_candidates_stays_ambiguous(self):
        """Same filename under same-named dirs in two libraries: give up."""
        with patch("models.komga.os.path.exists", return_value=False):
            path, reason = resolve_komga_book_path(
                _info(url="/comics/Marvel/Spider-Man 001.cbz"), [],
                lookup=_lookup(
                    "/data/libA/Marvel/Spider-Man 001.cbz",
                    "/data/libB/Marvel/Spider-Man 001.cbz",
                ),
            )
        assert path is None
        assert reason == "ambiguous"

    def test_parent_matching_no_candidates_stays_ambiguous(self):
        with patch("models.komga.os.path.exists", return_value=False):
            path, reason = resolve_komga_book_path(
                _info(url="/comics/Elsewhere/Spider-Man 001.cbz"), [],
                lookup=_lookup(
                    "/data/Marvel/Spider-Man 001.cbz",
                    "/data/Backups/Spider-Man 001.cbz",
                ),
            )
        assert path is None
        assert reason == "ambiguous"
