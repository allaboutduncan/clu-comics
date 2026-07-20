"""The issue-pad-width preference round-trips through the DB.

The config route (`/api/config/file-processing`) persists `issue_pad_width` via
`set_user_preference`, and the renamer reads it via `load_issue_pad_width`. This
verifies that write->read contract against a real SQLite database.
"""
import pytest

from core.database import set_user_preference
from cbz_ops.rename import load_issue_pad_width


class TestIssuePadWidthPref:

    @pytest.mark.parametrize("stored,expected", [
        ("none", 0),
        ("3", 3),
        ("4", 4),
    ])
    def test_round_trip(self, db_connection, stored, expected):
        set_user_preference("issue_pad_width", stored, category="file_processing")
        assert load_issue_pad_width() == expected

    def test_default_when_unset(self, db_connection):
        # Nothing written -> renamer falls back to the historical width of 3.
        assert load_issue_pad_width() == 3

    def test_unexpected_value_falls_back(self, db_connection):
        # A value outside the allowlist should not crash the renamer.
        set_user_preference("issue_pad_width", "bogus", category="file_processing")
        assert load_issue_pad_width() == 3
