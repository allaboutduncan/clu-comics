"""Unit tests for helpers.match_parent_permissions.

CLU writes sidecars (cvinfo, series.json) with a plain open()/mkstemp, leaving
them unwritable for shared/NAS accounts even when the containing folder is
group-writable. match_parent_permissions makes a freshly written file inherit
its parent folder's accessibility (group + rwx minus execute), best-effort.
"""
import os
import stat

import pytest

from helpers import match_parent_permissions


@pytest.mark.skipif(os.name == 'nt', reason='POSIX chmod/group semantics')
class TestMatchParentPermissions:

    def test_file_inherits_parent_mode_minus_execute(self, tmp_path):
        folder = tmp_path
        os.chmod(folder, 0o775)
        f = folder / 'cvinfo'
        f.write_text('x')
        os.chmod(f, 0o600)  # start owner-only, like mkstemp output

        match_parent_permissions(str(f))

        mode = stat.S_IMODE(os.stat(f).st_mode)
        assert mode == 0o664  # 775 dir -> 664 file (execute dropped)

    def test_world_writable_folder_yields_666(self, tmp_path):
        folder = tmp_path
        os.chmod(folder, 0o777)
        f = folder / 'series.json'
        f.write_text('{}')
        os.chmod(f, 0o600)

        match_parent_permissions(str(f))

        assert stat.S_IMODE(os.stat(f).st_mode) == 0o666

    def test_file_inherits_parent_group(self, tmp_path):
        f = tmp_path / 'cvinfo'
        f.write_text('x')

        match_parent_permissions(str(f))

        assert os.stat(f).st_gid == os.stat(tmp_path).st_gid


class TestMatchParentPermissionsBestEffort:
    """Must never raise — it is a best-effort cosmetic step."""

    def test_missing_path_is_silent_noop(self, tmp_path):
        # Parent exists, file does not: chmod fails, helper swallows it.
        match_parent_permissions(str(tmp_path / 'does-not-exist'))

    def test_missing_parent_is_silent_noop(self, tmp_path):
        match_parent_permissions(str(tmp_path / 'no' / 'such' / 'cvinfo'))
