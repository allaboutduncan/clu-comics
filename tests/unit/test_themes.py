"""Theme catalog + the shared personalization partials.

The partials are rendered by both /config (site defaults) and /account (per-user
overrides). /config lives in app.py and isn't reachable from the route tests, so
these cover the shared markup directly.
"""
import os

import pytest
from jinja2 import Environment, FileSystemLoader

from core.themes import (
    BOOTSWATCH_THEMES,
    DARK_BOOTSWATCH_THEMES,
    THEME_IDS,
    THEME_ORDER,
    is_dark_theme,
    is_valid_theme,
)

TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "templates",
)


@pytest.fixture
def env():
    return Environment(loader=FileSystemLoader(TEMPLATE_DIR))


class TestThemeCatalog:

    def test_ids_are_unique(self):
        assert len(THEME_ORDER) == len(set(THEME_ORDER))

    def test_default_is_first(self):
        assert THEME_ORDER[0] == "default"
        assert BOOTSWATCH_THEMES[0] == ("default", "Default (Bootstrap)")

    def test_every_dark_theme_is_offered(self):
        """A dark theme not in the picker could never be selected."""
        assert DARK_BOOTSWATCH_THEMES <= THEME_IDS

    def test_dark_labels_match_the_dark_set(self):
        """The label suffix is derived, so it can't drift from the set again."""
        for theme_id, label in BOOTSWATCH_THEMES:
            assert label.endswith(" (Dark)") == (theme_id in DARK_BOOTSWATCH_THEMES), theme_id

    def test_validators(self):
        assert is_valid_theme("darkly")
        assert is_valid_theme("default")
        assert not is_valid_theme("not-a-theme")
        assert not is_valid_theme("")
        assert is_dark_theme("cyborg")
        assert not is_dark_theme("flatly")


class TestThemePickerPartial:

    def _render(self, env, **kwargs):
        kwargs.setdefault("bootswatch_themes", BOOTSWATCH_THEMES)
        return env.get_template("partials/theme_picker.html").render(**kwargs)

    def test_renders_every_theme(self, env):
        html = self._render(env, selected_theme="default")
        assert html.count("<option") == len(BOOTSWATCH_THEMES)

    def test_marks_the_selected_theme(self, env):
        html = self._render(env, selected_theme="cyborg")
        assert '<option value="cyborg" selected>Cyborg (Dark)</option>' in html
        assert '<option value="flatly" selected>' not in html

    def test_element_ids_are_parameterised(self, env):
        """/config and /account render this on different pages; ids must not collide."""
        html = self._render(
            env, select_id="accountTheme", img_id="accountImg", name_id="accountName",
            selected_theme="lux",
        )
        assert 'id="accountTheme"' in html
        assert 'id="accountImg"' in html
        assert 'id="accountName"' in html
        assert 'id="bootstrapTheme"' not in html

    def test_defaults_to_config_ids(self, env):
        html = self._render(env, selected_theme="default")
        assert 'id="bootstrapTheme"' in html
        assert 'id="themePreviewImg"' in html
        assert 'id="themePreviewName"' in html


class TestDashboardEditorPartial:

    def _render(self, env, **kwargs):
        from routes.collection import DEFAULT_DASHBOARD_ORDER, dashboard_section_meta

        kwargs.setdefault("order", DEFAULT_DASHBOARD_ORDER)
        kwargs.setdefault("hidden", [])
        kwargs.setdefault("section_meta", dashboard_section_meta())
        return env.get_template("partials/dashboard_layout_editor.html").render(**kwargs)

    def test_renders_one_row_per_section(self, env):
        from routes.collection import DEFAULT_DASHBOARD_ORDER

        html = self._render(env)
        assert html.count("<li") == len(DEFAULT_DASHBOARD_ORDER)
        for section_id in DEFAULT_DASHBOARD_ORDER:
            assert f'data-section-id="{section_id}"' in html

    def test_hidden_sections_are_unchecked(self, env):
        html = self._render(env, hidden=["library"])

        library_row = html.split('data-section-id="library"')[1].split("</li>")[0]
        favorites_row = html.split('data-section-id="favorites"')[1].split("</li>")[0]

        assert "checked" not in library_row
        assert "checked" in favorites_row

    def test_uses_the_settings_label_for_discover(self, env):
        html = self._render(env)
        assert "Discover / Recommendations" in html
        assert "Also requires Recommendations enabled" in html

    def test_respects_the_given_order(self, env):
        html = self._render(env, order=["library", "favorites"])
        assert html.index('data-section-id="library"') < html.index('data-section-id="favorites"')

    def test_list_id_is_parameterised(self, env):
        html = self._render(env, list_id="accountDashboardList")
        assert 'id="accountDashboardList"' in html
        assert 'id="dashboardSectionsList"' not in html


class TestDashboardSectionMeta:

    def test_covers_every_section(self):
        from routes.collection import DASHBOARD_SECTION_DEFS, dashboard_section_meta

        meta = dashboard_section_meta()
        assert set(meta) == set(DASHBOARD_SECTION_DEFS)
        for entry in meta.values():
            assert entry["name"]
            assert entry["icon"]
