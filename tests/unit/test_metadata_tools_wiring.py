"""Structural checks for #626 / #579: the folder menu offers both actions, the
page loads the modals and script, and the bulk contract carries the new fields
(the UI is JavaScript, so none of this is visible to a Python behaviour test)."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_folder_menu_offers_both_actions():
    js = _read("static/js/files.js")
    assert "CLU.openRetagModal" in js
    assert "CLU.openBackfillModal" in js


def test_files_page_loads_modals_and_script():
    html = _read("templates/files.html")
    assert "partials/modal_metadata_tools.html" in html
    assert "clu-metadata-tools.js" in html


def test_modal_partial_has_the_ids_the_script_binds():
    html = _read("templates/partials/modal_metadata_tools.html")
    js = _read("static/js/clu-metadata-tools.js")
    for dom_id in ("retagModal", "retagProvider", "retagReplace", "retagConfirmBtn",
                   "backfillModal", "backfillDryRun", "backfillConfirmBtn"):
        assert f'id="{dom_id}"' in html, dom_id
        assert dom_id in js, dom_id


def test_bulk_contract_carries_url_and_extra():
    js = _read("static/js/bulk-metadata-review.js")
    assert "contract.url" in js and "contract.extra" in js


def test_script_posts_the_fields_the_route_reads():
    js = _read("static/js/clu-metadata-tools.js")
    for field in ("provider:", "replace:", "only_from:", "dry_run:",
                  "/api/bulk-metadata/backfill", "/api/bulk-metadata/provider-mix"):
        assert field in js, field
    route = _read("routes/bulk_metadata.py")
    for key in ("'provider'", "'replace'", "'only_from'", "'dry_run'"):
        assert key in route, key


def test_scanner_and_index_write_the_provider():
    assert "ci_provider" in _read("core/metadata_scanner.py")
    assert "queue_provider_backfill()" in _read("core/metadata_scanner.py")
    db = _read("core/database.py")
    assert "ci_provider = COALESCE(?, ci_provider)" in db
