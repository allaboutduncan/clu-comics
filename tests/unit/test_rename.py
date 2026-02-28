"""Tests for cbz_ops/rename.py -- filename parsing and renaming logic."""
import os
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Patch heavy imports that rename.py pulls in at module level
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _mock_rename_deps():
    with patch("cbz_ops.rename.app_logger"), \
         patch("cbz_ops.rename.is_hidden", return_value=False):
        yield


# ===== smart_title_case =====

class TestSmartTitleCase:

    def test_basic(self):
        from cbz_ops.rename import smart_title_case
        assert smart_title_case("batman the dark knight") == "Batman the Dark Knight"

    def test_first_word_always_capitalised(self):
        from cbz_ops.rename import smart_title_case
        assert smart_title_case("the amazing spider-man") == "The Amazing Spider-Man"

    def test_hyphenated_words(self):
        from cbz_ops.rename import smart_title_case
        assert smart_title_case("x-men") == "X-Men"

    def test_articles_lowercase(self):
        from cbz_ops.rename import smart_title_case
        assert smart_title_case("lord of the rings") == "Lord of the Rings"

    def test_conjunctions(self):
        from cbz_ops.rename import smart_title_case
        assert smart_title_case("romeo and juliet") == "Romeo and Juliet"

    def test_empty_string(self):
        from cbz_ops.rename import smart_title_case
        assert smart_title_case("") == ""

    def test_single_word(self):
        from cbz_ops.rename import smart_title_case
        assert smart_title_case("batman") == "Batman"

    def test_all_caps(self):
        from cbz_ops.rename import smart_title_case
        assert smart_title_case("BATMAN THE DARK KNIGHT") == "Batman the Dark Knight"


# ===== _capitalize_word =====

class TestCapitalizeWord:

    def test_simple(self):
        from cbz_ops.rename import _capitalize_word
        assert _capitalize_word("batman") == "Batman"

    def test_hyphenated(self):
        from cbz_ops.rename import _capitalize_word
        assert _capitalize_word("spider-man") == "Spider-Man"

    def test_double_hyphen(self):
        from cbz_ops.rename import _capitalize_word
        assert _capitalize_word("x-force-one") == "X-Force-One"


# ===== norm_issue =====

class TestNormIssue:

    @pytest.mark.parametrize("input_val,expected", [
        ("1", "001"),
        ("12", "012"),
        ("123", "123"),
        ("0", "000"),
        ("1234", "1234"),
        ("", ""),
        (None, ""),
    ])
    def test_norm_issue(self, input_val, expected):
        from cbz_ops.rename import norm_issue
        assert norm_issue(input_val) == expected


# ===== clean_final_filename =====

class TestCleanFinalFilename:

    def test_removes_empty_parens(self):
        from cbz_ops.rename import clean_final_filename
        assert clean_final_filename("Title () .cbz") == "Title .cbz"

    def test_collapses_spaces(self):
        from cbz_ops.rename import clean_final_filename
        assert clean_final_filename("Title   Name.cbz") == "Title Name.cbz"

    def test_none_returns_none(self):
        from cbz_ops.rename import clean_final_filename
        assert clean_final_filename(None) is None

    def test_empty_returns_empty(self):
        from cbz_ops.rename import clean_final_filename
        assert clean_final_filename("") == ""


# ===== clean_parentheses_content =====

class TestCleanParenthesesContent:
    """Tests derived from the inline test_parentheses_cleaning() in rename.py."""

    @pytest.mark.parametrize("input_name,expected", [
        # Remove parentheses without 4-digit year
        ("2000AD 1700 (01-09-10).cbz", "2000AD 1700.cbz"),
        # Keep 4-digit year, remove everything after
        ("Comic Name v3 051 (2018) (DCP-Scan Final).cbz", "Comic Name v3 051 (2018).cbz"),
        # Keep 4-digit year, remove digital/scan
        ("Title (2019) (digital) (scan).cbz", "Title (2019).cbz"),
        # No 4-digit year, remove all parentheses
        ("Comic (digital) (scan) (final).cbz", "Comic.cbz"),
        # Multiple years, keep first
        ("Comic (2018) (2019) (digital).cbz", "Comic (2018).cbz"),
        # Year in middle
        ("Comic (scan) (2018) (digital).cbz", "Comic (2018).cbz"),
        # No parentheses at all
        ("Comic Name 001.cbz", "Comic Name 001.cbz"),
        # Only year parentheses (no change)
        ("Comic Name (2020).cbz", "Comic Name (2020).cbz"),
    ])
    def test_parentheses_cleaning(self, input_name, expected):
        from cbz_ops.rename import clean_parentheses_content
        assert clean_parentheses_content(input_name) == expected


