"""Credit-role bucketing for ComicVine person credits.

ComicVine returns a creator's roles as ONE comma-joined string, so the parser
has to split it: matching the whole string with a first-match-wins chain
assigned each creator to exactly one bucket and silently dropped every other
credit they held (a "penciler, inker" lost the Inker credit outright).
"""

import pytest

from models.comicvine import parse_creator_roles


class _Creator:
    """Stand-in for Simyan's GenericCreator (``.name`` / ``.roles``)."""

    def __init__(self, name, roles):
        self.name = name
        self.roles = roles


def buckets(*pairs):
    return parse_creator_roles([_Creator(n, r) for n, r in pairs])


class TestMultiRoleStrings:

    def test_penciler_and_inker_fills_both(self):
        b = buckets(("Ben Dunn", "penciler, inker"))
        assert b['pencillers'] == ["Ben Dunn"]
        assert b['inkers'] == ["Ben Dunn"]

    def test_writer_and_cover_fills_both(self):
        b = buckets(("Chuck Dixon", "writer, cover"))
        assert b['writers'] == ["Chuck Dixon"]
        assert b['cover_artists'] == ["Chuck Dixon"]

    def test_three_roles(self):
        b = buckets(("Flint Henry", "penciler, inker, colorist"))
        assert b['pencillers'] == ["Flint Henry"]
        assert b['inkers'] == ["Flint Henry"]
        assert b['colorists'] == ["Flint Henry"]

    def test_whitespace_and_empty_tokens_tolerated(self):
        b = buckets(("Sam Parsons", "  colorist ,, "))
        assert b['colorists'] == ["Sam Parsons"]


class TestNewBuckets:

    def test_editor(self):
        assert buckets(("Tim Truman", "editor"))['editors'] == ["Tim Truman"]

    def test_editor_variants(self):
        b = buckets(("A", "assistant editor"), ("B", "editor-in-chief"))
        assert b['editors'] == ["A", "B"]

    def test_translator(self):
        assert buckets(("T", "translator"))['translators'] == ["T"]

    def test_bare_artist_is_a_penciller(self):
        # ComicVine uses a bare "artist" role constantly; it previously matched
        # neither "pencil" nor "illustrat" and produced nothing at all.
        assert buckets(("A", "artist"))['pencillers'] == ["A"]

    @pytest.mark.parametrize("role", ["painter", "breakdowns", "layouts"])
    def test_other_art_roles_are_pencillers(self, role):
        assert buckets(("A", role))['pencillers'] == ["A"]

    @pytest.mark.parametrize("role", ["finishes", "embellisher"])
    def test_finishing_roles_are_inkers(self, role):
        assert buckets(("A", role))['inkers'] == ["A"]

    @pytest.mark.parametrize("role", ["plot", "script", "story"])
    def test_writing_roles(self, role):
        assert buckets(("A", role))['writers'] == ["A"]

    def test_british_spelling(self):
        assert buckets(("A", "colourist"))['colorists'] == ["A"]


class TestOrderingGuards:
    """Matcher order stops compound roles landing in the wrong bucket."""

    def test_cover_artist_is_not_a_penciller(self):
        b = buckets(("A", "cover artist"))
        assert b['cover_artists'] == ["A"]
        assert b['pencillers'] == []

    def test_color_artist_is_not_a_penciller(self):
        b = buckets(("A", "color artist"))
        assert b['colorists'] == ["A"]
        assert b['pencillers'] == []

    def test_inker_is_not_a_penciller(self):
        b = buckets(("A", "inker"))
        assert b['inkers'] == ["A"]
        assert b['pencillers'] == []


class TestHousekeeping:

    def test_duplicates_deduped_within_a_bucket(self):
        b = buckets(("A", "writer"), ("A", "writer, script"))
        assert b['writers'] == ["A"]

    def test_order_preserved(self):
        b = buckets(("First", "writer"), ("Second", "writer"))
        assert b['writers'] == ["First", "Second"]

    def test_unknown_role_dropped(self):
        b = buckets(("A", "journalist"))
        assert all(v == [] for v in b.values())

    def test_every_bucket_present_even_when_empty(self):
        b = parse_creator_roles([])
        assert set(b) == {
            'writers', 'pencillers', 'inkers', 'colorists', 'letterers',
            'cover_artists', 'editors', 'translators',
        }
        assert all(v == [] for v in b.values())

    @pytest.mark.parametrize("creators", [None, [], ()])
    def test_no_creators(self, creators):
        assert all(v == [] for v in parse_creator_roles(creators).values())

    def test_missing_or_none_roles_are_safe(self):
        b = parse_creator_roles([_Creator("A", None), _Creator("B", "")])
        assert all(v == [] for v in b.values())

    def test_nameless_credit_skipped(self):
        assert parse_creator_roles([{"role": "writer"}])['writers'] == []

    def test_dict_credits_from_the_local_dump(self):
        # The SQLite dump stores person_credits as JSON with a "role" key.
        b = parse_creator_roles([{"name": "Bob Kane", "role": "writer, penciler"}])
        assert b['writers'] == ["Bob Kane"]
        assert b['pencillers'] == ["Bob Kane"]
