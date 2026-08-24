"""Tests for core/notifications.py (Apprise dispatch)."""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def fake_apprise(monkeypatch):
    """Install a stand-in ``apprise`` module and hand back its Apprise mock.

    Mirrors how tests/conftest.py fakes apscheduler/cloudscraper/mokkari: the
    real package must not be required for the suite to run.
    """
    client = MagicMock()
    client.add.return_value = True
    client.notify.return_value = True
    client.__len__.return_value = 1

    module = types.ModuleType("apprise")
    module.Apprise = MagicMock(return_value=client)
    monkeypatch.setitem(sys.modules, "apprise", module)
    return client


def _settings(enabled=True, urls=None, events=None):
    from core.notifications import EVENT_DEFS

    return {
        "enabled": enabled,
        "urls": urls if urls is not None else ["ntfy://ntfy.sh/test"],
        "events": events if events is not None else {k: True for k in EVENT_DEFS},
    }


class TestParseUrls:
    def test_splits_lines_and_strips_whitespace(self):
        from core.notifications import parse_urls

        assert parse_urls("  a://one  \n b://two ") == ["a://one", "b://two"]

    def test_drops_blank_lines_and_comments(self):
        from core.notifications import parse_urls

        raw = "a://one\n\n# disabled for now\nb://two\n   \n"
        assert parse_urls(raw) == ["a://one", "b://two"]

    def test_accepts_a_list(self):
        from core.notifications import parse_urls

        assert parse_urls(["a://one", "", "b://two"]) == ["a://one", "b://two"]

    def test_tolerates_none_and_junk(self):
        from core.notifications import parse_urls

        assert parse_urls(None) == []
        assert parse_urls(12345) == []
        assert parse_urls([None, 7, "a://one"]) == ["a://one"]


class TestSanitizeEvents:
    def test_drops_unknown_ids(self):
        from core.notifications import EVENT_DEFS, sanitize_events

        result = sanitize_events({"bogus_event": True})
        assert "bogus_event" not in result
        assert set(result) == set(EVENT_DEFS)

    def test_missing_ids_fall_back_to_default(self):
        from core.notifications import EVENT_DEFS, sanitize_events

        result = sanitize_events({})
        for event_id, defn in EVENT_DEFS.items():
            assert result[event_id] is defn["default"]

    def test_explicit_false_is_kept(self):
        from core.notifications import EVENT_DOWNLOAD_FAILED, sanitize_events

        result = sanitize_events({EVENT_DOWNLOAD_FAILED: False})
        assert result[EVENT_DOWNLOAD_FAILED] is False

    def test_non_dict_input_is_all_defaults(self):
        from core.notifications import sanitize_events

        assert sanitize_events("nonsense") == sanitize_events({})


class TestIsEventEnabled:
    def test_requires_master_switch(self):
        from core.notifications import EVENT_DOWNLOAD_COMPLETE, is_event_enabled

        assert not is_event_enabled(
            EVENT_DOWNLOAD_COMPLETE, _settings(enabled=False)
        )

    def test_requires_at_least_one_url(self):
        from core.notifications import EVENT_DOWNLOAD_COMPLETE, is_event_enabled

        assert not is_event_enabled(EVENT_DOWNLOAD_COMPLETE, _settings(urls=[]))

    def test_requires_the_event_to_be_ticked(self):
        from core.notifications import EVENT_DOWNLOAD_COMPLETE, is_event_enabled

        settings = _settings(events={EVENT_DOWNLOAD_COMPLETE: False})
        assert not is_event_enabled(EVENT_DOWNLOAD_COMPLETE, settings)

    def test_all_three_satisfied(self):
        from core.notifications import EVENT_DOWNLOAD_COMPLETE, is_event_enabled

        assert is_event_enabled(EVENT_DOWNLOAD_COMPLETE, _settings())


