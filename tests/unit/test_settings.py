"""core.settings: schema coverage, typing, cache, and the ini import."""
import configparser
import os
from unittest.mock import patch

import pytest

import core.config as cc
import core.settings as cs
from core.database import get_user_preference, set_user_preference


@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    with patch("core.database.get_db_path", return_value=db_path):
        from core.database import init_db
        init_db()
        cs.invalidate()
        yield db_path
        cs.invalidate()


def _write_ini(path, **kv):
    path.write_text("[SETTINGS]\n" + "".join(f"{k} = {v}\n" for k, v in kv.items()))


class TestSchema:
    LEGACY_KEYS = (
        "IGNORED_TERMS IGNORED_FILES IGNORED_EXTENSIONS AUTOCONVERT READ_SUBDIRECTORIES "
        "CONVERT_SUBDIRECTORIES XML_YEAR XML_MARKDOWN XML_LIST MOVE_DIRECTORY "
        "CONSOLIDATE_DIRECTORIES AUTO_RENAME_MONITOR RECONCILE_INTERVAL_MINUTES SKIPPED_FILES "
        "DELETED_FILES DOWNLOAD_PROVIDER_PRIORITY PIXELDRAIN_API_KEY GCD_METADATA_LANGUAGES "
        "COMICVINE_API_KEY ENABLE_CUSTOM_RENAME CUSTOM_RENAME_PATTERN ENABLE_AUTO_RENAME "
        "ENABLE_AUTO_MOVE CUSTOM_MOVE_PATTERN ENABLE_DEBUG_LOGGING CACHE_DIR BOOTSTRAP_THEME "
        "ENABLE_METADATA_SCAN METADATA_SCAN_THREADS TRASH_ENABLED TRASH_DIR TRASH_MAX_SIZE_MB "
        "PUBLICATION_TYPES VARIANT_TYPES SEQUEL_KEYWORDS ONESHOT_FOLDERS HEADERS "
        "AUTO_CLEANUP_ORPHAN_FILES CLEANUP_INTERVAL_HOURS CROSSOVER_KEYWORDS "
        "OPERATION_TIMEOUT LARGE_FILE_THRESHOLD"
    ).split()

    def test_every_legacy_key_is_covered(self):
        """Nothing config.ini used to hold may be left without a home."""
        missing = set(self.LEGACY_KEYS) - set(cs.SETTINGS_SCHEMA) - cs.NON_SCHEMA_INI_KEYS
        assert not missing, f"ini keys with no preference: {sorted(missing)}"

    def test_pref_keys_are_unique(self):
        prefs = [s.pref for s in cs.SETTINGS_SCHEMA.values()]
        assert len(prefs) == len(set(prefs))

    def test_existing_pref_names_are_reused(self):
        s = cs.SETTINGS_SCHEMA
        assert s["ENABLE_CUSTOM_RENAME"].pref == "enable_custom_rename"
        assert s["CUSTOM_RENAME_PATTERN"].pref == "custom_rename_pattern"
        assert s["BOOTSTRAP_THEME"].pref == "bootstrap_theme"
        assert s["HEADERS"].pref == "custom_headers"
        assert s["GCD_METADATA_LANGUAGES"].pref == "gcd_metadata_languages"


class TestAccess:
    def test_defaults_when_unset(self, db):
        assert cs.get_bool("AUTO_RENAME_MONITOR") is True
        assert cs.get_int("TRASH_MAX_SIZE_MB") == 1024
        assert cs.get_setting("CUSTOM_MOVE_PATTERN") == "{publisher}/{series_name}/v{start_year}"

    def test_roundtrip_and_types(self, db):
        assert cs.set_setting("AUTOCONVERT", "True")
        assert cs.get_setting("AUTOCONVERT") is True
        assert get_user_preference("autoconvert") is True
        cs.set_setting("CLEANUP_INTERVAL_HOURS", "6")
        assert cs.get_int("CLEANUP_INTERVAL_HOURS") == 6

    def test_bad_stored_value_falls_back(self, db):
        set_user_preference("cleanup_interval_hours", "soon")
        assert cs.get_int("CLEANUP_INTERVAL_HOURS") == 1

    def test_list(self, db):
        cs.set_setting("SKIPPED_FILES", " .xml , .nfo,, ")
        assert cs.get_list("SKIPPED_FILES") == [".xml", ".nfo"]

    def test_unknown_key_raises(self, db):
        with pytest.raises(KeyError):
            cs.get_setting("NOPE")

    def test_cache_ttl_and_local_invalidation(self, db):
        cs.set_setting("XML_YEAR", True)
        assert cs.get_bool("XML_YEAR") is True
        # A write from another process is invisible until the TTL lapses...
        set_user_preference("xml_year", False)
        assert cs.get_bool("XML_YEAR") is True
        cs.invalidate()
        assert cs.get_bool("XML_YEAR") is False

    def test_failed_read_is_not_cached(self, db):
        """A read taken before the database is ready must not pin the defaults."""
        with patch.object(cs, "_load_snapshot", return_value=None):
            assert cs.get_bool("XML_YEAR") is False
        set_user_preference("xml_year", True)
        assert cs.get_bool("XML_YEAR") is True


