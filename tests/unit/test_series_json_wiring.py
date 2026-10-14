"""Structural checks for #618: every series.json writer gets the description
fallback, and every surface that offers the action loads the shared modal."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_series_routes_writers_use_the_fallback():
    src = _read("routes/series.py")
    writes = re.findall(r"write_series_json\(", src)
    fallbacks = re.findall(r"with_description_fallback\(", src)
    assert len(writes) == 3
    assert len(fallbacks) == 3


def test_bulk_sidecar_writer_uses_the_fallback():
    assert "with_description_fallback(" in _read("core/bulk_metadata.py")


def test_menus_open_the_shared_modal():
    assert "CLU.openSeriesJsonModal" in _read("static/js/files.js")
    assert "CLU.openSeriesJsonModal" in _read("static/js/collection.js")
    assert "CLU.openSeriesJsonModal" in _read("templates/series.html")


def test_pages_load_modal_and_script():
    for page in ("files", "collection", "series"):
        html = _read(f"templates/{page}.html")
        assert "partials/modal_series_json.html" in html, page
        assert "clu-series-json.js" in html, page
