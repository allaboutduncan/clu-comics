"""core.provider_client.LibraryClient: method-named clients (#704 PR 4).

Callers write the library's own method names (``api.series(5)``); every one
runs under the provider's GuardedClient policy. These tests use a fake policy
and a mock library, so they pin the base class itself.
"""
from unittest.mock import MagicMock

import pytest

from core.provider_client import CallOptions, LibraryClient


class _Policy:
    """Records what LibraryClient asked the policy to do, and runs fn."""

    def __init__(self):
        self.calls = []

    def call(self, fn, context, default=None, **flags):
        self.calls.append({"context": context, "default": default, **flags})
        return fn()


class _Client(LibraryClient):
    READ_METHODS = frozenset({"series", "issues_list"})


def _client():
    library = MagicMock()
    policy = _Policy()
    return _Client(library, policy), library, policy


class TestGeneratedMethods:

    def test_each_listed_name_calls_the_library_under_the_policy(self):
        client, library, policy = _client()
        library.series.return_value = "S"

        assert client.series(5) == "S"
        library.series.assert_called_once_with(5)
        assert len(policy.calls) == 1

    def test_an_unlisted_name_is_an_attribute_error(self):
        """No __getattr__: a method the library adds is never silently
        reachable around the policy."""
        client, library, _ = _client()
        with pytest.raises(AttributeError):
            client.publisher(1)
        library.publisher.assert_not_called()

    def test_a_subclass_can_override_a_listed_name(self):
        class Custom(LibraryClient):
            READ_METHODS = frozenset({"series"})

            def series(self, sid):
                return f"custom {sid}"

        assert Custom(MagicMock(), _Policy()).series(3) == "custom 3"

    def test_a_client_is_always_truthy(self):
        """``if not api:`` keeps meaning "no client was issued"."""
        client, _, _ = _client()
        assert bool(client) is True


class TestUsing:

    def test_using_returns_a_view_and_leaves_the_original_alone(self):
        client, library, policy = _client()
        view = client.using(default=[], raise_api_errors=True, context="ctx")

        view.issues_list({"a": 1})
        client.issues_list({"a": 1})

        assert view is not client
        assert view._library is library
        assert policy.calls[0] == {
            "context": "ctx", "default": [], "raise_api_errors": True,
            "ignore_auth_lock": False, "raise_auth_errors": False,
        }
        assert policy.calls[1]["default"] is None
        assert policy.calls[1]["raise_api_errors"] is False
        assert client._options == CallOptions()

    def test_views_compose(self):
        client, _, policy = _client()
        client.using(default=[]).using(ignore_auth_lock=True).series(1)
        assert policy.calls[0]["default"] == []
        assert policy.calls[0]["ignore_auth_lock"] is True

    def test_unknown_options_are_refused(self):
        client, _, _ = _client()
        with pytest.raises(TypeError):
            client.using(retries=1)


class TestAutomaticContext:

    def test_names_the_method_and_its_arguments(self):
        client, _, policy = _client()
        client.issues_list({"series_id": 7}, page=2)
        assert policy.calls[0]["context"] == "issues_list({'series_id': 7}, page=2)"

    def test_is_capped_for_long_arguments(self):
        client, _, policy = _client()
        client.series("x" * 500)
        context = policy.calls[0]["context"]
        assert len(context) <= 60
        assert context.startswith("series(")

    def test_an_explicit_context_wins(self):
        client, _, policy = _client()
        client.using(context="syncing series 5").series(5)
        assert policy.calls[0]["context"] == "syncing series 5"
