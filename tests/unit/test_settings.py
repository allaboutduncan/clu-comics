"""core.settings: schema coverage, typing, cache, and the retired config.ini."""
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


class TestIniIsRetired:
    """Nothing reads or writes config.ini any more."""

    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    # .claude holds agent worktrees: full copies of the repo, at other commits.
    SKIP_DIRS = {"tests", ".git", ".claude", "venv", ".venv", "node_modules", "__pycache__", "docs", "tools"}

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

    def test_repo_ships_no_ini(self):
        """The shipped config.ini is gone; settings defaults live in SETTINGS_SCHEMA.
        A restored copy would invite new keys back into the deprecated file."""
        assert not os.path.exists(os.path.join(self.ROOT, "config.ini"))

    def test_no_ini_watcher_thread(self):
        assert not hasattr(cc, "monitor_config") and not hasattr(cc, "write_config")

    def test_load_config_is_idempotent_and_writes_no_file(self, db, tmp_path):
        with patch("core.config.CONFIG_DIR", str(tmp_path)):
            cc.load_config()
            cc.load_config()
        assert not (tmp_path / "config.ini").exists()

    def test_nothing_parses_an_ini_for_settings(self):
        """configparser survives only where it reads someone else's file
        (Mylar's config.ini, a rename rules file) or renders the debug package."""
        allowed = {"core/mylar_reader.py", "core/debug_package.py", "cbz_ops/rename.py"}
        users = {rel.replace(os.sep, "/") for rel, text in self._sources()
                 if "import configparser" in text}
        assert users <= allowed, sorted(users - allowed)


class TestNoNewIniStyleReads:
    """The ``config.get("SETTINGS", ...)`` view is deleted; nothing may bring it back.

    It was a read-only stand-in for the old RawConfigParser, kept while callers
    were converted to ``core.settings`` one file at a time. A new import of it,
    or a call shaped like the old ones, is how the pattern would return.
    """

    GONE = ("config", "CONFIG_FILE", "migrate_ini_to_user_preferences",
            "seed_monitor_from_env", "PREF_INI_MIGRATED")
    # .claude holds agent worktrees: full copies of the repo, at other commits.
    SKIP_DIRS = {"tests", ".git", ".claude", "venv", ".venv", "node_modules", "__pycache__", "docs", "tools"}

    @classmethod
    def _trees(cls):
        import ast
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in cls.SKIP_DIRS]
            for name in files:
                if name.endswith(".py"):
                    path = os.path.join(base, name)
                    rel = os.path.relpath(path, root).replace(os.sep, "/")
                    yield rel, ast.parse(open(path, encoding="utf-8").read())

    def test_nothing_imports_the_deleted_names(self):
        import ast
        offenders = []
        for rel, tree in self._trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "core.config":
                    offenders += [f"{rel}:{node.lineno} {a.name}" for a in node.names
                                  if a.name in self.GONE]
        assert not offenders, offenders

    def test_no_ini_style_reads(self):
        import ast
        offenders = []
        for rel, tree in self._trees():
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("get", "getboolean", "getint")
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in ("config", "raw_config")
                        and node.args and isinstance(node.args[0], ast.Constant)
                        and node.args[0].value == "SETTINGS"):
                    offenders.append(f"{rel}:{node.lineno}")
                elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                      and node.value.id == "config" and isinstance(node.slice, ast.Constant)
                      and node.slice.value == "SETTINGS"):
                    offenders.append(f"{rel}:{node.lineno}")
        assert not offenders, "use core.settings: " + ", ".join(offenders)
