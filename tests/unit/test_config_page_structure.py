"""App Settings (/config): layout, headings, labels and the remembered tab.

The page is one template of nine tabs and ~35 sections, so these are asserted
against its source rather than by rendering every tab.

- Sections are ``clu-settings-panel``, never a nested ``.container``: an inner
  container stops at 1320px, so on a Wide page the header and tabs stretched
  while every panel stayed a centred column.
- The page has one ``h1`` (the page header); section titles are ``h2`` and
  sub-sections ``h3``, keeping their old look through a size class. They were
  ``h4``/``h5``/``h6`` straight under the ``h1``, so a screen reader's heading
  list started three levels deep.
- The open tab is written to the URL hash, so a reload comes back to it.
- Every tab that holds settings ends with one sticky Save bar
  (``clu-settings-save``) and has no other Save. Personalization used to have
  five, Komga and series.json one each inline, and the rest a centred
  "Save X Settings" button wherever the tab ended.
- Buttons are primary, danger or outline-secondary: DESIGN.md gives colour on
  a button exactly two meanings.
- The grey is the header's theme-safe neutral (``.clu-neutral-buttons``), not
  ``--bs-secondary``: on Darkly that grey is about 1.6:1 against the page.
  Option cards (page width, thumbnail style) share one ``.clu-choice`` look,
  and a chosen option wears the selection tint.
- Fills carry readable text (``text-bg-*``), never a fixed ``bg-light``
  surface, which turns light-on-light on a dark theme.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def config_html():
    return (ROOT / "templates" / "config.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dashboard_editor():
    return (ROOT / "templates" / "partials" / "dashboard_layout_editor.html").read_text(encoding="utf-8")


def test_sections_are_settings_panels_not_nested_containers(config_html):
    assert 'class="container' not in config_html
    assert config_html.count('class="clu-settings-panel') >= 30


def test_settings_panels_have_no_width_or_shadow_of_their_own(config_html):
    rule = re.search(r"\.clu-settings-panel\s*\{([^}]*)\}", config_html).group(1)
    assert not re.search(r"(^|[\s;])(max-|min-)?width\s*:", rule)
    assert "shadow" not in rule


def test_tabs_scroll_instead_of_wrapping(config_html):
    tabs = re.search(r'<ul[^>]*id="configTabs"[^>]*>', config_html).group(0)
    assert "clu-settings-tabs" in tabs
    assert "clu-filter-scroll" in tabs  # edge fade + active tab kept in view


def test_no_heading_skips_from_the_page_title(config_html):
    # Modal titles are their own dialog and keep Bootstrap's h5.
    body = re.sub(r'<h5 class="modal-title[^>]*>.*?</h5>', "", config_html, flags=re.S)
    assert not re.search(r"<h[456][\s>]", body), "section headings are h2, sub-sections h3"
    assert "<h1" not in body  # page_header renders the only h1


def test_wanted_simulation_numbers_are_not_headings(config_html):
    for stat in ("Total", "Accept", "Fallback", "NoMatch", "NoResults"):
        assert f'<div class="h4 mb-0" id="gcWantedSim{stat}">' in config_html


def test_open_tab_is_kept_in_the_url(config_html):
    assert "shown.bs.tab" in config_html
    assert "history.replaceState" in config_html
    assert "addEventListener('hashchange', showTabFromHash)" in config_html


@pytest.mark.parametrize("label", [
    'aria-label="series.json provider"',
    "aria-label=\"Komga path prefix for ' + escapeHtml(m.library_name)",
])
def test_controls_without_a_visible_label_carry_one(config_html, label):
    assert label in config_html


def test_dashboard_rows_wrap_and_checkboxes_are_labelled(dashboard_editor):
    assert 'aria-label="Show {{ meta.name }}"' in dashboard_editor
    assert "min-width: 0" in dashboard_editor
    assert "text-break" in dashboard_editor


SETTINGS_TABS = {
    "file-processing": "saveFileProcessingSettings",
    "download-api": "saveDownloadApiSettings",
    "metadata-providers": "saveSeriesJsonProvider",
    "system-perf": "saveSystemPerfSettings",
    "notifications": "saveNotificationSettings",
    "personalization": "savePersonalizationSettings",
}
RECORD_TABS = ("libraries", "download-clients", "database")


def _tab(config_html, tab_id):
    tab = config_html.split(f'id="{tab_id}" role="tabpanel"')[1]
    return tab.split('role="tabpanel"')[0].split("<!-- Convert Library Confirmation Modal -->")[0]


@pytest.mark.parametrize("tab_id,handler", sorted(SETTINGS_TABS.items()))
def test_each_settings_tab_has_one_save_bar(config_html, tab_id, handler):
    tab = _tab(config_html, tab_id)
    assert tab.count('class="clu-settings-save"') == 1
    bar = tab.split('class="clu-settings-save"')[1]
    assert f'onclick="{handler}()"' in bar.split("</div>")[0]
    # Nothing follows the bar inside the tab, so it is the tab's last word.
    tail = bar.split("</div>", 1)[1]
    assert "clu-settings-panel" not in tail
    # No other Save button anywhere in the tab's markup.
    assert tab.count("bi-floppy") == 1


@pytest.mark.parametrize("tab_id", RECORD_TABS)
def test_tabs_of_records_have_no_save_bar(config_html, tab_id):
    assert "clu-settings-save" not in _tab(config_html, tab_id)


def test_save_bar_sticks_and_is_flat(config_html):
    rule = re.search(r"\.clu-settings-save\s*\{([^}]*)\}", config_html).group(1)
    assert "position: sticky" in rule
    assert "bottom: 0" in rule
    assert "var(--bs-body-bg)" in rule  # opaque: fields scroll under it
    assert "shadow" not in rule


def test_personalization_saves_every_section_in_one_go(config_html):
    fn = config_html.split("async function savePersonalizationSettings()")[1].split("\n    }\n")[0]
    for step in ("saveStylingSettings", "savePageWidthSettings", "saveDashboardSettings",
                 "saveThumbnailStyleSettings", "saveRecommendationSettings"):
        assert step in fn
    # Page width is rendered server-side: reload only when it changed.
    assert "if (widthChanged) setTimeout(() => window.location.reload()" in fn


def test_download_save_leaves_an_unconfigured_komga_alone(config_html):
    fn = config_html.split("async function saveKomgaConfig()")[1].split("\n    }\n")[0]
    # Its payload says enabled: true, so a blank form must not be posted.
    assert "&& !komgaLoadedUrl) return null" in fn.split("postSettings(")[0]
    assert "komgaLoadedUrl = cfg.server_url" in config_html
    download = config_html.split("async function saveDownloadApiSettings()")[1].split("\n    }\n")[0]
    assert "saveKomgaConfig" in download


def test_buttons_are_primary_danger_or_neutral(config_html):
    assert not re.search(r"btn-(outline-)?(info|warning|success)\b", config_html)
    assert "btn-outline-primary" not in config_html


def test_header_actions_live_in_more(config_html):
    header = config_html.split("page_header('App Settings'")[1].split("{% endcall %}")[0]
    assert "slot == 'actions'" not in header
    more = header.split("slot == 'more'")[1]
    for item in ("Run Guided Setup", "Import from Mylar3", 'onclick="restartApp()"'):
        assert item in more
    assert more.index("dropdown-divider") < more.index("restartApp")
    assert config_html.count("restartApp()") == 2  # the menu item and its definition


@pytest.fixture(scope="module")
def page_css():
    return (ROOT / "static" / "css" / "clu-page.css").read_text(encoding="utf-8")


def test_settings_buttons_use_the_theme_safe_neutral(config_html, page_css):
    assert '<div class="tab-content clu-neutral-buttons" id="configTabsContent">' in config_html
    rule = page_css.split(".clu-neutral-buttons .btn-outline-secondary,")[1].split("}")[0]
    assert "--bs-btn-color: var(--bs-body-color)" in rule
    checked = page_css.split(".clu-neutral-buttons .btn-check:checked + .btn-outline-secondary,")[1].split("}")[0]
    assert "color-mix(in srgb, var(--bs-primary) 70%, var(--bs-emphasis-color))" in checked


def test_option_cards_share_one_look():
    picker = (ROOT / "templates" / "partials" / "page_width_picker.html").read_text(encoding="utf-8")
    assert "btn btn-outline-secondary clu-choice" in picker
    assert "btn-outline-primary" not in picker  # /account renders it too


def test_thumbnail_style_cards_are_choices(config_html):
    assert 'class="btn btn-outline-secondary clu-choice w-100 h-100' in config_html


def test_fills_carry_readable_text(config_html):
    assert not re.search(r"\bbadge bg-", config_html)
    assert "bg-light" not in config_html
    assert "text-dark" not in config_html


def test_labels_have_no_trailing_colon_or_shouting(config_html):
    assert ":</label>" not in config_html
    labels = re.findall(r'class="form-label[^"]*">([^<]+)</label>', config_html)
    assert labels
    assert not [l for l in labels if l.isupper() and len(l) > 4], "Title Case, like the rest of the page"


def test_code_in_alerts_takes_the_alert_colour(page_css):
    assert re.search(r"\.alert code\s*\{\s*color: inherit;", page_css)
