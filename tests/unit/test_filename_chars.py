"""Tests for core/filename_chars.py -- the character map shared by file and
folder names (#421, #588)."""
import ast
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fake_db(monkeypatch, stored=None, raise_on_read=False):
    def _get(key, default=None):
        if raise_on_read:
            raise RuntimeError("no db")
        return (stored or {}).get(key, default)

    fake = type(sys)("fake_core_database")
    fake.get_user_preference = _get
    monkeypatch.setitem(sys.modules, "core.database", fake)


class TestApplyCharMap:

    def test_hostile_chars_removed_by_default(self):
        from core.filename_chars import apply_char_map
        assert apply_char_map('A\\/:*?"<>|&$;B', {}) == "AB"

    def test_mapped_char_replaced(self):
        from core.filename_chars import apply_char_map
        out = apply_char_map("Batman / Punisher: Lake of Fire", {"/": "-", ":": "-"})
        assert out == "Batman - Punisher- Lake of Fire"

    def test_whitespace_collapsed(self):
        from core.filename_chars import apply_char_map
        assert apply_char_map("Armageddon / X-Men", {}) == "Armageddon X-Men"

    def test_single_pass(self):
        # A replacement is never itself replaced: '!' -> '#', '#' -> 'x'.
        from core.filename_chars import apply_char_map
        assert apply_char_map("a!b#c", {"!": "#", "#": "x"}) == "a#bxc"

    def test_non_hostile_char_untouched_unless_mapped(self):
        from core.filename_chars import apply_char_map
        assert apply_char_map("Hey!", {}) == "Hey!"
        assert apply_char_map("Hey!", {"!": ""}) == "Hey"

    def test_empty_text(self):
        from core.filename_chars import apply_char_map
        assert apply_char_map("", {"/": "-"}) == ""
        assert apply_char_map(None, {}) is None


class TestNormaliseCharMap:

    def test_hostile_chars_stripped_from_replacement(self):
        from core.filename_chars import normalise_char_map
        assert normalise_char_map({"&": " & and /"}) == {"&": "  and "}

    def test_replacement_capped(self):
        from core.filename_chars import normalise_char_map, MAX_REPLACEMENT_LEN
        assert len(normalise_char_map({"!": "x" * 50})["!"]) == MAX_REPLACEMENT_LEN

    @pytest.mark.parametrize("key", ["", "ab", " ", "\t", None, 5])
    def test_bad_keys_dropped(self, key):
        from core.filename_chars import normalise_char_map
        assert normalise_char_map({key: "-"}) == {}

    def test_none_replacement_means_remove(self):
        from core.filename_chars import normalise_char_map
        assert normalise_char_map({"/": None}) == {"/": ""}

    @pytest.mark.parametrize("raw", [None, [], "/:-", 3])
    def test_non_dict_is_empty(self, raw):
        from core.filename_chars import normalise_char_map
        assert normalise_char_map(raw) == {}

    def test_invalid_replacement_chars(self):
        from core.filename_chars import invalid_replacement_chars
        assert invalid_replacement_chars(" - ") == []
        assert invalid_replacement_chars("a/b:") == ["/", ":"]
        assert invalid_replacement_chars(None) == []


