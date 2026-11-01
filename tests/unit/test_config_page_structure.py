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
  five, Komga one inline, and the rest a centred "Save X Settings" button
  wherever the tab ended. Metadata Providers is the exception: its credentials
  and library provider orders are records, each saved in place, so a tab Save
  there would read as saving them too. Its series.json choice keeps an inline
  Save beside the select.
- The bar is one macro (``partials/settings_save_bar.html``), not five copies.
- A tab Save sends only what it must. Personalization is five sections with
  five endpoints, sent only when their form changed: saving a site default
  for the theme, page width or dashboard also clears the owner's own choice.
  Komga's save switches sync on and reschedules it, so it too goes only when
  changed.
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
    assert "clu-filter-scroll-always" in tabs  # the tabs overflow a tablet too


def test_tabs_reuse_the_shared_scroller_rules(config_html):
    # The page used to carry its own copy of clu-page.css's scroll rules and
    # edge masks, to apply them above 768px; the copies would drift.
    style = config_html.split("<style>")[1].split("</style>")[0]
    assert "mask-image" not in style
    assert "overflow-x" not in style


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
    "system-perf": "saveSystemPerfSettings",
    "notifications": "saveNotificationSettings",
    "personalization": "savePersonalizationSettings",
}
RECORD_TABS = ("libraries", "download-clients", "database", "metadata-providers")


def _tab(config_html, tab_id):
    tab = config_html.split(f'id="{tab_id}" role="tabpanel"')[1]
    return tab.split('role="tabpanel"')[0].split("<!-- Convert Library Confirmation Modal -->")[0]


@pytest.mark.parametrize("tab_id,handler", sorted(SETTINGS_TABS.items()))
def test_each_settings_tab_has_one_save_bar(config_html, tab_id, handler):
    tab = _tab(config_html, tab_id)
    # The bar is the settings_save_bar macro, used plain or with {% call %}.
    bars = re.findall(r"settings_save_bar\('(\w+)', '(\w+)'\)", tab)
    assert len(bars) == 1
    button_id, bar_handler = bars[0]
    assert bar_handler == handler
    # The handler drives that button's spinner.
    fn = config_html.split(f"async function {handler}()")[1].split("\n    }\n")[0]
    assert f"runTabSave('{button_id}'" in fn
    # Nothing follows the bar inside the tab, so it is the tab's last word.
    tail = tab.split("settings_save_bar(")[1]
    assert "clu-settings-panel" not in tail
    # No other Save button anywhere in the tab's markup.
    assert "bi-floppy" not in tab
    assert "clu-settings-save" not in tab  # only the macro builds the bar


def test_save_bar_macro(config_html):
    macro = (ROOT / "templates" / "partials" / "settings_save_bar.html").read_text(encoding="utf-8")
    assert '<div class="clu-settings-save">' in macro
    assert 'id="{{ button_id }}" onclick="{{ handler }}()"' in macro
    assert macro.count("bi-floppy") == 1
    assert "if caller is defined %}{{ caller() }}{% endif %}" in macro
    assert "import settings_save_bar" in config_html


@pytest.mark.parametrize("tab_id", RECORD_TABS)
def test_tabs_of_records_have_no_save_bar(config_html, tab_id):
    tab = _tab(config_html, tab_id)
    assert "clu-settings-save" not in tab
    assert "settings_save_bar" not in tab


def test_save_bar_sticks_and_is_flat(config_html):
    rule = re.search(r"\.clu-settings-save\s*\{([^}]*)\}", config_html).group(1)
    assert "position: sticky" in rule
    assert "bottom: 0" in rule
    assert "var(--bs-body-bg)" in rule  # opaque: fields scroll under it
    assert "shadow" not in rule


def test_metadata_providers_series_json_saves_in_place(config_html):
    tab = _tab(config_html, "metadata-providers")
    panel = tab.split('id="seriesJsonProviderPref"')[1].split("</div>\n                </div>")[0]
    assert 'onclick="saveSeriesJsonProvider()"' in panel
    fn = config_html.split("async function saveSeriesJsonProvider()")[1].split("\n    }\n")[0]
    assert "'series.json provider saved'" in fn  # not "Metadata provider settings"


def _personalization_save(config_html):
    return config_html.split("async function savePersonalizationSettings()")[1].split("\n    }\n")[0]


