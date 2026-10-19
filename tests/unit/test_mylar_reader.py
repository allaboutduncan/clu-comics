"""core.mylar_reader: Mylar3's mylar.db and config.ini, quirks included."""

import base64
import sqlite3

import pytest

from core.mylar_reader import (
    MylarReadError,
    decode_secret,
    open_mylar_db,
    parse_client_url,
    parse_extra_newznabs,
    read_config,
    read_mylar_db,
)


def encode(plain, salt=b"12345678"):
    """Mylar's encrypt_it: prefix + base64(plaintext + 8 salt bytes)."""
    return "^~$z$" + base64.b64encode(plain.encode("utf-8") + salt).decode("ascii")


def make_db(path, comics_columns=None, comics=(), issues=(), annuals=0):
    columns = comics_columns or [
        "ComicID", "ComicName", "ComicYear", "Corrected_SeriesYear", "ComicPublisher",
        "ComicLocation", "Status", "Type", "Corrected_Type", "Total", "Have",
    ]
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE comics ({', '.join(c + ' TEXT' for c in columns)})")
    for row in comics:
        conn.execute(
            f"INSERT INTO comics ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
            list(row.values()),
        )
    conn.execute("CREATE TABLE issues (IssueID TEXT, ComicID TEXT, Issue_Number TEXT, Status TEXT)")
    conn.executemany("INSERT INTO issues VALUES (?, ?, ?, ?)", issues)
    conn.execute("CREATE TABLE annuals (IssueID TEXT, ComicID TEXT)")
    conn.executemany("INSERT INTO annuals VALUES (?, ?)", [(str(i), "1") for i in range(annuals)])
    conn.commit()
    conn.close()
    return str(path)


class TestDecodeSecret:
    def test_round_trip(self):
        assert decode_secret(encode("s3cret-key")) == "s3cret-key"

    def test_plain_value_passes_through(self):
        assert decode_secret("plainkey") == "plainkey"
        assert decode_secret(None) is None

    def test_undecodable_value_is_returned_unchanged(self):
        assert decode_secret("^~$z$not base64!!") == "^~$z$not base64!!"


class TestReadMylarDb:
    def test_reads_watchlist(self, tmp_path):
        db = make_db(tmp_path / "mylar.db", comics=[
            {"ComicID": "12345", "ComicName": "Batman", "ComicYear": "2011",
             "Corrected_SeriesYear": "None", "ComicPublisher": "DC Comics",
             "ComicLocation": "/comics/Batman (2011)", "Status": "Active", "Total": "52"},
            {"ComicID": "777", "ComicName": "Saga", "ComicYear": "2012",
             "Corrected_SeriesYear": "2013", "ComicLocation": "/comics/Saga",
             "Status": "Paused"},
        ], issues=[
            ("1", "12345", "1", "Downloaded"),
            ("2", "12345", "2", "Skipped"),
            ("3", "12345", "3", None),
        ], annuals=2)

        result = read_mylar_db(db)
        assert [s["name"] for s in result["series"]] == ["Batman", "Saga"]
        batman, saga = result["series"]
        assert batman["cv_id"] == 12345
        assert batman["year"] == 2011
        assert batman["publisher"] == "DC Comics"
        assert batman["status"] == "Active"
        assert batman["total"] == 52
        # NULL issue status is Skipped, as in Mylar's UI.
        assert [i["status"] for i in batman["issues"]] == ["Downloaded", "Skipped", "Skipped"]
        # Corrected_SeriesYear overrides ComicYear.
        assert saga["year"] == 2013
        assert saga["status"] == "Paused"
        assert result["annual_count"] == 2

    def test_old_schema_without_optional_columns(self, tmp_path):
        db = make_db(
            tmp_path / "mylar.db",
            comics_columns=["ComicID", "ComicName", "ComicYear", "ComicLocation", "Status"],
            comics=[{"ComicID": "5", "ComicName": "Hulk", "ComicYear": "None",
                     "ComicLocation": "None", "Status": "Loading"}],
        )
        (hulk,) = read_mylar_db(db)["series"]
        assert hulk["year"] is None
        assert hulk["location"] is None
        assert hulk["publisher"] is None
        assert hulk["status"] == "Active"  # Loading is a series still being added

    def test_gcd_and_bad_ids_are_excluded_with_a_reason(self, tmp_path):
        db = make_db(tmp_path / "mylar.db", comics=[
            {"ComicID": "G1234", "ComicName": "Old Series"},
            {"ComicID": "abc", "ComicName": "Odd"},
        ])
        result = read_mylar_db(db)
        assert result["series"] == []
        reasons = {e["name"]: e["reason"] for e in result["excluded"]}
        assert "Grand Comics Database" in reasons["Old Series"]
        assert "abc" in reasons["Odd"]

    def test_not_a_mylar_db(self, tmp_path):
        path = tmp_path / "other.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE x (a)")
        conn.close()
        with pytest.raises(MylarReadError):
            open_mylar_db(path)

    def test_not_a_database(self, tmp_path):
        path = tmp_path / "mylar.db"
        path.write_bytes(b"this is not sqlite" * 100)
        with pytest.raises(MylarReadError):
            read_mylar_db(path)


