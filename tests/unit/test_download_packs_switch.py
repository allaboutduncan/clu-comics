"""The "Download Packs" switch.

A pack is a download holding more than one issue: a range post ("Batman
#1-50") or a range part of a split post ("#1-15"). Automated downloads used to
fall back to one whenever no single issue was found, so a missing #15 could
fetch 80 issues. The switch is OFF by default; manual grabs are never gated.

The gate and the pack test live in ``core.config`` and ``models.getcomics`` so
they can be tested directly. app.py cannot be imported in tests, so where the
sweep applies them, and the config page round trip, are asserted against its
source.
"""
import ast
import pathlib
from unittest.mock import patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def app_py():
    return (ROOT / "app.py").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def config_html():
    return (ROOT / "templates" / "config.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def save_handler(app_py):
    """Source of the /api/config/download-api handler, bounded by the next route."""
    return app_py.split('@app.route("/api/config/download-api"')[1].split("@app.route")[0]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class TestGate:
    def test_defaults_to_off(self, monkeypatch):
        import core.config as cfg

        monkeypatch.setattr(
            "core.database.get_user_preference", lambda key, default=None: default)
        assert cfg.is_download_packs_enabled() is False

    def test_on_when_the_preference_is_true(self, monkeypatch):
        import core.config as cfg

        monkeypatch.setattr(
            "core.database.get_user_preference", lambda key, default=None: True)
        assert cfg.is_download_packs_enabled() is True

    def test_reads_the_shared_key(self, monkeypatch):
        import core.config as cfg

        seen = []
        monkeypatch.setattr(
            "core.database.get_user_preference",
            lambda key, default=None: seen.append(key) or default)
        cfg.is_download_packs_enabled()
        assert seen == [cfg.PREF_DOWNLOAD_PACKS]

    def test_unreadable_preference_store_keeps_the_default(self, monkeypatch):
        import core.config as cfg

        def boom(key, default=None):
            raise RuntimeError("db gone")

        monkeypatch.setattr("core.database.get_user_preference", boom)
        assert cfg.is_download_packs_enabled() is False


# ---------------------------------------------------------------------------
# What counts as a pack
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("part,tier,expected", [
    # A post that is not split is the whole post: a pack when matched as a range.
    ({"label": None, "issue_range": None}, "range fallback", True),
    ({"label": None, "issue_range": None}, "direct match", False),
    # A split post's part is a pack when its own label is a range, whatever the
    # post title says: Supergirl's #1-15 is, Ginseng Roots' #11 is not.
    ({"label": "Supergirl Vol. 4 #1 – 15", "issue_range": (1, 15)}, "range fallback", True),
    ({"label": "Supergirl Vol. 4 #1 – 15", "issue_range": (1, 15)}, "direct match", True),
    ({"label": "Ginseng Roots #11 (2022)", "issue_range": None}, "range fallback", False),
    ({"label": "Series #7 – 7", "issue_range": (7, 7)}, "range fallback", False),
])
def test_is_pack_download(part, tier, expected):
    from models.getcomics import is_pack_download

    assert is_pack_download(part, tier) is expected


# ---------------------------------------------------------------------------
# Usenet and DC++ auto-download
# ---------------------------------------------------------------------------


def _usenet_search(tier):
    from models.indexers import NZBSearchResult

    result = NZBSearchResult(indexer_id=5, indexer_name="NZBgeek",
                             title="Batman 001-050 (2020)", nzb_url="https://x/a.nzb")
    return {"chosen": (result, 39), "tier": tier, "best_accept": None,
            "best_fallback": (result, 39), "all_results": [{}]}


def _dcpp_search(tier):
    entry = {"title": "Batman 001-050 (2020)", "result_token": "tok", "size": 1}
    return {"chosen": (entry, 39), "tier": tier, "best_accept": None,
            "best_fallback": (entry, 39), "all_results": [{}], "errors": []}


SOURCES = [
    ("models.usenet", "search_usenet_for_issue", "grab_nzb", _usenet_search),
    ("models.dcpp", "search_dcpp_for_issue", "grab_dcpp", _dcpp_search),
]


@pytest.mark.parametrize("module,search,grab,fake", SOURCES)
def test_source_skips_a_range_pack_when_off(module, search, grab, fake):
    import importlib

    mod = importlib.import_module(module)
    with patch(f"{module}.{search}", return_value=fake("range fallback")), \
         patch(f"{module}.{grab}") as mock_grab, \
         patch("core.config.is_download_packs_enabled", return_value=False):
        out = mod.try_download_for_issue("Batman", "20")
    assert out["status"] == "pack_skipped"
    assert out["submitted"] is False
    # The skipped pack is still named, for the log and the simulation.
    assert out["chosen"]["title"] == "Batman 001-050 (2020)"
    mock_grab.assert_not_called()


@pytest.mark.parametrize("module,search,grab,fake", SOURCES)
def test_source_takes_a_range_pack_when_on(module, search, grab, fake):
    import importlib

    mod = importlib.import_module(module)
    with patch(f"{module}.{search}", return_value=fake("range fallback")), \
         patch(f"{module}.{grab}", return_value="dl-1") as mock_grab, \
         patch("core.config.is_download_packs_enabled", return_value=True):
        out = mod.try_download_for_issue("Batman", "20")
    assert out["status"] == "submitted"
    mock_grab.assert_called_once()


@pytest.mark.parametrize("module,search,grab,fake", SOURCES)
def test_source_direct_match_ignores_the_switch(module, search, grab, fake):
    import importlib

    mod = importlib.import_module(module)
    with patch(f"{module}.{search}", return_value=fake("direct match")), \
         patch(f"{module}.{grab}", return_value="dl-1"), \
         patch("core.config.is_download_packs_enabled", return_value=False):
        out = mod.try_download_for_issue("Batman", "20")
    assert out["status"] == "submitted"


# ---------------------------------------------------------------------------
# Where the sweep applies it (app.py, by AST)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sweep(app_py):
    for node in ast.parse(app_py).body:
        if isinstance(node, ast.FunctionDef) and node.name == "scheduled_getcomics_download":
            return node
    pytest.fail("scheduled_getcomics_download not found in app.py")


def _assigned(node, name):
    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)
    ]


