"""#796: where a series already lives, asked before subscribe makes a folder."""
from unittest.mock import patch

import pytest

from core import series_home
from core.series_home import find_existing_series_home, same_folder
from models import cbl
from models.comicvine import parse_cvinfo_ids, parse_cvinfo_volume_id


def _folder(root, name, cvinfo=None):
    folder = root / name
    folder.mkdir(parents=True)
    if cvinfo is not None:
        (folder / "cvinfo").write_text(cvinfo)
    return str(folder)


@pytest.fixture
def no_db_rows():
    with patch("core.database.find_mapped_series_by_cv_id", return_value=[]):
        yield


def _maps(cv=None, metron=None):
    return (patch.object(cbl, "cv_volume_folders", return_value=cv or {}),
            patch.object(cbl, "metron_series_folders", return_value=metron or {}))


class TestParseCvinfoIds:

    def test_both_ids_from_one_file(self, tmp_path):
        path = tmp_path / "cvinfo"
        path.write_text("https://comicvine.gamespot.com/x/4050-91273/\nseries_id: 100\n")
        assert parse_cvinfo_ids(str(path)) == (91273, 100)
        assert parse_cvinfo_volume_id(str(path)) == 91273

    def test_either_may_be_missing(self, tmp_path):
        path = tmp_path / "cvinfo"
        path.write_text("series_id: 100\n")
        assert parse_cvinfo_ids(str(path)) == (None, 100)
        path.write_text("https://comicvine.gamespot.com/x/4050-91273/\n")
        assert parse_cvinfo_ids(str(path)) == (91273, None)

    def test_unreadable_file_is_no_ids(self, tmp_path):
        assert parse_cvinfo_ids(str(tmp_path / "missing")) == (None, None)


class TestMetronSeriesFolders:

    def test_built_with_the_comicvine_map(self, tmp_path):
        both = _folder(tmp_path, "Both", "https://x/4050-91273/\nseries_id: 100\n")
        metron_only = _folder(tmp_path, "Metron only", "series_id: 200\n")
        bare = _folder(tmp_path, "Bare")
        cbl._cv_folder_cache.update(at=0.0, map=None)
        try:
            with patch("core.database.list_comic_dirs",
                       return_value=[both, metron_only, bare]):
                assert cbl.metron_series_folders() == {100: [both], 200: [metron_only]}
                assert cbl.cv_volume_folders() == {91273: [both]}
        finally:
            cbl._cv_folder_cache.update(at=0.0, map=None, metron=None)


class TestSameFolder:

    def test_spelling_differences(self, tmp_path):
        folder = _folder(tmp_path, "X (2016)")
        assert same_folder(folder, folder + "/")
        assert same_folder("/data/X", "/data/Y/../X")
        assert not same_folder("/data/X", "/data/X (2016)")
        assert not same_folder(None, "/data/X")