# ===== clean_filename_pre =====

class TestCleanFilenamePre:

    def test_removes_brackets(self):
        from cbz_ops.rename import clean_filename_pre
        result = clean_filename_pre("Comic [Tag] 001.cbz")
        assert "[" not in result
        assert "Tag" not in result

    def test_underscores_to_spaces(self):
        from cbz_ops.rename import clean_filename_pre
        result = clean_filename_pre("Comic_Name_001.cbz")
        assert "_" not in result
        assert "Comic Name" in result

    def test_removes_dash_issue(self):
        from cbz_ops.rename import clean_filename_pre
        result = clean_filename_pre("Title - Issue 001.cbz")
        assert "- Issue" not in result

    def test_year_dash_month_keeps_year(self):
        from cbz_ops.rename import clean_filename_pre
        result = clean_filename_pre("Title 2018-04 001.cbz")
        assert "2018" in result
        assert "2018-04" not in result


# ===== apply_custom_pattern =====

class TestApplyCustomPattern:
    """Tests derived from the inline test_custom_rename() in rename.py."""

    @pytest.fixture
    def sample_values(self):
        return {
            "series_name": "Spider-Man 2099",
            "volume_number": "v2",
            "year": "1992",
            "issue_number": "044",
            "issue_title": "The Last Dance",
        }

    @pytest.mark.parametrize("pattern,expected", [
        ("{series_name} {issue_number} ({year})", "Spider-Man 2099 044 (1992)"),
        ("{series_name} [{year}] {issue_number}", "Spider-Man 2099 [1992] 044"),
        ("issue{issue_number}", "issue044"),
        ("{volume_number}_{issue_number}", "v2_044"),
        ("{series_name} - {year}", "Spider-Man 2099 - 1992"),
        ("{series_name} {volume_number} {issue_number}", "Spider-Man 2099 v2 044"),
        (
            "{series_name} {issue_number} - {issue_title} ({year})",
            "Spider-Man 2099 044 - The Last Dance (1992)",
        ),
    ])
    def test_custom_patterns(self, sample_values, pattern, expected):
        from cbz_ops.rename import apply_custom_pattern
        assert apply_custom_pattern(sample_values, pattern) == expected

    def test_empty_pattern_returns_empty(self):
        from cbz_ops.rename import apply_custom_pattern
        assert apply_custom_pattern({"series_name": "X", "issue_number": "1"}, "") == ""

    def test_missing_series_returns_empty(self):
        from cbz_ops.rename import apply_custom_pattern
        assert apply_custom_pattern({"series_name": "", "issue_number": "001"}, "{series_name}") == ""

    def test_missing_issue_returns_empty(self):
        from cbz_ops.rename import apply_custom_pattern
        assert apply_custom_pattern({"series_name": "X", "issue_number": ""}, "{series_name}") == ""

    def test_sanitises_issue_title(self):
        from cbz_ops.rename import apply_custom_pattern
        values = {
            "series_name": "Test",
            "issue_number": "001",
            "issue_title": 'Bad:Name/With\\Chars"Here',
        }
        result = apply_custom_pattern(values, "{issue_title}")
        assert ":" not in result
        assert "/" not in result
        assert "\\" not in result
        assert '"' not in result


# ===== _apply_filters =====

class TestApplyFilters:

    def test_digits(self):
        from cbz_ops.rename import _apply_filters
        assert _apply_filters("abc123def", ["digits"]) == "123"

    def test_year4(self):
        from cbz_ops.rename import _apply_filters
        assert _apply_filters("199309", ["year4"]) == "1993"

    def test_pad3(self):
        from cbz_ops.rename import _apply_filters
        assert _apply_filters("5", ["pad3"]) == "005"

    def test_pad4(self):
        from cbz_ops.rename import _apply_filters
        assert _apply_filters("5", ["pad4"]) == "0005"

    def test_upper(self):
        from cbz_ops.rename import _apply_filters
        assert _apply_filters("hello", ["upper"]) == "HELLO"

    def test_lower(self):
        from cbz_ops.rename import _apply_filters
        assert _apply_filters("HELLO", ["lower"]) == "hello"

    def test_title(self):
        from cbz_ops.rename import _apply_filters
        assert _apply_filters("the dark knight", ["title"]) == "The Dark Knight"

    def test_chained_filters(self):
        from cbz_ops.rename import _apply_filters
        assert _apply_filters("abc045xyz", ["digits", "pad3"]) == "045"


