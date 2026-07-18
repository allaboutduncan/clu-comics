"""ComicVine-only series: Metron sync guards and issue-list fetch."""
import types
from unittest.mock import MagicMock

from helpers.comicvine_ids import make_comicvine_series_id


def test_sync_series_from_api_skips_comicvine():
    from sync import sync_series_from_api

    res = sync_series_from_api(None, make_comicvine_series_id(5))
    assert res["success"] is False
    assert res.get("skipped") is True


def test_get_series_needing_sync_excludes_comicvine(db_connection):
    from core.database import save_series_mapping, get_series_needing_sync

    save_series_mapping({"id": 100, "name": "Metron One"}, "/data/M")
    save_series_mapping(
        {"id": make_comicvine_series_id(5), "name": "CV One"}, "/data/CV"
    )
    ids = {s["id"] for s in get_series_needing_sync(24)}
    assert 100 in ids
    assert make_comicvine_series_id(5) not in ids


def test_get_all_issues_for_volume(monkeypatch):
    from models import comicvine

    monkeypatch.setattr(comicvine, "SIMYAN_AVAILABLE", True)

    def issue(iid, num):
        return types.SimpleNamespace(
            id=iid, number=num, name=f"Issue {num}",
            cover_date="2012-03-01", store_date=None,
            image=None, site_detail_url=None,
        )

    cv = MagicMock()
    cv.list_issues.return_value = [issue(500, "1"), issue(501, "2")]
    monkeypatch.setattr(comicvine, "_make_cv_client", lambda key: cv)

    issues = comicvine.get_all_issues_for_volume("key", 18705)
    assert len(issues) == 2
    assert issues[0]["number"] == "1"
    assert issues[0]["cv_id"] == 500
    # issue id is offset so it can't collide with a Metron issue id
    assert issues[0]["id"] == make_comicvine_series_id(500)
