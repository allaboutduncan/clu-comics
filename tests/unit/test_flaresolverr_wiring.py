"""Where the bundled FlareSolverr is wired in.

api.py and app.py cannot be imported in tests (worker threads, cloudscraper,
a second scheduler), so their hook sites are asserted structurally, the same
way as tests/unit/test_download_name_claim.py.
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"{name} not found")


def _calls(node, attr):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == attr]


@pytest.fixture(scope="module")
def api_src():
    return _read("api.py")


@pytest.fixture(scope="module")
def api_tree(api_src):
    return ast.parse(api_src)


class TestDownloadGetcomics:
    def test_clears_the_challenge_before_recording_manual_url(self, api_tree):
        fn = _function(api_tree, "download_getcomics")
        src = ast.unparse(fn)
        assert _calls(fn, "clear_challenge"), "download_getcomics no longer asks FlareSolverr"
        assert src.index("clear_challenge") < src.index("'manual_url'")

    def test_solves_the_mirror_on_the_downloading_session(self, api_tree):
        fn = _function(api_tree, "download_getcomics")
        call, = _calls(fn, "clear_challenge")
        assert [ast.unparse(a) for a in call.args] == ["url", "session"]
        assert {k.arg: ast.unparse(k.value) for k in call.keywords} == {"context": "'download'"}

    def test_reports_a_retry_that_was_still_challenged(self, api_tree):
        # Without this the debug log shows that a solve ran, never whether it helped.
        src = ast.unparse(_function(api_tree, "download_getcomics"))
        assert "flaresolverr_cleared = True" in src
        assert "flaresolverr.note_still_challenged(url, 'download')" in src
        assert src.index("note_still_challenged") < src.index("'manual_url'")

    def test_one_flaresolverr_pass_per_download(self, api_tree):
        # A clearance that is still challenged must end in the manual notice,
        # not loop through every attempt re-solving.
        src = ast.unparse(_function(api_tree, "download_getcomics"))
        assert "flaresolverr_tried = False" in src
        assert "flaresolverr_tried = True" in src
        assert "not flaresolverr_tried" in src

    def test_never_spends_the_last_attempt_on_a_solve(self, api_tree):
        # On the last attempt a `continue` would leave the loop with no
        # manual_url set -- and auto-retry keys on manual_url to stand down.
        src = ast.unparse(_function(api_tree, "download_getcomics"))
        guard = src.split("flaresolverr_tried = True")[0].rsplit("if ", 1)[-1]
        assert "not flaresolverr_tried" in guard
        assert "attempt < retries - 1" in guard


class TestResolveFinalUrl:
    def test_tries_flaresolverr_before_a_fresh_scraper(self, api_tree):
        src = ast.unparse(_function(api_tree, "resolve_final_url"))
        assert "flaresolverr.clear_challenge(current, gc_scraper, context='resolve')" in src
        assert "flaresolverr.note_still_challenged(current, 'resolve')" in src
        assert src.index("clear_challenge") < src.index("replace_session")


class TestLifecycle:
    @pytest.fixture(scope="class")
    def app_tree(self):
        return ast.parse(_read("app.py"))

    @pytest.mark.parametrize("fn_name", ["restart_app", "shutdown_server"])
    def test_solver_is_stopped(self, app_tree, fn_name):
        src = ast.unparse(_function(app_tree, fn_name))
        assert "flaresolverr_supervisor.stop()" in src

    def test_restart_stops_it_before_execv(self, app_tree):
        src = ast.unparse(_function(app_tree, "restart_app"))
        assert src.index("flaresolverr_supervisor.stop()") < src.index("os.execv")

    def test_nothing_starts_it_at_import(self):
        # Starting a browser on gunicorn's import thread would spend its
        # --timeout budget. Only a challenge (or the Test button) starts it.
        for name in ("app.py", "api.py"):
            tree = ast.parse(_read(name))
            for node in tree.body:
                for call in ast.walk(node) if not isinstance(node, (ast.FunctionDef, ast.ClassDef)) else ():
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                        assert call.func.attr != "ensure_running", f"{name} starts FlareSolverr at import"


class TestSettingsPage:
    def test_save_writes_the_switch_only_when_sent(self):
        src = _read("app.py").split('@app.route("/api/config/download-api"')[1].split("@app.route")[0]
        assert '"flaresolverrEnabled" in data' in src
        assert 'set_setting("FLARESOLVERR_ENABLED"' in src

    def test_template_has_switch_payload_and_test_button(self):
        html = _read("templates", "config.html")
        assert 'id="flaresolverrEnabled"' in html
        assert "flaresolverrEnabled: document.getElementById('flaresolverrEnabled')?.checked" in html
        assert 'url_for("downloads.test_flaresolverr")' in html
        tab = html.split('id="download-api" role="tabpanel"')[1]
        assert 'id="flaresolverrEnabled"' in tab.split('role="tabpanel"')[0]

    def test_config_page_passes_the_value(self):
        assert 'flaresolverrEnabled=st["FLARESOLVERR_ENABLED"]' in _read("app.py")


class TestDockerfile:
    @pytest.fixture(scope="class")
    def dockerfile(self):
        return _read("Dockerfile")

    def test_version_is_pinned(self, dockerfile):
        assert re.search(r"ARG FLARESOLVERR_VERSION=\d+\.\d+\.\d+", dockerfile)
        assert "latest" not in dockerfile.split("ARG FLARESOLVERR_VERSION")[1].split("\n\n")[0]

    def test_own_venv(self, dockerfile):
        assert "python -m venv /opt/flaresolverr-venv" in dockerfile
        assert "COPY --from=builder /opt/flaresolverr-venv /opt/flaresolverr-venv" in dockerfile

    def test_browser_packages(self, dockerfile):
        for pkg in ("chromium", "chromium-driver", "xvfb", "xauth"):
            assert re.search(rf"^\s+{re.escape(pkg)} \\$", dockerfile, re.M), pkg

    def test_driver_is_prepatched_at_the_hardcoded_path(self, dockerfile):
        # FlareSolverr skips its runtime driver download only for this exact
        # path, and patching it at runtime fails as PUID.
        assert "cp /usr/bin/chromedriver /app/chromedriver" in dockerfile
        assert "patch_exe()" in dockerfile and "is_binary_patched()" in dockerfile
        # After the source COPY, so nothing in the build context can shadow it.
        assert dockerfile.index("COPY . .") < dockerfile.index("cp /usr/bin/chromedriver")

    def test_solver_port_is_never_exposed(self, dockerfile):
        assert "8191" not in dockerfile

    def test_dummy_packages_installed_before_apt(self, dockerfile):
        final = dockerfile.split("Stage 2: Final")[1]
        assert final.index("dpkg -i /tmp/libgl1-mesa-dri.deb") < final.index("chromium \\")


class TestDocs:
    def test_claude_md_states_the_rule(self):
        md = _read("CLAUDE.md")
        assert "Do not reintroduce Playwright/Chromium" not in md
        assert "core.flaresolverr.clear_challenge" in md