# ===== _format_from_groups =====

class TestFormatFromGroups:

    def test_simple_substitution(self):
        from cbz_ops.rename import _format_from_groups
        result = _format_from_groups("{series} {issue}", {"series": "Batman", "issue": "42"})
        assert result == "Batman 42"

    def test_with_filters(self):
        from cbz_ops.rename import _format_from_groups
        result = _format_from_groups("{issue|pad3}", {"issue": "5"})
        assert result == "005"

    def test_missing_key_returns_empty(self):
        from cbz_ops.rename import _format_from_groups
        result = _format_from_groups("{missing}", {})
        assert result == ""


# ===== extract_comic_values =====

class TestExtractComicValues:

    @pytest.mark.parametrize("filename,expected_series,expected_issue,expected_year", [
        # Volume Issue keyword pattern
        ("Top 10 (1999) Volume 01 Issue 010.cbz", "Top 10", "010", "1999"),
        # Issue keyword pattern
        ("The Amazing Spider-Man (2018) Issue 080.BEY.cbz", "The Amazing Spider-Man", "080.BEY", "2018"),
        # Issue keyword decimal
        ("The Amazing Spider-Man (1999) Issue 700.1.cbz", "The Amazing Spider-Man", "700.1", "1999"),
        # Underscore series issue year
        (
            "Batman_-_Superman_-_Worlds_Finest_045_2025_Webrip_The_Last_Kryptonian-DCP.cbr",
            "Batman - Superman - Worlds Finest",
            "045",
            "2025",
        ),
        # YYYYMM Series v# ### pattern
        ("199309 Hokum & Hex v1 001.cbz", "Hokum & Hex V1", "001", "1993"),
        # Series YYYY-MM (NN) (YYYY) pattern
        ("Mister Miracle 1989-08 ( 08) (1989) (Digital) (Shadowcat-Empire).cbz", "Mister Miracle", "008", "1989"),
        # Title, YYYY-MM-DD (NN) pattern
        ("Blue Devil, 1984-04-00 (_01) (digital) (Glorith-Novus-HD).cbz", "Blue Devil", "001", "1984"),
        # Title, YYYY-MM-DD (#NN) pattern
        ("Legion of Super-Heroes, 1985-07-00 (#14) (digital) (Glorith-Novus-HD).cbz", "Legion of Super-Heroes", "014", "1985"),
        # Series (YYYY-MM) ### pattern
        ("Justice League (1987-09) 05 (DobisP.R.-Novus-HD).cbz", "Justice League", "005", "1987"),
    ])
    def test_value_extraction(self, filename, expected_series, expected_issue, expected_year):
        from cbz_ops.rename import extract_comic_values
        values = extract_comic_values(filename)
        assert values["series_name"] == expected_series
        assert values["issue_number"] == expected_issue
        assert values["year"] == expected_year

    def test_returns_all_keys(self):
        from cbz_ops.rename import extract_comic_values
        values = extract_comic_values("Batman 001 (2020).cbz")
        assert "series_name" in values
        assert "volume_number" in values
        assert "year" in values
        assert "issue_number" in values
        assert "issue_title" in values


# ===== get_renamed_filename =====

