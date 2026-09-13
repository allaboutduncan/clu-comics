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
         patch("routes.downloads.get_result_parts",
               return_value=[{"label": None, "links": {}}]), \
         patch("routes.downloads.select_download_url", return_value=(("pixeldrain", None), [])), \
         patch("core.config.is_download_packs_enabled", return_value=True), \
         patch("models.usenet.usenet_enabled_and_configured", return_value=False):
        dl._run_wanted_simulation(limit=10, target_series_id=None, target_series_name=None)

    # Issue 1 resolves to a range pack (#1-3); issues 2 and 3 are covered by it
    # and must not be searched again.
    assert searched == ["1"]


def test_simulation_skips_only_the_range_of_the_part_it_picks():
    """#542: a split post covers only the part that would be downloaded.

    The post title says #1-80, but each part is its own download. Recording
    the title's range would mark #16-80 as covered after grabbing #1-15.
    """
    import routes.downloads as dl

    series = [_series(1)]
    issues = [{"number": str(n), "store_date": "2000-01-01"} for n in range(1, 31)]
    parts = [
        {"label": "S1 #1 – 15 (2000)", "links": {"pixeldrain": "https://getcomics.org/dls/a"}},
        {"label": "S1 #16 – 28 (2001)", "links": {"pixeldrain": "https://getcomics.org/dls/b"}},
        {"label": "S1 Annual #1 – 2", "links": {"pixeldrain": "https://getcomics.org/dls/c"}},
    ]

    searched = []

    def fake_search(**kw):
        searched.append(kw["issue_num"])
        return [{"title": "S1 #1 – 80 (2000-2003)", "link": "http://x/1", "download_url": ""}]

    with patch("routes.downloads.get_all_mapped_series", return_value=series), \
         patch("routes.downloads.get_issues_for_series", return_value=issues), \
         patch("routes.downloads.get_manual_status_for_series", return_value={}), \
         patch("routes.downloads.get_series_alias_list", return_value=[]), \
         patch("routes.downloads.match_issues_to_collection", return_value={}), \
         patch("routes.downloads.search_getcomics_for_issue", side_effect=fake_search), \
         patch("routes.downloads.score_getcomics_result", return_value=(39, True, True)), \
         patch("routes.downloads.accept_result", return_value="FALLBACK"), \
         patch("routes.downloads.get_result_parts", return_value=parts), \
         patch("core.config.is_download_packs_enabled", return_value=True), \
         patch("models.usenet.usenet_enabled_and_configured", return_value=False):
        dl._run_wanted_simulation(limit=10, target_series_id=None, target_series_name=None)

    # #1 takes part #1-15 and #16 takes part #16-28; no part holds #29 or #30,
    # so each is searched and nothing is recorded for it.
    assert searched == ["1", "16", "29", "30"]


def _run_pack_sim(title, parts, issue_numbers):
    """Simulate one series whose every search returns *title* as a range
    fallback, with Download Packs off. Returns (issues searched, results)."""
    import routes.downloads as dl

    issues = [{"number": n, "store_date": "2000-01-01"} for n in issue_numbers]
    searched = []

    def fake_search(**kw):
        searched.append(kw["issue_num"])
        return [{"title": title, "link": "http://x/1", "download_url": ""}]

    with patch("routes.downloads.get_all_mapped_series", return_value=[_series(1)]), \
         patch("routes.downloads.get_issues_for_series", return_value=issues), \
         patch("routes.downloads.get_manual_status_for_series", return_value={}), \
         patch("routes.downloads.get_series_alias_list", return_value=[]), \
         patch("routes.downloads.match_issues_to_collection", return_value={}), \
         patch("routes.downloads.search_getcomics_for_issue", side_effect=fake_search), \
         patch("routes.downloads.score_getcomics_result", return_value=(39, True, True)), \
         patch("routes.downloads.accept_result", return_value="FALLBACK"), \
         patch("routes.downloads.get_result_parts", return_value=parts), \
         patch("core.config.is_download_packs_enabled", return_value=False), \
         patch("models.usenet.usenet_enabled_and_configured", return_value=False):
        results = dl._run_wanted_simulation(limit=10, target_series_id=None,
                                            target_series_name=None)
    return searched, results