class TestIniImport:
    def test_imports_unset_prefs_with_types(self, db, tmp_path):
        ini = tmp_path / "config.ini"
        _write_ini(ini, AUTOCONVERT="True", TRASH_MAX_SIZE_MB="2048",
                   IGNORED_EXTENSIONS=".a,.b", CUSTOM_RENAME_PATTERN="{series_name} ({year})")
        assert cc.migrate_ini_to_user_preferences(str(ini)) >= 4
        assert get_user_preference("autoconvert") is True
        assert get_user_preference("trash_max_size_mb") == 2048
        assert get_user_preference("ignored_extensions") == ".a,.b"
        assert get_user_preference("custom_rename_pattern") == "{series_name} ({volume_year})"
        assert get_user_preference(cc.PREF_INI_MIGRATED) is True
        assert not ini.exists() and (tmp_path / "config.ini.migrated").exists()

    def test_never_overwrites_existing_pref(self, db, tmp_path):
        set_user_preference("bootstrap_theme", "darkly")
        ini = tmp_path / "config.ini"
        _write_ini(ini, BOOTSTRAP_THEME="default", XML_YEAR="True")
        cc.migrate_ini_to_user_preferences(str(ini))
        assert get_user_preference("bootstrap_theme") == "darkly"
        assert get_user_preference("xml_year") is True

    def test_runs_once(self, db, tmp_path):
        ini = tmp_path / "config.ini"
        _write_ini(ini, XML_YEAR="True")
        cc.migrate_ini_to_user_preferences(str(ini))
        set_user_preference("xml_year", False)
        _write_ini(ini, XML_YEAR="True")
        assert cc.migrate_ini_to_user_preferences(str(ini)) is None
        assert get_user_preference("xml_year") is False
        assert ini.exists()  # untouched on the second call

    def test_no_file_sets_flag(self, db, tmp_path):
        assert cc.migrate_ini_to_user_preferences(str(tmp_path / "missing.ini")) is None
        assert get_user_preference(cc.PREF_INI_MIGRATED) is True

    def test_failed_write_leaves_flag_and_file(self, db, tmp_path):
        ini = tmp_path / "config.ini"
        _write_ini(ini, XML_YEAR="True")
        with patch("core.database.set_user_preference", return_value=False):
            assert cc.migrate_ini_to_user_preferences(str(ini)) is None
        assert ini.exists()
        assert not get_user_preference(cc.PREF_INI_MIGRATED, default=False)

    def test_comicvine_key_goes_to_credentials(self, db, tmp_path):
        from core.database import get_provider_credentials
        ini = tmp_path / "config.ini"
        _write_ini(ini, COMICVINE_API_KEY="abc123")
        cc.migrate_ini_to_user_preferences(str(ini))
        assert (get_provider_credentials("comicvine") or {}).get("api_key") == "abc123"


class TestIniIsRetired:
    """Nothing writes config.ini any more; the import is the only reader."""

    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    SKIP_DIRS = {"tests", ".git", "venv", ".venv", "node_modules", "__pycache__", "docs", "tools"}

    def _sources(self):
        for base, dirs, files in os.walk(self.ROOT):
            dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS]
            for f in files:
                if f.endswith(".py"):
                    path = os.path.join(base, f)
                    yield os.path.relpath(path, self.ROOT), open(path, encoding="utf-8").read()

    def test_nothing_writes_the_ini(self):
        offenders = []
        for rel, text in self._sources():
            for token in ("write_config", 'config["SETTINGS"][', "config.set(", "monitor_config"):
                if token not in text:
                    continue
                # core/config.py names them only to say they are gone / refuse them.
                if rel.replace("\\", "/") == "core/config.py":
                    continue
                for ln in text.splitlines():
                    if token in ln and not ln.lstrip().startswith("#") and "=" in ln.split(token, 1)[1][:60] + "=":
                        if token == 'config["SETTINGS"][' and "==" in ln:
                            continue
                        if token == 'config["SETTINGS"][' and "=" not in ln.split("]", 2)[-1][:4]:
                            continue  # a read
                        offenders.append(f"{rel}: {ln.strip()}")
        assert not offenders, "\n".join(offenders)

    def test_no_ini_watcher_thread(self):
        assert not hasattr(cc, "monitor_config") and not hasattr(cc, "write_config")

    def test_load_config_is_idempotent_and_writes_no_file(self, db, tmp_path):
        with patch("core.config.CONFIG_FILE", str(tmp_path / "config.ini")):
            cc.load_config()
            cc.load_config()
        assert not (tmp_path / "config.ini").exists()


