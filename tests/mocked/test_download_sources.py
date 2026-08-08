"""Tests for models/download_sources.py -- ordering shared by all sources."""
import pytest
from unittest.mock import patch

import models.download_sources as ds


class TestGetSourcePriority:

    @patch("core.database.get_user_preference", return_value=None)
    def test_default_is_getcomics_only(self, mock_pref):
        # A fresh install must behave exactly as it did before Usenet/DC++.
        assert ds.get_source_priority() == ["getcomics"]

    @patch("core.database.get_user_preference", return_value='["dcpp","usenet","getcomics"]')
    def test_json_string(self, mock_pref):
        assert ds.get_source_priority() == ["dcpp", "usenet", "getcomics"]

    @patch("core.database.get_user_preference", return_value=["usenet", "getcomics"])
    def test_list_value(self, mock_pref):
        assert ds.get_source_priority() == ["usenet", "getcomics"]

    @patch("core.database.get_user_preference", return_value="not json")
    def test_unparseable_falls_back(self, mock_pref):
        assert ds.get_source_priority() == ["getcomics"]

    @patch("core.database.get_user_preference", side_effect=Exception("db down"))
    def test_error_falls_back(self, mock_pref):
        assert ds.get_source_priority() == ["getcomics"]

    @patch("core.database.get_user_preference", return_value='["getcomics"]')
    def test_default_list_is_not_shared(self, mock_pref):
        # Callers must not be able to mutate the module's default.
        first = ds.get_source_priority()
        first.append("dcpp")
        assert ds.get_source_priority() == ["getcomics"]


class TestRankingAndOrdering:

    @patch("core.database.get_user_preference", return_value='["dcpp","getcomics","usenet"]')
    def test_source_rank(self, mock_pref):
        assert ds.source_rank("dcpp") == 0
        assert ds.source_rank("getcomics") == 1
        assert ds.source_rank("usenet") == 2

    @patch("core.database.get_user_preference", return_value='["getcomics"]')
    def test_unlisted_source_sorts_last(self, mock_pref):
        assert ds.source_rank("dcpp") == 999

    @patch("core.database.get_user_preference", return_value='["dcpp","getcomics"]')
    def test_source_enabled(self, mock_pref):
        assert ds.source_enabled("dcpp") is True
        assert ds.source_enabled("usenet") is False

    @patch("core.database.get_user_preference", return_value='["dcpp","usenet","getcomics"]')
    def test_ordered_sources(self, mock_pref):
        assert ds.ordered_sources() == ["dcpp", "usenet", "getcomics"]
        assert ds.ordered_sources(["getcomics", "dcpp"]) == ["dcpp", "getcomics"]

    @patch("core.database.get_user_preference", return_value='["getcomics","usenet"]')
    def test_ordered_sources_drops_disabled(self, mock_pref):
        assert ds.ordered_sources() == ["getcomics", "usenet"]


class TestSplitAroundGetComics:

    def _split(self, order, sources):
        with patch("core.database.get_user_preference", return_value=order):
            return ds.split_around_getcomics(sources)

    def test_dcpp_first_usenet_fallback(self):
        sources = [("dcpp", "DC++", None), ("usenet", "Usenet", None)]
        pre, post = self._split('["dcpp","getcomics","usenet"]', sources)
        assert [s[0] for s in pre] == ["dcpp"]
        assert [s[0] for s in post] == ["usenet"]

    def test_both_before_getcomics_keeps_priority_order(self):
        sources = [("usenet", "Usenet", None), ("dcpp", "DC++", None)]
        pre, post = self._split('["dcpp","usenet","getcomics"]', sources)
        assert [s[0] for s in pre] == ["dcpp", "usenet"]
        assert post == []

    def test_both_after_getcomics(self):
        sources = [("usenet", "Usenet", None), ("dcpp", "DC++", None)]
        pre, post = self._split('["getcomics","usenet","dcpp"]', sources)
        assert pre == []
        assert [s[0] for s in post] == ["usenet", "dcpp"]

    def test_getcomics_absent_means_everything_runs_first(self):
        # With GetComics unlisted its rank is 999, so every external source
        # outranks it and is tried up front.
        sources = [("usenet", "Usenet", None), ("dcpp", "DC++", None)]
        pre, post = self._split('["dcpp","usenet"]', sources)
        assert [s[0] for s in pre] == ["dcpp", "usenet"]
        assert post == []


class TestGetExternalSources:

    @patch("models.dcpp.dcpp_enabled_and_configured", return_value=True)
    @patch("models.usenet.usenet_enabled_and_configured", return_value=True)
    @patch("core.database.get_user_preference", return_value='["dcpp","getcomics","usenet"]')
    def test_returns_callables_in_priority_order(self, mock_pref, mock_un, mock_dc):
        import models.dcpp
        import models.usenet

        sources = ds.get_external_sources()
        assert [s[0] for s in sources] == ["dcpp", "usenet"]
        assert [s[1] for s in sources] == ["DC++", "Usenet"]
        assert sources[0][2] is models.dcpp.try_download_for_issue
        assert sources[1][2] is models.usenet.try_download_for_issue

    @patch("models.dcpp.dcpp_enabled_and_configured", return_value=False)
    @patch("models.usenet.usenet_enabled_and_configured", return_value=True)
    @patch("core.database.get_user_preference", return_value='["dcpp","usenet","getcomics"]')
    def test_unconfigured_source_is_skipped(self, mock_pref, mock_un, mock_dc):
        assert [s[0] for s in ds.get_external_sources()] == ["usenet"]

    @patch("models.dcpp.dcpp_enabled_and_configured", side_effect=Exception("boom"))
    @patch("models.usenet.usenet_enabled_and_configured", return_value=True)
    @patch("core.database.get_user_preference", return_value='["dcpp","usenet","getcomics"]')
    def test_broken_source_does_not_break_the_run(self, mock_pref, mock_un, mock_dc):
        assert [s[0] for s in ds.get_external_sources()] == ["usenet"]

    @patch("models.dcpp.dcpp_enabled_and_configured", return_value=True)
    @patch("models.usenet.usenet_enabled_and_configured", return_value=True)
    @patch("core.database.get_user_preference", return_value='["getcomics"]')
    def test_sources_not_in_priority_are_excluded(self, mock_pref, mock_un, mock_dc):
        assert ds.get_external_sources() == []


class TestBuildQueries:

    def test_zero_pads_numeric_issues(self):
        assert ds.build_queries("Batman", "1") == ["Batman 1", "Batman 01", "Batman 001"]

    def test_non_numeric_issue(self):
        assert ds.build_queries("Batman", "1.MU") == ["Batman 1.MU"]

    def test_no_issue(self):
        assert ds.build_queries("Batman", None) == ["Batman"]

    def test_shared_with_usenet(self):
        import models.usenet as un
        assert un._build_queries("Batman", "5") == ds.build_queries("Batman", "5")
