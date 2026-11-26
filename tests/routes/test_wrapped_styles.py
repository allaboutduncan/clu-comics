"""Wrapped styles (core/wrapped_styles.py): palettes, per-scope defaults, the
saved preference, and every style drawing every slide."""
import io
import pathlib
from unittest.mock import patch

import pytest
from PIL import Image

import wrapped
from core import wrapped_slides, wrapped_styles
from core.database import get_user_setting_override, mark_issue_read, set_user_preference
from core.wrapped_styles import MONTHLY, STYLES, YEARLY, resolve_style, styles_for

ROOT = pathlib.Path(__file__).resolve().parents[2]
PNG_MAGIC = bytes([0x89]) + b"PNG"


@pytest.fixture
def reads(db_connection):
    """Reads with credits, characters, reading time and a goal: enough for
    every slide in the registry to have something to draw."""
    from models.reading_insights import set_reading_goal

    for n in range(1, 4):
        mark_issue_read(f"/data/DC/Batman (2016)/Batman 00{n}.cbz",
                        read_at=f"2024-06-0{n}T10:00:00", page_count=20,
                        time_spent=600, publisher="DC Comics",
                        writer="Tom King", penciller="David Finch",
                        characters="Batman, Robin")
    set_reading_goal(2024, 10)


def _pixel(data, xy):
    return Image.open(io.BytesIO(data)).convert("RGB").getpixel(xy)


class TestRegistry:
    def test_every_palette_has_the_keys_a_theme_palette_has(self):
        theme_keys = set(wrapped.THEME_COLORS["default"])
        assert theme_keys == set(wrapped_styles.PALETTE_KEYS)
        for style in STYLES.values():
            if style.palette is not None:
                assert set(style.palette) == theme_keys, style.id

    def test_theme_has_no_palette_of_its_own(self):
        assert STYLES["theme"].palette is None and STYLES["theme"].paint is None

    def test_scopes(self):
        every = ["theme", "classic", "halftone", "newsprint", "neon"]
        assert [s.id for s in styles_for(YEARLY)] == every
        assert [s.id for s in styles_for(MONTHLY)] == every

    def test_defaults_are_the_look_each_recap_always_had(self):
        assert wrapped_styles.DEFAULTS == {YEARLY: "theme", MONTHLY: "classic"}

    @pytest.mark.parametrize("style,scope,expected", [
        ("neon", YEARLY, "neon"),
        ("classic", MONTHLY, "classic"),
        ("classic", YEARLY, "classic"),
        ("vaporwave", MONTHLY, "classic"),
        ("", YEARLY, "theme"),
        (None, MONTHLY, "classic"),
        (42, YEARLY, "theme"),
    ])
    def test_resolve(self, style, scope, expected):
        assert resolve_style(style, scope) == expected

    def test_fonts_exist_for_every_family_used(self):
        for style in STYLES.values():
            assert style.font_family in wrapped._FONT_FILES


class TestActiveStyle:
    def test_palette_follows_the_style_inside_the_block_only(self):
        assert wrapped.get_theme_colors("darkly")["bg"] == "#222222"
        with wrapped.use_style("neon"):
            assert wrapped.get_theme_colors("darkly") == STYLES["neon"].palette
        assert wrapped.get_theme_colors("darkly")["bg"] == "#222222"

    def test_theme_style_and_no_style_use_the_theme(self):
        for style in ("theme", None, "no-such-style"):
            with wrapped.use_style(style):
                assert wrapped.get_theme_colors("darkly")["bg"] == "#222222"

    def test_a_failed_render_does_not_leave_the_style_on(self):
        with pytest.raises(RuntimeError):
            with wrapped.use_style("neon"):
                raise RuntimeError("boom")
        assert wrapped.get_theme_colors("default")["bg"] == "#ffffff"

    def test_a_designed_style_paints_its_own_background(self):
        with wrapped.use_style("newsprint"):
            img = wrapped.create_base_image(wrapped.get_theme_colors("default"))
        assert img.size == (1080, 1920)
        # Paper in the middle, ink on the frame.
        assert img.getpixel((540, 962)) in ((244, 239, 228), (235, 228, 214))
        assert img.getpixel((26, 960)) == (27, 27, 27)

    def test_get_font_returns_a_font_in_every_family(self):
        for style in ("theme", "newsprint", "neon"):
            with wrapped.use_style(style):
                assert wrapped.get_font(40, bold=True) is not None


