"""Source Wall: setting the provider of files whose provider is Unknown."""
import pathlib
import types
import zipfile
from unittest.mock import patch

import pytest

from core.comicinfo import (
    NOTES_LABELS, SETTABLE_PROVIDERS, notes_for_provider, provider_from_notes,
    read_comicinfo_from_zip,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------- core helper

class TestNotesForProvider:

    @pytest.mark.parametrize("provider", SETTABLE_PROVIDERS)
    def test_round_trips_through_the_scanner(self, provider):
        """The stamp must read back as the provider the user picked, or the
        next rescan would quietly turn the file Unknown again."""
        assert provider_from_notes(notes_for_provider(provider)) == provider

    @pytest.mark.parametrize("provider", sorted(NOTES_LABELS))
    def test_round_trips_with_free_text_kept(self, provider):
        notes = notes_for_provider(provider, "Bought at SDCC")
        assert provider_from_notes(notes) == provider
        assert notes.endswith("Bought at SDCC")

    def test_scraper_marker_is_dropped(self):
        notes = notes_for_provider("metron", "Scraped metadata from Amazon")
        assert "Amazon" not in notes and provider_from_notes(notes) == "metron"

    def test_manual_uses_the_manual_stamp(self):
        assert provider_from_notes(notes_for_provider("manual", "anything")) == "manual"

    def test_unknown_provider_rejected(self):
        with pytest.raises(ValueError):
            notes_for_provider("nope")


# ---------------------------------------------------------------- the route

def _cbz(path, notes):
    xml = "<ComicInfo><Series>X</Series>"
    if notes is not None:
        xml += f"<Notes>{notes}</Notes>"
    xml += "</ComicInfo>"
    with zipfile.ZipFile(str(path), "w") as zf:
        zf.writestr("page1.png", b"img")
        zf.writestr("ComicInfo.xml", xml)
    return str(path)


def _index(db, path, provider, has_ci=1):
    from core.database import add_file_index_entry
    add_file_index_entry(name=path.replace("\\", "/").rsplit("/", 1)[-1], path=path,
                         entry_type="file", size=1, parent=str(pathlib.Path(path).parent))
    db.execute("UPDATE file_index SET ci_provider=?, has_comicinfo=? WHERE path=?",
               (provider, has_ci, path))
    db.commit()


class _InlineThread:
    """Run the worker immediately so the test can assert on its effects."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._t, self._a, self._k = target, args, kwargs or {}

    def start(self):
        self._t(*self._a, **self._k)


@pytest.fixture
def inline(client):
    with patch("routes.source_wall.is_valid_library_path", return_value=True), \
         patch("routes.source_wall.threading", types.SimpleNamespace(Thread=_InlineThread)):
        yield client


def _provider_in_db(db, path):
    return db.execute("SELECT ci_provider FROM file_index WHERE path=?", (path,)).fetchone()[0]


class TestSetProviderRoute:

    def test_sets_notes_and_index_for_unknown_files(self, inline, db_connection, tmp_path):
        a = _cbz(tmp_path / "a.cbz", "just a note")
        b = _cbz(tmp_path / "b.cbz", None)
        _index(db_connection, a, "")
        _index(db_connection, b, "")

        r = inline.post("/api/source-wall/set-provider",
                        json={"paths": [a, b], "provider": "metron"})
        data = r.get_json()
        assert r.status_code == 200 and data["affected"] == 2 and data["skipped"] == 0

        for p in (a, b):
            assert provider_from_notes(read_comicinfo_from_zip(p).get("Notes")) == "metron"
            assert _provider_in_db(db_connection, p) == "metron"
        assert "just a note" in read_comicinfo_from_zip(a)["Notes"]

    def test_manual_is_settable(self, inline, db_connection, tmp_path):
        a = _cbz(tmp_path / "a.cbz", None)
        _index(db_connection, a, "")
        r = inline.post("/api/source-wall/set-provider", json={"paths": [a], "provider": "manual"})
        assert r.status_code == 200
        assert provider_from_notes(read_comicinfo_from_zip(a).get("Notes")) == "manual"
        assert _provider_in_db(db_connection, a) == "manual"

    def test_known_provider_and_no_xml_are_left_alone(self, inline, db_connection, tmp_path):
        known = _cbz(tmp_path / "k.cbz", "Metadata from ComicVine. Issue ID: 1")
        noxml = _cbz(tmp_path / "n.cbz", None)
        unknown = _cbz(tmp_path / "u.cbz", None)
        _index(db_connection, known, "comicvine")
        _index(db_connection, noxml, "", has_ci=0)
        _index(db_connection, unknown, "")
        before = read_comicinfo_from_zip(known)

        r = inline.post("/api/source-wall/set-provider",
                        json={"paths": [known, noxml, unknown], "provider": "metron"})
        data = r.get_json()
        assert data["affected"] == 1 and data["skipped"] == 2
        assert read_comicinfo_from_zip(known) == before
        assert _provider_in_db(db_connection, known) == "comicvine"
        assert _provider_in_db(db_connection, noxml) == ""

    def test_nothing_eligible_is_a_400(self, inline, db_connection, tmp_path):
        known = _cbz(tmp_path / "k.cbz", "Metadata from Metron. Issue ID: 1")
        _index(db_connection, known, "metron")
        r = inline.post("/api/source-wall/set-provider",
                        json={"paths": [known], "provider": "comicvine"})
        assert r.status_code == 400 and r.get_json()["skipped"] == 1

    def test_failed_write_leaves_the_row_unknown(self, inline, db_connection, tmp_path):
        a = _cbz(tmp_path / "a.cbz", None)
        _index(db_connection, a, "")

        def failing(items, progress_callback=None, max_workers=4):
            for i, (path, _u) in enumerate(items, 1):
                progress_callback(i, len(items), path, RuntimeError("disk full"))

        with patch("core.comicinfo.bulk_update_comicinfo_in_zips", side_effect=failing):
            inline.post("/api/source-wall/set-provider", json={"paths": [a], "provider": "metron"})
        assert _provider_in_db(db_connection, a) == ""

    def test_rejects_bad_input(self, client):
        with patch("routes.source_wall.is_valid_library_path", return_value=True):
            assert client.post("/api/source-wall/set-provider",
                               json={"paths": ["/data/a.cbz"], "provider": "nope"}).status_code == 400
            assert client.post("/api/source-wall/set-provider",
                               json={"paths": [], "provider": "metron"}).status_code == 400
            assert client.post("/api/source-wall/set-provider",
                               json={"paths": [1], "provider": "metron"}).status_code == 400

    def test_path_outside_library_forbidden(self, client):
        with patch("routes.source_wall.is_valid_library_path", return_value=False):
            r = client.post("/api/source-wall/set-provider",
                            json={"paths": ["/etc/a.cbz"], "provider": "metron"})
        assert r.status_code == 403


# ---------------------------------------------------------------- the page

class TestSetProviderUi:

    def test_entered_by_hand_is_now_manual(self):
        for rel in ("static/js/source_wall.js", "static/js/clu-metadata-tools.js"):
            js = _read(rel)
            assert "manual: 'Manual'" in js, rel
            assert "Entered by hand" not in js, rel

    def test_only_unknown_rows_get_a_select(self):
        js = _read("static/js/source_wall.js")
        assert "!!file.has_comicinfo && file.ci_provider === ''" in js
        assert "buildProviderSelect(file)" in js
        assert "/api/source-wall/set-provider" in js

    def test_bulk_bar_has_the_control(self):
        html = _read("templates/source_wall.html")
        assert 'id="swBulkProvider"' in html and "function setProviderForSelectedSW" in _read("static/js/source_wall.js")

    def test_followup_polls_the_single_operation_endpoint(self):
        js = _read("static/js/source_wall.js")
        body = js.split("function waitForProviderSet")[1].split("// ── By-provider view")[0]
        assert "/api/operation/" in body and "/api/operations" not in body.replace("/api/operation/", "")

    def test_provider_is_a_bulk_field_not_a_separate_control(self):
        html = _read("templates/source_wall.html")
        assert '<option value="ci_provider">Provider</option>' in html
        assert 'onchange="onBulkFieldChange()"' in html
        js = _read("static/js/source_wall.js")
        body = js.split("function applyBulkUpdate")[1].split("function onBulkFieldChange")[0]
        assert "field === 'ci_provider'" in body and "setProviderForSelectedSW()" in body

    def test_update_is_inline_with_no_reload_on_success(self):
        js = _read("static/js/source_wall.js")
        body = js.split("function setProviderForFiles")[1].split("// ── By-provider view")[0]
        assert "swApplyProviderLocally(paths, provider)" in body
        assert "swSettleProviderRows(changed)" in body
        # reloadCurrentView is allowed only on the unknown-failure path
        assert body.count("reloadCurrentView()") == 1
        assert body.index("reloadCurrentView()") > body.index("op.status === 'error'")

    def test_shift_range_select_uses_visible_rows_and_a_path_anchor(self):
        js = _read("static/js/source_wall.js")
        assert "swLastSelectedIndex" not in js
        body = js.split("function handleFileSelect")[1].split("function getSortedFiles")[0]
        assert "#swTableBody tr[data-path]" in body and "swLastSelectedPath" in body
