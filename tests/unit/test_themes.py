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
    DARK_THEMES,
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
        assert DARK_THEMES <= THEME_IDS

    def test_dark_labels_match_the_dark_set(self):
        """The label suffix is derived, so it can't drift from the set again."""
        for theme_id, label in BOOTSWATCH_THEMES:
            assert label.endswith(" (Dark)") == (theme_id in DARK_THEMES), theme_id

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


class TestLocalThemes:
    """Themes Bootswatch does not ship (Gruvbox, Catppuccin): stock Bootstrap
    plus stylesheets and a preview image of ours."""

    def test_gruvbox_is_offered_and_dark(self):
        from core.themes import LOCAL_THEMES
        assert "gruvbox-dark" in LOCAL_THEMES
        assert is_valid_theme("gruvbox-dark")
        assert is_dark_theme("gruvbox-dark")
        assert ("gruvbox-dark", "Gruvbox (Dark)") in BOOTSWATCH_THEMES

    def test_every_local_theme_has_its_files(self):
        from core.themes import LOCAL_THEMES, local_stylesheets
        static = os.path.join(os.path.dirname(TEMPLATE_DIR), "static")
        for theme_id, spec in LOCAL_THEMES.items():
            assert theme_id in THEME_IDS
            assert local_stylesheets(theme_id), theme_id
            for css in local_stylesheets(theme_id):
                assert os.path.isfile(os.path.join(static, css)), css
            assert os.path.isfile(os.path.join(static, spec["preview"])), spec["preview"]

    def test_every_local_theme_has_year_in_review_colours(self):
        """wrapped.py falls back to the default palette for a theme it does
        not know, which would draw a dark theme's year in review white."""
        from core.themes import LOCAL_THEMES
        import wrapped
        for theme_id in LOCAL_THEMES:
            colours = wrapped.THEME_COLORS[theme_id]
            assert colours["is_dark"] == is_dark_theme(theme_id), theme_id

    def test_stylesheets(self):
        from core.themes import (
            BOOTSTRAP_CSS, DEFAULT_BOOTSTRAP_CSS, theme_stylesheets,
        )
        assert theme_stylesheets("gruvbox-dark") == (BOOTSTRAP_CSS, ("css/themes/gruvbox-dark.css",))
        # Palette first, then the rules every flavour shares
        assert theme_stylesheets("catppuccin-mocha") == (
            BOOTSTRAP_CSS,
            ("css/themes/catppuccin-mocha.css", "css/themes/catppuccin.css"),
        )
        assert theme_stylesheets("cyborg") == (
            "https://cdn.jsdelivr.net/npm/bootswatch@5.3.8/dist/cyborg/bootstrap.min.css", ())
        assert theme_stylesheets("default") == (DEFAULT_BOOTSTRAP_CSS, ())
        # An unknown id must not become a 404 stylesheet
        assert theme_stylesheets("not-a-theme") == (DEFAULT_BOOTSTRAP_CSS, ())
        assert theme_stylesheets(None) == (DEFAULT_BOOTSTRAP_CSS, ())

    def test_previews(self):
        from core.themes import theme_previews
        previews = theme_previews(lambda path: "/prefix/static/" + path)
        assert previews["gruvbox-dark"] == "/prefix/static/images/themes/gruvbox-dark.png"
        assert previews["cyborg"] == "https://bootswatch.com/cyborg/thumbnail.png"
        assert set(previews) == set(THEME_IDS)

    def test_picker_carries_the_previews_and_shows_a_local_one(self, env):
        from core.themes import theme_previews
        html = env.get_template("partials/theme_picker.html").render(
            bootswatch_themes=BOOTSWATCH_THEMES,
            theme_previews=theme_previews(lambda path: "/static/" + path),
            selected_theme="gruvbox-dark",
        )
        assert "data-previews=" in html and "/static/images/themes/gruvbox-dark.png" in html
        assert 'src="/static/images/themes/gruvbox-dark.png"' in html
        assert ">Gruvbox</strong>" in html

    def test_picker_does_not_rebuild_bootswatch_urls(self, env):
        """theme_previews() is the one place the thumbnail URL is built."""
        html = env.get_template("partials/theme_picker.html").render(
            bootswatch_themes=BOOTSWATCH_THEMES, selected_theme="cyborg")
        assert "bootswatch.com" not in html

    def test_head_partial_without_the_context_processor(self, env):
        """A bare Jinja env (no auth_bp) still gets a stylesheet, not an
        UndefinedError. It is what the "default" theme loads, so the
        literal in the template must follow DEFAULT_BOOTSTRAP_CSS; it never
        loads a local sheet, so BOOTSTRAP_CSS (5.3.8) would be the wrong pin."""
        from core.themes import DEFAULT_BOOTSTRAP_CSS
        html = env.get_template("partials/theme_css.html").render()
        assert f'<link href="{DEFAULT_BOOTSTRAP_CSS}" rel="stylesheet">' in html
        assert "css/themes/" not in html

    def test_head_partial_loads_bootstrap_then_the_local_sheet(self, env):
        from flask import Flask
        from core.themes import theme_stylesheets
        app = Flask(__name__, template_folder=TEMPLATE_DIR)
        with app.test_request_context():
            html = app.jinja_env.get_template("partials/theme_css.html").render(
                bootstrap_theme="gruvbox-dark", theme_stylesheets=theme_stylesheets)
        assert html.index("bootstrap@5.3.8") < html.index("/static/css/themes/gruvbox-dark.css")
        assert "bootswatch" not in html

    def test_head_partial_loads_every_local_sheet_in_order(self, env):
        from flask import Flask
        from core.themes import theme_stylesheets
        app = Flask(__name__, template_folder=TEMPLATE_DIR)
        with app.test_request_context():
            html = app.jinja_env.get_template("partials/theme_css.html").render(
                bootstrap_theme="catppuccin-latte", theme_stylesheets=theme_stylesheets)
        assert (html.index("bootstrap@5.3.8")
                < html.index("/static/css/themes/catppuccin-latte.css")
                < html.index("/static/css/themes/catppuccin.css\""))


