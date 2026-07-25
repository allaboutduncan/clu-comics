"""Regression tests for the PR #248 review fixes.

Covers defects found reviewing commit e96e49b:
  1. Format-variant regex matched mid-word ("os"/"omb") → valid issues rejected.
  2. getcomics_urls had no UNIQUE(url) → INSERT OR REPLACE appended duplicates.
  4. update_scrape_index raised NameError (idx_at) on the proactive-refresh path.

Fix 3 (per-issue error isolation in app.scheduled_getcomics_download) is a
structural try/except wrap around the loop body; it can't be exercised here
because importing the real app.py pulls in optional deps not installed in the
test env (the repo's route tests use a mock app module for the same reason).
"""

from datetime import datetime


# ── Fix 1: format-variant word boundary ─────────────────────────────────────
class TestFormatVariantWordBoundary:
    def test_midword_variants_not_flagged(self):
        from models.getcomics import parse_result_title
        for title in ["Chaos War #1", "Cosmos #3", "Bomb Queen #5",
                      "Kudos #1", "Los Angeles #1"]:
            assert parse_result_title(title).format_variants == [], title

    def test_standalone_variant_still_flagged(self):
        # A genuinely standalone "OS" token must still be detected — the boundary
        # guard only blocks mid-word matches, not real trailing format tokens.
        from models.getcomics import parse_result_title
        assert "os" in [v.lower() for v in parse_result_title("Justice League OS #1").format_variants]

    def test_midword_variant_series_now_accepts(self):
        # "Chaos War" scored -5 (REJECT) before the fix because "os" matched
        # mid-word and applied the format-mismatch penalty. It must now ACCEPT.
        from models.getcomics import score_getcomics_result
        score, _is_range, match = score_getcomics_result("Chaos War #1", "Chaos War", "1", None)
        assert match is True
        assert score >= 40, f"expected ACCEPT, got {score}"


# ── Fix 2: UNIQUE(url) so INSERT OR REPLACE dedups ──────────────────────────
class TestGetcomicsUrlUnique:
    def test_insert_or_replace_replaces_not_appends(self, db_connection):
        from models.getcomics import _ensure_urls_table
        from core.database import get_db_connection
        _ensure_urls_table()
        conn = get_db_connection()
        for title in ("First", "Second"):
            conn.execute(
                "INSERT OR REPLACE INTO getcomics_urls (url, full_url, series_norm, title) "
                "VALUES (?, ?, ?, ?)",
                ("https://getcomics.org/x", "https://getcomics.org/x", "X", title),
            )
        conn.commit()
        rows = conn.execute(
            "SELECT title FROM getcomics_urls WHERE url = ?", ("https://getcomics.org/x",)
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "Second"

    def test_dedup_migration_collapses_existing_duplicates(self, db_connection):
        from models.getcomics import _ensure_urls_table
        from core.database import get_db_connection
        _ensure_urls_table()
        # Simulate a legacy DB: drop the unique index and insert duplicate rows.
        conn = get_db_connection()
        conn.execute("DROP INDEX IF EXISTS idx_urls_url")
        for t in ("old", "new"):
            conn.execute(
                "INSERT INTO getcomics_urls (url, full_url, series_norm, title) VALUES (?,?,?,?)",
                ("https://getcomics.org/dup", "https://getcomics.org/dup", "Dup", t),
            )
        conn.commit()
        conn.close()

        # Re-running the ensure/migration de-dupes (keeps newest) + re-adds index.
        _ensure_urls_table()
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT title FROM getcomics_urls WHERE url = ?", ("https://getcomics.org/dup",)
        ).fetchall()
        idx = {r[1] for r in conn.execute("PRAGMA index_list(getcomics_urls)").fetchall()}
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "new"
        assert "idx_urls_url" in idx


# ── Fix 9: alias lookups use the indexed, em-dash-aware normalized column ────
class TestAliasLookupNormalizedColumn:
    def test_em_dash_series_alias_is_found(self, db_connection):
        from models.getcomics import (
            get_series_aliases, _normalize_alias, _ensure_urls_table,
        )
        from core.database import get_db_connection, set_user_preference

        set_user_preference("getcomics_scrape_aliases_cleared_v1", "1", category="getcomics")
        _ensure_urls_table()
        name = "Nightwing—Rebirth"  # em-dash — the old 2-dash WHERE missed this
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO getcomics_urls "
            "(url, full_url, series_norm, series_norm_norm, search_aliases, title) "
            "VALUES (?,?,?,?,?,?)",
            ("http://x/nw", "http://x/nw", name, _normalize_alias(name), "Nightwing", "t"),
        )
        conn.commit()
        conn.close()

        assert get_series_aliases(name) == "Nightwing"


# ── Fix 4: proactive-refresh branch no longer NameErrors ────────────────────
class TestUpdateScrapeIndexProactiveRefresh:
    def test_proactive_refresh_reaches_branch_without_nameerror(self, db_connection):
        from models.getcomics import _ensure_urls_table, update_scrape_index
        from core.database import get_db_connection

        _ensure_urls_table()
        url = "https://getcomics.org/download/test-series-50-2024/"
        conn = get_db_connection()
        conn.execute(
            "INSERT OR REPLACE INTO getcomics_urls "
            "(url, full_url, series_norm, series_norm_norm, title, scrape_status, lastmod, indexed_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (url, url, "Test Series", "test series", "Test Series #50",
             "success", "", datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

        # Reaches the proactive-refresh branch (issue 50 already indexed & fresh).
        # Before the fix this raised NameError: idx_at. Now it returns 0 with no
        # network work because nothing is stale.
        assert update_scrape_index("Test Series", refresh_for_issue="50") == 0