@pytest.mark.usefixtures("no_db_rows")
class TestFindExistingSeriesHome:

    def test_nothing_known_is_no_home(self):
        cv, metron = _maps()
        with cv, metron:
            assert find_existing_series_home(
                {"id": 100, "name": "X", "cv_id": 91273}, "/data/X (2016)") is None

    def test_own_mapping_elsewhere(self):
        series = {"id": 100, "cv_id": 91273, "mapped_path": "/data/X/X Volume 01 (2016)"}
        home = find_existing_series_home(series, "/data/X (2016)")
        assert home["reason"] == "mapped"
        assert home["path"] == "/data/X/X Volume 01 (2016)"

    def test_own_mapping_to_the_requested_folder_is_not_a_conflict(self):
        series = {"id": 100, "cv_id": None, "mapped_path": "/data/X (2016)"}
        cv, metron = _maps()
        with cv, metron:
            assert find_existing_series_home(series, "/data/X (2016)/") is None

    def test_another_row_with_the_same_comicvine_volume(self):
        other = [{"id": 555, "name": "X again", "mapped_path": "/data/X/Old"}]
        with patch("core.database.find_mapped_series_by_cv_id", return_value=other):
            home = find_existing_series_home(
                {"id": 100, "cv_id": 91273}, "/data/X (2016)")
        assert home == {"path": "/data/X/Old", "reason": "cv_mapped",
                        "series_id": 555, "series_name": "X again"}

    def test_folder_whose_cvinfo_names_the_volume(self, tmp_path):
        folder = _folder(tmp_path, "X Volume 01 (2016)", "https://x/4050-91273/\n")
        cv, metron = _maps(cv={91273: [folder]})
        with cv, metron:
            home = find_existing_series_home(
                {"id": 100, "cv_id": 91273}, str(tmp_path / "X (2016)"))
        assert home["reason"] == "cvinfo" and home["path"] == folder

    def test_folder_whose_cvinfo_names_the_metron_series(self, tmp_path):
        folder = _folder(tmp_path, "X Volume 01 (2016)", "series_id: 100\n")
        cv, metron = _maps(metron={100: [folder]})
        with cv, metron:
            home = find_existing_series_home(
                {"id": 100, "cv_id": None}, str(tmp_path / "X (2016)"))
        assert home["path"] == folder

    def test_the_requested_folder_itself_is_not_a_conflict(self, tmp_path):
        folder = _folder(tmp_path, "X (2016)", "https://x/4050-91273/\n")
        cv, metron = _maps(cv={91273: [folder]})
        with cv, metron:
            assert find_existing_series_home(
                {"id": 100, "cv_id": 91273}, folder) is None

    def test_a_stale_map_entry_is_rechecked_on_disk(self, tmp_path):
        """The maps are cached for minutes: a folder deleted or re-tagged
        since must not block a subscribe."""
        retagged = _folder(tmp_path, "Retagged", "https://x/4050-5/\n")
        gone = str(tmp_path / "Deleted")
        cv, metron = _maps(cv={91273: [retagged, gone]})
        with cv, metron:
            assert find_existing_series_home(
                {"id": 100, "cv_id": 91273}, str(tmp_path / "X (2016)")) is None

    def test_a_failed_folder_read_does_not_block(self):
        with patch.object(cbl, "cv_volume_folders", side_effect=OSError("nas")):
            assert find_existing_series_home(
                {"id": 100, "cv_id": 91273}, "/data/X (2016)") is None

    def test_names_are_never_evidence(self, tmp_path):
        """Identity only. A similarly named folder with no ids is not a home."""
        _folder(tmp_path, "X Volume 01 (2016)")
        cv, metron = _maps()
        with cv, metron:
            assert find_existing_series_home(
                {"id": 100, "name": "X", "cv_id": 91273},
                str(tmp_path / "X (2016)")) is None


class TestFindMappedSeriesByCvId:

    def test_other_mapped_rows_only(self, db_connection):
        from core.database import find_mapped_series_by_cv_id

        db_connection.executemany(
            "INSERT INTO series (id, name, cv_id, mapped_path) VALUES (?, ?, ?, ?)",
            [(100, "X", 91273, None),
             (555, "X again", 91273, "/data/X/Old"),
             (556, "X unmapped", 91273, ""),
             (557, "Y", 7, "/data/Y")],
        )
        db_connection.commit()

        assert find_mapped_series_by_cv_id(91273, exclude_id=100) == [
            {"id": 555, "name": "X again", "mapped_path": "/data/X/Old"}]
        assert find_mapped_series_by_cv_id(91273, exclude_id=555) == []
        assert find_mapped_series_by_cv_id(None) == []


def test_subscribe_asks_before_it_creates_the_folder():
    """The check must sit ahead of os.makedirs in the route."""
    import inspect
    import routes.series

    body = inspect.getsource(routes.series.subscribe_series)
    assert body.index("_existing_home_conflict(") < body.index("os.makedirs(")
    assert series_home.find_existing_series_home.__name__ in inspect.getsource(
        routes.series._existing_home_conflict)
