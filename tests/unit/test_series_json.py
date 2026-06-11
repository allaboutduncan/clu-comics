"""Tests for models.series_json -- Mylar3-compatible series.json support."""
import json
import os
from unittest.mock import MagicMock, patch

import pytest


# ===== _normalize_status =====

class TestNormalizeStatus:

    def test_none_defaults_to_continuing(self):
        from models.series_json import _normalize_status
        assert _normalize_status(None) == "Continuing"

    def test_empty_string_defaults_to_continuing(self):
        from models.series_json import _normalize_status
        assert _normalize_status("") == "Continuing"

    def test_ongoing_string_maps_to_continuing(self):
        from models.series_json import _normalize_status
        assert _normalize_status("Ongoing") == "Continuing"
        assert _normalize_status("Continuing") == "Continuing"

    def test_ended_maps_to_ended(self):
        from models.series_json import _normalize_status
        assert _normalize_status("Ended") == "Ended"
        assert _normalize_status("ended") == "Ended"

    def test_cancelled_maps_to_ended(self):
        from models.series_json import _normalize_status
        assert _normalize_status("Cancelled") == "Ended"
        assert _normalize_status("Canceled") == "Ended"
        assert _normalize_status("Completed") == "Ended"

    def test_status_object_with_name(self):
        from models.series_json import _normalize_status
        assert _normalize_status({"name": "Ended"}) == "Ended"
        assert _normalize_status({"name": "Ongoing"}) == "Continuing"


# ===== _format_publication_run =====

class TestFormatPublicationRun:

    def test_continuing_with_year_began(self):
        from models.series_json import _format_publication_run
        assert _format_publication_run(2021, None, "Ongoing") == "2021 - Present"

    def test_continuing_ignores_year_end(self):
        from models.series_json import _format_publication_run
        # If status says continuing, even an end year doesn't matter
        assert _format_publication_run(2021, 2022, "Ongoing") == "2021 - Present"

    def test_ended_with_both_years(self):
        from models.series_json import _format_publication_run
        assert _format_publication_run(2011, 2016, "Ended") == "2011 - 2016"

    def test_ended_without_year_end(self):
        from models.series_json import _format_publication_run
        assert _format_publication_run(2011, None, "Ended") == "2011"

    def test_missing_year_began_returns_empty(self):
        from models.series_json import _format_publication_run
        assert _format_publication_run(None, None, "Ongoing") == ""


# ===== build_metadata =====

