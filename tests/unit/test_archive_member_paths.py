"""A hand-written archive member never leaves its extraction folder.

GHSA-6p2x-pmgg-9qjj: ``zipfile.extract`` sanitises a member name, but the CRC
fallbacks in ``cbz_ops/edit.py`` and ``routes/metadata.py`` rebuilt the
destination from the raw name, so ``../../x`` with a wrong CRC-32 was written
outside the folder. Real archives throughout -- what matters is what zipfile
really does with a hostile entry.
"""

import ast
import os
import struct
import zipfile
import zlib
from unittest.mock import patch

import pytest

from helpers.archive_paths import UnsafeArchiveMember, safe_member_path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HOSTILE_NAMES = [
    "../escaped.bin",
    "../../escaped.bin",
    "a/../../../escaped.bin",
    "/escaped.bin",
    "//server/share/escaped.bin",
    "..\\..\\escaped.bin",
    "C:\\escaped.bin",
    "C:/escaped.bin",
    "sub/./../../escaped.bin",
]


def _break_crc(path, payload):
    """Rewrite every stored copy of *payload*'s CRC-32 (local + central)."""
    raw = bytearray(open(path, "rb").read())
    good = struct.pack("<I", zlib.crc32(payload))
    patched = 0
    at = raw.find(good)
    while at != -1:
        raw[at:at + 4] = b"\xde\xad\xbe\xef"
        patched += 1
        at = raw.find(good, at + 4)
    assert patched == 2, "expected the local header and the central directory"
    with open(path, "wb") as fh:
        fh.write(bytes(raw))


def _hostile_cbz(path, name, payload):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("001.txt", b"an honest page")
        zf.writestr(name, payload)
    _break_crc(path, payload)


def _files_under(root):
    return sorted(
        os.path.relpath(os.path.join(d, f), root)
        for d, _dirs, files in os.walk(root)
        for f in files
    )


