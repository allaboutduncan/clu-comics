"""Results the GetComics gate used to accept and must not (#730).

Every title here is from a real sweep. Each was either downloaded as the
wanted issue or queued as a "range fallback" pack, and each one is a different
book: a reprint, a fan edit, a tie-in, another series whose name starts with
the wanted one, or another volume's run.
"""
import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from models.getcomics import (
    ACCEPT_THRESHOLD,
    accept_result,
    better_fallback,
    score_getcomics_result,
)

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _fixed_clock():
    """Pin "this year" so the back-catalogue rule does not move with the date."""
    with patch("models.getcomics._current_year", return_value=2026):
        yield


def _score(title, series, issue, year=None, volume=None, volume_year=None):
    return score_getcomics_result(
        title, series, issue, year, series_volume=volume, volume_year=volume_year)


def _decide(title, series, issue, year=None, volume=None, volume_year=None):
    return accept_result(*_score(title, series, issue, year, volume, volume_year))


class TestUndatedTitle:
    """A title with no year is no evidence for which volume it is."""

    @pytest.mark.parametrize("title,series,issue,year,volume_year", [
        # Was the 2012 reprint.
        ("Amazing Fantasy #15", "Amazing Fantasy", "15", 2006, 2004),
        # Was the 2016 issue.
        ("New Avengers #14", "New Avengers", "14", 2011, 2010),
        # No volume year known either (a reading-list entry).
        ("New Avengers #14", "New Avengers", "14", 2011, None),
    ])
    def test_back_catalogue_issue_is_not_accepted(self, title, series, issue, year, volume_year):
        score, _, _ = _score(title, series, issue, year, None, volume_year)
        assert score < ACCEPT_THRESHOLD
        assert _decide(title, series, issue, year, None, volume_year) == "REJECT"

    def test_dated_title_of_the_wrong_volume_stays_rejected(self):
        assert _decide("New Avengers #14 (2016)", "New Avengers", "14", 2011, None, 2010) == "REJECT"

    def test_dated_title_of_the_right_volume_is_accepted(self):
        assert _decide("New Avengers #14 (2011)", "New Avengers", "14", 2011, None, 2010) == "ACCEPT"

    @pytest.mark.parametrize("year", [2025, 2026])
    def test_a_recent_issue_still_downloads_from_an_undated_listing(self, year):
        assert _decide("Absolute Batman #14", "Absolute Batman", "14", year, None, 2024) == "ACCEPT"

    def test_an_issue_with_no_known_year_is_left_alone(self):
        assert _decide("Saga #14", "Saga", "14") == "ACCEPT"

    def test_a_year_inside_the_series_name_is_not_a_date(self):
        # "2099" belongs to the name; the title is still undated.
        assert _decide("Spider-Man 2099 #3", "Spider-Man 2099", "3", 2015, None, 2015) == "REJECT"
        assert _decide("Spider-Man 2099 #3 (2015)", "Spider-Man 2099", "3", 2015, None, 2015) == "ACCEPT"


class TestUnofficialEditions:

    @pytest.mark.parametrize("title", [
        "Batman #1 (2016) (Fan-Made)",
        "Batman #1 (2016) (Fan Made)",
        "Batman #1 (2016) Fan Edit",
        "Batman #1 (2016) [Unofficial]",
        "Batman #1 (2016) (Bootleg)",
        "Batman #1 – 12 (2016) (Fan-Made)",
        "Batman Custom Omnibus #1 – 12 (2016)",
    ])
    def test_rejected_whatever_else_matches(self, title):
        score, _, _ = _score(title, "Batman", "1", 2016, None, 2016)
        assert score < 0
        assert _decide(title, "Batman", "1", 2016, None, 2016) == "REJECT"

    def test_official_title_is_untouched(self):
        assert _decide("Batman #1 (2016)", "Batman", "1", 2016, None, 2016) == "ACCEPT"

    def test_a_series_named_with_the_word_is_not_caught(self):
        assert _decide("The Unofficial Guide #1 (2016)", "The Unofficial Guide",
                       "1", 2016, None, 2016) == "ACCEPT"

    def test_word_boundaries(self):
        # "fan" on its own, or inside another word, is not a marker: the title
        # keeps an ordinary score instead of the outright -100.
        for title in ("Batman #1 (2016) Fantastic Edition", "Batman #1 (2016) Fan Favourite"):
            score, _, _ = _score(title, "Batman", "1", 2016, None, 2016)
            assert score > 0