def test_simulation_with_packs_off_skips_a_range_post():
    """Download Packs off: a #1-3 pack is not taken, so nothing is covered."""
    searched, results = _run_pack_sim(
        "S1 #1-3", [{"label": None, "links": {"pixeldrain": "https://pixeldrain.com/u/a"}}],
        ["1", "2", "3"])
    assert searched == ["1", "2", "3"]
    assert [r["status"] for r in results] == ["pack_skipped"] * 3
    assert all(r["best_fallback"] is None and r["best_accept"] is None for r in results)
    assert results[0]["skipped_pack"]["title"] == "S1 #1-3"
    assert results[0]["skipped_pack"]["tier"] == "range fallback"


def test_simulation_with_packs_off_still_takes_a_single_issue_part():
    """Ginseng Roots: the post is titled #1-12, but #11 is a download of its own."""
    parts = [
        {"label": "S1 #1 – 10", "links": {"pixeldrain": "https://getcomics.org/dls/a"}},
        {"label": "S1 #11 (2022)", "links": {"pixeldrain": "https://getcomics.org/dls/b"}},
        {"label": "S1 #12 (2023)", "links": {"pixeldrain": "https://getcomics.org/dls/c"}},
    ]
    searched, results = _run_pack_sim("S1 #1 – 12 (2019-2023)", parts, ["5", "11"])
    assert searched == ["5", "11"]
    by_issue = {r["issue"]: r for r in results}
    assert by_issue["5"]["status"] == "pack_skipped"
    assert by_issue["11"]["status"] == "match_found"
    assert by_issue["11"]["best_fallback"] is not None


def _sim_patches(order):
    """Common patches for a simulation run with a given source priority."""
    return [
        patch("routes.downloads.get_issues_for_series",
              return_value=[{"number": "1", "store_date": "2000-01-01"}]),
        patch("routes.downloads.get_manual_status_for_series", return_value={}),
        patch("routes.downloads.get_series_alias_list", return_value=[]),
        patch("routes.downloads.match_issues_to_collection", return_value={}),
        patch("core.database.get_user_preference", return_value=order),
    ]


def test_simulation_tries_dcpp_before_getcomics():
    """A source ranked above GetComics is searched first and short-circuits it."""
    import contextlib
    import routes.downloads as dl

    getcomics_searched = []
    dcpp_match = {
        "source": "dcpp", "status": "match_found",
        "chosen": {"title": "S1 001", "result_token": "t1", "filename": "S1 1.cbz"},
        "all_results": [{"title": "S1 001", "score": 90, "decision": "ACCEPT"}],
    }

    with contextlib.ExitStack() as stack:
        for p in _sim_patches('["dcpp","getcomics","usenet"]'):
            stack.enter_context(p)
        stack.enter_context(
            patch("routes.downloads.get_all_mapped_series", return_value=[_series(1)]))
        stack.enter_context(
            patch("routes.downloads.search_getcomics_for_issue",
                  side_effect=lambda **kw: getcomics_searched.append(kw) or []))
        stack.enter_context(
            patch("models.dcpp.dcpp_enabled_and_configured", return_value=True))
        stack.enter_context(
            patch("models.usenet.usenet_enabled_and_configured", return_value=False))
        stack.enter_context(
            patch("models.dcpp.try_download_for_issue", return_value=dcpp_match))

        results = dl._run_wanted_simulation(limit=10, target_series_id=None,
                                            target_series_name=None)

    assert getcomics_searched == [], "GetComics ran despite a DC++ match"
    assert any(r["source"] == "dcpp" and r["status"] == "match_found" for r in results)


def test_simulation_skips_dcpp_ranked_below_getcomics():
    """Fallback sources never run in the simulation — it does not submit."""
    import contextlib
    import routes.downloads as dl

    with contextlib.ExitStack() as stack:
        for p in _sim_patches('["getcomics","dcpp"]'):
            stack.enter_context(p)
        stack.enter_context(
            patch("routes.downloads.get_all_mapped_series", return_value=[_series(1)]))
        stack.enter_context(
            patch("routes.downloads.search_getcomics_for_issue", return_value=[]))
        stack.enter_context(
            patch("models.dcpp.dcpp_enabled_and_configured", return_value=True))
        stack.enter_context(
            patch("models.usenet.usenet_enabled_and_configured", return_value=False))
        dcpp_try = stack.enter_context(patch("models.dcpp.try_download_for_issue"))

        dl._run_wanted_simulation(limit=10, target_series_id=None,
                                  target_series_name=None)

    dcpp_try.assert_not_called()
