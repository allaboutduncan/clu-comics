"""core/series_json_builder -- which provider fills series.json (#618)."""
from unittest.mock import patch

import pytest

from core import series_json_builder as sjb


def _series(name="Criminal", desc=None, **extra):
    d = {"name": name, "year_began": 2006, "desc": desc, "publisher_name": "Icon"}
    d.update(extra)
    return d


@pytest.fixture
def fetchers():
    """Swap both provider fetchers for recorders."""
    calls = []
    results = {"metron": None, "comicvine": None}

    def make(name):
        def fetch(ids):
            calls.append(name)
            r = results[name]
            if isinstance(r, Exception):
                raise r
            return dict(r) if r else None
        return fetch

    with patch.dict(sjb._FETCHERS, {"metron": make("metron"), "comicvine": make("comicvine")}), \
         patch.object(sjb, "folder_ids", return_value={"cv_id": 42, "metron_id": None}):
        yield results, calls


class TestProviderOrder:
    def test_unconfigured_library_uses_default_order(self):
        with patch("core.database.get_library_providers", return_value=[]):
            assert sjb.provider_order(1) == ["metron", "comicvine"]

    def test_auto_follows_library_order(self):
        provs = [{"provider_type": "comicvine", "enabled": True},
                 {"provider_type": "metron", "enabled": True}]
        with patch("core.database.get_library_providers", return_value=provs):
            assert sjb.provider_order(1, "auto") == ["comicvine", "metron"]

    def test_disabled_and_unsupported_providers_are_skipped(self):
        provs = [{"provider_type": "gcd", "enabled": True},
                 {"provider_type": "metron", "enabled": False},
                 {"provider_type": "comicvine", "enabled": True}]
        with patch("core.database.get_library_providers", return_value=provs):
            assert sjb.provider_order(1) == ["comicvine"]

    def test_preferred_leads_and_the_rest_still_follow(self):
        with patch("core.database.get_library_providers", return_value=[]):
            assert sjb.provider_order(None, "comicvine") == ["comicvine", "metron"]


class TestResolve:
    def test_first_provider_with_a_description_wins(self, fetchers):
        results, calls = fetchers
        results["metron"] = _series(desc="From Metron", id=7)
        with patch.object(sjb, "provider_order", return_value=["metron", "comicvine"]):
            series, used, desc_from, tried = sjb.resolve_series_for_folder("/x")
        assert (used, desc_from, calls) == ("metron", "metron", ["metron"])

    def test_empty_metron_description_falls_back_to_comicvine(self, fetchers):
        results, calls = fetchers
        results["metron"] = _series(desc=None, id=7)
        results["comicvine"] = _series(desc="<p>From CV</p>")
        with patch.object(sjb, "provider_order", return_value=["metron", "comicvine"]):
            series, used, desc_from, tried = sjb.resolve_series_for_folder("/x")
        assert used == "metron"            # other fields stay with the chosen provider
        assert series["id"] == 7
        assert desc_from == "comicvine"
        assert "From CV" in series["description"]

    def test_comicvine_first_when_preferred(self, fetchers):
        results, calls = fetchers
        results["metron"] = _series(desc="M", id=7)
        results["comicvine"] = _series(desc="C")
        with patch("core.database.get_library_providers", return_value=[]):
            _, used, _, _ = sjb.resolve_series_for_folder("/x", None, "comicvine")
        assert used == "comicvine"
        assert calls == ["comicvine"]

    def test_failing_provider_falls_through(self, fetchers):
        results, calls = fetchers
        results["metron"] = RuntimeError("down")
        results["comicvine"] = _series(desc="C")
        with patch.object(sjb, "provider_order", return_value=["metron", "comicvine"]):
            _, used, _, tried = sjb.resolve_series_for_folder("/x")
        assert used == "comicvine"
        assert tried == ["metron", "comicvine"]

    def test_no_provider_answering_returns_none(self, fetchers):
        with patch.object(sjb, "provider_order", return_value=["metron", "comicvine"]):
            series, used, desc_from, tried = sjb.resolve_series_for_folder("/x")
        assert series is None and used is None and tried == ["metron", "comicvine"]

    def test_no_description_anywhere_keeps_the_primary(self, fetchers):
        results, _ = fetchers
        results["metron"] = _series(desc=None, id=7)
        results["comicvine"] = _series(desc="")
        with patch.object(sjb, "provider_order", return_value=["metron", "comicvine"]):
            series, used, desc_from, _ = sjb.resolve_series_for_folder("/x")
        assert used == "metron" and desc_from is None


