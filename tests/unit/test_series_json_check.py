"""series.json blank-value detection (the pure half) and its classification."""

import json

import pytest

from core.problem_files import (
    CLASS_SERIES_JSON_BLANK,
    CLASS_SERIES_JSON_INVALID,
    classify,
)
from core.series_json_check import blank_fields, check_file


def _full(**over):
    metadata = {
        "name": "Batman",
        "description_text": "The Dark Knight.",
        "comic_image": "https://example.com/cover.jpg",
    }
    metadata.update(over)
    return {"metadata": metadata}


class TestBlankFields:
    def test_complete_file_has_no_blanks(self):
        assert blank_fields(_full()) == []

    @pytest.mark.parametrize("value", [None, "", "   ", "\n\t", 0, 12, [], {}])
    def test_each_blank_name_shape_is_flagged(self, value):
        assert blank_fields(_full(name=value)) == ["name"]

    @pytest.mark.parametrize("field", ["comic_image", "description_text"])
    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_string_is_valid_for_description_and_cover(self, field, value):
        assert blank_fields(_full(**{field: value})) == []

    @pytest.mark.parametrize("field", ["comic_image", "description_text"])
    @pytest.mark.parametrize("value", [None, 0, 12, [], {}])
    def test_null_or_non_string_is_flagged_for_description_and_cover(self, field, value):
        assert blank_fields(_full(**{field: value})) == [field]

    def test_missing_key_is_blank(self):
        data = _full()
        del data["metadata"]["comic_image"]
        assert blank_fields(data) == ["comic_image"]

    def test_empty_name_is_never_valid(self):
        assert blank_fields(_full(name="")) == ["name"]

    def test_reports_every_blank_field_in_order(self):
        data = _full(name="", description_text=None, comic_image=None)
        assert blank_fields(data) == ["name", "description_text", "comic_image"]

    @pytest.mark.parametrize("data", [None, [], "x", {}, {"metadata": None},
                                      {"metadata": []}, {"name": "Batman"}])
    def test_not_a_mylar_file_is_none_not_blank(self, data):
        """Distinct from "blank": it needs a different fix and message."""
        assert blank_fields(data) is None

    def test_other_blank_fields_are_not_checked(self):
        assert blank_fields(_full(imprint="", publisher="", volume=None)) == []


class TestCheckFile:
    def _write(self, tmp_path, text):
        path = tmp_path / "series.json"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_complete(self, tmp_path):
        assert check_file(self._write(tmp_path, json.dumps(_full()))) is None

    def test_null_cover_is_flagged_with_its_name(self, tmp_path):
        text = json.dumps(_full(comic_image=None))
        klass, message = check_file(self._write(tmp_path, text))
        assert klass == CLASS_SERIES_JSON_BLANK
        assert "comic_image" in message and "name" not in message

    def test_malformed_json_is_invalid(self, tmp_path):
        klass, _msg = check_file(self._write(tmp_path, "{not json"))
        assert klass == CLASS_SERIES_JSON_INVALID

    def test_missing_metadata_object_is_invalid(self, tmp_path):
        klass, _msg = check_file(self._write(tmp_path, "{}"))
        assert klass == CLASS_SERIES_JSON_INVALID

    def test_non_utf8_file_is_invalid_not_a_crash(self, tmp_path):
        path = tmp_path / "series.json"
        path.write_bytes(b"\xff\xfe\x00bad")
        klass, _msg = check_file(str(path))
        assert klass == CLASS_SERIES_JSON_INVALID


class TestClassification:
    def test_blank_fields_offer_refresh_and_never_delete(self):
        c = classify(CLASS_SERIES_JSON_BLANK, "Blank: name")
        assert c["action"] == "refresh"
        assert c["healthy_file"] is True, "the page hides Delete for healthy files"

    def test_invalid_offers_refresh(self):
        c = classify(CLASS_SERIES_JSON_INVALID, "Could not parse series.json: x")
        assert c["action"] == "refresh"
        assert c["healthy_file"] is True


class TestWriterAgreesWithChecker:
    def test_metron_series_without_image_is_written_empty_and_passes(self, tmp_path):
        """A Metron series with no cover gets "", which the checker allows."""
        from models.series_json import write_series_json, read_series_json

        series = {"id": 1, "name": "Batman", "desc": "Hero.", "year_began": 2016,
                  "image": None}
        assert write_series_json(str(tmp_path), series) is True
        meta = read_series_json(str(tmp_path))["metadata"]
        assert meta["comic_image"] == ""
        assert blank_fields({"metadata": meta}) == []
