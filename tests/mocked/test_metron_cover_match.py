"""Matching a file to a Metron issue by its cover hash (Metron PR #683).

``find_issues_by_cover`` is the request; ``match_issue_by_cover`` is the policy
every tagging path shares: a close hash is evidence, and only a candidate the
filename corroborates is accepted.
"""
from datetime import date
from unittest.mock import MagicMock, patch

from mokkari.exceptions import ApiError

HASH = "c585eb18bf1e5423"
ONE_OFF = "c585eb18bf1e5422"
FOUR_OFF = "c585eb18bf1e542c"
FIVE_OFF = "c585eb18bf1e546c"
TEN_OFF = "c585eb18bf1e57dc"
TWELVE_OFF = "c585eb18bf1eabdc"
FAR = "3a7a14e740e1abdc"


def _client(library):
    """A real ``MetronClient`` over ``library``, so the call policy runs."""
    from models.metron import MetronClient

    return MetronClient(library)


def _row(id, number, cover_hash, series="Batman", year_began=2016,
         cover_date=(2020, 1, 15), series_id=100):
    """One row of an issue list, as mokkari's BaseIssue carries it."""
    row = MagicMock()
    row.id = id
    row.number = number
    row.cover_hash = cover_hash
    row.cover_date = date(*cover_date)
    row.series.id = series_id
    row.series.name = series
    row.series.year_began = year_began
    return row


class TestFindIssuesByCover:

    def test_sends_the_hash_and_distance(self):
        from models.metron import find_issues_by_cover

        library = MagicMock()
        library.issues_list.return_value = []
        assert find_issues_by_cover(_client(library), HASH) == []
        library.issues_list.assert_called_once_with(
            {"cover_hash_near": HASH, "cover_hash_max_distance": 10})

    def test_cover_year_is_sent_only_when_given(self):
        from models.metron import find_issues_by_cover

        library = MagicMock()
        library.issues_list.return_value = []
        find_issues_by_cover(_client(library), HASH, 16, cover_year=2018)
        library.issues_list.assert_called_once_with(
            {"cover_hash_near": HASH, "cover_hash_max_distance": 16, "cover_year": 2018})

    def test_rows_are_shaped_and_ordered_closest_first(self):
        from models.metron import find_issues_by_cover

        library = MagicMock()
        library.issues_list.return_value = [_row(2, "2", FOUR_OFF), _row(1, "1", HASH)]
        found = find_issues_by_cover(_client(library), HASH)
        assert [(c["id"], c["distance"]) for c in found] == [(1, 0), (2, 4)]
        assert found[0] == {
            "id": 1, "number": "1", "series_id": 100, "series_name": "Batman",
            "series_year": 2016, "cover_year": 2020, "cover_hash": HASH, "distance": 0,
        }

    def test_a_server_that_ignores_the_filter_yields_no_matches(self):
        # django-filter drops a parameter it does not know and returns the
        # unfiltered list. Those rows must not be read as cover matches.
        from models.metron import find_issues_by_cover

        library = MagicMock()
        library.issues_list.return_value = [
            _row(1, "1", FAR), _row(2, "2", TWELVE_OFF),
            _row(3, "3", ""), _row(4, "4", "not-a-hash")]
        assert find_issues_by_cover(_client(library), HASH) == []

    def test_a_failed_request_is_none_not_empty(self):
        from models.metron import find_issues_by_cover

        library = MagicMock()
        library.issues_list.side_effect = ApiError("boom")
        assert find_issues_by_cover(_client(library), HASH) is None

    def test_no_client_or_no_hash_is_none(self):
        from models.metron import find_issues_by_cover

        assert find_issues_by_cover(None, HASH) is None
        assert find_issues_by_cover(_client(MagicMock()), "") is None