class TestBuildMetadata:

    def _dict_series(self, **overrides):
        base = {
            "id": 12345,
            "name": "Test Series",
            "cv_id": 43022,
            "volume": 5,
            "status": "Ended",
            "year_began": 2011,
            "year_end": 2016,
            "desc": "A series description.",
            "publisher": {"id": 1, "name": "DC Comics"},
            "imprint": None,
            "cover_image": "https://example.com/cover.jpg",
            "issue_count": 55,
        }
        base.update(overrides)
        return base

    def test_full_dict_series_produces_correct_schema(self):
        from models.series_json import build_metadata
        meta = build_metadata(self._dict_series())
        assert meta == {
            "type": "comicSeries",
            "publisher": "DC Comics",
            "imprint": None,
            "name": "Test Series",
            "comicid": 43022,
            "metron_id": 12345,
            "year": 2011,
            "description_text": "A series description.",
            "description_formatted": None,
            "volume": 5,
            "booktype": "Print",
            "collects": None,
            "comic_image": "https://example.com/cover.jpg",
            "total_issues": 55,
            "publication_run": "2011 - 2016",
            "status": "Ended",
        }

    def test_missing_cv_id_leaves_comicid_null(self):
        from models.series_json import build_metadata
        series = self._dict_series(cv_id=None)
        meta = build_metadata(series)
        assert meta["comicid"] is None
        assert meta["metron_id"] == 12345

    def test_issues_count_overrides_issue_count_field(self):
        from models.series_json import build_metadata
        series = self._dict_series(issue_count=5)
        issues = [{"number": str(i)} for i in range(1, 11)]
        meta = build_metadata(series, issues=issues)
        assert meta["total_issues"] == 10

    def test_continuing_series_uses_present(self):
        from models.series_json import build_metadata
        series = self._dict_series(status="Ongoing", year_end=None)
        meta = build_metadata(series)
        assert meta["publication_run"] == "2011 - Present"
        assert meta["status"] == "Continuing"

    def test_publisher_name_falls_back_to_joined_column(self):
        from models.series_json import build_metadata
        series = self._dict_series(publisher=None)
        series["publisher_name"] = "Marvel"
        meta = build_metadata(series)
        assert meta["publisher"] == "Marvel"

    def test_volume_year_used_when_year_began_missing(self):
        from models.series_json import build_metadata
        series = self._dict_series(year_began=None)
        series["volume_year"] = 2018
        meta = build_metadata(series)
        assert meta["year"] == 2018

    def test_mokkari_style_object(self):
        from models.series_json import build_metadata
        publisher = MagicMock()
        publisher.name = "DC Comics"
        publisher.id = 1
        series = MagicMock(spec=[
            "id", "name", "cv_id", "volume", "status", "year_began",
            "year_end", "desc", "publisher", "imprint", "cover_image",
            "issue_count",
        ])
        series.id = 99
        series.name = "Mokkari Series"
        series.cv_id = 11111
        series.volume = 1
        series.status = "Ongoing"
        series.year_began = 2023
        series.year_end = None
        series.desc = "Mokkari desc"
        series.publisher = publisher
        series.imprint = None
        series.cover_image = None
        series.issue_count = 3
        meta = build_metadata(series)
        assert meta["name"] == "Mokkari Series"
        assert meta["publisher"] == "DC Comics"
        assert meta["metron_id"] == 99
        assert meta["comicid"] == 11111
        assert meta["status"] == "Continuing"

    def test_backfills_cv_id_from_api_when_missing(self):
        from models.series_json import build_metadata
        series = self._dict_series(cv_id=None)
        api = MagicMock()
        api.series.return_value = MagicMock(cv_id=77777)
        meta = build_metadata(series, api=api)
        assert meta["comicid"] == 77777
        api.series.assert_called_once_with(12345)

    def test_api_failure_during_backfill_is_silent(self):
        from models.series_json import build_metadata
        series = self._dict_series(cv_id=None)
        api = MagicMock()
        api.series.side_effect = RuntimeError("network down")
        meta = build_metadata(series, api=api)
        assert meta["comicid"] is None


# ===== write_series_json =====

