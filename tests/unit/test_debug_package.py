"""Unit tests for core/debug_package.py helpers (no Flask, no DB)."""
import json

from core import debug_package as dp


class TestIsSensitiveKey:

    def test_marker_substrings_match(self):
        assert dp._is_sensitive_key("COMICVINE_API_KEY")
        assert dp._is_sensitive_key("METRON_PASSWORD")
        assert dp._is_sensitive_key("api_token")
        assert dp._is_sensitive_key("client_secret")

    def test_explicit_keys_match(self):
        assert dp._is_sensitive_key("METRON_USERNAME")

    def test_plain_keys_not_sensitive(self):
        assert not dp._is_sensitive_key("BOOTSTRAP_THEME")
        assert not dp._is_sensitive_key("AUTOCONVERT")
        assert not dp._is_sensitive_key("")


class TestRedactedConfigIni:

    def test_secrets_masked_others_preserved(self, tmp_path):
        cfg = tmp_path / "config.ini"
        cfg.write_text(
            "[SETTINGS]\n"
            "COMICVINE_API_KEY = SECRET1234VALUE\n"
            "BOOTSTRAP_THEME = darkly\n"
        )
        out = dp._redacted_config_ini(str(cfg))
        assert "SECRET1234VALUE" not in out
        assert "BOOTSTRAP_THEME = darkly" in out
        assert "..." in out  # masked value present

    def test_empty_secret_left_alone(self, tmp_path):
        cfg = tmp_path / "config.ini"
        cfg.write_text("[SETTINGS]\nPIXELDRAIN_API_KEY = \n")
        out = dp._redacted_config_ini(str(cfg))
        # Empty value stays empty (nothing to mask)
        assert "PIXELDRAIN_API_KEY" in out

    def test_missing_file_placeholder(self, tmp_path):
        out = dp._redacted_config_ini(str(tmp_path / "nope.ini"))
        assert "not found" in out


class TestTail:

    def test_returns_last_n_lines(self, tmp_path):
        log = tmp_path / "app.log"
        log.write_text("".join(f"line {i}\n" for i in range(100)))
        out = dp._tail(str(log), lines=10)
        assert out.splitlines() == [f"line {i}" for i in range(90, 100)]

    def test_missing_file_placeholder(self, tmp_path):
        out = dp._tail(str(tmp_path / "missing.log"))
        assert "not found" in out


class TestSystemInfoJson:

    def test_has_version_and_no_obvious_secret(self):
        info = json.loads(dp._system_info_json())
        assert "version" in info
        assert "paths" in info
        assert "flags" in info