class TestMatchIssueByCover:

    def _match(self, rows_per_call, series="Batman", number="1", year=2020,
               hashes=(HASH,), enabled=True):
        from models import metron

        library = MagicMock()
        library.issues_list.side_effect = list(rows_per_call)
        with patch("core.cover_hash.cover_hashes", return_value=list(hashes)), \
             patch("core.settings.get_bool", return_value=enabled):
            hit = metron.match_issue_by_cover(
                _client(library), "/data/x/Batman 001 (2020).cbz", series, number, year)
        return hit, library

    def test_one_hit_the_filename_agrees_with(self):
        hit, library = self._match([[_row(7, "1", ONE_OFF)]])
        assert hit["id"] == 7
        assert library.issues_list.call_count == 1

    def test_a_collected_edition_sharing_the_cover_does_not_win(self):
        # The trade reuses the issue's cover exactly and sorts first. The
        # filename says #17, so only the issue is a match.
        hit, _ = self._match(
            [[_row(90, "4", HASH, series="Batman TPB"), _row(7, "17", ONE_OFF)]],
            number="17")
        assert hit["id"] == 7

    def test_a_number_that_contradicts_is_never_accepted(self):
        hit, _ = self._match([[_row(7, "2", HASH)]])
        assert hit is None

    def test_number_alone_is_not_enough(self):
        # An unrelated cover within 10 bits that happens to share the number.
        hit, _ = self._match(
            [[_row(7, "1", FOUR_OFF, series="Saga", year_began=2012, cover_date=(2012, 3, 1))]])
        assert hit is None

    def test_number_and_year_carry_a_differently_named_series(self):
        hit, _ = self._match(
            [[_row(7, "1", HASH, series="Detective Comics", year_began=1937)]])
        assert hit["id"] == 7

    def test_number_and_year_do_not_carry_a_stranger_at_the_edge(self):
        # "Simon Dark" is not on Metron. An unrelated #1 from the year before,
        # 10 bits off, was accepted and its series written to the cvinfo.
        hit, library = self._match(
            [[_row(7, "1", TEN_OFF, series="Marvel Adventures The Avengers",
                   year_began=2006, cover_date=(2006, 7, 1))]],
            series="Simon Dark", number="001", year=2007)
        assert hit is None
        assert library.issues_list.call_count == 1

    def test_a_differently_named_series_needs_a_near_identical_cover(self):
        def other(cover_hash):
            return [[_row(7, "1", cover_hash, series="Detective Comics", year_began=1937)]]

        assert self._match(other(FOUR_OFF))[0]["id"] == 7
        assert self._match(other(FIVE_OFF))[0] is None

    def test_a_named_series_is_accepted_out_to_the_search_distance(self):
        hit, _ = self._match([[_row(7, "1", TEN_OFF)]])
        assert hit["id"] == 7

    def test_series_start_year_counts_as_the_year(self):
        hit, _ = self._match(
            [[_row(7, "1", HASH, series="Zzz", year_began=2020, cover_date=(2023, 1, 1))]])
        assert hit["id"] == 7

    def test_no_number_in_the_filename_needs_series_and_year(self):
        hit, _ = self._match([[_row(7, "1", HASH)]], number=None)
        assert hit["id"] == 7
        hit, _ = self._match([[_row(7, "1", HASH)]], number=None, year=None)
        assert hit is None

    def test_leading_zeros_do_not_matter_and_the_sign_does(self):
        hit, _ = self._match([[_row(7, "1", HASH)]], number="001")
        assert hit["id"] == 7
        hit, _ = self._match([[_row(7, "-1", HASH)]], number="1")
        assert hit is None

    def test_two_corroborated_candidates_is_no_answer(self):
        hit, _ = self._match(
            [[_row(7, "1", HASH), _row(8, "1", ONE_OFF, series_id=101)]])
        assert hit is None

    def test_nothing_near_retries_wider_within_the_cover_year(self):
        hit, library = self._match([[], [_row(7, "1", TWELVE_OFF)]])
        assert hit["id"] == 7
        assert library.issues_list.call_args_list[1].args[0] == {
            "cover_hash_near": HASH, "cover_hash_max_distance": 16, "cover_year": 2020}

    def test_the_wide_retry_needs_series_and_number(self):
        hit, _ = self._match([[], [_row(7, "1", TWELVE_OFF, series="Detective Comics")]])
        assert hit is None

    def test_no_wide_retry_without_a_year(self):
        hit, library = self._match([[]], year=None)
        assert hit is None
        assert library.issues_list.call_count == 1

    def test_candidates_nobody_agrees_with_end_the_search(self):
        # The wide search exists for "nothing came back", not for second-guessing.
        hit, library = self._match([[_row(7, "9", HASH)], []])
        assert hit is None
        assert library.issues_list.call_count == 1

    def test_a_spread_tries_the_front_half_then_the_back(self):
        hit, library = self._match(
            [[], [_row(7, "1", "00000000000000ff")], []],
            hashes=("aaaaaaaaaaaaaaaa", "00000000000000ff", "ffffffffffffffff"))
        assert hit["id"] == 7
        assert [c.args[0]["cover_hash_near"] for c in library.issues_list.call_args_list] == [
            "aaaaaaaaaaaaaaaa", "00000000000000ff"]

    def test_never_more_than_two_requests(self):
        hit, library = self._match(
            [[], [], []], hashes=("aaaaaaaaaaaaaaaa", "00000000000000ff", "ffffffffffffffff"))
        assert hit is None
        assert library.issues_list.call_count == 2

    def test_an_outage_stops_after_one_request(self):
        hit, library = self._match([ApiError("boom"), []])
        assert hit is None
        assert library.issues_list.call_count == 1

    def test_no_cover_or_switched_off_sends_nothing(self):
        hit, library = self._match([[]], hashes=())
        assert hit is None and library.issues_list.call_count == 0
        hit, library = self._match([[_row(7, "1", HASH)]], enabled=False)
        assert hit is None and library.issues_list.call_count == 0


