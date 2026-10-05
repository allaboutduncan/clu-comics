"""Local / OpenAI-compatible model support in the recommendation service."""
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import recommendations

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "..", "templates", "config.html")
APP_PY = os.path.join(os.path.dirname(__file__), "..", "..", "app.py")

HISTORY = [{"path": "/data/Image/Saga (2012)/Saga 001 (2012).cbz"}]
RECS = [{"title": "Paper Girls", "publisher": "Image", "reason": "Same writer."}]


def _response(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


@pytest.fixture
def fake_openai():
    """Stand in for the openai package so no network call is made."""
    module = MagicMock()

    class BadRequestError(Exception):
        pass

    module.BadRequestError = BadRequestError
    client = module.OpenAI.return_value
    client.chat.completions.create.return_value = _response(json.dumps(RECS))
    with patch.object(recommendations, "openai", module), \
            patch("core.database.get_to_read_items", return_value=[]):
        yield module


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)


class TestLocalProvider:
    def test_no_api_key_needed(self, fake_openai):
        result = recommendations.get_recommendations(
            "", "local", "mistral", HISTORY,
            base_url="http://host.docker.internal:11434/v1",
        )
        assert result == RECS
        kwargs = fake_openai.OpenAI.call_args.kwargs
        assert kwargs["base_url"] == "http://host.docker.internal:11434/v1"
        # The openai client refuses an empty key; local servers ignore it.
        assert kwargs["api_key"]
        create = fake_openai.OpenAI.return_value.chat.completions.create
        assert create.call_args.kwargs["model"] == "mistral"

    def test_supplied_api_key_is_passed_through(self, fake_openai):
        recommendations.get_recommendations(
            "secret", "local", "mistral", HISTORY, base_url="http://x:1234/v1"
        )
        assert fake_openai.OpenAI.call_args.kwargs["api_key"] == "secret"

    def test_timeout_stays_under_gunicorn_worker_timeout(self, fake_openai):
        recommendations.get_recommendations(
            "", "local", "mistral", HISTORY, base_url="http://x:1234/v1"
        )
        assert fake_openai.OpenAI.call_args.kwargs["timeout"] < 120

    def test_env_vars_fill_blank_settings(self, fake_openai, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "http://host.docker.internal:11434/v1")
        monkeypatch.setenv("OPENAI_MODEL", "mistral")
        recommendations.get_recommendations("", "local", "", HISTORY, base_url="")
        assert fake_openai.OpenAI.call_args.kwargs["base_url"] == \
            "http://host.docker.internal:11434/v1"
        create = fake_openai.OpenAI.return_value.chat.completions.create
        assert create.call_args.kwargs["model"] == "mistral"

    def test_saved_settings_win_over_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "http://env/v1")
        monkeypatch.setenv("OPENAI_MODEL", "env-model")
        assert recommendations.resolve_local_settings("http://saved/v1", "llama3.1") == \
            ("http://saved/v1", "llama3.1")

    def test_missing_base_url_is_an_error(self, fake_openai):
        result = recommendations.get_recommendations("", "local", "mistral", HISTORY)
        assert "Base URL" in result["error"]
        fake_openai.OpenAI.assert_not_called()

    def test_missing_model_is_an_error(self, fake_openai):
        result = recommendations.get_recommendations(
            "", "local", "", HISTORY, base_url="http://x/v1"
        )
        assert "model" in result["error"]

    def test_falls_back_when_json_mode_rejected(self, fake_openai):
        create = fake_openai.OpenAI.return_value.chat.completions.create
        create.side_effect = [
            fake_openai.BadRequestError("response_format must be json_schema"),
            _response(json.dumps(RECS)),
        ]
        result = recommendations.get_recommendations(
            "", "local", "mistral", HISTORY, base_url="http://x/v1"
        )
        assert result == RECS
        assert "response_format" in create.call_args_list[0].kwargs
        assert "response_format" not in create.call_args_list[1].kwargs

    def test_unwraps_object_wrapper(self, fake_openai):
        create = fake_openai.OpenAI.return_value.chat.completions.create
        create.return_value = _response(json.dumps({"recommendations": RECS}))
        result = recommendations.get_recommendations(
            "", "local", "mistral", HISTORY, base_url="http://x/v1"
        )
        assert result == RECS

    def test_hosted_providers_still_require_api_key(self, fake_openai):
        result = recommendations.get_recommendations("", "openai", "gpt-4.1", HISTORY)
        assert result == {"error": "API Key is required"}


class TestParseJsonResponse:
    def test_prose_around_array(self):
        content = "Sure! Here are some picks:\n" + json.dumps(RECS) + "\nEnjoy!"
        assert recommendations._parse_json_response(content) == RECS

    def test_untagged_code_fence(self):
        content = "```\n" + json.dumps(RECS) + "\n```"
        assert recommendations._parse_json_response(content) == RECS

    def test_json_code_fence(self):
        content = "```json\n" + json.dumps(RECS) + "\n```"
        assert recommendations._parse_json_response(content) == RECS

    def test_garbage_is_an_error(self):
        assert "error" in recommendations._parse_json_response("no json here")

    def test_none_is_an_error(self):
        assert "error" in recommendations._parse_json_response(None)


class TestWiring:
    def test_settings_page_offers_local_provider(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            html = f.read()
        assert '<option value="local"' in html
        assert 'id="recBaseUrl"' in html
        assert "recBaseUrl:" in html  # sent by saveRecommendationSettings

    def test_app_saves_and_uses_base_url(self):
        with open(APP_PY, encoding="utf-8") as f:
            src = f.read()
        assert '"rec_base_url"' in src
        assert 'data.get("recBaseUrl")' in src
        assert "base_url=base_url" in src
        # The URL must come from saved settings, never the request body.
        assert 'data.get("base_url")' not in src