class TestNoNewIniStyleReads:
    """A ratchet on the config.get("SETTINGS", ...) compatibility view.

    The view exists only so callers could be converted one at a time. This
    counts the calls still using it, per file, and fails if any file gains one
    or a new file appears. Converting a file means lowering its number here; the
    table is empty when the view can be deleted.
    """

    REMAINING = {
    }
    SKIP_DIRS = {"tests", ".git", "venv", ".venv", "node_modules", "__pycache__", "docs", "tools"}

    @classmethod
    def _count(cls):
        import ast
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        found = {}
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in cls.SKIP_DIRS]
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(base, name)
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                if rel == "core/config.py":  # defines the view
                    continue
                tree = ast.parse(open(path, encoding="utf-8").read())
                n = 0
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr in ("get", "getboolean", "getint")
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id in ("config", "raw_config")
                            and node.args and isinstance(node.args[0], ast.Constant)
                            and node.args[0].value == "SETTINGS"):
                        n += 1
                    elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                          and node.value.id == "config" and isinstance(node.slice, ast.Constant)
                          and node.slice.value == "SETTINGS"):
                        n += 1
                if n:
                    found[rel] = n
        return found

    def test_no_file_gains_an_ini_style_read(self):
        found = self._count()
        grew = {f: (n, self.REMAINING.get(f, 0)) for f, n in found.items()
                if n > self.REMAINING.get(f, 0)}
        assert not grew, f"new config.get('SETTINGS', ...) reads (found, allowed): {grew}"

    def test_table_is_tight(self):
        """When a file is converted its entry must come off, or the ratchet stops ratcheting."""
        found = self._count()
        stale = {f: (self.REMAINING[f], found.get(f, 0)) for f in self.REMAINING
                 if found.get(f, 0) < self.REMAINING[f]}
        assert not stale, f"lower these entries in REMAINING (allowed, found): {stale}"


class TestEveryKeyUsedExists:
    """Modules like app.py cannot be imported in tests, so a misspelt key in
    get_setting("...") / st["..."] would only fail at runtime (KeyError on the
    settings page, or a silently dead read). Check every literal against the
    schema."""

    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    SKIP_DIRS = {"tests", ".git", "venv", ".venv", "node_modules", "__pycache__", "docs", "tools"}

    def test_literal_keys_are_in_the_schema(self):
        import re
        call = re.compile(r'\b(?:get_setting|get_bool|get_int|get_list|get_ext_list|set_setting)\(\s*["\']([A-Z_]+)["\']')
        page = re.compile(r'\bst\["([A-Z_]+)"\]')
        bad = []
        for base, dirs, files in os.walk(self.ROOT):
            dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS]
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(base, name)
                text = open(path, encoding="utf-8").read()
                rel = os.path.relpath(path, self.ROOT)
                for rx in (call, page):
                    for m in rx.finditer(text):
                        if m.group(1) not in cs.SETTINGS_SCHEMA:
                            bad.append(f"{rel}: {m.group(1)}")
        assert not bad, bad


def test_app_refreshes_flask_config_after_init_db():
    """api.py builds app.config before the database exists; app.py must redo it
    once init_db() has run, or the first start after the ini import serves
    defaults (trash, rename pattern, ...) until the next save. app.py cannot be
    imported in tests, so assert the order in its source."""
    import ast
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    tree = ast.parse(open(os.path.join(root, "app.py"), encoding="utf-8").read())
    order = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Name):
            order.append(node.value.func.id)
    assert "init_db" in order and "load_flask_config" in order
    assert order.index("init_db") < order.index("load_flask_config")