class TestCatppuccin:
    """Four flavours: one shared stylesheet of rules, plus a palette per
    flavour generated by tools/build_catppuccin_themes.py."""

    FLAVOURS = ("latte", "frappe", "macchiato", "mocha")

    @staticmethod
    def _tool():
        import importlib.util
        path = os.path.join(os.path.dirname(TEMPLATE_DIR), "tools", "build_catppuccin_themes.py")
        spec = importlib.util.spec_from_file_location("build_catppuccin_themes", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_theme_display_name(self):
        """The account page's "Use site default (...)" button used to
        title-case the id: "Catppuccin-Frappe"."""
        from core.themes import theme_display_name
        assert theme_display_name("catppuccin-frappe") == "Catppuccin Frappé"
        assert theme_display_name("gruvbox-dark") == "Gruvbox"
        assert theme_display_name("cyborg") == "Cyborg"
        assert theme_display_name("default") == "Default (Bootstrap)"
        assert theme_display_name(None) == "Default (Bootstrap)"

    def test_offered_with_latte_as_the_light_one(self):
        labels = dict(BOOTSWATCH_THEMES)
        assert labels["catppuccin-latte"] == "Catppuccin Latte"
        assert labels["catppuccin-frappe"] == "Catppuccin Frappé (Dark)"
        assert labels["catppuccin-macchiato"] == "Catppuccin Macchiato (Dark)"
        assert labels["catppuccin-mocha"] == "Catppuccin Mocha (Dark)"
        assert not is_dark_theme("catppuccin-latte")

    def test_generated_palettes_are_current(self):
        """A hand edit to a generated file would be lost on the next run."""
        tool = self._tool()
        for path, text in tool.outputs().items():
            with open(path, encoding="utf-8") as fh:
                assert fh.read() == text, (
                    f"{os.path.basename(path)} is stale: run tools/build_catppuccin_themes.py")

    def test_every_variable_the_rules_read_is_defined(self):
        import re
        static = os.path.join(os.path.dirname(TEMPLATE_DIR), "static", "css", "themes")
        with open(os.path.join(static, "catppuccin.css"), encoding="utf-8") as fh:
            rules = fh.read()
        used = set(re.findall(r"var\(--(ctp-[a-z0-9-]+)\)", rules))
        # Set inside the rules themselves (per-component fills)
        local = set(re.findall(r"--(ctp-[a-z0-9-]+)\s*:", rules))
        for flavour in self.FLAVOURS:
            with open(os.path.join(static, f"catppuccin-{flavour}.css"), encoding="utf-8") as fh:
                defined = set(re.findall(r"--(ctp-[a-z0-9-]+)\s*:", fh.read()))
            assert used - local - defined == set(), flavour

    def test_role_colours_read_on_every_surface(self):
        """Each role is text on the page, cards and headers (base, mantle,
        crust) and a fill carrying base text, so all of those need 4.5:1."""
        tool = self._tool()
        for flavour in self.FLAVOURS:
            p, dark = tool.PALETTE[flavour], tool.DARK[flavour]
            for colour in tool.ROLES.values():
                value = tool.role_colour(p, colour, dark)
                for bg in ("base", "mantle", "crust"):
                    assert tool.contrast(value, p[bg]) >= 4.5, (flavour, colour, bg)

    def test_alert_text_reads_on_its_wash(self):
        """.alert-x draws --bs-x-text-emphasis on --bs-x-bg-subtle, which the
        stylesheet mixes at the generator's SUBTLE_SHARE into base."""
        tool = self._tool()
        static = os.path.join(os.path.dirname(TEMPLATE_DIR), "static", "css", "themes")
        with open(os.path.join(static, "catppuccin.css"), encoding="utf-8") as fh:
            rules = fh.read()
        share = round(tool.SUBTLE_SHARE * 100)
        assert f"--bs-danger-bg-subtle: color-mix(in srgb, var(--ctp-danger) {share}%, var(--ctp-base))" in rules
        for flavour in self.FLAVOURS:
            p, dark = tool.PALETTE[flavour], tool.DARK[flavour]
            for role in tool.ALERT_ROLES:
                fill = tool.role_colour(p, tool.ROLES[role], dark)
                wash = tool.mix(fill, p["base"], tool.SUBTLE_SHARE)
                assert tool.contrast(tool.emphasis_colour(p, fill, dark), wash) >= 4.5, (flavour, role)

    def test_muted_text_reads_on_every_surface(self):
        tool = self._tool()
        for flavour in self.FLAVOURS:
            p = tool.PALETTE[flavour]
            muted = p[tool.muted_colour(p)]
            for bg in ("base", "mantle", "crust"):
                assert tool.contrast(muted, p[bg]) >= 4.5, (flavour, bg)

    def test_only_latte_departs_from_the_official_palette(self):
        tool = self._tool()
        for flavour in ("frappe", "macchiato", "mocha"):
            p = tool.PALETTE[flavour]
            for colour in tool.ROLES.values():
                assert tool.role_colour(p, colour, True) == p[colour], (flavour, colour)

    @staticmethod
    def _rules():
        static = os.path.join(os.path.dirname(TEMPLATE_DIR), "static", "css", "themes")
        with open(os.path.join(static, "catppuccin.css"), encoding="utf-8") as fh:
            return fh.read()

    @staticmethod
    def _palette_css(flavour):
        """The flavour's generated --ctp-* colours as {name: '#hex'}, with
        var(--ctp-x) aliases resolved. Read from the stylesheet the browser
        gets, not from the generator, so the checks below are independent."""
        import re
        static = os.path.join(os.path.dirname(TEMPLATE_DIR), "static", "css", "themes")
        with open(os.path.join(static, f"catppuccin-{flavour}.css"), encoding="utf-8") as fh:
            text = fh.read()
        raw = dict(re.findall(r"--(ctp-[a-z0-9-]+):\s*(#[0-9a-f]{6}|var\(--ctp-[a-z0-9-]+\))", text))
        def resolve(name):
            value = raw[name]
            return resolve(value[6:-1]) if value.startswith("var(") else value
        return {name[4:]: resolve(name) for name in raw if not name.endswith("-rgb")}

    def test_year_in_review_uses_the_stylesheet_colours(self):
        """wrapped.py draws the year-in-review images with each theme's
        colours; Catppuccin's must be the ones its stylesheet uses."""
        import wrapped
        for flavour in self.FLAVOURS:
            css = self._palette_css(flavour)
            colours = wrapped.THEME_COLORS[f"catppuccin-{flavour}"]
            for key in ("primary", "secondary", "success", "info", "warning", "danger"):
                assert colours[key] == css[key], (flavour, key)
            assert colours["bg"] == css["base"], flavour
            # bg_secondary is the raised surface: crust on Latte, not surface0
            assert colours["bg_secondary"] == css["raised"], flavour
            assert colours["text"] == css["text"], flavour
            assert colours["text_muted"] == css["muted"], flavour
            assert colours["is_dark"] == (flavour != "latte"), flavour

    def test_button_states_read(self):
        """Every state of the neutral buttons carries its text at 4.5:1, not
        just the button at rest (hover once sat on surface2, active on
        overlay0, both below 4.5:1)."""
        tool = self._tool()
        rules = self._rules()
        hover = round(tool.NEUTRAL_HOVER_SHARE * 100)
        active = round(tool.NEUTRAL_ACTIVE_SHARE * 100)
        for fill in ("secondary", "surface0"):
            assert f"color-mix(in srgb, var(--ctp-{fill}) {hover}%, var(--ctp-base))" in rules
            assert f"color-mix(in srgb, var(--ctp-{fill}) {active}%, var(--ctp-base))" in rules
        for flavour in self.FLAVOURS:
            css = self._palette_css(flavour)
            text = css["text"]
            fills = {
                "secondary rest": css["secondary"],
                "light rest": css["surface0"],
                "dark rest": css["crust"],
                "dark hover": css["mantle"],
                "dark active": css["surface0"],
            }
            for name, share in (("hover", tool.NEUTRAL_HOVER_SHARE), ("active", tool.NEUTRAL_ACTIVE_SHARE)):
                fills[f"secondary {name}"] = tool.mix(css["secondary"], css["base"], share)
                fills[f"light {name}"] = tool.mix(css["surface0"], css["base"], share)
            # Outline secondary/dark fill on hover (60%) and active (the fill itself)
            fills["outline hover"] = tool.mix(css["secondary"], css["base"], 0.6)
            fills["outline active"] = css["secondary"]
            for state, fill in fills.items():
                assert tool.contrast(text, fill) >= 4.5, (flavour, state, fill)

    # Surfaces that are dark on every theme: the reader's header (black at
    # 80% over the black reader) and the bulk-action bars' gradient stops.
    ALWAYS_DARK = ("#000000", "#1a1a2e", "#16213e", "#1a1d21", "#0d0f12")

    def test_light_and_dark_stay_light_and_dark_on_every_flavour(self):
        """Latte once mapped .text-light to its dark text and --bs-dark to a
        light grey, so the reader's controls and the bulk bars vanished."""
        tool = self._tool()
        rules = self._rules()
        assert ".text-light     { color: var(--ctp-on-dark) !important; }" in rules
        assert "--bs-dark: var(--ctp-dark-fill);" in rules
        assert "--bs-black-rgb: var(--ctp-dark-fill-rgb);" in rules
        assert "--bs-white-rgb: var(--ctp-on-dark-rgb);" in rules
        for flavour in self.FLAVOURS:
            css = self._palette_css(flavour)
            for surface in self.ALWAYS_DARK:
                assert tool.contrast(css["on-dark"], surface) >= 4.5, (flavour, surface)
            # .sw-bulk-action-bar: white on var(--bs-dark); .text-bg-dark and
            # outline-light's hover: on-dark and dark-fill against each other.
            assert tool.contrast("#ffffff", css["dark-fill"]) >= 4.5, flavour
            assert tool.contrast(css["on-dark"], css["dark-fill"]) >= 4.5, flavour

    def test_no_per_page_patches_for_dark_bars(self):
        """Dark bars read light from the variables above; a per-page rule is
        how the source-wall and reading-list bars were missed."""
        assert "bulk-action-bar" not in self._rules()

    def test_close_button_on_a_fill_is_drawn_in_base(self):
        """A white X on a pastel fill was about 2:1. Drawn in base, at full
        opacity, it reads as the fill's text does (test above)."""
        rules = self._rules()
        assert "--bs-btn-close-bg: var(--ctp-close-on-fill);" in rules
        assert "--bs-btn-close-filter: none;" in rules
        assert "--bs-btn-close-opacity: 1;" in rules
        for flavour in self.FLAVOURS:
            static = os.path.join(os.path.dirname(TEMPLATE_DIR), "static", "css", "themes")
            with open(os.path.join(static, f"catppuccin-{flavour}.css"), encoding="utf-8") as fh:
                text = fh.read()
            base = self._palette_css(flavour)["base"]
            close = next(line for line in text.splitlines() if "--ctp-close-on-fill:" in line)
            assert f"fill='%23{base[1:]}'" in close, flavour

    def test_rules_never_branch_on_dark_mode(self):
        """The navbar is data-bs-theme="dark" on every theme, so a rule keyed
        on it would leak dark styling into Latte's header."""
        static = os.path.join(os.path.dirname(TEMPLATE_DIR), "static", "css", "themes")
        with open(os.path.join(static, "catppuccin.css"), encoding="utf-8") as fh:
            rules = fh.read()
        import re
        rules = re.sub(r"/\*.*?\*/", "", rules, flags=re.S)
        assert 'data-bs-theme="dark"' not in rules and "data-bs-theme=dark" not in rules
