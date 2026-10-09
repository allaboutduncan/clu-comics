"""Scene/usenet release names: underscores for spaces and a bare "NN of MM".

A real download, ``Cyberpunk_2077_-_Psycho_Squad_01_of_04_2025_digital_
Son_of_Ultron-Empire.cbr``, parsed to nothing, and the fallback renamer then
took "2077" for the issue and emitted ``Cyberpunk 2077.cbr`` for issues 1, 2
and 4 alike. They collided as `` (1)`` in TARGET and none could match the
wanted "Psycho Squad #N".
"""
import pytest

from cbz_ops.rename import (
    extract_comic_values,
    get_renamed_filename,
    normalize_release_name,
)

SCENE = "Cyberpunk_2077_-_Psycho_Squad_{n}_of_04_2025_digital_Son_of_Ultron-Empire.cbr"


class TestNormalizeReleaseName:

    def test_ordinary_name_is_untouched(self):
        name = "Batman 050 - Part 2 (2018).cbz"
        assert normalize_release_name(name) == name

    def test_year_in_an_ordinary_name_is_untouched(self):
        name = "Hulk vs. The Marvel Universe 2008 Digital.cbz"
        assert normalize_release_name(name) == name

    def test_scene_name_becomes_ordinary(self):
        assert normalize_release_name(SCENE.format(n="01")) == (
            "Cyberpunk 2077 - Psycho Squad #01 (2025).cbr"
        )

    def test_of_count_alone_marks_the_issue(self):
        assert normalize_release_name("Batman 01 of 04 (2025).cbz") == (
            "Batman #01 (2025).cbz"
        )

    def test_of_inside_a_title_survives(self):
        name = "House of Secrets 012 (1970).cbz"
        assert normalize_release_name(name) == name

    def test_no_extension_is_returned_as_is(self):
        assert normalize_release_name("Batman_01_of_04") == "Batman_01_of_04"


class TestSceneNamesParse:

    @pytest.mark.parametrize("n", ["01", "02", "04"])
    def test_series_issue_and_year_are_extracted(self, n):
        v = extract_comic_values(SCENE.format(n=n))
        assert v["series_name"] == "Cyberpunk 2077 - Psycho Squad"
        assert v["issue_number"] == n.rjust(3, "0")
        assert v["year"] == "2025"

    @pytest.mark.parametrize("n", ["01", "02", "04"])
    def test_default_rename_keeps_the_issue_number(self, n):
        """The old output was ``Cyberpunk 2077.cbr`` for every one of them."""
        new = get_renamed_filename(SCENE.format(n=n))
        assert new is not None
        assert "Psycho Squad" in new
        assert n.rjust(3, "0") in new

    def test_distinct_issues_get_distinct_names(self):
        names = {get_renamed_filename(SCENE.format(n=n)) for n in ("01", "02", "04")}
        assert len(names) == 3

    def test_bare_of_count_with_spaces(self):
        v = extract_comic_values("Absolute Batman 03 of 12 (2025).cbz")
        assert v["series_name"] == "Absolute Batman"
        assert v["issue_number"] == "003"