class TestSafeMemberPath:
    @pytest.mark.parametrize("name", HOSTILE_NAMES)
    def test_hostile_name_stays_inside(self, tmp_path, name):
        dest = tmp_path / "out"
        dest.mkdir()
        target = safe_member_path(str(dest), name)
        real = os.path.realpath(target)
        assert real.startswith(os.path.realpath(str(dest)) + os.sep)

    @pytest.mark.parametrize("name", HOSTILE_NAMES + ["pages/001.jpg", "001.jpg", "a b/c.d.jpg"])
    def test_same_path_zipfile_extract_writes(self, tmp_path, name):
        """The fallback must land on the file extract() started writing, so
        this pins the mirror of zipfile's sanitising against a Python upgrade."""
        archive = tmp_path / "a.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(name, b"x")
        dest = tmp_path / "out"
        dest.mkdir()
        with zipfile.ZipFile(archive) as zf:
            written = zf.extract(zf.infolist()[0], str(dest))
        assert os.path.normcase(safe_member_path(str(dest), name)) == os.path.normcase(written)

    @pytest.mark.parametrize("name", ["", "..", "../..", "/", "./.", "bad\0name"])
    def test_a_name_with_nothing_left_is_refused(self, tmp_path, name):
        with pytest.raises(UnsafeArchiveMember):
            safe_member_path(str(tmp_path), name)

    def test_sibling_with_a_shared_prefix_is_not_inside(self, tmp_path):
        """Separator-bounded: ``out2`` is not inside ``out``."""
        dest = tmp_path / "out"
        dest.mkdir()
        outside = tmp_path / "out2"
        outside.mkdir()
        link = dest / "link"
        try:
            os.symlink(str(outside), str(link), target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available here")
        with pytest.raises(UnsafeArchiveMember):
            safe_member_path(str(dest), "link/x.bin")


class TestEditorFallback:
    @pytest.mark.parametrize("name", HOSTILE_NAMES)
    def test_bad_crc_traversal_entry_stays_in_the_folder(self, tmp_path, name):
        from cbz_ops import edit

        lib = tmp_path / "lib" / "Series"
        lib.mkdir(parents=True)
        comic = lib / "Comic 001.cbz"
        _hostile_cbz(comic, name, b"PWNED" * 5000)

        with patch.object(edit, "get_ext_list", return_value=[]):
            result = edit.process_cbz_file(str(comic))

        folder = str(lib / "Comic 001_folder")
        assert _files_under(str(tmp_path)) == sorted(
            os.path.relpath(p, str(tmp_path))
            for p in (
                str(lib / "Comic 001.zip"),
                os.path.join(folder, "001.txt"),
                safe_member_path(folder, name),
            )
        )
        # Still shown and flagged, at the path the modal will find on disk.
        (flagged,) = result["damaged"]
        assert os.path.normcase(flagged) == os.path.normcase(safe_member_path(folder, name))
        with open(flagged, "rb") as fh:
            assert fh.read() == b"PWNED" * 5000


class TestMetadataWriteFallback:
    def _tag(self, comic, cache):
        from routes.metadata import add_comicinfo_to_cbz

        with patch("core.config.get_cache_dir", return_value=str(cache)), \
             patch("helpers.match_parent_permissions"), \
             patch("core.problem_files.record_problem"):
            add_comicinfo_to_cbz(str(comic), b"<ComicInfo/>")

    @pytest.mark.parametrize("name", HOSTILE_NAMES)
    def test_bad_crc_traversal_entry_writes_nothing_outside(self, tmp_path, name):
        lib = tmp_path / "lib" / "Series"
        lib.mkdir(parents=True)
        comic = lib / "Comic 001.cbz"
        _hostile_cbz(comic, name, b"PWNED" * 5000)

        self._tag(comic, tmp_path / "cache")

        outside = [p for p in _files_under(str(tmp_path))
                   if not p.startswith("cache" + os.sep)]
        assert outside == [os.path.relpath(str(comic), str(tmp_path))]

    def test_a_crc_damaged_page_keeps_every_byte(self, tmp_path):
        """The chunked read this replaced dropped the final chunk when the
        CRC check fired, so a small page came back empty."""
        payload = b"page bytes " * 3000
        comic = tmp_path / "Comic 001.cbz"
        _hostile_cbz(comic, "002.jpg", payload)

        self._tag(comic, tmp_path / "cache")

        with zipfile.ZipFile(comic) as zf:
            assert zf.read("002.jpg") == payload
            assert zf.read("ComicInfo.xml") == b"<ComicInfo/>"


class TestNoRawMemberNameJoins:
    """Ratchet: a name that came out of an archive listing is never handed to
    ``os.path.join``. ``safe_member_path`` is the only way to place one."""

    LISTING_CALLS = {"namelist", "infolist"}
    SKIP_DIRS = {".git", "tests", "node_modules", "__pycache__", "venv", ".venv"}

    def _modules(self):
        for dirpath, dirs, files in os.walk(ROOT):
            # Dot-directories too: .claude/worktrees holds whole stale checkouts.
            dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS and not d.startswith(".")]
            for f in files:
                if f.endswith(".py"):
                    yield os.path.join(dirpath, f)

    @staticmethod
    def _names_in(node):
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

    def _offences(self, path):
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        found = []
        for loop in ast.walk(tree):
            if not isinstance(loop, (ast.For, ast.comprehension)):
                continue
            calls = {
                c.func.attr for c in ast.walk(loop.iter)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            }
            if not calls & self.LISTING_CALLS:
                continue
            members = self._names_in(loop.target)
            body = loop.body if isinstance(loop, ast.For) else []
            for stmt in body:
                for call in ast.walk(stmt):
                    if not (isinstance(call, ast.Call)
                            and isinstance(call.func, ast.Attribute)
                            and call.func.attr == "join"
                            and isinstance(call.func.value, ast.Attribute)
                            and call.func.value.attr == "path"):
                        continue
                    # The first argument is the base; a member name anywhere
                    # after it is the bug.
                    used = set()
                    for arg in call.args[1:]:
                        used |= self._names_in(arg)
                    if used & members:
                        found.append(f"{os.path.relpath(path, ROOT)}:{call.lineno}")
        return found

    def test_no_member_name_reaches_os_path_join(self):
        offences = [o for m in self._modules() for o in self._offences(m)]
        assert not offences, (
            "raw archive member name joined onto a directory -- use "
            f"helpers.archive_paths.safe_member_path: {offences}"
        )

    def test_the_ratchet_sees_the_original_bug(self, tmp_path):
        bad = tmp_path / "bad.py"
        bad.write_text(
            "import os\n"
            "def f(zf, folder):\n"
            "    for info in zf.infolist():\n"
            "        target = os.path.join(folder, info.filename)\n"
        )
        assert self._offences(str(bad))

    def test_only_salvage_waives_a_crc(self):
        """``_expected_crc`` is the private switch that reads past a checksum;
        one owner keeps the 'what do we do with those bytes' rule in one place."""
        owners = []
        for path in self._modules():
            with open(path, encoding="utf-8") as fh:
                if "_expected_crc" in fh.read():
                    owners.append(os.path.relpath(path, ROOT).replace(os.sep, "/"))
        assert owners == ["cbz_ops/salvage.py"]
