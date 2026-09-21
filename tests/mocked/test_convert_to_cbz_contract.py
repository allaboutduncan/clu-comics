"""``convert_to_cbz`` reports its own outcome, and callers must believe it.

The bug: a conversion can write a complete, valid CBZ and *still* fail -- the
observed case was ``shutil.move``'s ``copystat`` raising EPERM on a mount that
refuses ``utime``/``chmod``, after the archive had been copied in full. When it
does, ``convert_to_cbz`` deliberately leaves the source ``.cbr`` in place.

Every caller used to answer "did it work?" with ``os.path.exists(<base>.cbz)``,
which is a different question, and the answer was yes. So monitor.py logged
"Converted to" one second after app.log logged "Failed to convert", the .cbr
was never deleted, and nothing ever retried it. 23 CBR/CBZ pairs accumulated in
one user's TARGET folder.
"""
import os
from unittest.mock import patch

import pytest


def _patched_db():
    """Silence the file-index writes convert_to_cbz does on success."""
    return (
        patch("core.database.invalidate_browse_cache"),
        patch("core.database.delete_file_index_entry"),
        patch("core.database.add_file_index_entry"),
        patch("core.database.move_path_references"),
    )


def _run(cbr_path, inner):
    from cbz_ops.single_file import convert_to_cbz

    patches = _patched_db()
    for p in patches:
        p.start()
    try:
        with patch("cbz_ops.single_file.convert_single_rar_file",
                   side_effect=inner):
            return convert_to_cbz(str(cbr_path))
    finally:
        for p in patches:
            p.stop()


class TestReturnValue:
    def test_true_and_source_removed_on_success(self, tmp_path):
        cbr = tmp_path / "Batman 001.cbr"
        cbr.write_bytes(b"fake rar")

        def ok(rar_path, cbz_path, temp_extraction_dir, problem_source=None):
            with open(cbz_path, "wb") as f:
                f.write(b"fake cbz")
            return True

        assert _run(cbr, ok) is True
        assert (tmp_path / "Batman 001.cbz").exists()
        assert not cbr.exists(), "the source archive must be consumed"

    def test_false_and_source_kept_when_a_cbz_was_still_written(self, tmp_path):
        """The exact shape of the user's failure.

        The CBZ is on disk and perfectly readable; the conversion still failed.
        Returning True here -- or letting a caller infer it from the
        filesystem -- is what left a CBR beside every CBZ.
        """
        cbr = tmp_path / "Batman 002.cbr"
        cbr.write_bytes(b"fake rar")

        def wrote_then_failed(rar_path, cbz_path, temp_extraction_dir,
                              problem_source=None):
            with open(cbz_path, "wb") as f:
                f.write(b"a complete cbz")
            return False

        assert _run(cbr, wrote_then_failed) is False
        assert (tmp_path / "Batman 002.cbz").exists(), "fixture precondition"
        assert cbr.exists(), "a failed conversion must not delete the source"

    def test_false_for_a_missing_file(self, tmp_path):
        from cbz_ops.single_file import convert_to_cbz

        assert convert_to_cbz(str(tmp_path / "nope.cbr")) is False

    def test_false_for_an_unrecognised_extension(self, tmp_path):
        from cbz_ops.single_file import convert_to_cbz

        other = tmp_path / "notes.txt"
        other.write_bytes(b"hello")
        assert convert_to_cbz(str(other)) is False

    def test_cbz_input_reports_the_rebuild_outcome(self, tmp_path):
        from cbz_ops.single_file import convert_to_cbz

        cbz = tmp_path / "Batman 003.cbz"
        cbz.write_bytes(b"fake cbz")

        with patch("cbz_ops.single_file.rebuild_single_cbz_file",
                   return_value=False):
            assert convert_to_cbz(str(cbz)) is False
        with patch("cbz_ops.single_file.rebuild_single_cbz_file",
                   return_value=True):
            assert convert_to_cbz(str(cbz)) is True


class TestProblemHandoff:
    """A failed conversion is otherwise invisible: one ERROR line in a log."""

    def test_failure_records_a_problem_against_the_source(self, tmp_path):
        from core.problem_files import SOURCE_CONVERT

        cbr = tmp_path / "Batman 004.cbr"
        cbr.write_bytes(b"fake rar")
        recorded = []

        def fail(rar_path, cbz_path, temp_extraction_dir, problem_source=None):
            # The real convert_single_rar_file records on its own failure
            # paths; here we only need to see the source reach it.
            recorded.append((rar_path, problem_source))
            return False

        assert _run(cbr, fail) is False
        assert recorded == [(str(cbr), SOURCE_CONVERT)]

    def test_success_clears_the_problem_row(self, tmp_path):
        from core.problem_files import SOURCE_CONVERT

        cbr = tmp_path / "Batman 005.cbr"
        cbr.write_bytes(b"fake rar")
        cleared = []

        def ok(rar_path, cbz_path, temp_extraction_dir, problem_source=None):
            with open(cbz_path, "wb") as f:
                f.write(b"fake cbz")
            return True

        with patch("core.problem_files.clear_problem",
                   side_effect=lambda p, s=None: cleared.append((p, s))):
            assert _run(cbr, ok) is True

        assert cleared == [(str(cbr), SOURCE_CONVERT)]

    def test_extraction_failure_is_classified_as_a_rar_problem(self, tmp_path):
        """Damaged or password-protected archives are the common real case.

        CLASS_RAR_FAILED is what makes classify() say "replace the file" and
        demote Rebuild, rather than falling back to neutral wording.
        """
        from cbz_ops.single_file import convert_single_rar_file
        from core.problem_files import CLASS_RAR_FAILED, SOURCE_CONVERT

        rar = tmp_path / "Batman 006.cbr"
        rar.write_bytes(b"not really a rar")
        recorded = []

        with patch("cbz_ops.single_file.extract_rar_with_unar",
                   return_value=(False, 0)), \
                patch("core.problem_files.record_problem",
                      side_effect=lambda *a, **k: recorded.append((a, k))):
            ok = convert_single_rar_file(
                str(rar), str(tmp_path / "Batman 006.cbz"),
                str(tmp_path / ".temp"), problem_source=SOURCE_CONVERT,
            )

        assert ok is False
        assert len(recorded) == 1
        args, kwargs = recorded[0]
        assert args == (str(rar), SOURCE_CONVERT)
        assert kwargs["error_class"] == CLASS_RAR_FAILED

    def test_no_source_means_no_row(self, tmp_path):
        """The rebuild fallback records its own row against the .cbz path.

        Two rows for one failure is how they drift apart, so an unnamed source
        must stay silent.
        """
        from cbz_ops.single_file import convert_single_rar_file

        rar = tmp_path / "Batman 007.cbr"
        rar.write_bytes(b"not really a rar")

        with patch("cbz_ops.single_file.extract_rar_with_unar",
                   return_value=(False, 0)), \
                patch("core.problem_files.record_problem",
                      side_effect=AssertionError("must not record")):
            assert convert_single_rar_file(
                str(rar), str(tmp_path / "Batman 007.cbz"),
                str(tmp_path / ".temp"),
            ) is False