class TestParseClientUrl:
    def test_sab_url_with_base(self):
        assert parse_client_url("https://sab.local:8443/sabnzbd/") == {
            "host": "sab.local", "port": 8443, "use_ssl": True, "url_base": "sabnzbd",
        }

    def test_bare_host_and_separate_port(self):
        assert parse_client_url("nzbget", "6789") == {
            "host": "nzbget", "port": 6789, "use_ssl": False, "url_base": None,
        }

    def test_blank(self):
        assert parse_client_url("None") is None
        assert parse_client_url("") is None


class TestExtraNewznabs:
    def test_seven_field_format(self):
        raw = (
            "NZBGeek, https://api.nzbgeek.info, 1, abcdef, 12345#7030#7020, 1, 2, "
            "Hydra[nzbhydra], http://hydra:5076, 0, " + encode("hydrakey") + ", , 0, 3"
        )
        providers, error = parse_extra_newznabs(raw, 15)
        assert error is None
        assert providers == [
            {"name": "NZBGeek", "url": "https://api.nzbgeek.info", "api_key": "abcdef",
             "categories": "7030,7020", "enabled": True},
            {"name": "Hydra", "url": "http://hydra:5076", "api_key": "hydrakey",
             "categories": "7030", "enabled": False},
        ]

    def test_six_field_format_before_version_11(self):
        raw = "Old, http://old.example, 1, key, uid, 1"
        providers, error = parse_extra_newznabs(raw, 10)
        assert error is None
        assert providers[0]["url"] == "http://old.example"
        assert providers[0]["categories"] == "7030"

    def test_ragged_string_reports_an_error(self):
        providers, error = parse_extra_newznabs("A, http://a, 1, k, u, 1, 1, B, http://b", 15)
        assert len(providers) == 1
        assert "not a multiple" in error

    def test_empty(self):
        assert parse_extra_newznabs("None", 15) == ([], None)


INI = """
[General]
config_version = 15
destination_dir = /comics
folder_format = $Series ($Year)

[CV]
comicvine_api = {cv}

[Client]
nzb_downloader = 0

[SABnzbd]
sab_host = http://sabnzbd:8080
sab_apikey = {sab}
sab_category = comics
sab_priority = High

[NZBGet]
nzbget_host = http://nzbget
nzbget_port = 6789
nzbget_password = 100%%pass

[Newznab]
newznab = True
extra_newznabs = Geek, https://api.nzbgeek.info, 1, gk, 1#7030, 1, 1

[Torznab]
extra_torznabs = T1, http://t1, 1, k, 8020, 1, 1, T2, http://t2, 1, k, 8020, 1, 2
"""


class TestReadConfig:
    def test_reads_everything(self, tmp_path):
        path = tmp_path / "config.ini"
        path.write_text(INI.format(cv=encode("cvkey"), sab=encode("sabkey")), encoding="utf-8")
        config = read_config(path)
        assert config["destination_dir"] == "/comics"
        assert config["comicvine_api"] == "cvkey"
        assert config["nzb_downloader"] == "sabnzbd"
        assert config["sabnzbd"] == {
            "host": "sabnzbd", "port": 8080, "use_ssl": False, "url_base": None,
            "api_key": "sabkey", "username": None, "password": None,
            "category": "comics", "priority": 1,
        }
        assert config["nzbget"]["port"] == 6789
        assert config["nzbget"]["password"] == "100%pass"  # %% unescaped
        assert config["newznabs"][0]["api_key"] == "gk"
        assert config["torznab_count"] == 2

    def test_minimal_ini_missing_keys(self, tmp_path):
        path = tmp_path / "config.ini"
        path.write_text("[General]\nconfig_version = 15\n", encoding="utf-8")
        config = read_config(path)
        assert config["comicvine_api"] is None
        assert config["nzb_downloader"] is None
        assert config["sabnzbd"] is None
        assert config["newznabs"] == []
        assert config["torznab_count"] == 0

    def test_newznab_master_switch_disables_all(self, tmp_path):
        path = tmp_path / "config.ini"
        path.write_text(
            "[Newznab]\nnewznab = False\n"
            "extra_newznabs = Geek, https://g, 1, gk, 1, 1, 1\n",
            encoding="utf-8",
        )
        assert read_config(path)["newznabs"][0]["enabled"] is False

    def test_not_an_ini(self, tmp_path):
        path = tmp_path / "config.ini"
        path.write_text("just text", encoding="utf-8")
        with pytest.raises(MylarReadError):
            read_config(path)