class TestNameContinuesPastTheWantedName:
    """A longer series name that starts with the wanted one is another series."""

    @pytest.mark.parametrize("title,series,issue,year,volume_year", [
        # A number that belongs to the name.
        ("Spider-Man 2099 #1 (2015)", "Spider-Man", "1", 2016, 2016),
        ("Spider-Man 2099 #1 (2015)", "Spider-Man", "1", None, None),
        ("Spider-Man 2099 #1 (2015)", "Spider-Man", "1", 2015, 2015),
        ("Justice League 3001 #6 (2015)", "Justice League", "6", 2012, 2011),
        ("Justice League 3001 #6 (2015)", "Justice League", "6", 2015, 2015),
        ("Justice League 3001 #1 – 12 (2015-2016)", "Justice League", "6", 2015, 2015),
        # A ":" subtitle, the twin of the "–" form that was already rejected.
        ("Empyre: Avengers #1 – 3 (2020)", "Empyre", "3", 2020, 2020),
        ("Empyre – Captain America #1 – 3 (2020)", "Empyre", "3", 2020, 2020),
        ("Astonishing X-Men: Infinity Comic #33 – 38 (2025)", "Astonishing X-Men", "34", 2007, 2004),
        ("Empyre: Avengers #3 (2020)", "Empyre", "3", 2020, 2020),
    ])
    def test_rejected(self, title, series, issue, year, volume_year):
        assert _decide(title, series, issue, year, None, volume_year) == "REJECT"

    @pytest.mark.parametrize("title,series,issue,year,volume_year", [
        # The longer name, when it is the one wanted.
        ("Spider-Man 2099 #1 (2015)", "Spider-Man 2099", "1", 2015, 2015),
        ("Justice League 3001 #6 (2015)", "Justice League 3001", "6", 2015, 2015),
        ("Empyre: Avengers #3 (2020)", "Empyre: Avengers", "3", 2020, 2020),
        ("Empyre – Avengers #3 (2020)", "Empyre: Avengers", "3", 2020, 2020),
        ("Batman: Year One #1 (2020)", "Batman: Year One", "1", 2020, 2020),
        # A plain issue and a volume marker are not name text.
        ("Spider-Man #1 (2016)", "Spider-Man", "1", 2016, 2016),
        ("Catwoman Vol. 2 #5 (2002)", "Catwoman", "5", 2002, 2002),
    ])
    def test_the_named_series_is_still_accepted(self, title, series, issue, year, volume_year):
        assert _decide(title, series, issue, year, None, volume_year) == "ACCEPT"

    def test_a_leading_year_is_a_date_not_a_name(self):
        # "Flash Gordon 2024 #1" for the 2024 volume: the number is its year.
        assert _decide("Flash Gordon 2024 #1", "Flash Gordon", "1", 2024, None, 2024) == "ACCEPT"