class TestWithDescriptionFallback:
    def test_non_dict_and_described_series_pass_through(self):
        obj = object()
        assert sjb.with_description_fallback(obj, "/x") is obj
        s = _series(desc="Has one", id=1)
        assert sjb.with_description_fallback(s, "/x") is s

    def test_metron_series_is_topped_up_from_comicvine(self, fetchers, tmp_path):
        results, calls = fetchers
        results["comicvine"] = _series(desc="CV text")
        s = _series(desc=None, id=7, cv_id=42)
        with patch.object(sjb, "provider_order", return_value=["metron", "comicvine"]):
            out = sjb.with_description_fallback(s, str(tmp_path))
        assert out["description"] == "CV text"
        assert out["id"] == 7
        assert calls == ["comicvine"]          # never re-asks the origin provider
        assert s["desc"] is None               # input not mutated

    def test_failure_returns_the_original(self, fetchers, tmp_path):
        results, _ = fetchers
        results["comicvine"] = RuntimeError("boom")
        s = _series(desc=None, id=7)
        with patch.object(sjb, "provider_order", return_value=["metron", "comicvine"]):
            assert sjb.with_description_fallback(s, str(tmp_path)) is s


class TestRefresh:
    def test_missing_folder(self, tmp_path):
        r = sjb.refresh_series_json(str(tmp_path / "nope"))
        assert r["success"] is False

    def test_no_provider_data_writes_nothing(self, tmp_path, fetchers):
        with patch.object(sjb, "provider_order", return_value=["metron", "comicvine"]):
            r = sjb.refresh_series_json(str(tmp_path))
        assert r["success"] is False
        assert not (tmp_path / "series.json").exists()

    def test_writes_and_reports_provider(self, tmp_path, fetchers):
        results, _ = fetchers
        results["comicvine"] = _series(desc="CV text", cv_id=42)
        with patch.object(sjb, "provider_order", return_value=["comicvine"]), \
             patch.object(sjb, "get_default_provider", return_value="auto"):
            r = sjb.refresh_series_json(str(tmp_path))
        assert r["success"] and r["provider"] == "comicvine"
        assert (tmp_path / "series.json").is_file()

    def test_saved_preference_is_used_for_auto(self, tmp_path, fetchers):
        results, calls = fetchers
        results["metron"] = _series(desc="M", id=7)
        results["comicvine"] = _series(desc="C")
        with patch("core.database.get_library_providers", return_value=[]), \
             patch.object(sjb, "get_default_provider", return_value="comicvine"):
            r = sjb.refresh_series_json(str(tmp_path), provider="auto")
        assert r["provider"] == "comicvine"

    def test_force_discards_hand_edits(self, tmp_path, fetchers):
        import json
        results, _ = fetchers
        results["comicvine"] = _series(desc="Fresh", cv_id=42)
        (tmp_path / "series.json").write_text(
            json.dumps({"metadata": {"name": "Criminal", "description_text": "Mine"}}))
        with patch.object(sjb, "provider_order", return_value=["comicvine"]):
            sjb.refresh_series_json(str(tmp_path), provider="comicvine", force=False)
            kept = json.loads((tmp_path / "series.json").read_text())["metadata"]["description_text"]
            sjb.refresh_series_json(str(tmp_path), provider="comicvine", force=True)
            rebuilt = json.loads((tmp_path / "series.json").read_text())["metadata"]["description_text"]
        assert kept == "Mine"
        assert rebuilt == "Fresh"


class TestDefaultProvider:
    @pytest.mark.parametrize("stored,expected", [
        ("metron", "metron"), ("COMICVINE", "comicvine"), ("bogus", "auto"), (None, "auto"),
    ])
    def test_preference(self, stored, expected):
        with patch("core.database.get_user_preference", return_value=stored):
            assert sjb.get_default_provider() == expected