class TestEveryStyleDrawsEverySlide:
    @pytest.mark.parametrize("style", [s.id for s in styles_for(YEARLY)])
    def test_yearly(self, reads, style):
        manifest = wrapped_slides.build_manifest(YEARLY, 2024, style=style, user_id=1)
        # The fixture gives every slide something to draw.
        assert [m["id"] for m in manifest] == [s.id for s in wrapped_slides.slides_for(YEARLY)]
        for spec in wrapped_slides.slides_for(YEARLY):
            data = wrapped_slides.render_slide(YEARLY, spec.id, 2024, style=style, user_id=1)
            assert data and data.startswith(PNG_MAGIC), (style, spec.id)
            assert Image.open(io.BytesIO(data)).size[0] == 1080

    @pytest.mark.parametrize("style", [s.id for s in styles_for(MONTHLY)])
    def test_monthly(self, reads, style):
        manifest = wrapped_slides.build_manifest(MONTHLY, 2024, 6, style=style, user_id=1)
        assert [m["id"] for m in manifest] == [s.id for s in wrapped_slides.slides_for(MONTHLY)]
        for spec in wrapped_slides.slides_for(MONTHLY):
            data = wrapped_slides.render_slide(MONTHLY, spec.id, 2024, 6, style=style, user_id=1)
            assert data and data.startswith(PNG_MAGIC), (style, spec.id)
            assert Image.open(io.BytesIO(data)).size == (1080, 1920)

    def test_styles_really_differ(self, reads):
        corners = {
            style: _pixel(wrapped_slides.render_slide(YEARLY, "summary", 2024, style=style, user_id=1), (540, 4))
            for style in ("theme", "halftone", "newsprint", "neon")
        }
        assert len(set(corners.values())) == 4

    def test_monthly_follows_the_theme_when_asked_to(self, reads):
        # The recap ignored the theme entirely; in the theme style it is drawn
        # on the theme's background.
        classic = wrapped_slides.render_slide(MONTHLY, "recap", 2024, 6, theme="default",
                                              style="classic", user_id=1)
        themed = wrapped_slides.render_slide(MONTHLY, "recap", 2024, 6, theme="default",
                                             style="theme", user_id=1)
        # A light gradient, not the dark panel art.
        assert sum(_pixel(themed, (4, 4))) > 700
        assert sum(_pixel(classic, (4, 4))) < 200


class TestComicPanelsArtwork:
    """The panel artwork has "READING RECAP" painted into its top. Monthly
    slides are laid out under it; a yearly slide draws its own logo or title
    there, so it gets the artwork from below that title."""

    def test_yearly_background_is_the_art_without_its_title(self):
        img = wrapped_styles.paint_comic_panels(1080, 1920, STYLES["classic"].palette)
        assert img.size == (1080, 1920)
        # The painted title is near-white lettering across the top; with it
        # cropped away nothing in that band is bright.
        band = img.crop((200, 20, 880, 190)).convert("L")
        assert band.getextrema()[1] < 140

    def test_the_whole_file_does_have_the_title(self):
        # Guards the crop above: if the artwork is ever replaced by one with
        # no title, PANEL_TITLE_SHARE should go, not silently crop good art.
        art = Image.open(wrapped_styles.PANEL_ARTWORK).convert("L")
        band = art.crop((int(art.width * 0.2), 0, int(art.width * 0.8),
                         int(art.height * wrapped_styles.PANEL_TITLE_SHARE)))
        assert band.getextrema()[1] > 200

    def test_a_missing_file_falls_back_to_the_panel_colour(self, monkeypatch):
        monkeypatch.setattr(wrapped_styles, "PANEL_ARTWORK", "/nope/recap-bg.png")
        img = wrapped_styles.paint_comic_panels(1080, 1920, STYLES["classic"].palette)
        assert img.size == (1080, 1920)
        assert img.getpixel((540, 960)) == (30, 37, 46)

    def test_monthly_still_uses_the_whole_artwork(self, reads):
        data = wrapped_slides.render_slide(MONTHLY, "recap", 2024, 6, style="classic", user_id=1)
        band = Image.open(io.BytesIO(data)).crop((200, 20, 880, 190)).convert("L")
        assert band.getextrema()[1] > 200