class TestWriteSeriesJson:

    @pytest.fixture
    def series(self):
        return {
            "id": 1,
            "name": "Aquaman",
            "cv_id": 43022,
            "volume": 5,
            "status": "Ended",
            "year_began": 2011,
            "year_end": 2016,
            "desc": "Original description.",
            "publisher": {"id": 1, "name": "DC Comics"},
            "imprint": None,
            "cover_image": "https://example.com/cover.jpg",
            "issue_count": 55,
        }

    def test_creates_file_when_missing(self, tmp_path, series):
        from models.series_json import write_series_json
        assert write_series_json(str(tmp_path), series) is True

        target = tmp_path / "series.json"
        assert target.exists()
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["metadata"]["name"] == "Aquaman"
        assert data["metadata"]["comicid"] == 43022
        assert data["metadata"]["metron_id"] == 1

    def test_returns_false_for_missing_folder(self, tmp_path, series):
        from models.series_json import write_series_json
        missing = tmp_path / "does-not-exist"
        assert write_series_json(str(missing), series) is False

    def test_preserve_existing_keeps_user_editable_fields(self, tmp_path, series):
        from models.series_json import write_series_json
        target = tmp_path / "series.json"
        existing = {
            "metadata": {
                "name": "Aquaman",
                "description_text": "User-edited description.",
                "description_formatted": "**User formatted**",
                "volume": 99,
                "booktype": "HC",
                "status": "Ended",
                "total_issues": 1,
                "publication_run": "stale",
            }
        }
        with open(target, "w", encoding="utf-8") as f:
            json.dump(existing, f)

        # New fetched data has different values for everything
        new_series = dict(series, desc="API description", volume=5, status="Ongoing")
        assert write_series_json(str(tmp_path), new_series, preserve_existing=True) is True

        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        meta = data["metadata"]

        # Preserved fields keep old values
        assert meta["description_text"] == "User-edited description."
        assert meta["description_formatted"] == "**User formatted**"
        assert meta["volume"] == 99
        assert meta["booktype"] == "HC"
        assert meta["status"] == "Ended"

        # Dynamic fields refresh from current data
        assert meta["total_issues"] == 55
        assert meta["publication_run"] != "stale"

    def test_preserve_existing_false_overwrites_everything(self, tmp_path, series):
        from models.series_json import write_series_json
        target = tmp_path / "series.json"
        existing = {
            "metadata": {
                "description_text": "User-edited description.",
                "volume": 99,
                "booktype": "HC",
            }
        }
        with open(target, "w", encoding="utf-8") as f:
            json.dump(existing, f)

        assert write_series_json(
            str(tmp_path), series, preserve_existing=False
        ) is True

        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        meta = data["metadata"]
        assert meta["description_text"] == "Original description."
        assert meta["volume"] == 5
        assert meta["booktype"] == "Print"

    def test_null_preserved_field_does_not_override(self, tmp_path, series):
        """A null preserved field in the existing file should not blank out
        the new computed value."""
        from models.series_json import write_series_json
        target = tmp_path / "series.json"
        existing = {
            "metadata": {
                "description_text": None,
                "volume": None,
            }
        }
        with open(target, "w", encoding="utf-8") as f:
            json.dump(existing, f)

        write_series_json(str(tmp_path), series, preserve_existing=True)

        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        meta = data["metadata"]
        assert meta["description_text"] == "Original description."
        assert meta["volume"] == 5

    def test_corrupt_existing_file_falls_back_to_overwrite(self, tmp_path, series):
        from models.series_json import write_series_json
        target = tmp_path / "series.json"
        target.write_text("not valid json {{{", encoding="utf-8")

        assert write_series_json(str(tmp_path), series) is True

        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["metadata"]["name"] == "Aquaman"

    def test_atomic_write_no_partial_file_on_failure(self, tmp_path, series):
        """If json.dump fails, no partial series.json should remain."""
        from models import series_json as sj

        target = tmp_path / "series.json"
        target.write_text(json.dumps({"metadata": {"name": "old"}}), encoding="utf-8")
        original_content = target.read_text(encoding="utf-8")

        with patch("models.series_json.json.dump", side_effect=RuntimeError("disk full")):
            result = sj.write_series_json(str(tmp_path), series)

        assert result is False
        # Original file remains intact (os.replace never ran)
        assert target.read_text(encoding="utf-8") == original_content
        # No stray .tmp files were left behind
        leftovers = [p for p in os.listdir(tmp_path) if p.startswith(".series.json.")]
        assert leftovers == []

    def test_return_reason_success(self, tmp_path, series):
        from models.series_json import write_series_json
        ok, reason = write_series_json(str(tmp_path), series, return_reason=True)
        assert ok is True
        assert reason is None

    def test_return_reason_missing_folder(self, tmp_path, series):
        from models.series_json import write_series_json
        missing = tmp_path / "does-not-exist"
        ok, reason = write_series_json(str(missing), series, return_reason=True)
        assert ok is False
        assert "does not exist" in reason

    def test_return_reason_surfaces_exception(self, tmp_path, series):
        from models import series_json as sj
        with patch("models.series_json.json.dump", side_effect=RuntimeError("disk full")):
            ok, reason = sj.write_series_json(str(tmp_path), series, return_reason=True)
        assert ok is False
        assert "disk full" in reason


# ===== read_series_json =====

class TestReadSeriesJson:

    def test_returns_none_when_missing(self, tmp_path):
        from models.series_json import read_series_json
        assert read_series_json(str(tmp_path)) is None

    def test_returns_none_for_invalid_path(self):
        from models.series_json import read_series_json
        assert read_series_json("") is None
        assert read_series_json(None) is None

    def test_returns_parsed_dict(self, tmp_path):
        from models.series_json import read_series_json
        payload = {"metadata": {"name": "Test"}}
        (tmp_path / "series.json").write_text(json.dumps(payload), encoding="utf-8")
        assert read_series_json(str(tmp_path)) == payload

    def test_returns_none_on_malformed_json(self, tmp_path):
        from models.series_json import read_series_json
        (tmp_path / "series.json").write_text("{{not json", encoding="utf-8")
        assert read_series_json(str(tmp_path)) is None
