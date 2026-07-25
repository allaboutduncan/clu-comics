"""Tests for the GetComics wanted-issue simulation (PR #248 review fixes 5 & 8).

Fix 5: the simulation must honor `limit` and bound the live search work, rather
       than searching every issue of every series synchronously.
Fix 8: the simulation must skip issues covered by a range pack it would download,
       mirroring the real scheduled_getcomics_download.
"""

from unittest.mock import patch


def _series(i):
    return {"id": i, "name": f"S{i}", "mapped_path": f"/data/S{i}",
            "monitored": 1, "volume": 1, "volume_year": 2020,
            "year_began": 2020, "publisher_name": "DC"}


def test_simulation_bounds_series_to_limit():
    import routes.downloads as dl

    series = [_series(i) for i in range(5)]
    issues = [{"number": "1", "store_date": "2000-01-01"}]

    searched = []

    def fake_search(**kw):
        searched.append(kw["series_name"])
        return []

    with patch("routes.downloads.get_all_mapped_series", return_value=series), \
         patch("routes.downloads.get_issues_for_series", return_value=issues), \
         patch("routes.downloads.get_manual_status_for_series", return_value={}), \
         patch("routes.downloads.get_series_alias_list", return_value=[]), \
         patch("routes.downloads.match_issues_to_collection", return_value={}), \
         patch("routes.downloads.search_getcomics_for_issue", side_effect=fake_search), \
         patch("models.usenet.usenet_enabled_and_configured", return_value=False):
        dl._run_wanted_simulation(limit=2, target_series_id=None, target_series_name=None)

    # Only 2 of the 5 mapped series were simulated (live searches bounded).
    assert len(set(searched)) == 2


def test_simulation_skips_issues_covered_by_range():
    import routes.downloads as dl

    series = [_series(1)]
    issues = [{"number": n, "store_date": "2000-01-01"} for n in ("1", "2", "3")]

    searched = []

    def fake_search(**kw):
        searched.append(kw["issue_num"])
        return [{"title": "S1 #1-3", "link": "http://x/1", "download_url": ""}]

    with patch("routes.downloads.get_all_mapped_series", return_value=series), \
         patch("routes.downloads.get_issues_for_series", return_value=issues), \
         patch("routes.downloads.get_manual_status_for_series", return_value={}), \
         patch("routes.downloads.get_series_alias_list", return_value=[]), \
         patch("routes.downloads.match_issues_to_collection", return_value={}), \
         patch("routes.downloads.search_getcomics_for_issue", side_effect=fake_search), \
         patch("routes.downloads.score_getcomics_result", return_value=(39, True, True)), \
         patch("routes.downloads.accept_result", return_value="FALLBACK"), \
         patch("routes.downloads.get_download_links", return_value={}), \
         patch("routes.downloads.select_download_url", return_value=(("pixeldrain", None), [])), \
         patch("models.usenet.usenet_enabled_and_configured", return_value=False):
        dl._run_wanted_simulation(limit=10, target_series_id=None, target_series_name=None)

    # Issue 1 resolves to a range pack (#1-3); issues 2 and 3 are covered by it
    # and must not be searched again.
    assert searched == ["1"]