class TestResolveSeriesByCover:
    """A folder's series, read from the covers of its first files."""

    HIT = {"id": 7, "number": "1", "series_id": 55, "series_name": "Crimson",
           "series_year": 1998, "cover_year": 1998, "cover_hash": HASH, "distance": 1}

    SAMPLES = [
        ("/data/Crimson/Crimson 001 (1998).cbz", "Crimson", "1", 1998),
        ("/data/Crimson/Crimson 002 (1998).cbz", "Crimson", "2", 1998),
        ("/data/Crimson/Crimson 003 (1998).cbz", "Crimson", "3", 1998),
        ("/data/Crimson/Crimson 004 (1998).cbz", "Crimson", "4", 1998),
    ]

    def _resolve(self, answers, enabled=True, paused=None, samples=None):
        from models import metron

        with patch("models.metron.match_issue_by_cover", side_effect=list(answers)) as match, \
             patch("models.metron.paused_reason", return_value=paused), \
             patch("core.settings.get_bool", return_value=enabled):
            found = metron.resolve_series_by_cover(
                MagicMock(), self.SAMPLES if samples is None else samples)
        return found, match

    def test_the_first_file_decides(self):
        found, match = self._resolve([self.HIT])
        assert found == {
            "series_id": 55, "series_name": "Crimson", "series_year": 1998,
            "issue_number": "1", "file": self.SAMPLES[0][0],
        }
        assert match.call_count == 1
        assert match.call_args.args[1:] == self.SAMPLES[0]

    def test_a_miss_moves_on_to_the_next_file(self):
        found, match = self._resolve([None, self.HIT])
        assert found["series_id"] == 55
        assert found["file"] == self.SAMPLES[1][0]
        assert match.call_count == 2

    def test_never_more_than_three_files(self):
        found, match = self._resolve([None, None, None, self.HIT])
        assert found is None
        assert match.call_count == 3

    def test_a_paused_metron_stops_after_one_file(self):
        found, match = self._resolve([None, self.HIT], paused="Metron's daily limit is spent")
        assert found is None
        assert match.call_count == 1

    def test_switched_off_or_nothing_to_hash_asks_nothing(self):
        found, match = self._resolve([self.HIT], enabled=False)
        assert found is None and match.call_count == 0
        found, match = self._resolve([self.HIT], samples=[])
        assert found is None and match.call_count == 0

    def test_no_client_is_none(self):
        from models import metron

        assert metron.resolve_series_by_cover(None, self.SAMPLES) is None
