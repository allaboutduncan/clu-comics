"""The two issue-number keys, side by side.

``core.database.issue_identity_key`` (reading-list matching) and
``helpers.collection.issue_match_key`` (collection and TARGET matching) were
both named ``normalize_issue_number``. They are different functions with
different answers, and a caller that imported the wrong one compared values
that could never be equal. They are renamed, not merged: merging changes which
files an issue claims, which needs a cache purge and a forced re-match.

This table is the record of where they agree and where they do not. A row
changing is a behaviour change in a matcher, not a tidy-up.
"""
import pytest

from core.database import issue_identity_key
from helpers.collection import issue_match_key

# raw, identity key (reading lists), match key (collection / TARGET)
AGREE = [
    ("1", "1"),
    ("001", "1"),
    ("0", "0"),
    ("000", "0"),
    ("-1", "-1"),
    ("-001", "-1"),
    ("12.5", "12.5"),
]

DIFFER = [
    # Empty: "no number" for a reading-list entry, issue 0 for the collection.
    ("", "", "0"),
    (None, "", "0"),
    # Case: the identity key lowercases, the match key leaves that to callers.
    ("50.LR", "50.lr", "50.LR"),
    ("1.MU", "1.mu", "1.MU"),
    # A leading "#" is dropped only by the identity key.
    ("#5", "5", "#5"),
    # A trailing "." is dropped only by the identity key.
    ("5.", "5", "5."),
    # Unicode minus and en dash are signs only to the match key.
    ("−1", "−1", "-1"),
    ("–1", "–1", "-1"),
]


@pytest.mark.parametrize("raw, expected", AGREE)
def test_where_they_agree(raw, expected):
    assert issue_identity_key(raw) == expected
    assert issue_match_key(raw) == expected


@pytest.mark.parametrize("raw, identity, match", DIFFER)
def test_where_they_differ(raw, identity, match):
    assert issue_identity_key(raw) == identity
    assert issue_match_key(raw) == match
    assert identity != match


def test_the_sign_is_part_of_the_number_in_both():
    for key in (issue_identity_key, issue_match_key):
        assert key("-1") != key("1")


def test_the_shared_name_is_gone():
    """One name for two functions is what this replaces."""
    import core.database
    import helpers.collection

    assert not hasattr(core.database, "normalize_issue_number")
    assert not hasattr(helpers.collection, "normalize_issue_number")
