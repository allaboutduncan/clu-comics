"""Corrupt archives must cost one log line, not a raw header dump.

A physically damaged CBZ (bad CRC, local headers that no longer line up with the
central directory) makes zipfile raise BadZipFile with a message built from the
%r of the raw bytes it read out of the broken header -- several KB of binary per
failed entry. The reader requests every page of a comic, and the old handler
logged that message *and* a traceback repeating it, so one bad file could bury
the log under megabytes of garbage.

helpers.describe_archive_error collapses those messages; these tests pin the two
properties that matter (binary is stripped, length is capped) and assert the
reader routes in app.py actually route corrupt-archive errors through it. app.py
cannot be imported in tests, so that half is checked against the parsed AST.
"""

import ast
import os
import zipfile
import zlib

import pytest

from helpers import describe_archive_error

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PATH = os.path.join(PROJECT_ROOT, "app.py")


def _real_header_mismatch_message():
    """The exact message shape zipfile.ZipFile.open raises on a bad header."""
    blob = bytes(range(256)) * 8
    return (
        "File name in directory 'Wizard Magazine 218/wizard_magazine_218_01.jpg' "
        "and header %r differ." % blob
    )


class TestDescribeArchiveError:
    def test_strips_the_embedded_binary_blob(self):
        raw = _real_header_mismatch_message()
        assert len(raw) > 2000

        described = describe_archive_error(zipfile.BadZipFile(raw))

        assert "<binary>" in described
        assert "\\x" not in described
        assert len(described) < 300

    def test_keeps_the_parts_a_human_needs(self):
        described = describe_archive_error(
            zipfile.BadZipFile(_real_header_mismatch_message())
        )

        assert "BadZipFile" in described
        assert "wizard_magazine_218_01.jpg" in described

    def test_caps_a_long_message_with_no_binary_in_it(self):
        described = describe_archive_error(zipfile.BadZipFile("x" * 5000))

        assert len(described) < 300
        assert described.endswith("...")

    def test_short_messages_pass_through_intact(self):
        assert describe_archive_error(
            zipfile.BadZipFile("Bad CRC-32 for file 'ComicInfo.xml'")
        ) == "BadZipFile: Bad CRC-32 for file 'ComicInfo.xml'"

    def test_handles_the_zlib_error_raised_on_corrupt_deflate_data(self):
        described = describe_archive_error(
            zlib.error("Error -3 while decompressing data: invalid block type")
        )

        assert "invalid block type" in described

    def test_never_returns_an_empty_string(self):
        assert describe_archive_error(zipfile.BadZipFile("")) == "BadZipFile"

    def test_collapses_newlines_so_one_error_stays_one_log_line(self):
        described = describe_archive_error(zipfile.BadZipFile("first\nsecond\nthird"))

        assert "\n" not in described


@pytest.fixture(scope="module")
def app_tree():
    with open(APP_PATH, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"{name} not found in app.py")


class TestReaderRoutesUseIt:
    def test_read_comic_page_has_a_corrupt_archive_handler(self, app_tree):
        """BadZipFile and zlib.error are caught before the generic handler."""
        func = _function(app_tree, "read_comic_page")
        caught = [
            ast.unparse(h.type)
            for h in ast.walk(func)
            if isinstance(h, ast.ExceptHandler) and h.type is not None
        ]
        assert any(
            "BadZipFile" in c and "zlib.error" in c for c in caught
        ), f"no corrupt-archive handler in read_comic_page: {caught}"

    def test_that_handler_logs_no_traceback(self, app_tree):
        """format_exc() would re-print the multi-KB message we just collapsed."""
        func = _function(app_tree, "read_comic_page")
        for handler in ast.walk(func):
            if not isinstance(handler, ast.ExceptHandler) or handler.type is None:
                continue
            spec = ast.unparse(handler.type)
            if "BadZipFile" not in spec:
                continue
            body = "\n".join(ast.unparse(stmt) for stmt in handler.body)
            assert "describe_archive_error" in body
            assert "format_exc" not in body
            return
        pytest.fail("corrupt-archive handler not found in read_comic_page")

    def test_page_info_does_not_hand_the_raw_message_to_the_client(self, app_tree):
        """str(e) in the JSON body would ship the binary blob to the browser."""
        func = _function(app_tree, "read_comic_page_info")
        for handler in ast.walk(func):
            if not isinstance(handler, ast.ExceptHandler):
                continue
            body = "\n".join(ast.unparse(stmt) for stmt in handler.body)
            if "jsonify" not in body:
                continue
            assert "describe_archive_error" in body
            assert "str(e)" not in body
            return
        pytest.fail("no jsonify error handler found in read_comic_page_info")
