"""Containment and folder grants on the routes that WRITE metadata.

Every route that writes ComicInfo.xml, a ``cvinfo``/``series.json`` sidecar,
folder art or a bulk metadata job for a path taken from the request resolves
that path through ``core.path_access`` first: its realpath must lie inside a
library (or WATCH/TARGET where the File Manager uses the route on staging
files) and, inside a library, a Clerk needs a *full* folder grant. A refusal
comes back before anything is written or queued. For a list of paths one
refusal refuses the whole request, as the File Manager's batch operations do.

Metadata has three parallel paths, and each is covered:

- single-file: ``/api/search-metadata``, ``/api/force-metadata-match``, the
  legacy GCD/ComicVine search routes and the CBZ Info modal's writes;
- batch: ``/api/batch-metadata``, ``/api/update-xml``, ``/api/save-cvinfo``,
  ``/cbz-bulk-clear-comicinfo``;
- the bulk orchestrator: ``/api/bulk-metadata/start`` and ``/backfill``, whose
  folder expansion is narrowed to the requester's grants in the request
  (the queued runner has no request user), plus the review/audit routes that
  act on rows another user's job wrote.

Every test builds real directories under ``tmp_path`` and replaces the
library table with them, so nothing depends on ``/data`` existing on the host.
Each test logs in as exactly one user, or none for the single-user cases:
under pytest-flask's shared application context ``g.current_user`` survives
between requests, so switching users inside one test would test that leak.
"""
import ast
import os
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.database import (
    add_library,
    add_review_item,
    create_bulk_job,
    create_user,
    get_bulk_job,
    get_db_connection,
    get_libraries,
    get_user_by_username,
    log_bulk_audit,
    set_user_folders,
    set_user_libraries,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _login(client, username, password):
    resp = client.post("/login", data={"username": username, "password": password})
    assert resp.status_code == 302
    return resp


def _cbz(path, comicinfo=True):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("p01.png", b"fake image")
        if comicinfo:
            zf.writestr("ComicInfo.xml",
                        "<ComicInfo><Series>Original</Series></ComicInfo>")
    return str(path)


def _has_comicinfo(path):
    with zipfile.ZipFile(path) as zf:
        return any(os.path.basename(n).lower() == "comicinfo.xml" for n in zf.namelist())


def _symlink_or_skip(src, dst):
    try:
        os.symlink(src, dst, target_is_directory=os.path.isdir(src))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")


@pytest.fixture
def layout(app, db_connection, monkeypatch, tmp_path):
    """One library with a granted folder and an ungranted sibling (plus a
    sibling whose name extends the grant's), a TARGET and a WATCH outside it,
    and a folder outside everything."""
    monkeypatch.delenv("CLU_USERNAME", raising=False)
    monkeypatch.delenv("CLU_PASSWORD", raising=False)

    root = tmp_path / "LibA"
    grant = root / "Marvel"
    sibling = root / "DC"
    lookalike = root / "Marvel Knights"   # shares the grant's name as a prefix
    target = tmp_path / "processed"       # also app.config["TARGET"]
    watch = tmp_path / "watch"
    outside = tmp_path / "elsewhere"
    for d in (grant, sibling, lookalike, target, watch, outside):
        d.mkdir(parents=True, exist_ok=True)

    class L:
        pass

    L.root, L.grant, L.sibling = str(root), str(grant), str(sibling)
    L.lookalike = str(lookalike)
    L.target, L.watch, L.outside = str(target), str(watch), str(outside)
    L.granted_cbz = _cbz(grant / "XMen 001 (2020).cbz")
    L.sibling_cbz = _cbz(sibling / "Batman 001 (2020).cbz")
    L.lookalike_cbz = _cbz(lookalike / "Daredevil 001 (2020).cbz")
    L.target_cbz = _cbz(target / "Incoming 001 (2020).cbz")
    L.watch_cbz = _cbz(watch / "Watched 001 (2020).cbz")
    L.outside_cbz = _cbz(outside / "Private 001 (2020).cbz")

    monkeypatch.setattr("core.config.get_target_dir", lambda: str(target))
    monkeypatch.setattr("core.config.get_watch_dir", lambda: str(watch))

    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM libraries")
        conn.commit()
    finally:
        conn.close()
    assert add_library("Library A", str(root))
    L.lib_id = get_libraries(enabled_only=True)[0]["id"]
    return L


def _test_login_value(username):
    """A throwaway login value for a test account, derived from its name."""
    return "fake-" + username


@pytest.fixture
def multi_user(layout):
    """An owner and a Clerk who holds only LibA/Marvel."""
    for name, role in (("mw_owner", "owner"), ("mw_clerk", "clerk")):
        create_user(name, password=_test_login_value(name), role=role)
    uid = get_user_by_username("mw_clerk")["id"]
    set_user_libraries(uid, [layout.lib_id])
    set_user_folders(uid, [layout.grant])
    return layout


def _as_clerk(client):
    _login(client, "mw_clerk", _test_login_value("mw_clerk"))


def _as_owner(client):
    _login(client, "mw_owner", _test_login_value("mw_owner"))


def _no_providers():
    """Every provider unconfigured: a cascade ends without a network call."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("models.metron.is_metron_configured", return_value=False))
    stack.enter_context(patch("models.metron.is_connection_error", return_value=False))
    stack.enter_context(patch("models.gcd.is_database_available", return_value=False))
    stack.enter_context(patch("models.gcd.check_database_status",
                              return_value={"gcd_available": False}))
    stack.enter_context(patch("models.gcd.get_connection", return_value=None))
    stack.enter_context(patch("models.comicvine.find_cvinfo_in_folder", return_value=None))
    stack.enter_context(patch("core.database.get_library_providers", return_value=[]))
    return stack


# ---------------------------------------------------------------------------
# Single-file path
# ---------------------------------------------------------------------------
SINGLE_FILE_ROUTES = [
    ("/api/search-metadata", lambda p: {"file_path": p, "file_name": os.path.basename(p)}),
    ("/api/force-metadata-match",
     lambda p: {"file_path": p, "provider": "metron", "issue_id": "1"}),
    ("/search-gcd-metadata", lambda p: {"file_path": p, "file_name": os.path.basename(p)}),
    ("/search-gcd-metadata-with-selection",
     lambda p: {"file_path": p, "file_name": os.path.basename(p),
                "series_id": 1, "issue_number": "1"}),
    ("/search-comicvine-metadata",
     lambda p: {"file_path": p, "file_name": os.path.basename(p)}),
    ("/search-comicvine-metadata-with-selection",
     lambda p: {"file_path": p, "file_name": os.path.basename(p),
                "volume_id": 1, "issue_number": "1"}),
    ("/cbz-clear-comicinfo", lambda p: {"path": p}),
    ("/cbz-update-comicinfo", lambda p: {"path": p, "updates": {"Series": "Changed"}}),
]
SINGLE_IDS = [r for r, _ in SINGLE_FILE_ROUTES]


class TestSingleFileRoutes:
    @pytest.mark.parametrize("route,body", SINGLE_FILE_ROUTES, ids=SINGLE_IDS)
    def test_clerk_refused_ungranted_sibling(self, client, multi_user, route, body):
        _as_clerk(client)
        write = MagicMock()
        with _no_providers(), patch("routes.metadata.add_comicinfo_to_cbz", write):
            resp = client.post(route, json=body(multi_user.sibling_cbz))
        assert resp.status_code == 403
        write.assert_not_called()
        assert _has_comicinfo(multi_user.sibling_cbz)
        # The refusal names nothing about the folder it refused.
        assert multi_user.sibling not in resp.get_data(as_text=True)

    @pytest.mark.parametrize("route,body", SINGLE_FILE_ROUTES, ids=SINGLE_IDS)
    def test_clerk_refused_name_prefix_lookalike(self, client, multi_user, route, body):
        # "Marvel Knights" is not inside the "Marvel" grant.
        _as_clerk(client)
        with _no_providers():
            resp = client.post(route, json=body(multi_user.lookalike_cbz))
        assert resp.status_code == 403

    @pytest.mark.parametrize("route,body", SINGLE_FILE_ROUTES, ids=SINGLE_IDS)
    def test_clerk_granted_file_passes_the_gate(self, client, multi_user, route, body):
        _as_clerk(client)
        with _no_providers(), patch("routes.metadata.add_comicinfo_to_cbz"):
            resp = client.post(route, json=body(multi_user.granted_cbz))
        assert resp.status_code != 403

    @pytest.mark.parametrize("route,body", SINGLE_FILE_ROUTES, ids=SINGLE_IDS)
    def test_owner_unaffected_on_ungranted_sibling(self, client, multi_user, route, body):
        _as_owner(client)
        with _no_providers(), patch("routes.metadata.add_comicinfo_to_cbz"):
            resp = client.post(route, json=body(multi_user.sibling_cbz))
        assert resp.status_code != 403

    @pytest.mark.parametrize("route,body", SINGLE_FILE_ROUTES, ids=SINGLE_IDS)
    def test_outside_every_root_refused_even_for_owner(self, client, multi_user, route, body):
        _as_owner(client)
        write = MagicMock()
        with _no_providers(), patch("routes.metadata.add_comicinfo_to_cbz", write):
            resp = client.post(route, json=body(multi_user.outside_cbz))
        assert resp.status_code == 403
        write.assert_not_called()
        assert _has_comicinfo(multi_user.outside_cbz)

    @pytest.mark.parametrize("route,body", SINGLE_FILE_ROUTES, ids=SINGLE_IDS)
    def test_symlink_in_grant_to_sibling_refused(self, client, multi_user, route, body):
        link = os.path.join(multi_user.grant, "shortcut.cbz")
        _symlink_or_skip(multi_user.sibling_cbz, link)
        _as_clerk(client)
        with _no_providers(), patch("routes.metadata.add_comicinfo_to_cbz"):
            resp = client.post(route, json=body(link))
        assert resp.status_code == 403
        assert _has_comicinfo(multi_user.sibling_cbz)

    def test_clerk_clears_comicinfo_in_granted_file(self, client, multi_user):
        _as_clerk(client)
        resp = client.post("/cbz-clear-comicinfo", json={"path": multi_user.granted_cbz})
        assert resp.status_code == 200
        assert not _has_comicinfo(multi_user.granted_cbz)

    def test_clerk_edits_granted_file(self, client, multi_user):
        _as_clerk(client)
        resp = client.post("/cbz-update-comicinfo", json={
            "path": multi_user.granted_cbz, "updates": {"Series": "Changed"}})
        assert resp.status_code == 200
        assert resp.get_json()["comicinfo"]["Series"] == "Changed"

    def test_clerk_may_tag_a_watch_file_from_the_file_manager(self, client, multi_user):
        # The File Manager browses WATCH; the single-file fetch serves it.
        _as_clerk(client)
        with _no_providers(), patch("routes.metadata.add_comicinfo_to_cbz"):
            resp = client.post("/api/search-metadata", json={
                "file_path": multi_user.watch_cbz,
                "file_name": os.path.basename(multi_user.watch_cbz)})
        assert resp.status_code != 403

    def test_cbz_editor_keeps_its_target_only_staging(self, client, multi_user):
        # /cbz-update-comicinfo served libraries and TARGET before; not WATCH.
        _as_clerk(client)
        ok = client.post("/cbz-update-comicinfo", json={
            "path": multi_user.target_cbz, "updates": {"Series": "T"}})
        assert ok.status_code == 200
        refused = client.post("/cbz-update-comicinfo", json={
            "path": multi_user.watch_cbz, "updates": {"Series": "W"}})
        assert refused.status_code == 403

    def test_single_user_install_unaffected(self, client, layout):
        # No accounts: implicit owner, grants never apply.
        resp = client.post("/cbz-clear-comicinfo", json={"path": layout.sibling_cbz})
        assert resp.status_code == 200
        assert not _has_comicinfo(layout.sibling_cbz)


# ---------------------------------------------------------------------------
# Batch path
# ---------------------------------------------------------------------------
class TestBatchRoutes:
    # /api/save-cvinfo sits under core.auth's owner-only "/api/save-" prefix,
    # so a Clerk never reaches it; the containment gate still applies to owners.
    def test_save_cvinfo_refused_for_clerk(self, client, multi_user):
        _as_clerk(client)
        resp = client.post("/api/save-cvinfo", json={
            "directory": multi_user.sibling, "content": "https://example.invalid/4050-1/"})
        assert resp.status_code == 403
        assert not os.path.exists(os.path.join(multi_user.sibling, "cvinfo"))

    def test_save_cvinfo_owner_writes_in_library_and_target(self, client, multi_user):
        _as_owner(client)
        for folder in (multi_user.sibling, multi_user.target):
            resp = client.post("/api/save-cvinfo", json={"directory": folder, "content": "x"})
            assert resp.status_code == 200
            assert os.path.exists(os.path.join(folder, "cvinfo"))

    def test_save_cvinfo_outside_libraries_refused(self, client, multi_user):
        _as_owner(client)
        resp = client.post("/api/save-cvinfo", json={
            "directory": multi_user.outside, "content": "x"})
        assert resp.status_code == 403
        assert not os.path.exists(os.path.join(multi_user.outside, "cvinfo"))

    def test_save_cvinfo_name_prefix_of_target_refused(self, client, multi_user, tmp_path):
        # The old check was a bare startswith(TARGET): "processed-evil" passed.
        evil = tmp_path / "processed-evil"
        evil.mkdir()
        _as_owner(client)
        resp = client.post("/api/save-cvinfo", json={"directory": str(evil), "content": "x"})
        assert resp.status_code == 403
        assert not os.path.exists(os.path.join(str(evil), "cvinfo"))

    def test_update_xml_refused_in_ungranted_folder(self, client, multi_user):
        _as_clerk(client)
        with patch("models.update_xml.update_field_in_cbz_files") as update:
            resp = client.post("/api/update-xml", json={
                "directory": multi_user.sibling, "field": "Volume", "value": "2020"})
        assert resp.status_code == 403
        update.assert_not_called()

    def test_update_xml_granted_folder(self, client, multi_user):
        _as_clerk(client)
        with patch("models.update_xml.update_field_in_cbz_files",
                   return_value={"updated": [], "errors": []}) as update, \
                patch("routes.metadata._sync_file_index_after_xml_update"):
            resp = client.post("/api/update-xml", json={
                "directory": multi_user.grant, "field": "Volume", "value": "2020"})
        assert resp.status_code == 200
        update.assert_called_once()

    def test_batch_metadata_refused_in_ungranted_folder(self, client, multi_user):
        _as_clerk(client)
        with _no_providers(), patch("routes.metadata.add_comicinfo_to_cbz") as write:
            resp = client.post("/api/batch-metadata", json={"directory": multi_user.sibling})
        assert resp.status_code == 403
        write.assert_not_called()

    def test_batch_metadata_refused_on_traverse_only_parent(self, client, multi_user):
        # The library root is only 'traverse' for this clerk: not enough to write.
        _as_clerk(client)
        with _no_providers():
            resp = client.post("/api/batch-metadata", json={"directory": multi_user.root})
        assert resp.status_code == 403

    def test_batch_metadata_granted_folder_passes_the_gate(self, client, multi_user):
        _as_clerk(client)
        with _no_providers(), patch("routes.metadata.add_comicinfo_to_cbz"):
            resp = client.post("/api/batch-metadata", json={"directory": multi_user.grant})
        assert resp.status_code != 403

    def test_bulk_clear_list_with_one_ungranted_refuses_the_whole_batch(
            self, client, multi_user):
        _as_clerk(client)
        with patch("core.job_queue.file_jobs.submit") as submit:
            resp = client.post("/cbz-bulk-clear-comicinfo", json={
                "paths": [multi_user.granted_cbz, multi_user.sibling_cbz]})
        assert resp.status_code == 403
        submit.assert_not_called()
        assert _has_comicinfo(multi_user.granted_cbz)
        assert _has_comicinfo(multi_user.sibling_cbz)

    def test_bulk_clear_ungranted_directory_refused(self, client, multi_user):
        _as_clerk(client)
        with patch("core.job_queue.file_jobs.submit") as submit:
            resp = client.post("/cbz-bulk-clear-comicinfo",
                               json={"directory": multi_user.sibling})
        assert resp.status_code == 403
        submit.assert_not_called()

    def test_bulk_clear_granted_list_is_queued(self, client, multi_user):
        _as_clerk(client)
        with patch("core.job_queue.file_jobs.submit", return_value="op-1") as submit:
            resp = client.post("/cbz-bulk-clear-comicinfo",
                               json={"paths": [multi_user.granted_cbz]})
        assert resp.status_code == 200
        assert submit.call_args.args[2] == 1

    def test_bulk_clear_directory_drops_symlinked_entry(self, client, multi_user):
        link = os.path.join(multi_user.grant, "shortcut.cbz")
        _symlink_or_skip(multi_user.sibling_cbz, link)
        _as_clerk(client)
        with patch("core.job_queue.file_jobs.submit", return_value="op-1") as submit:
            resp = client.post("/cbz-bulk-clear-comicinfo",
                               json={"directory": multi_user.grant})
        assert resp.status_code == 200
        # Only the real granted comic is handed to the job, not the link.
        assert submit.call_args.args[2] == 1

    def test_bulk_clear_owner_unaffected(self, client, multi_user):
        _as_owner(client)
        with patch("core.job_queue.file_jobs.submit", return_value="op-1"):
            resp = client.post("/cbz-bulk-clear-comicinfo", json={
                "paths": [multi_user.granted_cbz, multi_user.sibling_cbz]})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Bulk orchestrator
# ---------------------------------------------------------------------------
class TestBulkOrchestrator:
    def test_start_refused_for_ungranted_folder_before_anything_is_queued(
            self, client, multi_user):
        _as_clerk(client)
        with patch("routes.bulk_metadata.start_bulk_job") as start:
            resp = client.post("/api/bulk-metadata/start", json={
                "scope": "folder", "paths": [multi_user.sibling]})
        assert resp.status_code == 403
        start.assert_not_called()
        assert multi_user.sibling not in resp.get_data(as_text=True)

    def test_start_refused_when_one_of_several_files_is_ungranted(self, client, multi_user):
        _as_clerk(client)
        with patch("routes.bulk_metadata.start_bulk_job") as start:
            resp = client.post("/api/bulk-metadata/start", json={
                "scope": "files",
                "paths": [multi_user.granted_cbz, multi_user.sibling_cbz]})
        assert resp.status_code == 403
        start.assert_not_called()

    def test_start_refused_on_traverse_only_library_root(self, client, multi_user):
        _as_clerk(client)
        with patch("routes.bulk_metadata.start_bulk_job") as start:
            resp = client.post("/api/bulk-metadata/start", json={
                "scope": "folder", "paths": [multi_user.root]})
        assert resp.status_code == 403
        start.assert_not_called()

    def test_start_refused_outside_libraries_for_owner(self, client, multi_user):
        _as_owner(client)
        with patch("routes.bulk_metadata.start_bulk_job") as start:
            resp = client.post("/api/bulk-metadata/start", json={
                "scope": "folder", "paths": [multi_user.outside]})
        assert resp.status_code == 403
        start.assert_not_called()

    def test_granted_folder_job_carries_the_requesters_scope(self, client, multi_user):
        _as_clerk(client)
        with patch("routes.bulk_metadata.start_bulk_job", return_value="job-x") as start:
            resp = client.post("/api/bulk-metadata/start", json={
                "scope": "folder", "paths": [multi_user.grant]})
        assert resp.status_code == 200
        allowed = start.call_args.kwargs["allowed"]
        assert allowed is not None
        assert allowed(multi_user.granted_cbz)
        assert not allowed(multi_user.sibling_cbz)
        assert not allowed(multi_user.lookalike_cbz)

    def test_owner_job_is_unrestricted(self, client, multi_user):
        _as_owner(client)
        with patch("routes.bulk_metadata.start_bulk_job", return_value="job-x") as start:
            resp = client.post("/api/bulk-metadata/start", json={
                "scope": "folder", "paths": [multi_user.sibling]})
        assert resp.status_code == 200
        assert start.call_args.kwargs["allowed"] is None

    def test_folder_expansion_stays_inside_the_grant(self, client, multi_user):
        """The walk under a granted folder follows a symlinked comic into an
        ungranted folder; the queued job must not receive it."""
        link = os.path.join(multi_user.grant, "shortcut.cbz")
        _symlink_or_skip(multi_user.sibling_cbz, link)
        _as_clerk(client)
        with patch("core.job_queue.file_jobs.submit", return_value="op-q"):
            resp = client.post("/api/bulk-metadata/start", json={
                "scope": "folder", "paths": [multi_user.grant],
                "overwrite_existing": True})
        assert resp.status_code == 200
        job = get_bulk_job(resp.get_json()["job_id"])
        assert job["total_files"] == 1

    def test_backfill_refused_for_ungranted_folder(self, client, multi_user):
        _as_clerk(client)
        with patch("core.metadata_backfill.start_backfill_job") as start:
            resp = client.post("/api/bulk-metadata/backfill",
                               json={"paths": [multi_user.sibling]})
        assert resp.status_code == 403
        start.assert_not_called()

    def test_backfill_expansion_stays_inside_the_grant(self, client, multi_user):
        link = os.path.join(multi_user.grant, "shortcut.cbz")
        _symlink_or_skip(multi_user.sibling_cbz, link)
        _as_clerk(client)
        with patch("core.job_queue.file_jobs.submit", return_value="op-b"):
            resp = client.post("/api/bulk-metadata/backfill",
                               json={"paths": [multi_user.grant], "dry_run": True})
        assert resp.status_code == 200
        assert resp.get_json()["total_files"] == 1

    def test_resolve_review_row_in_ungranted_folder_refused(self, client, multi_user):
        create_bulk_job(job_id="job-other", scope_type="folder_recursive",
                        scope_payload={"paths": [multi_user.sibling]})
        review_id = add_review_item("job-other", multi_user.sibling, None,
                                    "Batman", None, None, "ambiguous", [])
        _as_clerk(client)
        with patch("core.bulk_metadata._instantiate_provider") as inst:
            resp = client.post(f"/api/bulk-metadata/review/{review_id}/resolve",
                               json={"provider": "metron", "series_id": "1"})
        assert resp.status_code == 403
        inst.assert_not_called()

    def test_apply_cvinfo_on_ungranted_review_row_refused(self, client, multi_user):
        create_bulk_job(job_id="job-other", scope_type="folder_recursive",
                        scope_payload={"paths": [multi_user.sibling]})
        review_id = add_review_item("job-other", multi_user.sibling, None,
                                    "Batman", None, None, "ambiguous", [])
        _as_clerk(client)
        resp = client.post(f"/api/bulk-metadata/review/{review_id}/apply-cvinfo",
                           json={"metron_series_id": "1"})
        assert resp.status_code == 403
        assert not os.path.exists(os.path.join(multi_user.sibling, "cvinfo"))

    def test_revert_audit_row_in_ungranted_folder_refused(self, client, multi_user):
        create_bulk_job(job_id="job-other", scope_type="files",
                        scope_payload={"paths": [multi_user.sibling_cbz]})
        audit_id = log_bulk_audit("job-other", multi_user.sibling_cbz, multi_user.sibling,
                                  "metron", 1, 1, "Batman", "1", 2020, "auto",
                                  None, b"<ComicInfo/>")
        _as_clerk(client)
        resp = client.post(f"/api/bulk-metadata/audit/{audit_id}/revert")
        assert resp.status_code == 403
        assert _has_comicinfo(multi_user.sibling_cbz)

    def test_owner_reverts_any_audit_row(self, client, multi_user):
        create_bulk_job(job_id="job-other", scope_type="files",
                        scope_payload={"paths": [multi_user.sibling_cbz]})
        audit_id = log_bulk_audit("job-other", multi_user.sibling_cbz, multi_user.sibling,
                                  "metron", 1, 1, "Batman", "1", 2020, "auto",
                                  None, b"<ComicInfo/>")
        _as_owner(client)
        resp = client.post(f"/api/bulk-metadata/audit/{audit_id}/revert")
        assert resp.status_code == 200
        assert not _has_comicinfo(multi_user.sibling_cbz)


class TestProviderMixGrant:
    """The provider counts describe a folder's comics: a Clerk needs a full
    grant on it, as for the re-tag the picker leads to."""

    def test_clerk_refused_on_ungranted_folder(self, client, multi_user):
        _as_clerk(client)
        with patch("core.database.get_provider_mix") as mix:
            resp = client.get("/api/bulk-metadata/provider-mix",
                              query_string={"path": multi_user.sibling})
        assert resp.status_code == 403
        mix.assert_not_called()

    def test_clerk_refused_on_traverse_only_root(self, client, multi_user):
        _as_clerk(client)
        with patch("core.database.get_provider_mix") as mix:
            resp = client.get("/api/bulk-metadata/provider-mix",
                              query_string={"path": multi_user.root})
        assert resp.status_code == 403
        mix.assert_not_called()

    def test_clerk_allowed_on_granted_folder(self, client, multi_user):
        _as_clerk(client)
        with patch("core.database.get_provider_mix", return_value={}):
            resp = client.get("/api/bulk-metadata/provider-mix",
                              query_string={"path": multi_user.grant})
        assert resp.status_code == 200


class TestBulkExpansionUnit:
    """The orchestrator's expansion, called directly with a captured scope."""

    def test_missing_xml_lookup_is_separator_bounded(self, layout):
        from core.bulk_metadata import _enumerate_folder_recursive

        rows = [{"path": layout.granted_cbz}, {"path": layout.lookalike_cbz}]
        with patch("core.bulk_metadata.get_files_missing_comicinfo", return_value=rows):
            buckets = _enumerate_folder_recursive(layout.grant, only_missing=True)
        files = [f for fs in buckets.values() for f in fs]
        assert files == [layout.granted_cbz]

    def test_start_bulk_job_drops_paths_the_scope_does_not_allow(self, layout):
        from core.bulk_metadata import start_bulk_job

        def allowed(p):
            return os.path.normpath(p).startswith(os.path.normpath(layout.grant) + os.sep) \
                or os.path.normpath(p) == os.path.normpath(layout.grant)

        with patch("core.job_queue.file_jobs.submit", return_value="op"):
            job_id = start_bulk_job(
                scope="files", paths=[layout.granted_cbz, layout.sibling_cbz],
                allowed=allowed)
        assert get_bulk_job(job_id)["total_files"] == 1


# ---------------------------------------------------------------------------
# series.json writers outside routes/metadata.py
# ---------------------------------------------------------------------------
class TestSeriesMappingWrites:
    SERIES = {"id": 1, "name": "Batman", "year_began": 2020}

    def test_clerk_cannot_map_series_to_ungranted_folder(self, client, multi_user):
        _as_clerk(client)
        with patch("core.database.save_series_mapping") as save:
            resp = client.post("/api/series/1/map", json={
                "mapped_path": multi_user.sibling, "series": self.SERIES})
        assert resp.status_code == 403
        save.assert_not_called()
        assert not os.path.exists(os.path.join(multi_user.sibling, "cvinfo"))

    def test_clerk_cannot_subscribe_into_ungranted_folder(self, client, multi_user):
        _as_clerk(client)
        new_folder = os.path.join(multi_user.sibling, "Batman (2020)")
        with patch("core.database.save_series_mapping") as save:
            resp = client.post("/api/series/1/subscribe", json={"path": new_folder})
        # 403 on a POSIX host. The route rebuilds the path from '/'-separated
        # segments, which turns a Windows drive path relative: refused as 400.
        assert resp.status_code in (400, 403)
        save.assert_not_called()
        assert not os.path.exists(new_folder)

    @staticmethod
    def _writers():
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(patch("models.metron.create_cvinfo_file", return_value=True))
        stack.enter_context(patch("models.metron.get_flask_api", return_value=None))
        stack.enter_context(patch("models.series_json.write_series_json"))
        stack.enter_context(patch("core.series_json_builder.with_description_fallback",
                                  side_effect=lambda s, _p: s))
        return stack

    def test_owner_maps_any_library_folder(self, client, multi_user):
        """The Clerk's ungranted sibling is still a library folder for an owner."""
        _as_owner(client)
        with patch("core.database.save_series_mapping", return_value=True) as save, \
                self._writers():
            resp = client.post("/api/series/1/map", json={
                "mapped_path": multi_user.sibling, "series": self.SERIES})
        assert resp.status_code == 200
        # Stored exactly as given.
        assert save.call_args[0][1] == multi_user.sibling

    def test_owner_cannot_map_outside_every_library(self, client, multi_user):
        """Downloads are filed into a mapped folder: containment applies to
        owners too, and is a 400, not a permission error."""
        _as_owner(client)
        with patch("core.database.save_series_mapping") as save, self._writers():
            resp = client.post("/api/series/1/map", json={
                "mapped_path": multi_user.outside, "series": self.SERIES})
        assert resp.status_code == 400
        assert "inside a library" in resp.get_json()["error"]
        save.assert_not_called()

    def test_owner_cannot_map_a_name_prefix_lookalike_of_the_root(
            self, client, multi_user, tmp_path):
        lookalike_root = tmp_path / "LibA2"   # "LibA2" starts with "LibA"
        lookalike_root.mkdir()
        _as_owner(client)
        with patch("core.database.save_series_mapping") as save, self._writers():
            resp = client.post("/api/series/1/map", json={
                "mapped_path": str(lookalike_root), "series": self.SERIES})
        assert resp.status_code == 400
        save.assert_not_called()

    def test_owner_cannot_map_a_symlink_out_of_the_library(self, client, multi_user):
        link = os.path.join(multi_user.root, "Linked")
        _symlink_or_skip(multi_user.outside, link)
        _as_owner(client)
        with patch("core.database.save_series_mapping") as save, self._writers():
            resp = client.post("/api/series/1/map", json={
                "mapped_path": link, "series": self.SERIES})
        assert resp.status_code == 400
        save.assert_not_called()

    def test_owner_cannot_subscribe_outside_every_library(self, client, multi_user):
        _as_owner(client)
        new_folder = os.path.join(multi_user.outside, "Batman (2020)")
        with patch("core.database.save_series_mapping") as save:
            resp = client.post("/api/series/1/subscribe", json={"path": new_folder})
        assert resp.status_code == 400
        save.assert_not_called()
        assert not os.path.exists(new_folder)

    def test_existing_outside_mapping_still_reads(self, client, multi_user):
        """A mapping saved before this rule is not touched by it."""
        _as_owner(client)
        with patch("core.database.get_series_mapping", return_value=multi_user.outside):
            resp = client.get("/api/series/1/mapping")
        assert resp.status_code == 200
        assert resp.get_json()["mapped_path"] == multi_user.outside

    def test_import_skips_a_mapping_outside_every_library(self, client, multi_user):
        import io
        import json as _json

        _as_owner(client)
        payload = {"version": 1, "series": [
            {"id": 5, "name": "Inside", "mapped_path": multi_user.grant},
            {"id": 6, "name": "Outside", "mapped_path": multi_user.outside},
        ]}
        with patch("core.database.import_series_row", return_value="inserted") as imp:
            resp = client.post(
                "/api/pull-list/import",
                data={"file": (io.BytesIO(_json.dumps(payload).encode()), "pull.json")},
                content_type="multipart/form-data",
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["imported_new"] == 1
        assert [e["id"] for e in body["errors"]] == [6]
        assert [c.args[0]["id"] for c in imp.call_args_list] == [5]

    def test_clerk_cannot_add_ungranted_folder_to_pull_list(self, client, multi_user):
        _as_clerk(client)
        with patch("models.library_automap.add_folder_to_pull_list") as add:
            resp = client.post("/api/pull-list/add-folder",
                               json={"folder": multi_user.sibling})
        assert resp.status_code == 403
        add.assert_not_called()


# ---------------------------------------------------------------------------
# Folder thumbnails
# ---------------------------------------------------------------------------
class TestFolderArtWriteRefusal:
    """``folder_art_write_refusal`` is what app.py's
    /api/generate-folder-thumbnail calls (app.py cannot be imported here)."""

    def _as(self, app, username):
        from flask import g
        from core.auth import current_user  # noqa: F401  (resolves via g)

        g.current_user = get_user_by_username(username)

    def test_clerk(self, app, multi_user):
        from core.path_access import folder_art_write_refusal

        with app.test_request_context():
            self._as(app, "mw_clerk")
            assert folder_art_write_refusal(multi_user.grant) is None
            assert folder_art_write_refusal(multi_user.sibling)[1] == 403
            assert folder_art_write_refusal(multi_user.lookalike)[1] == 403
            # A traverse-only ancestor may show art but not regenerate it.
            assert folder_art_write_refusal(multi_user.root)[1] == 403
            assert folder_art_write_refusal(multi_user.outside)[1] == 403
            assert folder_art_write_refusal(multi_user.target) is None

    def test_owner(self, app, multi_user):
        from core.path_access import folder_art_write_refusal

        with app.test_request_context():
            self._as(app, "mw_owner")
            assert folder_art_write_refusal(multi_user.sibling) is None
            assert folder_art_write_refusal(multi_user.outside)[1] == 403

    def test_single_user(self, app, layout):
        from core.path_access import folder_art_write_refusal

        with app.test_request_context():
            assert folder_art_write_refusal(layout.sibling) is None


class TestThumbnailSweepScope:
    def test_clerk_sweep_of_ungranted_folder_refused(self, client, multi_user):
        _as_clerk(client)
        with patch("app.generate_folder_thumbnail_internal") as gen:
            resp = client.post("/api/regenerate-all-thumbnails",
                               json={"path": multi_user.sibling})
        assert resp.status_code == 403
        gen.assert_not_called()

    def test_clerk_all_libraries_sweep_covers_only_granted_folders(self, client, multi_user):
        os.makedirs(os.path.join(multi_user.grant, "Volume 1"))
        os.makedirs(os.path.join(multi_user.sibling, "Volume 1"))
        _as_clerk(client)
        with patch("routes.collection.threading.Thread") as thread:
            resp = client.post("/api/regenerate-all-thumbnails", json={})
        assert resp.status_code == 200
        folders = thread.call_args.kwargs["args"][1]
        assert sorted(folders) == sorted([
            multi_user.grant, os.path.join(multi_user.grant, "Volume 1")])

    def test_owner_sweep_unrestricted(self, client, multi_user):
        _as_owner(client)
        with patch("routes.collection.threading.Thread") as thread:
            resp = client.post("/api/regenerate-all-thumbnails", json={})
        assert resp.status_code == 200
        folders = thread.call_args.kwargs["args"][1]
        assert multi_user.sibling in folders and multi_user.grant in folders


class TestGenerateFolderThumbnailCallSite:
    """app.py cannot be imported in tests, so the route is checked by its AST."""

    @staticmethod
    def _route():
        tree = ast.parse((PROJECT_ROOT / "app.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "generate_folder_thumbnail":
                return node
        raise AssertionError("generate_folder_thumbnail not found in app.py")

    @staticmethod
    def _first_line(fn, predicate):
        lines = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call) and predicate(n)]
        return min(lines) if lines else None

    def test_refusal_checked_before_the_folder_is_touched(self):
        fn = self._route()

        def named(name):
            return lambda c: (isinstance(c.func, ast.Name) and c.func.id == name) or (
                isinstance(c.func, ast.Attribute) and c.func.attr == name)

        refusal = self._first_line(fn, named("folder_art_write_refusal"))
        exists = self._first_line(fn, named("exists"))
        generate = self._first_line(fn, named("generate_folder_thumbnail_internal"))
        assert refusal is not None, "the route must call folder_art_write_refusal"
        assert exists is not None and generate is not None
        assert refusal < exists < generate

    def test_refusal_is_returned(self):
        fn = self._route()
        src = ast.get_source_segment(
            (PROJECT_ROOT / "app.py").read_text(encoding="utf-8"), fn)
        assert "refusal = folder_art_write_refusal(folder_path)" in src
        assert "if refusal:" in src
