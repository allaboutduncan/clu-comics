"""The classification table is the page's honesty, so it is pinned here.

The message strings below are taken verbatim from real logs (issue: corrupt
Wizard Magazine scans), which is the point -- a classifier tested only against
strings someone invented while writing it proves nothing about the errors users
actually hit.
"""

import pytest

from core.problem_files import (
    ACTION_CONFIG,
    ACTION_INSPECT,
    ACTION_REBUILD,
    ACTION_REPLACE,
    CLASS_CACHE_WRITE,
    CLASS_CORRUPT_ENTRIES,
    CLASS_NO_PAGES,
    classify,
)


class TestRealWorldLogLines:
    """Every one of these came out of a real CLU log."""

    @pytest.mark.parametrize(
        "error_class,message",
        [
            ("error", "Error -3 while decompressing data: invalid stored block lengths"),
            ("error", "Error -3 while decompressing data: invalid distance code"),
        ],
    )
    def test_zlib_damage_is_not_repairable(self, error_class, message):
        result = classify(error_class, message)
        assert result["action"] == ACTION_REPLACE
        assert result["repairable"] is False
        assert result["healthy_file"] is False

    def test_bad_crc_advises_replacement_not_rebuild(self):
        result = classify(
            "BadZipFile",
            "Bad CRC-32 for file 'Wizard Magazine 151 (2004)/tenchi_wizard151_p000.jpg'",
        )
        assert result["action"] == ACTION_REPLACE
        # The whole reason this table exists: rebuild aborts on a bad CRC and
        # cannot recreate the lost bytes, so it must never be the advice.
        assert result["repairable"] is False
        assert "rebuild" in result["advice"].lower()

    def test_bad_magic_number_is_the_repairable_case(self):
        result = classify("BadZipFile", "Bad magic number for file header")
        assert result["action"] == ACTION_REBUILD
        assert result["repairable"] is True

    def test_not_a_zip_file_is_the_repairable_case(self):
        result = classify("BadZipFile", "File is not a zip file")
        assert result["action"] == ACTION_REBUILD
        assert result["repairable"] is True

    def test_header_mismatch_with_binary_already_stripped(self):
        # helpers.archive_error_detail collapses the blob to <binary> before
        # this ever reaches the classifier.
        result = classify(
            "BadZipFile",
            "File name in directory 'Wizard #157 [ocd redfox]/Wizard_157_000.jpg' "
            "and header <binary> differ",
        )
        assert result["action"] == ACTION_REPLACE
        assert result["repairable"] is False

    def test_broken_image_stream(self):
        result = classify("OSError", "broken data stream when reading image file")
        assert result["action"] == ACTION_REPLACE


class TestNonExceptionClasses:
    def test_no_pages_is_inspect_not_replace(self):
        result = classify(CLASS_NO_PAGES, "Archive opened but contains no page images")
        assert result["action"] == ACTION_INSPECT
        assert result["repairable"] is False

    def test_cache_write_failure_marks_the_file_healthy(self):
        """The guard that stops the page offering to delete a fine comic."""
        result = classify(CLASS_CACHE_WRITE, "Could not write the thumbnail to /cache/x")
        assert result["healthy_file"] is True
        assert result["action"] == ACTION_CONFIG
        assert "not delete" in result["advice"].lower()

    def test_corrupt_entries_warns_about_blank_pages(self):
        result = classify(CLASS_CORRUPT_ENTRIES, "3 archive entries failed CRC")
        assert result["action"] == ACTION_REPLACE


class TestEnvironmentErrors:
    def test_permission_denied_is_a_config_problem(self):
        result = classify("PermissionError", "[Errno 13] Permission denied: '/data/x.cbz'")
        assert result["action"] == ACTION_CONFIG
        assert result["healthy_file"] is True

    def test_missing_file_is_a_config_problem(self):
        result = classify(
            "FileNotFoundError", "[Errno 2] No such file or directory: '/data/x.cbz'"
        )
        assert result["action"] == ACTION_CONFIG


class TestFallback:
    def test_unknown_error_claims_no_repairability(self):
        result = classify("SomethingNovelError", "a thing nobody has seen before")
        assert result["repairable"] is False
        assert result["healthy_file"] is False
        assert result["cause"]
        assert result["advice"]

    def test_empty_input_does_not_raise(self):
        result = classify(None, None)
        assert result["repairable"] is False
        assert result["cause"]

    def test_every_result_has_the_full_shape(self):
        for klass, message in [
            ("BadZipFile", "Bad CRC-32 for file 'x.jpg'"),
            (CLASS_CACHE_WRITE, ""),
            (None, None),
        ]:
            result = classify(klass, message)
            assert set(result) == {
                "cause",
                "advice",
                "action",
                "repairable",
                "healthy_file",
            }