def _calls(node, name):
    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) == name or getattr(n.func, "attr", None) == name)
    ]


def test_sweep_reads_the_switch_once_per_run(sweep):
    reads = _calls(sweep, "is_download_packs_enabled")
    assert len(reads) == 1
    (assign,) = _assigned(sweep, "packs_allowed")
    assert _calls(assign.value, "is_download_packs_enabled")


def test_sweep_decides_on_the_selected_part(sweep):
    (assign,) = _assigned(sweep, "pack_skipped")
    names = {n.id for n in ast.walk(assign.value) if isinstance(n, ast.Name)}
    assert {"packs_allowed", "parts", "tier"} <= names
    assert _calls(assign.value, "is_pack_download")


def test_a_skipped_pack_is_never_queued(sweep):
    """The queue sits in the else of `if pack_skipped`, never in its body."""
    gates = [n for n in ast.walk(sweep)
             if isinstance(n, ast.If) and isinstance(n.test, ast.Name)
             and n.test.id == "pack_skipped"]
    assert len(gates) == 1
    gate = gates[0]
    body = ast.Module(body=gate.body, type_ignores=[])
    orelse = ast.Module(body=gate.orelse, type_ignores=[])
    assert not _calls(body, "put"), "a skipped pack must not reach download_queue"
    assert _calls(orelse, "put"), "the queue must stay behind the gate"


def test_gate_is_imported_not_redefined(app_py):
    assert "is_download_packs_enabled," in app_py
    assert "def is_download_packs_enabled" not in app_py


# ---------------------------------------------------------------------------
# Config page round trip
# ---------------------------------------------------------------------------


def test_switch_is_rendered(config_html):
    assert 'id="downloadPacks"' in config_html
    assert "{% if downloadPacks %}checked{% endif %}" in config_html


def test_switch_lives_in_the_search_variant_settings(config_html):
    section = config_html.split("<h4>Search Variant Settings</h4>")[1].split("<h4>")[0]
    assert 'id="downloadPacks"' in section


def test_switch_is_on_the_tab_whose_save_posts_it(config_html):
    tab = config_html.split('id="download-api" role="tabpanel"')[1]
    tab = tab.split('role="tabpanel"')[0]
    assert 'id="downloadPacks"' in tab
    assert "saveDownloadApiSettings()" in tab


def test_switch_is_collected_into_the_save_payload(config_html):
    payload = config_html.split("async function saveDownloadApiSettings()")[1]
    payload = payload.split("fetch(")[0]
    line = [ln for ln in payload.splitlines() if "downloadPacks" in ln][0]
    # A missing element must read as the default, off.
    assert "?? false" in line


def test_endpoint_persists_the_preference(save_handler):
    assert "PREF_DOWNLOAD_PACKS" in save_handler
    line = [ln for ln in save_handler.splitlines() if "downloadPacks" in ln][0]
    assert "False" in line


def test_config_page_renders_the_current_value(app_py):
    assert "downloadPacks=is_download_packs_enabled()" in app_py


def test_setting_is_not_in_the_deprecated_config_ini():
    """CLAUDE.md: new settings live in user_preferences, not config.ini."""
    config_ini = (ROOT / "config.ini").read_text(encoding="utf-8")
    assert "DOWNLOAD_PACKS" not in config_ini