class TestGetRenamedFilename:
    """Tests for the main renaming entry point."""

    @pytest.fixture(autouse=True)
    def _disable_custom_rename(self):
        """Disable custom rename and rule engine for default-logic tests."""
        with patch("cbz_ops.rename.load_custom_rename_config", return_value=(False, "")), \
             patch("os.path.exists", return_value=False):
            yield

    # --- Pre-cleaning special-case patterns (run BEFORE clean_filename_pre) ---

    @pytest.mark.parametrize("filename,expected", [
        # ISSUE_YEAR_PARENTHESES_PATTERN
        ("Leonard Nimoy's Primortals (00 1996).cbz", "Leonard Nimoy's Primortals 000 (1996).cbz"),
        # TITLE_COMMA_YEAR_ISSUE_PATTERN
        (
            "Blue Devil, 1984-04-00 (_01) (digital) (Glorith-Novus-HD).cbz",
            "Blue Devil 001 (1984).cbz",
        ),
        # TITLE_COMMA_YEAR_ISSUE_PATTERN (regular numeric)
        (
            "Justice League Europe, 1990-02-00 ( 13) (digital) (OkC.O.M.P.U.T.O.-Novus-HD).cbz",
            "Justice League Europe 013 (1990).cbz",
        ),
        # TITLE_COMMA_YEAR_HASH_ISSUE_PATTERN
        (
            "Legion of Super-Heroes, 1985-07-00 (#14) (digital) (Glorith-Novus-HD).cbz",
            "Legion of Super-Heroes 014 (1985).cbz",
        ),
        # ISSUE_AFTER_YEAR_PATTERN
        (
            "Spider-Man 2099 (1992) #44 (digital) (Colecionadores.GO).cbz",
            "Spider-Man 2099 044 (1992).cbz",
        ),
        # YEAR_MONTH_SERIES_VOLUME_ISSUE_PATTERN
        ("199309 Hokum & Hex v1 001.cbz", "Hokum & Hex v1 001 (1993).cbz"),
        # SERIES_YEAR_MONTH_ISSUE_PATTERN
        (
            "Mister Miracle 1989-08 ( 08) (1989) (Digital) (Shadowcat-Empire).cbz",
            "Mister Miracle 008 (1989).cbz",
        ),
        # SERIES_YEAR_MONTH_DAY_ISSUE_PATTERN
        (
            "Mister Miracle 1990-09-18 ( 21) (digital) (Glorith-Novus-HD).cbz",
            "Mister Miracle 021 (1990).cbz",
        ),
    ])
    def test_pre_clean_patterns(self, filename, expected):
        from cbz_ops.rename import get_renamed_filename
        assert get_renamed_filename(filename) == expected

    # --- Post-cleaning patterns (run AFTER clean_filename_pre) ---

    @pytest.mark.parametrize("filename,expected", [
        # VOLUME_ISSUE_PATTERN
        ("Comic Name v3 051 (2018) (DCP-Scan Final).cbz", "Comic Name v3 051 (2018).cbz"),
        # ISSUE_HASH_PATTERN
        ("Title 2 #10 (2018).cbz", "Title 2 010 (2018).cbz"),
        # SERIES_ISSUE_PATTERN
        ("Injustice 2 001 (2018).cbz", "Injustice 2 001 (2018).cbz"),
        # ISSUE_PATTERN (single issue number)
        ("Comic Name 051 (2018).cbz", "Comic Name 051 (2018).cbz"),
        # ISSUE_PATTERN with volume
        ("Comic Name v3 (2022).cbr", "Comic Name v3 (2022).cbr"),
    ])
    def test_post_clean_patterns(self, filename, expected):
        from cbz_ops.rename import get_renamed_filename
        assert get_renamed_filename(filename) == expected

    def test_fallback_pattern(self):
        from cbz_ops.rename import get_renamed_filename
        result = get_renamed_filename("Comic Name (2018) some extra.cbz")
        assert result == "Comic Name (2018).cbz"

    def test_no_match_returns_none(self):
        from cbz_ops.rename import get_renamed_filename
        assert get_renamed_filename("random-file.txt") is None

    def test_2000ad_4digit_issue(self):
        from cbz_ops.rename import get_renamed_filename
        result = get_renamed_filename("2000AD (2018) #1795.cbz")
        assert result is not None
        assert "1795" in result
        assert "2018" in result

    def test_volume_subtitle(self):
        from cbz_ops.rename import get_renamed_filename
        result = get_renamed_filename("Infinity 8 v03 - The Gospel According to Emma (2019).cbr")
        assert result is not None
        assert "Infinity 8" in result
        assert "v03" in result
        assert "2019" in result

    def test_title_year_only(self):
        from cbz_ops.rename import get_renamed_filename
        result = get_renamed_filename("Hulk vs. The Marvel Universe 2008 Digital4K.cbz")
        assert result is not None
        assert "2008" in result
        # Should not include "Digital4K"
        assert "Digital" not in result


# ===== try_rule_engine =====