def _personalization_sections(config_html):
    return config_html.split("const personalizationSections = {")[1].split("\n    };\n")[0]


def test_personalization_saves_every_section_from_one_table(config_html):
    table = _personalization_sections(config_html)
    for url, reader in (
        ("/api/config/styling", "readStylingSettings"),
        ("/api/config/page-width", "readPageWidthSettings"),
        ("/api/config/dashboard", "readDashboardSettings"),
        ("/api/config/thumbnails", "readThumbnailStyleSettings"),
        ("/api/config/recommendations", "readRecommendationSettings"),
    ):
        assert f"url: '{url}', read: {reader} }}" in table
    # The readers only read: posting is the table's job.
    for reader in ("readStylingSettings", "readPageWidthSettings", "readDashboardSettings",
                   "readThumbnailStyleSettings", "readRecommendationSettings"):
        body = config_html.split(f"function {reader}()")[1].split("\n    }\n")[0]
        assert "postSettings" not in body


def test_personalization_sends_only_changed_sections(config_html):
    """/api/config/styling, page-width and dashboard each delete the owner's own
    override. Posting them on every Save wiped an owner's personal theme, width
    and layout when they only changed the recommendation model."""
    fn = _personalization_save(config_html)
    assert ".filter(c => c.json !== c.section.saved)" in fn
    assert "runTabSave('savePersonalizationBtn', changed.map(" in fn
    # The snapshot is taken once the dashboard editor has rendered.
    assert ("CLU.initDashboardEditor('dashboardSectionsList');\n"
            "    snapshotPersonalization();") in config_html


def test_personalization_records_each_saved_section(config_html):
    fn = _personalization_save(config_html)
    # A partial failure must not leave a saved width looking unsaved, or the
    # next Save reloads unexpectedly.
    assert "if (!error) c.section.saved = c.json;" in fn
    assert "if (widthSaved && saved) setTimeout(() => window.location.reload()" in fn
    assert "else if (widthSaved) CLU.toast(" in fn


def _komga_save(config_html):
    return config_html.split("async function saveKomgaConfig()")[1].split("\n    }\n")[0]


def test_download_save_leaves_an_unconfigured_komga_alone(config_html):
    fn = _komga_save(config_html)
    # Its payload says enabled: true, so a blank form must not be posted.
    assert "&& !komgaLoadedUrl) return null" in fn.split("postSettings(")[0]
    assert "komgaLoadedUrl = cfg.server_url" in config_html
    download = config_html.split("async function saveDownloadApiSettings()")[1].split("\n    }\n")[0]
    assert "saveKomgaConfig" in download


def test_download_save_leaves_an_unchanged_komga_alone(config_html):
    """Saving a PixelDrain key must not re-post Komga, reschedule its sync,
    or fail the tab on a Komga error."""
    fn = _komga_save(config_html)
    assert "if (!komgaFormChanged()) return null" in fn.split("postSettings(")[0]
    assert "komgaLoadedForm = JSON.stringify(readKomgaForm())" in config_html


def test_komga_test_saves_komga_first(config_html):
    """/api/komga/test reads the stored config, and Komga has no Save of its own."""
    fn = config_html.split("async function testKomgaConnection()")[1]
    before_test = fn.split("fetch('/api/komga/test'")[0]
    assert "if (komgaFormChanged())" in before_test
    assert "await saveKomgaConfig()" in before_test


def test_save_errors_do_not_parse_html_as_json(config_html):
    fn = config_html.split("async function postSettings(url, data)")[1].split("\n    }\n")[0]
    before_parse = fn.split("response.json()")[0]
    assert "response.status === 401" in before_parse
    assert "includes('application/json')" in before_parse


def test_tab_hash_scroll_reset_yields_to_the_reader(config_html):
    fn = config_html.split("function keepTopOfPage()")[1].split("\n        }\n")[0]
    assert fn.lstrip(" {\n").startswith("if (readerScrolled) return;")
    assert "window.addEventListener('load', keepTopOfPage)" in config_html


def test_sub_sections_inside_a_panel_are_h3(config_html):
    # Each panel opens with its one h2; anything after it is a sub-section.
    panels = config_html.split('class="clu-settings-panel')[1:]
    for panel in panels:
        assert panel.count("<h2") <= 1, panel[:200]


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