class TestBooksGridFollowsTheStyle:
    """The yearly grid of everything read builds its own, variable-height
    background, so it was the one slide a style's background did not reach."""

    @pytest.mark.parametrize("style,frame", [
        ("halftone", (17, 17, 17)),       # the ink border
        ("newsprint", (27, 27, 27)),
    ])
    def test_designed_background(self, reads, style, frame):
        data = wrapped_slides.render_slide(YEARLY, "books-grid", 2024, style=style, user_id=1)
        img = Image.open(io.BytesIO(data)).convert("RGB")
        assert img.getpixel((26, img.height // 2)) == frame

    def test_theme_style_keeps_its_gradient(self, reads):
        data = wrapped_slides.render_slide(YEARLY, "books-grid", 2024, theme="default",
                                           style="theme", user_id=1)
        assert sum(_pixel(data, (4, 4))) > 700


class TestMonthlyEdgeMargin:
    """Covers on the monthly recap and all-issues slides sat 10px from the
    edge of the image."""

    SOURCE = (ROOT / "wrapped.py").read_text(encoding="utf-8")

    def test_margin_is_generous_and_used_by_every_grid(self):
        assert wrapped.MONTHLY_EDGE_MARGIN >= 20
        assert "margin_x = 10" not in self.SOURCE
        assert self.SOURCE.count("margin_x = MONTHLY_EDGE_MARGIN") == 2
        assert "IMAGE_WIDTH - 2 * MONTHLY_EDGE_MARGIN" in self.SOURCE

    @pytest.mark.parametrize("slide", ["recap", "all-issues"])
    def test_nothing_is_drawn_in_the_margin(self, db_connection, slide):
        # Enough issues to fill the grids edge to edge. On the flat-colour
        # fallback background (no artwork), any pixel in the margin that is
        # not the background colour is a cover or a placeholder.
        for n in range(60):
            mark_issue_read(f"/data/S{n % 12}/S{n % 12} {n:03d}.cbz",
                            read_at=f"2024-06-{n % 28 + 1:02d}T10:00:00", page_count=20)
        with patch("wrapped.os.path.exists", return_value=False):
            with wrapped.use_style("classic"):
                generator = (wrapped.generate_monthly_recap_slide if slide == "recap"
                             else wrapped.generate_monthly_all_issues_slide)
                img = Image.open(io.BytesIO(generator(2024, 6, "default"))).convert("RGB")
        margin = wrapped.MONTHLY_EDGE_MARGIN - 4
        for strip in (img.crop((0, 260, margin, 1650)), img.crop((1080 - margin, 260, 1080, 1650))):
            assert strip.getcolors() == [(strip.width * strip.height, (30, 37, 46))]


class TestRoutes:
    def test_manifest_lists_the_scope_styles(self, client, reads):
        yearly = client.get("/api/wrapped/2024/manifest").get_json()
        assert yearly["style"] == "theme"
        assert [s["id"] for s in yearly["styles"]] == [
            "theme", "classic", "halftone", "newsprint", "neon"]
        assert all(s["label"] for s in yearly["styles"])
        monthly = client.get("/api/wrapped/monthly/2024/6/manifest").get_json()
        assert "classic" in [s["id"] for s in monthly["styles"]]

    def test_style_is_in_every_slide_url_and_changes_the_version(self, client, reads):
        theme = client.get("/api/wrapped/2024/manifest?style=theme").get_json()["slides"][0]
        neon = client.get("/api/wrapped/2024/manifest?style=neon").get_json()["slides"][0]
        assert "style=theme" in theme["url"] and "style=neon" in neon["url"]
        assert theme["version"] != neon["version"]
        assert "immutable" in client.get(neon["url"]).headers["Cache-Control"]

    def test_year_wrapped_can_be_drawn_in_comic_panels(self, client, reads):
        body = client.get("/api/wrapped/2024/manifest?style=classic").get_json()
        assert body["style"] == "classic"
        assert "classic" in [s["id"] for s in body["styles"]]
        for slide in body["slides"]:
            r = client.get(slide["url"])
            assert r.status_code == 200, slide["id"]
            # Dark panel art in the corner, not a light theme gradient.
            assert sum(_pixel(r.data, (6, 960))) < 250, slide["id"]

    def test_a_style_a_scope_lacks_falls_back(self, client, reads, monkeypatch):
        # Every style suits both recaps today; the fallback still has to hold
        # for one that does not.
        from dataclasses import replace

        monkeypatch.setitem(STYLES, "neon", replace(STYLES["neon"], scopes=(MONTHLY,)))
        assert client.get("/api/wrapped/2024/manifest?style=neon").get_json()["style"] == "theme"
        assert client.get("/api/wrapped/monthly/2024/6/manifest?style=neon").get_json()["style"] == "neon"

    def test_each_style_is_cached_separately(self, client, reads):
        with patch("wrapped.generate_summary_slide", return_value=PNG_MAGIC + b"x") as generator:
            for style in ("theme", "neon", "theme", "neon"):
                client.get(f"/api/wrapped/2024/slide/summary.png?style={style}")
        assert generator.call_count == 2

    def test_the_zip_is_drawn_in_the_requested_style(self, client, reads):
        seen = []

        def record(year, theme):
            seen.append(wrapped.get_theme_colors(theme)["bg"])
            return PNG_MAGIC + b"x"

        with patch("wrapped.generate_summary_slide", side_effect=record):
            assert client.get("/api/wrapped/2024/download?style=halftone").status_code == 200
        assert seen == [STYLES["halftone"].palette["bg"]]


class TestSavedPreference:
    def _save(self, client, style):
        return client.post("/api/account/wrapped-style", json={"style": style})

    def test_saved_style_is_used_when_none_is_asked_for(self, client, reads):
        assert self._save(client, "neon").get_json() == {"success": True, "style": "neon"}
        assert client.get("/api/wrapped/2024/manifest").get_json()["style"] == "neon"
        assert client.get("/api/wrapped/monthly/2024/6/manifest").get_json()["style"] == "neon"

    def test_an_explicit_style_wins_over_the_saved_one(self, client, reads):
        self._save(client, "neon")
        assert client.get("/api/wrapped/2024/manifest?style=halftone").get_json()["style"] == "halftone"

    def test_choosing_comic_panels_applies_to_both_recaps(self, client, reads):
        self._save(client, "classic")
        assert client.get("/api/wrapped/2024/manifest").get_json()["style"] == "classic"
        assert client.get("/api/wrapped/monthly/2024/6/manifest").get_json()["style"] == "classic"

    def test_site_default_applies_until_the_user_chooses(self, client, reads):
        set_user_preference("wrapped_style", "newsprint")
        assert client.get("/api/wrapped/2024/manifest").get_json()["style"] == "newsprint"
        self._save(client, "neon")
        assert client.get("/api/wrapped/2024/manifest").get_json()["style"] == "neon"

    def test_clearing(self, client, reads):
        self._save(client, "neon")
        assert self._save(client, "").get_json() == {"success": True, "style": None}
        assert get_user_setting_override("wrapped_style") is None
        assert client.get("/api/wrapped/monthly/2024/6/manifest").get_json()["style"] == "classic"

    @pytest.mark.parametrize("style", ["vaporwave", 7, ["neon"]])
    def test_unknown_style_is_refused(self, client, db_connection, style):
        r = self._save(client, style)
        assert r.status_code == 400
        assert get_user_setting_override("wrapped_style") is None

    def test_the_preference_is_personal(self, client, reads):
        self._save(client, "neon")
        assert get_user_setting_override("wrapped_style", user_id=2) is None

    def test_setting_is_in_the_schema(self):
        from core.settings import SETTINGS_SCHEMA

        assert "WRAPPED_STYLE" in SETTINGS_SCHEMA


class TestViewerPicker:
    JS = (ROOT / "static" / "js" / "clu-wrapped-story.js").read_text(encoding="utf-8")
    PARTIAL = (ROOT / "templates" / "partials" / "wrapped_story.html").read_text(encoding="utf-8")

    def test_picker_is_in_the_partial_and_labelled(self):
        assert 'id="wrappedStoryStyle"' in self.PARTIAL
        assert 'for="wrappedStoryStyle"' in self.PARTIAL

    def test_options_come_from_the_manifest(self):
        assert "manifest.styles" in self.JS
        for hard_coded in ("halftone", "newsprint", "neon", "classic"):
            assert hard_coded not in self.JS

    def test_changing_style_reloads_saves_and_restyles_the_download(self):
        body = self.JS[self.JS.index("async function changeStyle()"):]
        body = body[:body.index("\n    }\n")]
        assert "fetchManifest(state.options, style)" in body
        assert "/api/account/wrapped-style" in body
        assert "withStyle(state.options.downloadUrl, manifest.style)" in self.JS
