"""The settings-page half of the local-ComicVine-DB auto-update.

The template is not importable, so these are string assertions over
``templates/config.html``. Each one stands for a specific way the block breaks
silently.
"""

import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_HTML = os.path.join(PROJECT_ROOT, "templates", "config.html")


@pytest.fixture(scope="module")
def config_html():
    with open(CONFIG_HTML, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def cv_block(config_html):
    """The comicvine_sqlite card's extra markup."""
    return config_html.split("const cvSqliteExtra")[1].split("` : '';")[0]


class TestTheSourceIsNeverNamed:
    def test_template_does_not_carry_the_url(self, config_html):
        """The page is allowed to know the cadence, not the host."""
        assert "nerdfire" not in config_html.lower()
        assert "localcv" not in config_html.lower()

    def test_the_cadence_is_stated(self, cv_block):
        assert "2 weeks" in cv_block

    def test_the_size_is_stated(self, cv_block):
        """A 540 MB download that starts with no warning is a support ticket."""
        assert "540" in cv_block


class TestMarkup:
    def test_the_switch_exists(self, cv_block):
        assert 'id="cvSqliteAutoUpdate"' in cv_block
        assert "saveCvSqliteAutoUpdate()" in cv_block

    def test_the_download_button_exists(self, cv_block):
        assert 'id="cvSqliteUpdateBtn"' in cv_block
        assert "updateComicVineDb()" in cv_block

    def test_there_is_somewhere_to_report_status(self, cv_block):
        assert 'id="cvSqliteUpdateStatus"' in cv_block

    def test_the_status_loader_runs_with_the_other_provider_loaders(self, config_html):
        """Without this the switch renders unchecked whatever the user saved."""
        block = config_html.split("initPasswordToggles(container);")[1].split("}")[0]
        assert "loadCvSqliteUpdateStatus();" in block


class TestJavaScript:
    def test_the_switch_is_strict_about_true(self, config_html):
        """This preference defaults OFF.

        The "!== false" idiom used for default-on settings would turn automatic
        540 MB downloads on for everyone who has never touched the switch.
        """
        body = config_html.split("async function loadCvSqliteUpdateStatus()")[1]
        body = body.split("async function saveCvSqliteAutoUpdate()")[0]
        assignment = [
            line for line in body.splitlines()
            if "toggle.checked" in line and "//" not in line
        ]
        assert assignment, "loadCvSqliteUpdateStatus never sets the switch"
        assert "=== true" in assignment[0]
        assert "!== false" not in assignment[0]

    def test_progress_polls_the_single_operation_endpoint(self, config_html):
        """/api/operations clears the pending notification queue as a side
        effect, so only the one poller in base.html may use it."""
        body = config_html.split("function pollCvSqliteUpdate(")[1]
        body = body.split("function initPasswordToggles")[0]
        assert "/api/operation/" in body
        assert "'/api/operations'" not in body

    def test_an_absent_operation_counts_as_finished(self, config_html):
        """Completed ops are pruned on a short TTL."""
        body = config_html.split("function pollCvSqliteUpdate(")[1]
        body = body.split("function initPasswordToggles")[0]
        assert "finish()" in body

    def test_confirmation_uses_a_modal_not_a_native_dialog(self, config_html):
        body = config_html.split("function updateComicVineDb()")[1]
        body = body.split("async function startCvSqliteUpdate()")[0]
        assert "bootstrap.Modal" in body
        assert "confirm(" not in body
        assert "alert(" not in body

    def test_the_confirmation_modal_exists(self, config_html):
        assert 'id="cvSqliteUpdateModal"' in config_html
        assert 'id="confirmCvSqliteUpdateBtn"' in config_html
