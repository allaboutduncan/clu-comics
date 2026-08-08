"""Per-user settings: the user_settings override layer over user_preferences.

Resolution order under test:

    user_settings[user_id][key]  ->  user_preferences[key]  ->  default
         personal override           owner-set site default
"""
import json

import pytest

from core.database import (
    delete_user_setting,
    get_user_preference,
    get_user_setting,
    get_user_setting_override,
    set_user_preference,
    set_user_setting,
)


class TestFallbackChain:
    """get_user_setting resolves override -> global -> default."""

    def test_neither_set_returns_default(self, db_connection):
        assert get_user_setting("bootstrap_theme", default="default", user_id=1) == "default"

    def test_global_only_returns_global(self, db_connection):
        set_user_preference("bootstrap_theme", "flatly", category="personalization")
        assert get_user_setting("bootstrap_theme", default="default", user_id=1) == "flatly"

    def test_override_wins_over_global(self, db_connection):
        set_user_preference("bootstrap_theme", "flatly", category="personalization")
        set_user_setting("bootstrap_theme", "darkly", user_id=1)
        assert get_user_setting("bootstrap_theme", default="default", user_id=1) == "darkly"

    def test_corrupt_override_falls_through_to_global(self, db_connection):
        """A broken personal row must not mask the owner's site default.

        This is deliberately unlike get_user_preference, which returns its
        ``default`` on any error.
        """
        set_user_preference("bootstrap_theme", "flatly", category="personalization")
        db_connection.execute(
            "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
            (1, "bootstrap_theme", "{not valid json"),
        )
        db_connection.commit()

        assert get_user_setting("bootstrap_theme", default="default", user_id=1) == "flatly"

    def test_falsy_override_is_respected(self, db_connection):
        """An override of False/0/[] is a real choice, not "unset"."""
        set_user_preference("dashboard_hidden", ["library"], category="dashboard")
        set_user_setting("dashboard_hidden", [], user_id=1)
        assert get_user_setting("dashboard_hidden", default=None, user_id=1) == []


class TestOverrideAccessor:
    """get_user_setting_override never falls back."""

    def test_returns_none_when_only_global_exists(self, db_connection):
        set_user_preference("bootstrap_theme", "flatly", category="personalization")
        assert get_user_setting_override("bootstrap_theme", user_id=1) is None

    def test_returns_the_override(self, db_connection):
        set_user_setting("bootstrap_theme", "darkly", user_id=1)
        assert get_user_setting_override("bootstrap_theme", user_id=1) == "darkly"


class TestIsolation:
    """Overrides are per user."""

    def test_users_do_not_see_each_other(self, db_connection):
        set_user_preference("bootstrap_theme", "flatly", category="personalization")
        set_user_setting("bootstrap_theme", "darkly", user_id=1)
        set_user_setting("bootstrap_theme", "cyborg", user_id=2)

        assert get_user_setting("bootstrap_theme", user_id=1) == "darkly"
        assert get_user_setting("bootstrap_theme", user_id=2) == "cyborg"
        # A third user with no override still follows the site default.
        assert get_user_setting("bootstrap_theme", user_id=3) == "flatly"

    def test_delete_only_affects_one_user(self, db_connection):
        set_user_setting("bootstrap_theme", "darkly", user_id=1)
        set_user_setting("bootstrap_theme", "cyborg", user_id=2)

        delete_user_setting("bootstrap_theme", user_id=1)

        assert get_user_setting_override("bootstrap_theme", user_id=1) is None
        assert get_user_setting_override("bootstrap_theme", user_id=2) == "cyborg"


class TestDelete:

    def test_delete_reverts_to_global(self, db_connection):
        set_user_preference("bootstrap_theme", "flatly", category="personalization")
        set_user_setting("bootstrap_theme", "darkly", user_id=1)
        assert get_user_setting("bootstrap_theme", user_id=1) == "darkly"

        delete_user_setting("bootstrap_theme", user_id=1)
        assert get_user_setting("bootstrap_theme", user_id=1) == "flatly"

    def test_delete_missing_row_is_a_noop(self, db_connection):
        assert delete_user_setting("never_set", user_id=1) is True