class TestTryRuleEngine:

    def test_returns_none_when_no_config(self):
        from cbz_ops.rename import try_rule_engine
        with patch("os.path.exists", return_value=False):
            assert try_rule_engine("test.cbz", "/nonexistent/rules.ini") is None

    def test_returns_none_when_no_rename_section(self, tmp_path):
        from cbz_ops.rename import try_rule_engine
        cfg = tmp_path / "rules.ini"
        cfg.write_text("[OTHER]\nkey=val\n")
        assert try_rule_engine("test.cbz", str(cfg)) is None

    def test_matches_custom_rule(self, tmp_path):
        from cbz_ops.rename import try_rule_engine
        cfg = tmp_path / "rules.ini"
        cfg.write_text(
            "[RENAME]\n"
            "myrule.pattern = ^(?P<series>.+?)\\s+(?P<issue>\\d+)\\.cbz$\n"
            "myrule.output = {series|title} {issue|pad3}.cbz\n"
            "myrule.priority = 100\n"
        )
        result = try_rule_engine("batman 5.cbz", str(cfg))
        assert result == "Batman 005.cbz"


# ===== parentheses_replacer =====

class TestParenthesesReplacer:

    def test_keeps_year(self):
        import re
        from cbz_ops.rename import parentheses_replacer
        m = re.search(r'\([^)]*\)', "(2018)")
        assert parentheses_replacer(m) == "(2018)"

    def test_removes_non_year(self):
        import re
        from cbz_ops.rename import parentheses_replacer
        m = re.search(r'\([^)]*\)', "(digital)")
        assert parentheses_replacer(m) == ""


# ===== clean_directory_name =====

class TestCleanDirectoryName:

    def test_delegates_to_clean_filename_pre(self):
        from cbz_ops.rename import clean_directory_name
        result = clean_directory_name("Title [Tag] (2020) (scan)")
        assert result == "Title (2020)"


# ===== rename_comic_from_metadata =====

class TestRenameComicFromMetadata:

    @patch('cbz_ops.rename.load_custom_rename_config', return_value=(False, ''))
    def test_disabled_returns_original(self, mock_config, tmp_path):
        f = tmp_path / "old.cbz"
        f.write_bytes(b"fake")
        from cbz_ops.rename import rename_comic_from_metadata
        result_path, was_renamed = rename_comic_from_metadata(str(f), {'Series': 'Batman', 'Number': '1', 'Year': 2020})
        assert result_path == str(f)
        assert was_renamed is False

    @patch('cbz_ops.rename.load_custom_rename_config', return_value=(True, '{series_name} {issue_number} ({year})'))
    def test_renames_with_pattern(self, mock_config, tmp_path):
        f = tmp_path / "old.cbz"
        f.write_bytes(b"fake")
        from cbz_ops.rename import rename_comic_from_metadata
        result_path, was_renamed = rename_comic_from_metadata(str(f), {'Series': 'Batman', 'Number': '1', 'Year': 2020})
        assert was_renamed is True
        assert os.path.basename(result_path) == "Batman 001 (2020).cbz"
        assert os.path.exists(result_path)
        assert not os.path.exists(str(f))

    @patch('cbz_ops.rename.load_custom_rename_config', return_value=(True, '{series_name} {issue_number}'))
    def test_sanitizes_series_colons(self, mock_config, tmp_path):
        f = tmp_path / "old.cbz"
        f.write_bytes(b"fake")
        from cbz_ops.rename import rename_comic_from_metadata
        result_path, was_renamed = rename_comic_from_metadata(str(f), {'Series': 'Batman: The Dark Knight', 'Number': '5', 'Year': 2020})
        assert was_renamed is True
        assert ':' not in os.path.basename(result_path)
        assert 'Batman - the Dark Knight 005' in os.path.basename(result_path)

    @patch('cbz_ops.rename.load_custom_rename_config', return_value=(True, '{series_name} {issue_number}'))
    def test_skips_when_target_exists(self, mock_config, tmp_path):
        f = tmp_path / "old.cbz"
        f.write_bytes(b"fake")
        target = tmp_path / "Batman 001.cbz"
        target.write_bytes(b"existing")
        from cbz_ops.rename import rename_comic_from_metadata
        result_path, was_renamed = rename_comic_from_metadata(str(f), {'Series': 'Batman', 'Number': '1', 'Year': 2020})
        assert was_renamed is False
        assert result_path == str(f)
        assert os.path.exists(str(f))

    @patch('cbz_ops.rename.load_custom_rename_config', return_value=(True, '{series_name} {issue_number}'))
    def test_no_rename_when_name_unchanged(self, mock_config, tmp_path):
        f = tmp_path / "Batman 001.cbz"
        f.write_bytes(b"fake")
        from cbz_ops.rename import rename_comic_from_metadata
        result_path, was_renamed = rename_comic_from_metadata(str(f), {'Series': 'Batman', 'Number': '1', 'Year': 2020})
        assert was_renamed is False
        assert result_path == str(f)