class TestNotify:
    def test_sends_when_enabled(self, fake_apprise):
        from core.notifications import EVENT_DOWNLOAD_COMPLETE, notify

        with patch("core.notifications.get_settings", return_value=_settings()):
            assert notify(EVENT_DOWNLOAD_COMPLETE, "Title", "Body") is True

        fake_apprise.add.assert_called_once_with("ntfy://ntfy.sh/test")
        fake_apprise.notify.assert_called_once_with(
            title="Title", body="Body", notify_type="success"
        )

    def test_failure_event_uses_failure_notify_type(self, fake_apprise):
        from core.notifications import EVENT_DOWNLOAD_FAILED, notify

        with patch("core.notifications.get_settings", return_value=_settings()):
            notify(EVENT_DOWNLOAD_FAILED, "Title", "Body")

        assert fake_apprise.notify.call_args.kwargs["notify_type"] == "failure"

    def test_disabled_does_not_send(self, fake_apprise):
        from core.notifications import EVENT_DOWNLOAD_COMPLETE, notify

        with patch(
            "core.notifications.get_settings", return_value=_settings(enabled=False)
        ):
            assert notify(EVENT_DOWNLOAD_COMPLETE, "Title", "Body") is False

        fake_apprise.notify.assert_not_called()

    def test_no_urls_does_not_send(self, fake_apprise):
        from core.notifications import EVENT_DOWNLOAD_COMPLETE, notify

        with patch(
            "core.notifications.get_settings", return_value=_settings(urls=[])
        ):
            assert notify(EVENT_DOWNLOAD_COMPLETE, "Title", "Body") is False

        fake_apprise.notify.assert_not_called()

    def test_unticked_event_does_not_send(self, fake_apprise):
        from core.notifications import (
            EVENT_DOWNLOAD_COMPLETE, EVENT_DOWNLOAD_FAILED, notify,
        )

        settings = _settings(
            events={EVENT_DOWNLOAD_COMPLETE: False, EVENT_DOWNLOAD_FAILED: True}
        )
        with patch("core.notifications.get_settings", return_value=settings):
            assert notify(EVENT_DOWNLOAD_COMPLETE, "Title", "Body") is False

        fake_apprise.notify.assert_not_called()

    def test_unknown_event_is_rejected(self, fake_apprise):
        from core.notifications import notify

        assert notify("not_a_real_event", "Title", "Body") is False
        fake_apprise.notify.assert_not_called()

    def test_apprise_exception_is_swallowed(self, fake_apprise):
        from core.notifications import EVENT_DOWNLOAD_COMPLETE, notify

        fake_apprise.notify.side_effect = RuntimeError("boom")
        with patch("core.notifications.get_settings", return_value=_settings()):
            assert notify(EVENT_DOWNLOAD_COMPLETE, "Title", "Body") is False

    def test_delivery_refusal_returns_false(self, fake_apprise):
        from core.notifications import EVENT_DOWNLOAD_COMPLETE, notify

        fake_apprise.notify.return_value = False
        with patch("core.notifications.get_settings", return_value=_settings()):
            assert notify(EVENT_DOWNLOAD_COMPLETE, "Title", "Body") is False

    def test_missing_apprise_package_is_swallowed(self, monkeypatch):
        from core.notifications import EVENT_DOWNLOAD_COMPLETE, notify

        # Simulate the package not being installed at all.
        monkeypatch.setitem(sys.modules, "apprise", None)
        with patch("core.notifications.get_settings", return_value=_settings()):
            assert notify(EVENT_DOWNLOAD_COMPLETE, "Title", "Body") is False

    def test_rejected_url_still_delivers_to_the_rest(self, fake_apprise):
        from core.notifications import EVENT_DOWNLOAD_COMPLETE, notify

        fake_apprise.add.side_effect = [False, True]
        settings = _settings(urls=["bogus://x", "ntfy://ntfy.sh/test"])
        with patch("core.notifications.get_settings", return_value=settings):
            assert notify(EVENT_DOWNLOAD_COMPLETE, "Title", "Body") is True

    def test_all_urls_rejected_reports_failure(self, fake_apprise):
        from core.notifications import EVENT_DOWNLOAD_COMPLETE, notify

        fake_apprise.add.return_value = False
        fake_apprise.__len__.return_value = 0
        with patch("core.notifications.get_settings", return_value=_settings()):
            assert notify(EVENT_DOWNLOAD_COMPLETE, "Title", "Body") is False
        fake_apprise.notify.assert_not_called()


class TestNotifyAsync:
    def test_skips_thread_when_disabled(self):
        from core.notifications import EVENT_DOWNLOAD_COMPLETE, notify_async

        with patch(
            "core.notifications.get_settings", return_value=_settings(enabled=False)
        ), patch("core.notifications.threading.Thread") as thread:
            notify_async(EVENT_DOWNLOAD_COMPLETE, "Title", "Body")

        thread.assert_not_called()

    def test_spawns_daemon_thread_when_enabled(self):
        from core.notifications import EVENT_DOWNLOAD_COMPLETE, notify, notify_async

        with patch("core.notifications.get_settings", return_value=_settings()), \
                patch("core.notifications.threading.Thread") as thread:
            notify_async(EVENT_DOWNLOAD_COMPLETE, "Title", "Body")

        thread.assert_called_once()
        assert thread.call_args.kwargs["daemon"] is True
        assert thread.call_args.kwargs["target"] is notify
        assert thread.call_args.kwargs["args"] == (
            EVENT_DOWNLOAD_COMPLETE, "Title", "Body",
        )
        thread.return_value.start.assert_called_once()

    def test_never_raises(self):
        from core.notifications import EVENT_DOWNLOAD_COMPLETE, notify_async

        with patch(
            "core.notifications.get_settings", side_effect=RuntimeError("boom")
        ):
            notify_async(EVENT_DOWNLOAD_COMPLETE, "Title", "Body")  # must not raise


