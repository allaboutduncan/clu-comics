"""The "tag files automatically on move" switch.

Moving a comic into a folder that carries a ``cvinfo`` triggers a provider
lookup and a ComicInfo.xml write. Users who curate their own metadata want
moves to stay moves, so the behaviour is now switchable -- default ON, since
that is what every existing install already does.

The gate itself lives in ``core.config`` so it can be tested directly. Where
it *sits* inside the three ``auto_fetch_*_metadata`` functions is the part that
matters (a check placed after the provider call would still hit the API), and
app.py cannot be imported in tests, so that half is asserted against the AST.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

FETCHERS = [
    "auto_fetch_metron_metadata",
    "auto_fetch_comicvine_metadata",
    "auto_fetch_comicvine_sqlite_metadata",
]


@pytest.fixture(scope="module")
def app_py():
    return (ROOT / "app.py").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_tree(app_py):
    return ast.parse(app_py)


@pytest.fixture(scope="module")
def save_handler(app_py):
    """Source of the /api/config/file-processing handler, bounded by the next route."""
    return app_py.split('@app.route("/api/config/file-processing"')[1].split("@app.route")[0]


@pytest.fixture(scope="module")
def config_html():
    return (ROOT / "templates" / "config.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class TestGate:
    def test_defaults_to_enabled(self, monkeypatch):
        import core.config as cfg

        monkeypatch.setattr(
            "core.database.get_user_preference",
            lambda key, default=None: default,
        )
        assert cfg.is_auto_metadata_on_move_enabled() is True

    def test_disabled_when_the_preference_is_false(self, monkeypatch):
        import core.config as cfg

        monkeypatch.setattr(
            "core.database.get_user_preference",
            lambda key, default=None: False,
        )
        assert cfg.is_auto_metadata_on_move_enabled() is False

    def test_reads_the_shared_key(self, monkeypatch):
        import core.config as cfg

        seen = []
        monkeypatch.setattr(
            "core.database.get_user_preference",
            lambda key, default=None: seen.append(key) or default,
        )
        cfg.is_auto_metadata_on_move_enabled()
        assert seen == [cfg.PREF_AUTO_METADATA_ON_MOVE]

    def test_fails_open_when_the_preference_store_is_unreadable(self, monkeypatch):
        """A broken DB must not silently switch the feature off."""
        import core.config as cfg

        def boom(key, default=None):
            raise RuntimeError("db gone")

        monkeypatch.setattr("core.database.get_user_preference", boom)
        assert cfg.is_auto_metadata_on_move_enabled() is True


# ---------------------------------------------------------------------------
# Where the gate sits in app.py
# ---------------------------------------------------------------------------


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in app.py")


@pytest.mark.parametrize("name", FETCHERS)
def test_fetcher_checks_the_gate_before_anything_else(app_tree, name):
    """The gate must be the first statement after the docstring.

    Anything before it -- a provider handshake, an os.listdir of the folder --
    is work the user asked us not to do.
    """
    fn = _function(app_tree, name)
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    first = body[0]
    assert isinstance(first, ast.If), f"{name} does not open with the gate"
    calls = [
        n.func.id
        for n in ast.walk(first.test)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    assert "is_auto_metadata_on_move_enabled" in calls


@pytest.mark.parametrize("name", FETCHERS)
def test_disabled_fetcher_returns_the_path_untouched(app_tree, name):
    """Callers chain the return value -- the gate must not drop the path."""
    fn = _function(app_tree, name)
    body = [
        s for s in fn.body
        if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
    ]
    gate = body[0]
    returns = [n for n in ast.walk(gate) if isinstance(n, ast.Return)]
    assert len(returns) == 1
    assert isinstance(returns[0].value, ast.Name)
    assert returns[0].value.id == fn.args.args[0].arg


def test_gate_is_imported_not_redefined(app_py):
    assert "is_auto_metadata_on_move_enabled," in app_py
    assert "def is_auto_metadata_on_move_enabled" not in app_py


# ---------------------------------------------------------------------------
# Config page round trip
# ---------------------------------------------------------------------------


def test_switch_is_rendered(config_html):
    assert 'id="autoMetadataOnMove"' in config_html
    assert "{% if autoMetadataOnMove %}checked{% endif %}" in config_html


def test_switch_is_collected_into_the_save_payload(config_html):
    payload = config_html.split("async function saveFileProcessingSettings()")[1]
    payload = payload.split("fetch(")[0]
    assert "autoMetadataOnMove" in payload


def test_switch_defaults_to_on_in_the_payload(config_html):
    """A missing element must not read as "off" and disable the feature."""
    payload = config_html.split("async function saveFileProcessingSettings()")[1]
    payload = payload.split("fetch(")[0]
    line = [ln for ln in payload.splitlines() if "autoMetadataOnMove" in ln][0]
    assert "?? true" in line


def test_endpoint_persists_the_preference(save_handler):
    assert "autoMetadataOnMove" in save_handler
    assert "PREF_AUTO_METADATA_ON_MOVE" in save_handler


def test_endpoint_defaults_to_true(save_handler):
    line = [ln for ln in save_handler.splitlines() if "autoMetadataOnMove" in ln][0]
    assert "True" in line


def test_switch_lives_on_the_file_processing_tab(config_html):
    """The input must sit inside the tab whose Save button posts it."""
    tab = config_html.split('id="file-processing" role="tabpanel"')[1]
    tab = tab.split('role="tabpanel"')[0]
    assert 'id="autoMetadataOnMove"' in tab


def test_config_page_renders_the_current_value(app_py):
    assert "autoMetadataOnMove=is_auto_metadata_on_move_enabled()" in app_py


def test_setting_is_not_in_the_deprecated_config_ini():
    """CLAUDE.md: new settings live in user_preferences, not config.ini."""
    config_ini = (ROOT / "config.ini").read_text(encoding="utf-8")
    assert "AUTO_METADATA_ON_MOVE" not in config_ini
