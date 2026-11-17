"""Tests for cbz_ops/smart_rename.py -- metadata-driven bulk rename."""
import json
import os
from types import SimpleNamespace
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def _mock_rename_deps():
    """Stub the few module-level deps so importing smart_rename is cheap."""
    # Force-import so patch() can resolve the target.
    import cbz_ops.rename  # noqa: F401
    import cbz_ops.smart_rename  # noqa: F401
    with patch("cbz_ops.smart_rename.app_logger"), \
         patch("cbz_ops.rename.app_logger"), \
         patch("cbz_ops.rename.is_hidden", return_value=False):
        yield


def _metron_client(session):
    """A real MetronClient over a fake session (imported late: smart_rename
    keeps models.metron lazy)."""
    from models.metron import MetronClient

    return MetronClient(session)


def _write_series_json(folder, name="Sandman", volume=2, year=1989):
    payload = {
        "metadata": {
            "type": "comicSeries",
            "name": name,
            "volume": volume,
            "year": year,
            "publisher": "Vertigo",
            "status": "Ended",
        }
    }
    with open(os.path.join(folder, "series.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f)


def _write_cvinfo(folder, cv_id=12345):
    with open(os.path.join(folder, "cvinfo"), "w", encoding="utf-8") as f:
        f.write(f"https://comicvine.gamespot.com/volume/4050-{cv_id}/\n")


def _enable_custom_rename(
    pattern="{series_name} {volume_number} {issue_number} ({year})",
    exclude_terms="Annual,Special",
):
    """Patch the pref reader used by smart_rename to enable a custom pattern.

    cbz_ops.rename does ``from core.database import get_user_preference``
    inside functions, so patching ``core.database.get_user_preference`` is
    the safest interception point.
    """
    return patch(
        "core.database.get_user_preference",
        side_effect=lambda key, default=None: {
            "enable_custom_rename": True,
            "custom_rename_pattern": pattern,
            "rename_clean_spaces_enabled": False,
            "rename_char_replacements": {},
            "smart_rename_exclude_terms": exclude_terms,
        }.get(key, default),
    )


class TestPlanSmartRenameSetup:

    def test_missing_directory_returns_error(self):
        from cbz_ops.smart_rename import plan_smart_rename
        with _enable_custom_rename():
            plan = plan_smart_rename("/no/such/dir")
        assert "error" in plan
        assert "does not exist" in plan["error"].lower()

    def test_custom_pattern_disabled_returns_error(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        # Pref reader returns False/empty for every key (default)
        with patch(
            "core.database.get_user_preference",
            side_effect=lambda key, default=None: default,
        ):
            plan = plan_smart_rename(str(tmp_path))
        assert "error" in plan
        assert "Custom rename pattern" in plan["error"]


class TestPlanSmartRenameNeedsCvinfo:

    def test_missing_cvinfo_marks_directory(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "series"
        d.mkdir()
        (d / "Issue 001.cbz").write_bytes(b"x")  # one comic file
        with _enable_custom_rename():
            plan = plan_smart_rename(str(d))
        assert len(plan["directories"]) == 1
        assert plan["directories"][0]["status"] == "needs_cvinfo"
        assert plan["directories"][0]["files"] == []


class TestPlanSmartRenameSeriesJsonPath:

    def test_happy_path_files_get_renamed(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Sandman", volume=2, year=1989)
        (d / "Sandman 1.cbz").write_bytes(b"x")
        (d / "Sandman 12.cbz").write_bytes(b"x")
        (d / "ignore.txt").write_bytes(b"x")

        with _enable_custom_rename():
            plan = plan_smart_rename(str(d), recursive=False)

        assert len(plan["directories"]) == 1
        dir_entry = plan["directories"][0]
        assert dir_entry["status"] == "ok"
        names = [f["new_name"] for f in dir_entry["files"] if f["status"] == "ok"]
        assert "Sandman v2 001 (1989).cbz" in names
        assert "Sandman v2 012 (1989).cbz" in names

    def test_manga_volume_renamed_when_pattern_has_no_issue(self, tmp_path):
        # Volume-only manga (no issue number) must rename when the pattern doesn't
        # reference {issue_number}; the per-file volume ("v03") comes from the
        # filename, series/year from series.json.
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Frieren"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Frieren - Beyond Journey's End", year=2022)
        (d / "Frieren - Beyond Journey's End v03 (2022) (Digital).cbz").write_bytes(b"x")

        with _enable_custom_rename(
            pattern="{series_name} {volume_number} [{volume_year}]"
        ):
            plan = plan_smart_rename(str(d), recursive=False)

        files = plan["directories"][0]["files"]
        assert len(files) == 1
        assert files[0]["status"] == "ok"
        assert files[0]["new_name"] == "Frieren - Beyond Journey's End v03 [2022].cbz"

    def test_file_with_no_issue_number_is_skipped(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d)
        (d / "no-numbers-here.cbz").write_bytes(b"x")

        with _enable_custom_rename():
            plan = plan_smart_rename(str(d), recursive=False)

        files = plan["directories"][0]["files"]
        assert len(files) == 1
        assert files[0]["status"] == "no_issue"

    def test_collision_gets_suffix(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Sandman", volume=2, year=1989)
        # Existing file at the planned target
        (d / "Sandman v2 001 (1989).cbz").write_bytes(b"existing")
        (d / "Sandman 1.cbz").write_bytes(b"x")

        with _enable_custom_rename():
            plan = plan_smart_rename(str(d), recursive=False)

        renames = [f for f in plan["directories"][0]["files"] if f["status"] == "ok"]
        assert len(renames) == 1
        assert renames[0]["new_name"] == "Sandman v2 001 (1989) (2).cbz"

    def test_colon_in_series_name_is_removed_by_default(self, tmp_path):
        """#421: hostile characters are removed unless the character map says otherwise."""
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Batman Year One"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Batman: Year One", volume=1, year=1987)
        (d / "Batman 1.cbz").write_bytes(b"x")

        with _enable_custom_rename():
            plan = plan_smart_rename(str(d), recursive=False)

        names = [f["new_name"] for f in plan["directories"][0]["files"] if f["status"] == "ok"]
        assert names == ["Batman Year One v1 001 (1987).cbz"]

    def test_colon_in_series_name_follows_character_map(self, tmp_path, monkeypatch):
        from cbz_ops import rename
        from cbz_ops.smart_rename import plan_smart_rename
        monkeypatch.setattr(rename, "load_char_map", lambda: {":": " -"})
        d = tmp_path / "Batman Year One"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Batman: Year One", volume=1, year=1987)
        (d / "Batman 1.cbz").write_bytes(b"x")

        with _enable_custom_rename():
            plan = plan_smart_rename(str(d), recursive=False)

        names = [f["new_name"] for f in plan["directories"][0]["files"] if f["status"] == "ok"]
        assert names == ["Batman - Year One v1 001 (1987).cbz"]

    def test_excluded_term_skips_annual(self, tmp_path):
        """Default exclude list ('Annual,Special') keeps Annuals out of the main namespace."""
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Punisher War Zone"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Punisher War Zone", volume=1, year=1992)
        (d / "Punisher War Zone 001.cbz").write_bytes(b"x")
        (d / "Punisher War Zone - Annual 001.cbz").write_bytes(b"x")

        with _enable_custom_rename():
            plan = plan_smart_rename(str(d), recursive=False)

        files = plan["directories"][0]["files"]
        statuses = {f["old_name"]: f["status"] for f in files}
        assert statuses["Punisher War Zone 001.cbz"] == "ok"
        assert statuses["Punisher War Zone - Annual 001.cbz"] == "excluded_term"
        excluded = next(f for f in files if f["status"] == "excluded_term")
        assert excluded["matched_term"] == "annual"

    def test_excluded_term_case_insensitive(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Sandman", volume=2, year=1989)
        (d / "Sandman SPECIAL 5.cbz").write_bytes(b"x")

        with _enable_custom_rename(exclude_terms="Special"):
            plan = plan_smart_rename(str(d), recursive=False)

        files = plan["directories"][0]["files"]
        assert files[0]["status"] == "excluded_term"
        assert files[0]["matched_term"] == "special"

    def test_empty_exclude_terms_disables_filter(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Punisher War Zone"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Punisher War Zone", volume=1, year=1992)
        (d / "Punisher War Zone - Annual 001.cbz").write_bytes(b"x")

        with _enable_custom_rename(exclude_terms=""):
            plan = plan_smart_rename(str(d), recursive=False)

        files = plan["directories"][0]["files"]
        assert files[0]["status"] == "ok"
        assert files[0]["new_name"] == "Punisher War Zone v1 001 (1992).cbz"

    def test_recursive_walks_subdirs(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        # Subdir A
        a = tmp_path / "Sandman v1"
        a.mkdir()
        _write_cvinfo(a, cv_id=1)
        _write_series_json(a, name="Sandman", volume=1, year=1989)
        (a / "Sandman 1.cbz").write_bytes(b"x")
        # Subdir B
        b = tmp_path / "Sandman v2"
        b.mkdir()
        _write_cvinfo(b, cv_id=2)
        _write_series_json(b, name="Sandman", volume=2, year=2022)
        (b / "Sandman 1.cbz").write_bytes(b"x")
        # Empty top-level dir (no comic files) -- should be skipped

        with _enable_custom_rename():
            plan = plan_smart_rename(str(tmp_path), recursive=True)

        # Two directories with comics, neither one is the root (which has no comics).
        dirs = {d["dir"]: d for d in plan["directories"]}
        assert str(a) in dirs and str(b) in dirs
        a_names = [f["new_name"] for f in dirs[str(a)]["files"] if f["status"] == "ok"]
        b_names = [f["new_name"] for f in dirs[str(b)]["files"] if f["status"] == "ok"]
        assert a_names == ["Sandman v1 001 (1989).cbz"]
        assert b_names == ["Sandman v2 001 (2022).cbz"]


class TestPlanSmartRenameAutoCreateSeriesJson:

    def test_metron_provider_creates_series_json(self, tmp_path):
        """When series.json is missing, provider lookup builds and writes it."""
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d, cv_id=999)
        (d / "Sandman 5.cbz").write_bytes(b"x")

        # Fake the Metron series object returned by api.series(metron_id).
        # Use SimpleNamespace so getattr() on unset fields returns AttributeError
        # (instead of MagicMock leaking non-JSON-serializable values into series.json).
        series_obj = SimpleNamespace(
            id=42,
            cv_id=999,
            name="Sandman",
            volume=1,
            year_began=1989,
            year_end=1996,
            publisher=SimpleNamespace(name="Vertigo"),
            imprint=None,
            desc="",
            issue_count=75,
            status="Ended",
            cover_image=None,
            image=None,
        )

        fake_api = MagicMock()
        fake_api.series.return_value = series_obj

        fake_metron = MagicMock()
        fake_metron.is_metron_configured.return_value = True
        # A real MetronClient over the fake session, so the call policy runs.
        fake_metron.get_flask_api.return_value = _metron_client(fake_api)
        fake_metron.get_series_id.return_value = 42

        with _enable_custom_rename(), \
             patch("cbz_ops.smart_rename._get_metron_mod", return_value=fake_metron), \
             patch("core.database.get_library_providers",
                   return_value=[{"provider_type": "metron", "enabled": True}]):
            plan = plan_smart_rename(str(d), recursive=False, library_id=1)

        assert (d / "series.json").exists()
        dir_entry = plan["directories"][0]
        assert dir_entry["status"] == "ok"
        renames = [f for f in dir_entry["files"] if f["status"] == "ok"]
        assert renames[0]["new_name"] == "Sandman v1 005 (1989).cbz"

    def test_all_providers_fail_marks_series_json_failed(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        (d / "Sandman 5.cbz").write_bytes(b"x")

        fake_metron = MagicMock()
        fake_metron.is_metron_configured.return_value = False

        with _enable_custom_rename(), \
             patch("cbz_ops.smart_rename._get_metron_mod", return_value=fake_metron), \
             patch("core.database.get_library_providers",
                   return_value=[{"provider_type": "metron", "enabled": True}]):
            plan = plan_smart_rename(str(d), recursive=False, library_id=1)

        assert plan["directories"][0]["status"] == "series_json_failed"

    def _metron_patches(self, cv_id=999, **series_overrides):
        """Build the standard Metron provider patches for these tests."""
        series_fields = dict(
            id=42, cv_id=cv_id, name="Sandman", volume=1, year_began=1989,
            year_end=1996, publisher=SimpleNamespace(name="Vertigo"),
            imprint=None, desc="", issue_count=75, status="Ended",
            cover_image=None, image=None,
        )
        series_fields.update(series_overrides)
        series_obj = SimpleNamespace(**series_fields)
        fake_api = MagicMock()
        fake_api.series.return_value = series_obj
        fake_metron = MagicMock()
        fake_metron.is_metron_configured.return_value = True
        # A real MetronClient over the fake session, so the call policy runs.
        fake_metron.get_flask_api.return_value = _metron_client(fake_api)
        fake_metron.get_series_id.return_value = 42
        return fake_metron

    def test_persist_failure_still_renames_with_warning(self, tmp_path):
        """When the provider resolves but series.json can't be written, the
        rename still proceeds from in-memory metadata and surfaces the reason."""
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d, cv_id=999)
        (d / "Sandman 5.cbz").write_bytes(b"x")

        fake_metron = self._metron_patches()

        with _enable_custom_rename(), \
             patch("cbz_ops.smart_rename._get_metron_mod", return_value=fake_metron), \
             patch("cbz_ops.smart_rename.write_series_json",
                   return_value=(False, "Permission denied")), \
             patch("core.database.get_library_providers",
                   return_value=[{"provider_type": "metron", "enabled": True}]):
            plan = plan_smart_rename(str(d), recursive=False, library_id=1)

        dir_entry = plan["directories"][0]
        assert dir_entry["status"] == "ok"
        assert "Permission denied" in dir_entry["warning"]
        renames = [f for f in dir_entry["files"] if f["status"] == "ok"]
        assert renames[0]["new_name"] == "Sandman v1 005 (1989).cbz"

    def test_provider_series_without_name_fails(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d, cv_id=999)
        (d / "Sandman 5.cbz").write_bytes(b"x")

        fake_metron = self._metron_patches(name=None)

        with _enable_custom_rename(), \
             patch("cbz_ops.smart_rename._get_metron_mod", return_value=fake_metron), \
             patch("core.database.get_library_providers",
                   return_value=[{"provider_type": "metron", "enabled": True}]):
            plan = plan_smart_rename(str(d), recursive=False, library_id=1)

        dir_entry = plan["directories"][0]
        assert dir_entry["status"] == "series_json_failed"
        assert "no name" in dir_entry["reason"]


def _write_cbz_with_comicinfo(path, year=None, month=None, title=None):
    """Write a minimal .cbz containing a ComicInfo.xml with the given fields."""
    import zipfile
    parts = ["<?xml version='1.0' encoding='utf-8'?>", "<ComicInfo>"]
    if title is not None:
        parts.append(f"<Title>{title}</Title>")
    if year is not None:
        parts.append(f"<Year>{year}</Year>")
    if month is not None:
        parts.append(f"<Month>{month}</Month>")
    parts.append("</ComicInfo>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("page01.jpg", b"fake")
        z.writestr("ComicInfo.xml", "".join(parts))


class TestSmartRenameIssueTokens:
    """Issue-level tokens ({issue_year}, {issue_title}) read from ComicInfo."""

    def test_issue_year_from_comicinfo(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Sandman", volume=2, year=1989)
        _write_cbz_with_comicinfo(d / "Sandman 5.cbz", year=2026, month=3, title="Passengers")

        pattern = "{series_name} - {issue_number} ({issue_year})"
        with _enable_custom_rename(pattern=pattern):
            plan = plan_smart_rename(str(d), recursive=False)

        names = [f["new_name"] for f in plan["directories"][0]["files"] if f["status"] == "ok"]
        # {issue_year} = ComicInfo Year (2026), distinct from the series year (1989)
        assert names == ["Sandman - 005 (2026).cbz"]

    def test_issue_pad_width_honored(self, tmp_path):
        # Configurable leading-zero width flows through smart rename (4-digit here).
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Avengers"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Avengers", volume=2, year=1996)
        _write_cbz_with_comicinfo(d / "Avengers 44.cbz", year=1996, month=3)

        pattern = "{series_name} {issue_number} ({issue_year})"
        with _enable_custom_rename(pattern=pattern), \
             patch("cbz_ops.smart_rename.load_issue_pad_width", return_value=4):
            plan = plan_smart_rename(str(d), recursive=False)

        names = [f["new_name"] for f in plan["directories"][0]["files"] if f["status"] == "ok"]
        assert names == ["Avengers 0044 (1996).cbz"]

    def test_decimal_issue_suffix_preserved(self, tmp_path):
        # Point issues ("12.1", "1.MU") must keep their suffix end-to-end, not
        # truncate to the integer. The stem has no file extension, so this also
        # guards the ".1"-as-extension edge case in extract_comic_values.
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Avengers"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Avengers", volume=2, year=2016)
        _write_cbz_with_comicinfo(d / "Avengers 12.1.cbz", year=2017, month=3)
        _write_cbz_with_comicinfo(d / "Avengers 1.MU.cbz", year=2017, month=3)

        pattern = "{series_name} - {issue_number} ({issue_year})"
        with _enable_custom_rename(pattern=pattern):
            plan = plan_smart_rename(str(d), recursive=False)

        names = sorted(f["new_name"] for f in plan["directories"][0]["files"] if f["status"] == "ok")
        assert names == ["Avengers - 001.MU (2017).cbz", "Avengers - 012.1 (2017).cbz"]

    def test_missing_comicinfo_drops_issue_year_no_series_year(self, tmp_path):
        # {issue_year} must NOT fall back to the series start year.
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Sandman", volume=2, year=1989)
        (d / "Sandman 5.cbz").write_bytes(b"x")  # no ComicInfo.xml

        pattern = "{series_name} - {issue_number} ({issue_year})"
        with _enable_custom_rename(pattern=pattern):
            plan = plan_smart_rename(str(d), recursive=False)

        names = [f["new_name"] for f in plan["directories"][0]["files"] if f["status"] == "ok"]
        # Year group drops cleanly; the series year (1989) must not appear.
        assert names == ["Sandman - 005.cbz"]

    def test_retired_cover_token_dropped(self, tmp_path):
        # A retired {cover_year}/{cover_month_M} in a saved pattern is stripped.
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Sandman", volume=2, year=1989)
        _write_cbz_with_comicinfo(d / "Sandman 5.cbz", year=2026, month=3)

        pattern = "{series_name} - {issue_number} ({cover_month_M}, {cover_year})"
        with _enable_custom_rename(pattern=pattern):
            plan = plan_smart_rename(str(d), recursive=False)

        names = [f["new_name"] for f in plan["directories"][0]["files"] if f["status"] == "ok"]
        assert names == ["Sandman - 005.cbz"]

    def test_issue_month_from_comicinfo(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Sandman", volume=2, year=1989)
        _write_cbz_with_comicinfo(d / "Sandman 5.cbz", year=2026, month=3)

        pattern = "{series_name} - {issue_number} ({issue_year}-{issue_month_m})"
        with _enable_custom_rename(pattern=pattern):
            plan = plan_smart_rename(str(d), recursive=False)

        names = [f["new_name"] for f in plan["directories"][0]["files"] if f["status"] == "ok"]
        assert names == ["Sandman - 005 (2026-03).cbz"]

    def test_issue_title_from_comicinfo(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Sandman", volume=2, year=1989)
        _write_cbz_with_comicinfo(d / "Sandman 5.cbz", year=2026, title="The Sound of Her Wings")

        pattern = "{series_name} {issue_number} - {issue_title}"
        with _enable_custom_rename(pattern=pattern):
            plan = plan_smart_rename(str(d), recursive=False)

        names = [f["new_name"] for f in plan["directories"][0]["files"] if f["status"] == "ok"]
        assert names == ["Sandman 005 - The Sound of Her Wings.cbz"]

    def test_bare_year_filenames_keep_issue_and_year(self, tmp_path):
        # "Series ### YYYY" (unparenthesized year, no ComicInfo). The trailing
        # year used to be read as the issue number, so every file collapsed to
        # "Series <year>.cbz" and collided into " (2)", " (3)" suffixes.
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Poe"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Star Wars - Poe Dameron", volume=1, year=2016)
        for stem in (
            "Star Wars - Poe Dameron 001 2016",
            "Star Wars - Poe Dameron 006 2016",
            "Star Wars - Poe Dameron 007- 2016",
            "Star Wars - Poe Dameron 008 2017",
        ):
            (d / f"{stem}.cbz").write_bytes(b"x")

        pattern = "{series_name} {issue_number} ({issue_year})"
        with _enable_custom_rename(pattern=pattern):
            plan = plan_smart_rename(str(d), recursive=False)

        names = sorted(
            f["new_name"] for f in plan["directories"][0]["files"] if f["status"] == "ok"
        )
        assert names == [
            "Star Wars - Poe Dameron 001 (2016).cbz",
            "Star Wars - Poe Dameron 006 (2016).cbz",
            "Star Wars - Poe Dameron 007 (2016).cbz",
            "Star Wars - Poe Dameron 008 (2017).cbz",
        ]

    def test_comicinfo_year_wins_over_filename_year(self, tmp_path):
        # The filename year is only a fallback; ComicInfo stays authoritative.
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Sandman", volume=2, year=1989)
        _write_cbz_with_comicinfo(d / "Sandman 5 2016.cbz", year=2026, month=3)

        pattern = "{series_name} - {issue_number} ({issue_year})"
        with _enable_custom_rename(pattern=pattern):
            plan = plan_smart_rename(str(d), recursive=False)

        names = [f["new_name"] for f in plan["directories"][0]["files"] if f["status"] == "ok"]
        assert names == ["Sandman - 005 (2026).cbz"]


class TestApplySmartRename:

    def test_apply_renames_files_on_disk(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename, apply_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Sandman", volume=2, year=1989)
        (d / "Sandman 1.cbz").write_bytes(b"x")
        (d / "Sandman 2.cbz").write_bytes(b"x")

        with _enable_custom_rename():
            plan = plan_smart_rename(str(d), recursive=False)
            summary = apply_smart_rename(plan)

        assert summary["renamed"] == 2
        assert summary["failed"] == 0
        assert (d / "Sandman v2 001 (1989).cbz").exists()
        assert (d / "Sandman v2 002 (1989).cbz").exists()
        assert not (d / "Sandman 1.cbz").exists()

    def test_apply_skips_non_ok_entries(self, tmp_path):
        from cbz_ops.smart_rename import apply_smart_rename
        # Hand-built plan with a no-issue entry
        plan = {
            "directories": [
                {
                    "dir": str(tmp_path),
                    "status": "ok",
                    "files": [
                        {"status": "no_issue", "old_path": str(tmp_path / "a.cbz"),
                         "old_name": "a.cbz"},
                    ],
                }
            ]
        }
        summary = apply_smart_rename(plan)
        assert summary["renamed"] == 0
        assert summary["skipped"] >= 1


def _plan(directory, *pairs):
    """A hand-built apply plan renaming each (old_name, new_name) in *directory*."""
    return {
        "root": str(directory),
        "directories": [
            {
                "dir": str(directory),
                "status": "ok",
                "files": [
                    {
                        "status": "ok",
                        "old_path": str(directory / old),
                        "old_name": old,
                        "new_path": str(directory / new),
                        "new_name": new,
                    }
                    for old, new in pairs
                ],
            }
        ],
    }


@pytest.fixture
def index_hook():
    """Stand in for ``app.update_index_on_move`` (app.py cannot be imported in
    tests), recording every bookkeeping call apply_smart_rename makes."""
    import sys
    import types

    calls = []
    fake_app = types.ModuleType("app")
    fake_app.update_index_on_move = lambda old, new: calls.append((old, new))
    with patch.dict(sys.modules, {"app": fake_app}):
        yield calls


class TestApplyNeverOverwrites:
    """The preview is computed when the modal opens and applied whenever the
    user clicks Apply. A comic that lands at a planned name in between used to
    be silently replaced by os.rename on POSIX."""

    def test_destination_that_appeared_after_preview_is_left_alone(self, tmp_path, index_hook):
        from cbz_ops.smart_rename import plan_smart_rename, apply_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Sandman", volume=2, year=1989)
        (d / "Sandman 1.cbz").write_bytes(b"source bytes")
        (d / "Sandman 2.cbz").write_bytes(b"second issue")

        with _enable_custom_rename():
            plan = plan_smart_rename(str(d), recursive=False)
        # A download lands at the planned name between preview and apply.
        (d / "Sandman v2 001 (1989).cbz").write_bytes(b"newcomer bytes")

        summary = apply_smart_rename(plan)

        assert (d / "Sandman 1.cbz").read_bytes() == b"source bytes"
        assert (d / "Sandman v2 001 (1989).cbz").read_bytes() == b"newcomer bytes"
        assert summary["renamed"] == 1
        assert summary["failed"] == 0
        assert summary["skipped"] == 1
        [c] = summary["conflicts"]
        assert c["old_name"] == "Sandman 1.cbz"
        assert c["new_name"] == "Sandman v2 001 (1989).cbz"
        assert "already exists" in c["reason"]
        # The other item still renamed, and only it had its references moved.
        assert (d / "Sandman v2 002 (1989).cbz").read_bytes() == b"second issue"
        assert index_hook == [
            (str(d / "Sandman 2.cbz"), str(d / "Sandman v2 002 (1989).cbz"))
        ]

    def test_duplicate_targets_in_one_batch(self, tmp_path, index_hook):
        from cbz_ops.smart_rename import apply_smart_rename
        (tmp_path / "a.cbz").write_bytes(b"A")
        (tmp_path / "b.cbz").write_bytes(b"B")

        summary = apply_smart_rename(
            _plan(tmp_path, ("a.cbz", "Same 001.cbz"), ("b.cbz", "Same 001.cbz"))
        )

        assert (tmp_path / "Same 001.cbz").read_bytes() == b"A"
        assert (tmp_path / "b.cbz").read_bytes() == b"B"
        assert summary["renamed"] == 1
        [c] = summary["conflicts"]
        assert c["old_name"] == "b.cbz"
        assert "a.cbz" in c["reason"]
        assert index_hook == [(str(tmp_path / "a.cbz"), str(tmp_path / "Same 001.cbz"))]

    def test_chain_is_renamed_in_a_safe_order(self, tmp_path, index_hook):
        from cbz_ops.smart_rename import apply_smart_rename
        (tmp_path / "A.cbz").write_bytes(b"A")
        (tmp_path / "B.cbz").write_bytes(b"B")

        # A -> B is listed first, while B still occupies its target.
        summary = apply_smart_rename(
            _plan(tmp_path, ("A.cbz", "B.cbz"), ("B.cbz", "C.cbz"))
        )

        assert summary["renamed"] == 2
        assert summary["conflicts"] == []
        assert (tmp_path / "B.cbz").read_bytes() == b"A"
        assert (tmp_path / "C.cbz").read_bytes() == b"B"
        assert not (tmp_path / "A.cbz").exists()
        assert index_hook == [
            (str(tmp_path / "B.cbz"), str(tmp_path / "C.cbz")),
            (str(tmp_path / "A.cbz"), str(tmp_path / "B.cbz")),
        ]

    def test_cycle_is_refused_without_touching_either_file(self, tmp_path, index_hook):
        from cbz_ops.smart_rename import apply_smart_rename
        (tmp_path / "A.cbz").write_bytes(b"A")
        (tmp_path / "B.cbz").write_bytes(b"B")

        summary = apply_smart_rename(
            _plan(tmp_path, ("A.cbz", "B.cbz"), ("B.cbz", "A.cbz"))
        )

        assert summary["renamed"] == 0
        assert len(summary["conflicts"]) == 2
        assert (tmp_path / "A.cbz").read_bytes() == b"A"
        assert (tmp_path / "B.cbz").read_bytes() == b"B"
        assert index_hook == []

    def test_case_only_rename_still_succeeds(self, tmp_path, index_hook):
        from cbz_ops.smart_rename import apply_smart_rename
        (tmp_path / "sandman 001.cbz").write_bytes(b"S")

        summary = apply_smart_rename(_plan(tmp_path, ("sandman 001.cbz", "Sandman 001.cbz")))

        assert summary["renamed"] == 1
        assert summary["conflicts"] == []
        assert os.listdir(tmp_path) == ["Sandman 001.cbz"]
        assert (tmp_path / "Sandman 001.cbz").read_bytes() == b"S"
        assert index_hook == [
            (str(tmp_path / "sandman 001.cbz"), str(tmp_path / "Sandman 001.cbz"))
        ]

    def test_normal_rename_runs_bookkeeping(self, tmp_path, index_hook):
        from cbz_ops.smart_rename import plan_smart_rename, apply_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Sandman", volume=2, year=1989)
        (d / "Sandman 1.cbz").write_bytes(b"one")

        with _enable_custom_rename():
            plan = plan_smart_rename(str(d), recursive=False)
        summary = apply_smart_rename(plan)

        assert summary["renamed"] == 1
        assert summary["conflicts"] == []
        assert (d / "Sandman v2 001 (1989).cbz").read_bytes() == b"one"
        assert index_hook == [(str(d / "Sandman 1.cbz"), str(d / "Sandman v2 001 (1989).cbz"))]


class TestRenameNoClobberPrimitives:
    """Each tier of _rename_no_clobber. Windows (where the suite runs locally)
    takes the os.rename branch, so the POSIX tiers are exercised directly."""

    def test_link_then_unlink_renames(self, tmp_path):
        from cbz_ops.smart_rename import _link_then_unlink
        src, dst = tmp_path / "a.cbz", tmp_path / "b.cbz"
        src.write_bytes(b"A")
        assert _link_then_unlink(str(src), str(dst)) is True
        assert not src.exists()
        assert dst.read_bytes() == b"A"

    def test_link_then_unlink_refuses_existing(self, tmp_path):
        from cbz_ops.smart_rename import _link_then_unlink
        src, dst = tmp_path / "a.cbz", tmp_path / "b.cbz"
        src.write_bytes(b"A")
        dst.write_bytes(b"B")
        with pytest.raises(FileExistsError):
            _link_then_unlink(str(src), str(dst))
        assert src.read_bytes() == b"A"
        assert dst.read_bytes() == b"B"

    def test_link_unsupported_reports_false(self, tmp_path):
        from cbz_ops.smart_rename import _link_then_unlink
        src, dst = tmp_path / "a.cbz", tmp_path / "b.cbz"
        src.write_bytes(b"A")
        with patch("cbz_ops.smart_rename.os.link",
                   side_effect=PermissionError(1, "Operation not permitted")):
            assert _link_then_unlink(str(src), str(dst)) is False
        assert src.read_bytes() == b"A"
        assert not dst.exists()

    def test_failed_unlink_removes_the_new_link(self, tmp_path):
        from cbz_ops.smart_rename import _link_then_unlink
        src, dst = tmp_path / "a.cbz", tmp_path / "b.cbz"
        src.write_bytes(b"A")
        real_unlink = os.unlink

        def unlink(path):
            if path == str(src):
                raise PermissionError(13, "Permission denied")
            real_unlink(path)

        with patch("cbz_ops.smart_rename.os.unlink", side_effect=unlink):
            with pytest.raises(PermissionError):
                _link_then_unlink(str(src), str(dst))
        assert src.read_bytes() == b"A"
        assert not dst.exists()

    def _posix(self):
        """Run _rename_no_clobber's POSIX path on any platform."""
        import cbz_ops.smart_rename as sr
        fake_os = MagicMock(wraps=os)
        fake_os.name = "posix"
        fake_os.path = os.path
        return patch.object(sr, "os", fake_os)

    def test_posix_falls_back_to_link_when_renameat2_unavailable(self, tmp_path):
        import cbz_ops.smart_rename as sr
        src, dst = tmp_path / "a.cbz", tmp_path / "b.cbz"
        src.write_bytes(b"A")
        with self._posix(), patch.object(sr, "_renameat2_noreplace", return_value=False), \
             patch.object(sr, "_link_then_unlink", wraps=sr._link_then_unlink) as link:
            sr._rename_no_clobber(str(src), str(dst))
        link.assert_called_once()
        assert dst.read_bytes() == b"A"

    def test_posix_without_hard_links_falls_back_to_rename(self, tmp_path):
        import cbz_ops.smart_rename as sr
        src, dst = tmp_path / "a.cbz", tmp_path / "b.cbz"
        src.write_bytes(b"A")
        with self._posix(), patch.object(sr, "_renameat2_noreplace", return_value=False), \
             patch.object(sr, "_link_then_unlink", return_value=False):
            sr._rename_no_clobber(str(src), str(dst))
        assert not src.exists()
        assert dst.read_bytes() == b"A"

    def test_renameat2_eexist_raises_file_exists(self, tmp_path):
        import errno
        import cbz_ops.smart_rename as sr

        fake = MagicMock(return_value=-1)
        with patch.object(sr, "_load_renameat2", return_value=fake), \
             patch("ctypes.get_errno", return_value=errno.EEXIST):
            with pytest.raises(FileExistsError):
                sr._renameat2_noreplace(str(tmp_path / "a"), str(tmp_path / "b"))

    def test_renameat2_unsupported_by_filesystem_reports_false(self, tmp_path):
        import errno
        import cbz_ops.smart_rename as sr

        fake = MagicMock(return_value=-1)
        with patch.object(sr, "_load_renameat2", return_value=fake), \
             patch("ctypes.get_errno", return_value=errno.EINVAL):
            assert sr._renameat2_noreplace(str(tmp_path / "a"), str(tmp_path / "b")) is False

    def test_renameat2_success(self, tmp_path):
        import cbz_ops.smart_rename as sr

        fake = MagicMock(return_value=0)
        with patch.object(sr, "_load_renameat2", return_value=fake):
            assert sr._renameat2_noreplace(str(tmp_path / "a"), str(tmp_path / "b")) is True
        args = fake.call_args.args
        assert args[4] == sr._RENAME_NOREPLACE


def _client(library):
    """A real ComicVineClient over a mock Simyan, so the guard runs."""
    from models.comicvine import ComicVineClient

    return ComicVineClient(library)


class TestBuildComicvineSeriesDict:
    """Regression cover for a fallback that never worked: this built its own
    Simyan client with `cache=None`, which Simyan 3.x rejects with TypeError,
    and the bare `except Exception` swallowed it."""

    def _volume(self):
        return SimpleNamespace(
            name="Batman",
            start_year=2016,
            publisher=SimpleNamespace(name="DC Comics"),
            image=SimpleNamespace(original_url="http://img/batman.jpg"),
            issue_count=85,
            description="The Dark Knight.",
        )

    def test_maps_a_volume_to_the_series_json_shape(self):
        import cbz_ops.smart_rename as sr

        cv = MagicMock()
        cv.get_volume.return_value = self._volume()
        with patch("models.comicvine.is_simyan_available", return_value=True), \
             patch("models.comicvine.get_cv_client", return_value=_client(cv)):
            result = sr._build_comicvine_series_dict("KEY", 4050)

        assert result["name"] == "Batman"
        assert result["year_began"] == 2016
        assert result["publisher"] == "DC Comics"
        assert result["issue_count"] == 85
        assert result["image"] == "http://img/batman.jpg"
        assert result["cv_id"] == 4050

    def test_uses_the_shared_client_and_never_passes_cache(self):
        """The exact bug: a locally-built Comicvine(cache=None) raises TypeError
        on Simyan 3.x, so the ComicVine fallback silently did nothing."""
        import cbz_ops.smart_rename as sr
        import models.comicvine as cv_module

        cv = MagicMock()
        cv.get_volume.return_value = self._volume()
        with patch("models.comicvine.is_simyan_available", return_value=True), \
             patch("models.comicvine.get_cv_client", return_value=_client(cv)) as get_client, \
             patch.object(cv_module, "Comicvine", create=True) as raw_ctor:
            sr._build_comicvine_series_dict("KEY", 4050)

        get_client.assert_called_once_with("KEY")
        raw_ctor.assert_not_called()

    def test_goes_through_the_call_policy(self):
        import cbz_ops.smart_rename as sr
        import models.comicvine as cv_module

        cv = MagicMock()
        cv.get_volume.return_value = self._volume()
        with patch("models.comicvine.is_simyan_available", return_value=True), \
             patch("models.comicvine.get_cv_client", return_value=_client(cv)), \
             patch.object(cv_module._cv_policy, "call",
                          wraps=cv_module._cv_policy.call) as policy_call:
            sr._build_comicvine_series_dict("KEY", 4050)

        policy_call.assert_called_once()
        cv.get_volume.assert_called_once_with(4050)

    def test_missing_volume_returns_none(self):
        import cbz_ops.smart_rename as sr

        cv = MagicMock()
        cv.get_volume.return_value = None
        with patch("models.comicvine.is_simyan_available", return_value=True), \
             patch("models.comicvine.get_cv_client", return_value=_client(cv)):
            assert sr._build_comicvine_series_dict("KEY", 4050) is None

    def test_simyan_unavailable_returns_none(self):
        import cbz_ops.smart_rename as sr

        with patch("models.comicvine.is_simyan_available", return_value=False), \
             patch("models.comicvine.get_cv_client") as get_client:
            assert sr._build_comicvine_series_dict("KEY", 4050) is None

        get_client.assert_not_called()

    def test_api_error_returns_none_without_raising(self):
        import cbz_ops.smart_rename as sr

        cv = MagicMock()
        cv.get_volume.side_effect = RuntimeError("ComicVine down")
        with patch("models.comicvine.is_simyan_available", return_value=True), \
             patch("models.comicvine.get_cv_client", return_value=_client(cv)):
            assert sr._build_comicvine_series_dict("KEY", 4050) is None