class TestPenalisedPackIsNotAFallback:
    """A pack that names another volume, or is dated outside the run."""

    @pytest.mark.parametrize("title,series,issue,year,volume,volume_year", [
        # Scored exactly 0 and was queued: 189 issues of the wrong volume.
        ("Wolverine Vol. 2 #1 – 189 + Extras (1988-2003)", "Wolverine", "2", 2003, 3, 2003),
        ("Amazing Fantasy Vol. 2 #1 – 20 (2004-2006)", "Amazing Fantasy", "20", 2006, 3, 2006),
        # The 2017 IDW run for the series that started in 2012.
        ("30 Days of Night #1 – 6 (2017-2018)", "30 Days of Night", "1", 2012, None, 2012),
        # No volume year known: dated well outside the issue's year.
        ("30 Days of Night #1 – 6 (2017-2018)", "30 Days of Night", "1", 2012, None, None),
    ])
    def test_rejected(self, title, series, issue, year, volume, volume_year):
        score, is_range, _ = _score(title, series, issue, year, volume, volume_year)
        assert is_range
        assert score < 0
        assert _decide(title, series, issue, year, volume, volume_year) == "REJECT"

    @pytest.mark.parametrize("title,series,issue,year,volume,volume_year", [
        # The right packs from the same sweeps.
        ("Wolverine Vol. 3 #1 – 90 + Annuals (2003-2009)", "Wolverine", "1", 2003, 3, 2003),
        ("Exiles Vol. 1 #1 – 100 (2001-2008)", "Exiles", "100", 2008, 1, 2001),
        ("Green Lantern Vol. 3 #0 – 181 + Extras (1990-2004)", "Green Lantern", "3", 1990, 3, 1990),
        # No volume on either side.
        ("Batman #1 – 12 (2016)", "Batman", "5", 2016, None, 2016),
        # An issue from the middle of the span, with and without a volume year.
        ("Exiles Vol. 1 #1 – 100 (2001-2008)", "Exiles", "50", 2005, 1, 2001),
        ("Exiles #1 – 100 (2001-2008)", "Exiles", "50", 2005, None, None),
        # Dated one year off the issue (cover date against store date).
        ("Batman #1 – 12 (2016)", "Batman", "12", 2017, None, None),
        # An undated pack carries no contrary evidence.
        ("Batman #1 – 12", "Batman", "5", 2016, None, 2016),
    ])
    def test_the_right_pack_is_still_a_fallback(self, title, series, issue, year, volume, volume_year):
        assert _decide(title, series, issue, year, volume, volume_year) == "FALLBACK"

    def test_a_span_holding_the_issue_year_is_a_year_match(self):
        inside, _, _ = _score("Exiles #1 – 100 (2001-2008)", "Exiles", "50", 2005)
        assert inside == ACCEPT_THRESHOLD - 1


class TestBestFallbackWins:

    def test_first_fallback_is_kept_until_a_better_one(self):
        first = better_fallback(None, "a", 0)
        assert first == ("a", 0)
        assert better_fallback(first, "b", 39) == ("b", 39)

    def test_a_tie_keeps_the_earlier_result(self):
        assert better_fallback(("a", 39), "b", 39) == ("a", 39)

    def test_a_worse_one_never_replaces(self):
        assert better_fallback(("a", 39), "b", 10) == ("a", 39)

    @pytest.mark.parametrize("path", [
        "app.py", "routes/downloads.py", "models/usenet.py", "models/dcpp.py",
    ])
    def test_every_scoring_loop_uses_it(self, path):
        """app.py cannot be imported, so the four loops are checked by AST."""
        source = (REPO / path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "best_fallback" for t in node.targets)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "better_fallback"
        ]
        assert calls, f"{path} no longer picks its fallback with better_fallback()"
        # The old first-wins guard must not come back beside it.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            if (isinstance(node.left, ast.Name) and node.left.id == "best_fallback"
                    and any(isinstance(op, ast.Is) for op in node.ops)):
                pytest.fail(f"{path}:{node.lineno} keeps the first fallback instead of the best")

    @patch("models.indexers.get_indexer_impl")
    @patch("core.database.get_enabled_indexers", return_value=[
        {"id": 5, "name": "NZBgeek", "url": "https://x", "api_key": "k",
         "categories": None, "enabled": True, "indexer_type": "newznab"},
    ])
    def test_usenet_takes_the_better_pack_when_it_comes_second(self, _idx, mock_impl):
        from models import usenet
        from models.indexers import NZBSearchResult

        impl = MagicMock()
        impl.last_error = None
        impl.search.return_value = [
            NZBSearchResult(indexer_id=5, indexer_name="NZBgeek",
                            title="Batman #1 - 12",
                            nzb_url="https://x/undated.nzb", size=1),
            NZBSearchResult(indexer_id=5, indexer_name="NZBgeek",
                            title="Batman #1 - 12 (2016)",
                            nzb_url="https://x/dated.nzb", size=1),
        ]
        mock_impl.return_value = impl

        res = usenet.search_usenet_for_issue("Batman", "5", issue_year=2016)

        assert [r["decision"] for r in res["all_results"]] == ["FALLBACK", "FALLBACK"]
        assert res["all_results"][0]["score"] < res["all_results"][1]["score"]
        assert res["tier"] == "range fallback"
        assert res["chosen"][0].nzb_url == "https://x/dated.nzb"