class TestLoadCharMap:

    def test_stored_map(self, monkeypatch):
        from core.filename_chars import load_char_map
        _fake_db(monkeypatch, {"rename_char_replacements": {"/": " - ", "!": ""}})
        assert load_char_map() == {"/": " - ", "!": ""}

    def test_stored_map_wins_over_legacy(self, monkeypatch):
        from core.filename_chars import load_char_map
        _fake_db(monkeypatch, {
            "rename_char_replacements": {},
            "rename_clean_specials_enabled": True,
            "rename_clean_specials_charset": "&",
            "rename_clean_specials_mode": "replace",
            "rename_clean_specials_replacement": "+",
        })
        assert load_char_map() == {}

    def test_legacy_replace_settings_converted(self, monkeypatch):
        from core.filename_chars import load_char_map
        _fake_db(monkeypatch, {
            "rename_clean_specials_enabled": True,
            "rename_clean_specials_charset": "/: !",
            "rename_clean_specials_mode": "replace",
            "rename_clean_specials_replacement": "-",
        })
        assert load_char_map() == {"/": "-", ":": "-", "!": "-"}

    def test_legacy_remove_settings_converted(self, monkeypatch):
        from core.filename_chars import load_char_map
        _fake_db(monkeypatch, {
            "rename_clean_specials_enabled": True,
            "rename_clean_specials_charset": "!#",
            "rename_clean_specials_mode": "remove",
            "rename_clean_specials_replacement": "-",
        })
        assert load_char_map() == {"!": "", "#": ""}

    def test_legacy_disabled_is_empty(self, monkeypatch):
        from core.filename_chars import load_char_map
        _fake_db(monkeypatch, {
            "rename_clean_specials_enabled": False,
            "rename_clean_specials_charset": "!",
        })
        assert load_char_map() == {}

    def test_db_failure_removes_everything(self, monkeypatch):
        from core.filename_chars import load_char_map
        _fake_db(monkeypatch, raise_on_read=True)
        assert load_char_map() == {}


class TestSanitizePathSegment:
    """Folders use the same map as filenames (#588)."""

    def test_default_removes_like_filenames(self, monkeypatch):
        from helpers import sanitize_path_segment
        monkeypatch.setattr("core.filename_chars.load_char_map", lambda: {})
        assert sanitize_path_segment("Armageddon / X-Men CGD 2026") == "Armageddon X-Men CGD 2026"
        assert sanitize_path_segment("Batman: Year One") == "Batman Year One"

    def test_follows_char_map(self, monkeypatch):
        from helpers import sanitize_path_segment
        monkeypatch.setattr("core.filename_chars.load_char_map", lambda: {"/": "-", ":": " -"})
        assert sanitize_path_segment("Armageddon / X-Men") == "Armageddon - X-Men"
        assert sanitize_path_segment("Batman: Year One") == "Batman - Year One"

    def test_matches_filename_cleanup(self, monkeypatch):
        # The #588 invariant: a folder and a file named from the same series agree.
        from helpers import sanitize_path_segment
        from cbz_ops.rename import apply_filename_cleanup
        cmap = {"/": " - ", "&": " and "}
        monkeypatch.setattr("core.filename_chars.load_char_map", lambda: cmap)
        series = "Batman / Robin & Nightwing: Rebirth"
        cfg = {"spaces_enabled": False, "char_map": cmap}
        assert sanitize_path_segment(series) == apply_filename_cleanup(series, cfg)

    def test_slash_never_survives(self, monkeypatch):
        from helpers import sanitize_path_segment
        # Even a stored map that tries to keep '/' cannot make it a separator.
        monkeypatch.setattr("core.filename_chars.load_char_map", lambda: {})
        assert "/" not in sanitize_path_segment("a/b/c")

    def test_empty(self):
        from helpers import sanitize_path_segment
        assert sanitize_path_segment("") == ""
        assert sanitize_path_segment(None) is None


class TestMirrors:

    def test_js_mirror_uses_same_hostile_set(self):
        from core.filename_chars import FILENAME_ILLEGAL_CHARS
        src = open(os.path.join(REPO, "static", "js", "clu-utils.js"), encoding="utf-8").read()
        m = re.search(r"CLU\.FILENAME_ILLEGAL_CHARS = '(.*)';", src)
        assert m, "CLU.FILENAME_ILLEGAL_CHARS not found"
        js_value = m.group(1).encode().decode("unicode_escape")
        assert set(js_value) == set(FILENAME_ILLEGAL_CHARS)

    def test_config_save_validates_and_normalises_the_map(self):
        # app.py cannot be imported in tests, so assert the save path structurally.
        src = open(os.path.join(REPO, "app.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        called = {
            n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "invalid_replacement_chars" in called
        assert "normalise_char_map" in called
        assert '"renameCharMap"' in src
        # The legacy keys are read only by core.filename_chars, never written.
        assert '"rename_clean_specials_' not in src
