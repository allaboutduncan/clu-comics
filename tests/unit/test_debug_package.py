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

    def test_client_id_variants_match(self):
        # Separators are stripped before matching, so all of these are caught.
        assert dp._is_sensitive_key("CF-Access-Client-Id")
        assert dp._is_sensitive_key("client_id")
        assert dp._is_sensitive_key("ClientId")
        assert dp._is_sensitive_key("CF-Access-Client-Secret")

    def test_plain_keys_not_sensitive(self):
        assert not dp._is_sensitive_key("BOOTSTRAP_THEME")
        assert not dp._is_sensitive_key("AUTOCONVERT")
        assert not dp._is_sensitive_key("custom_headers")
        assert not dp._is_sensitive_key("")


class TestRedactBlob:

    def test_masks_nested_header_dict_in_config_value(self):
        # HEADERS-style value: top-level key is innocuous, secrets are nested.
        value = ('{"CF-Access-Client-Id": "4tyjwrtyhjdtyj.access", '
                 '"CF-Access-Client-Secret": "e5yjthyjfghjsecret"}')
        out = dp._redact_blob(value)
        assert "4tyjwrtyhjdtyj.access" not in out
        assert "e5yjthyjfghjsecret" not in out
        # Structure / key names are preserved.
        assert "CF-Access-Client-Id" in out
        assert "CF-Access-Client-Secret" in out

    def test_masks_json_encoded_db_blob_with_escapes(self):
        # Mirrors a JSON-encoded preference value (escaped inner quotes).
        value = ('"{\\"CF-Access-Client-Id\\": \\"4tyjwrtyhjdtyj.access\\",'
                 '\\"CF-Access-Client-Secret\\":\\"e5yjthyjfghjsecret\\"}"')
        out = dp._redact_blob(value)
        assert "4tyjwrtyhjdtyj.access" not in out
        assert "e5yjthyjfghjsecret" not in out

    def test_leaves_non_secret_pairs_untouched(self):
        value = '{"theme": "darkly", "limit": "10"}'
        assert dp._redact_blob(value) == value

    def test_passthrough_non_string(self):
        assert dp._redact_blob(None) is None
        assert dp._redact_blob("") == ""


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