class TestSendTest:
    def test_bypasses_the_enable_and_event_gates(self, fake_apprise):
        from core.notifications import send_test

        with patch(
            "core.notifications.get_settings", return_value=_settings(enabled=False)
        ):
            ok, error = send_test("ntfy://ntfy.sh/test")

        assert ok is True
        assert error is None
        fake_apprise.notify.assert_called_once()

    def test_requires_at_least_one_url(self, fake_apprise):
        from core.notifications import send_test

        ok, error = send_test("   \n# nothing here\n")
        assert ok is False
        assert "No notification URLs" in error
        fake_apprise.notify.assert_not_called()

    def test_reports_the_error_string(self, fake_apprise):
        from core.notifications import send_test

        fake_apprise.notify.side_effect = RuntimeError("smtp refused")
        ok, error = send_test("mailto://user:pass@example.com")
        assert ok is False
        assert "smtp refused" in error


class TestGetSettings:
    def test_database_failure_yields_a_disabled_config(self):
        from core.notifications import get_settings

        with patch(
            "core.database.get_user_preference", side_effect=RuntimeError("no db")
        ):
            settings = get_settings()

        assert settings["enabled"] is False
        assert settings["urls"] == []

    def test_reads_all_three_preferences(self):
        from core.notifications import (
            EVENT_DOWNLOAD_COMPLETE, PREF_ENABLED, PREF_EVENTS, PREF_URLS,
            get_settings,
        )

        stored = {
            PREF_ENABLED: True,
            PREF_URLS: ["ntfy://ntfy.sh/a", "ntfy://ntfy.sh/b"],
            PREF_EVENTS: {EVENT_DOWNLOAD_COMPLETE: False},
        }
        with patch(
            "core.database.get_user_preference",
            side_effect=lambda key, default=None: stored.get(key, default),
        ):
            settings = get_settings()

        assert settings["enabled"] is True
        assert settings["urls"] == ["ntfy://ntfy.sh/a", "ntfy://ntfy.sh/b"]
        assert settings["events"][EVENT_DOWNLOAD_COMPLETE] is False


class TestNotifyDownloadTerminal:
    """The helper both client-backed pollers share."""

    def test_complete_uses_the_complete_event(self):
        from core.notifications import (
            EVENT_DOWNLOAD_COMPLETE, notify_download_terminal,
        )

        with patch("core.notifications.notify_async") as notify:
            notify_download_terminal("complete", "a.cbz", source="Usenet")

        assert notify.call_args.args[0] == EVENT_DOWNLOAD_COMPLETE
        assert "Source: Usenet" in notify.call_args.args[2]

    def test_failed_uses_the_failed_event_and_carries_the_error(self):
        from core.notifications import EVENT_DOWNLOAD_FAILED, notify_download_terminal

        with patch("core.notifications.notify_async") as notify:
            notify_download_terminal("failed", "a.cbz", error="429", source="DC++")

        assert notify.call_args.args[0] == EVENT_DOWNLOAD_FAILED
        assert "Error: 429" in notify.call_args.args[2]

    def test_complete_no_move_flags_the_manual_step(self):
        from core.notifications import notify_download_terminal

        with patch("core.notifications.notify_async") as notify:
            notify_download_terminal("complete_no_move", "a.cbz", source="DC++")

        assert "not been imported" in notify.call_args.args[2]

    def test_a_non_terminal_status_sends_nothing(self):
        from core.notifications import notify_download_terminal

        with patch("core.notifications.notify_async") as notify:
            notify_download_terminal("downloading", "a.cbz", source="Usenet")

        notify.assert_not_called()

    def test_never_raises_into_the_poller(self):
        from core.notifications import notify_download_terminal

        with patch(
            "core.notifications.notify_async", side_effect=RuntimeError("boom")
        ):
            notify_download_terminal("complete", "a.cbz", source="Usenet")


class TestFormatDigest:
    def test_lists_every_issue_when_short(self):
        from core.notifications import format_digest

        body = format_digest("Added:", ["Batman #1", "Batman #2"])
        assert "Batman #1" in body
        assert "Batman #2" in body
        assert "more" not in body

    def test_truncates_a_long_list(self):
        from core.notifications import MAX_DIGEST_LINES, format_digest

        items = [f"Batman #{n}" for n in range(MAX_DIGEST_LINES + 5)]
        body = format_digest("Added:", items)

        assert f"Batman #{MAX_DIGEST_LINES - 1}" in body
        assert f"Batman #{MAX_DIGEST_LINES}" not in body
        assert "...and 5 more" in body

    def test_empty_list_is_just_the_header(self):
        from core.notifications import format_digest

        assert format_digest("Added:", []) == "Added:"


class TestRedact:
    def test_strips_credentials(self):
        from core.notifications import _redact

        assert "hunter2" not in _redact("mailto://bob:hunter2@smtp.example.com")

    def test_keeps_the_scheme_for_diagnosis(self):
        from core.notifications import _redact

        assert _redact("discord://id/token").startswith("discord://")

    def test_tolerates_garbage(self):
        from core.notifications import _redact

        assert _redact("not a url") == "not a url"