class TestRoundTrip:

    @pytest.mark.parametrize("value", [
        "darkly",
        ["favorites", "library"],
        {"a": 1, "b": [2, 3]},
        True,
        0,
        None,
    ])
    def test_json_round_trip(self, db_connection, value):
        set_user_setting("some_key", value, user_id=7)
        assert get_user_setting_override("some_key", user_id=7) == value

    def test_encoding_matches_user_preferences(self, db_connection):
        """Both tables must JSON-encode identically — the same caller reads either."""
        set_user_preference("shared_key", {"x": [1, 2]}, category="general")
        set_user_setting("shared_key", {"x": [1, 2]}, user_id=1)

        pref = db_connection.execute(
            "SELECT value FROM user_preferences WHERE key = 'shared_key'"
        ).fetchone()["value"]
        setting = db_connection.execute(
            "SELECT value FROM user_settings WHERE user_id = 1 AND key = 'shared_key'"
        ).fetchone()["value"]

        assert pref == setting
        assert json.loads(setting) == {"x": [1, 2]}

    def test_set_overwrites(self, db_connection):
        set_user_setting("bootstrap_theme", "darkly", user_id=1)
        set_user_setting("bootstrap_theme", "lux", user_id=1)
        assert get_user_setting_override("bootstrap_theme", user_id=1) == "lux"
        rows = db_connection.execute(
            "SELECT COUNT(*) c FROM user_settings WHERE user_id = 1 AND key = 'bootstrap_theme'"
        ).fetchone()["c"]
        assert rows == 1


class TestDashboardResolution:
    """routes.collection resolves dashboard layout through the same chain."""

    def test_order_falls_back_and_backfills(self, db_connection):
        from routes.collection import DEFAULT_DASHBOARD_ORDER, get_dashboard_order

        # A stale saved order missing a newer section still yields every section.
        set_user_setting("dashboard_order", ["library", "favorites"], user_id=1)
        order = get_dashboard_order(user_id=1)

        assert order[:2] == ["library", "favorites"]
        assert set(order) == set(DEFAULT_DASHBOARD_ORDER)

    def test_default_order_is_not_mutated_by_backfill(self, db_connection):
        """The fallback must hand back a copy, not the module-level list."""
        from routes.collection import DEFAULT_DASHBOARD_ORDER, get_dashboard_order

        before = list(DEFAULT_DASHBOARD_ORDER)
        get_dashboard_order(user_id=1)
        get_dashboard_order(user_id=2)
        assert DEFAULT_DASHBOARD_ORDER == before

    def test_site_default_ignores_personal_override(self, db_connection):
        from routes.collection import site_default_dashboard_order

        set_user_preference("dashboard_order", ["library", "favorites"], category="dashboard")
        set_user_setting("dashboard_order", ["discover", "library"], user_id=1)

        assert site_default_dashboard_order()[:2] == ["library", "favorites"]

    def test_sections_respect_per_user_hidden(self, db_connection):
        from routes.collection import get_dashboard_sections

        set_user_setting("dashboard_hidden", ["library"], user_id=1)
        set_user_setting("dashboard_hidden", [], user_id=2)

        ids_1 = [s["id"] for s in get_dashboard_sections(user_id=1)]
        ids_2 = [s["id"] for s in get_dashboard_sections(user_id=2)]

        assert "library" not in ids_1
        assert "library" in ids_2


class TestSanitizeDashboardPayload:

    def test_drops_unknown_and_backfills_deterministically(self, db_connection):
        from routes.collection import DEFAULT_DASHBOARD_ORDER, sanitize_dashboard_payload

        order, hidden = sanitize_dashboard_payload(["library", "bogus"], ["nope", "discover"])

        assert order[0] == "library"
        assert "bogus" not in order
        assert set(order) == set(DEFAULT_DASHBOARD_ORDER)
        # Backfill order follows DEFAULT_DASHBOARD_ORDER, not set iteration.
        assert order[1:] == [s for s in DEFAULT_DASHBOARD_ORDER if s != "library"]
        assert hidden == ["discover"]

    def test_accepts_comma_separated_strings(self, db_connection):
        from routes.collection import sanitize_dashboard_payload

        order, hidden = sanitize_dashboard_payload("library, favorites", "discover")
        assert order[:2] == ["library", "favorites"]
        assert hidden == ["discover"]

    def test_empty_payload_yields_full_default_order(self, db_connection):
        from routes.collection import DEFAULT_DASHBOARD_ORDER, sanitize_dashboard_payload

        order, hidden = sanitize_dashboard_payload([], [])
        assert order == list(DEFAULT_DASHBOARD_ORDER)
        assert hidden == []
