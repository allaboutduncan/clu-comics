"""
Route test fixtures.

Provides a Flask test client backed by a real SQLite database.
Blueprints are registered on a lightweight Flask app to avoid
api.py side effects (worker threads, cloudscraper, download dirs).
"""
import os
import sys
import types
import threading
import pytest
from unittest.mock import patch, MagicMock
from flask import Flask

# ---------------------------------------------------------------------------
# Ensure optional C-extension packages are importable (or faked) before
# blueprint imports.  Several production modules import these at the top
# level, but they are not installed in the test environment.
# ---------------------------------------------------------------------------

def _ensure_fake_module(name, attrs=None):
    """Insert a lightweight fake module if the real one is missing."""
    if name not in sys.modules:
        mod = types.ModuleType(name)
        if attrs:
            for k, v in attrs.items():
                setattr(mod, k, v)
        sys.modules[name] = mod
    return sys.modules[name]


# apscheduler -- used by app_state.py (imported by routes/series, downloads)
try:
    import apscheduler  # noqa: F401
except ImportError:
    _ensure_fake_module("apscheduler")
    _ensure_fake_module("apscheduler.schedulers")
    _ensure_fake_module("apscheduler.schedulers.background",
                        {"BackgroundScheduler": MagicMock})
    _ensure_fake_module("apscheduler.triggers")
    _ensure_fake_module("apscheduler.triggers.cron",
                        {"CronTrigger": MagicMock})
    _ensure_fake_module("apscheduler.triggers.date",
                        {"DateTrigger": MagicMock})

# cloudscraper -- used by models/getcomics.py and api.py
try:
    import cloudscraper  # noqa: F401
except ImportError:
    _cs = _ensure_fake_module("cloudscraper")
    _cs.create_scraper = MagicMock(return_value=MagicMock())

# mokkari -- used by models/metron.py (imported by routes/series)
try:
    import mokkari  # noqa: F401
except ImportError:
    _ensure_fake_module("mokkari")
    _ensure_fake_module("mokkari.session", {"Session": MagicMock})
    _ensure_fake_module("mokkari.exceptions",
                        {"ApiError": type("ApiError", (Exception,), {}),
                         "RateLimitError": type("RateLimitError", (Exception,), {})})
    _ensure_fake_module("mokkari.schemas")
    _ensure_fake_module("mokkari.schemas.collection",
                        {"ScrobbleRequest": MagicMock})


# ---------------------------------------------------------------------------
# Project root path (for templates / static)
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


# ---------------------------------------------------------------------------
# Helper: build a MagicMock that acts as the ``app`` module.
#
# Many route functions do ``from app import X`` inside the function body.
# Importing the real app.py triggers api.py which pulls in cloudscraper,
# starts worker threads, creates directories, etc.  Instead we inject a
# mock ``app`` module into sys.modules for the duration of each test.
# ---------------------------------------------------------------------------

def _make_mock_app_module(data_dir, target_dir):
    """Return a MagicMock configured with attributes routes need."""
    mock = MagicMock()
    # Attributes accessed by routes/collection.py, routes/files.py, etc.
    mock.DATA_DIR = data_dir
    mock.TARGET_DIR = target_dir
    mock.directory_cache = {}
    mock.cache_lock = threading.Lock()
    mock.cache_timestamps = {}
    mock.index_built = True
    # Functions that routes call
    mock.auto_fetch_metron_metadata = MagicMock(side_effect=lambda p: p)
    mock.auto_fetch_comicvine_metadata = MagicMock(side_effect=lambda p: p)
    mock.log_file_if_in_data = MagicMock()
    mock.update_index_on_move = MagicMock()
    mock.update_index_on_delete = MagicMock()
    mock.update_index_on_create = MagicMock()
    mock.clear_browse_cache = MagicMock()
    mock.find_folder_thumbnails_batch = MagicMock(return_value={})
    mock.configure_sync_schedule = MagicMock()
    mock.configure_getcomics_schedule = MagicMock()
    mock.configure_weekly_packs_schedule = MagicMock()
    mock.scheduled_getcomics_download = MagicMock()
    mock.scheduled_weekly_packs_download = MagicMock()
    mock.get_next_run_for_job = MagicMock(return_value=None)
    mock.refresh_wanted_cache_background = MagicMock()
    mock.generate_series_slug = MagicMock(return_value="test-slug")
    mock.process_incoming_wanted_issues = MagicMock()
    mock.invalidate_file_index = MagicMock()
    mock.invalidate_cache_for_path = MagicMock()
    mock.scan_filesystem_for_sync = MagicMock()
    mock.resize_upload = MagicMock()
    return mock


# ---------------------------------------------------------------------------
# Flask application fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def app(db_connection, tmp_path):
    """
    Create a minimal Flask test app with all blueprints registered.

    Avoids importing api.py / app.py directly (heavy side effects).
    Instead we create a fresh Flask app and register each blueprint.
    A mock ``app`` module is injected into sys.modules so that
    ``from app import X`` inside route functions picks up the mock.
    """
    test_app = Flask(
        __name__,
        template_folder=os.path.join(PROJECT_ROOT, "templates"),
        static_folder=os.path.join(PROJECT_ROOT, "static"),
        root_path=PROJECT_ROOT,
    )
    test_app.config["TESTING"] = True
    test_app.config["SECRET_KEY"] = "test-secret"

    # Paths that routes expect
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    target_dir = str(tmp_path / "processed")
    os.makedirs(target_dir, exist_ok=True)

    test_app.config["DATA_DIR"] = data_dir
    test_app.config["TARGET"] = target_dir
    test_app.config["CACHE_DIR"] = str(tmp_path / "cache")
    test_app.config["CUSTOM_RENAME_PATTERN"] = "{series_name} {issue_number}"
    test_app.config["METRON_USERNAME"] = ""
    test_app.config["METRON_PASSWORD"] = ""

    # Provide a signed int converter for publisher IDs (must be before blueprints)
    from werkzeug.routing import IntegerConverter

    class SignedIntConverter(IntegerConverter):
        regex = r'-?\d+'

    test_app.url_map.converters['signed'] = SignedIntConverter

    # Register blueprints --------------------------------------------------
    from favorites import favorites_bp
    from reading_lists import reading_lists_bp
    from opds import opds_bp
    from routes.collection import collection_bp
    from routes.files import files_bp
    from routes.downloads import downloads_bp
    from routes.series import series_bp
    from routes.metadata import metadata_bp

    test_app.register_blueprint(favorites_bp)
    test_app.register_blueprint(reading_lists_bp)
    test_app.register_blueprint(opds_bp)
    test_app.register_blueprint(collection_bp)
    test_app.register_blueprint(files_bp)
    test_app.register_blueprint(downloads_bp)
    test_app.register_blueprint(series_bp)
    test_app.register_blueprint(metadata_bp)

    # Stub routes that app.py defines but aren't in any blueprint.
    # Templates reference these via url_for().
    @test_app.route("/")
    def index():
        return "stub", 200

    @test_app.route("/scrape")
    def scrape_page():
        return "stub", 200

    @test_app.route("/status")
    def status():
        return "stub", 200

    @test_app.route("/config")
    def config_page():
        return "stub", 200

    @test_app.route("/insights")
    def insights_page():
        return "stub", 200

    @test_app.route("/logs")
    def logs_page():
        return "stub", 200

    @test_app.route("/timeline")
    def timeline():
        return "stub", 200

    @test_app.route("/api/thumbnail")
    def get_thumbnail():
        return "stub", 200

    @test_app.route("/api/download")
    def download_file():
        return "stub", 200

    @test_app.route("/api/operations")
    def active_operations():
        from flask import jsonify
        import app_state
        ops = app_state.get_active_operations()
        return jsonify({"operations": ops})

    # Inject mock ``app`` module so ``from app import X`` works in routes ---
    mock_app_module = _make_mock_app_module(data_dir, target_dir)
    old_app = sys.modules.get("app")
    sys.modules["app"] = mock_app_module

    # Inject mock ``api`` module so ``from api import download_queue, ...``
    # in route functions does not trigger the real api.py (which calls
    # os.makedirs on /downloads, starts worker threads, imports cloudscraper).
    mock_api_module = MagicMock()
    mock_api_module.download_queue = MagicMock()
    mock_api_module.download_progress = {}
    old_api = sys.modules.get("api")
    sys.modules["api"] = mock_api_module

    yield test_app

    # Restore
    if old_app is not None:
        sys.modules["app"] = old_app
    else:
        sys.modules.pop("app", None)
    if old_api is not None:
        sys.modules["api"] = old_api
    else:
        sys.modules.pop("api", None)


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture
def client_with_data(app, db_connection):
    """Flask test client backed by a populated database."""
    from tests.factories.db_factories import (
        create_publisher, create_series, create_issue,
        create_file_index_entry, create_directory_entry,
        create_issue_read, create_reading_list, create_reading_list_entry,
        create_user_preference, reset_counters,
    )

    reset_counters()

    # Seed data
    create_publisher(publisher_id=10, name="DC Comics", path="/data/DC Comics")
    create_publisher(publisher_id=20, name="Marvel", path="/data/Marvel")

    create_series(series_id=100, name="Batman", volume=2020,
                  publisher_id=10, mapped_path="/data/DC Comics/Batman")
    create_series(series_id=200, name="Amazing Spider-Man", volume=2018,
                  publisher_id=20, mapped_path="/data/Marvel/Amazing Spider-Man")

    create_directory_entry(name="DC Comics", path="/data/DC Comics", parent="/data")
    create_directory_entry(name="Batman", path="/data/DC Comics/Batman",
                           parent="/data/DC Comics")

    for i in range(1, 4):
        create_issue(issue_id=1000 + i, series_id=100, number=str(i),
                     cover_date=f"2020-{i:02d}-15", store_date=f"2020-{i:02d}-10")
        create_file_index_entry(
            name=f"Batman {i:03d} (2020).cbz",
            path=f"/data/DC Comics/Batman/Batman {i:03d} (2020).cbz",
            parent="/data/DC Comics/Batman",
            size=50_000_000,
        )

    create_issue_read(
        issue_path="/data/DC Comics/Batman/Batman 001 (2020).cbz",
        page_count=24, time_spent=600,
    )

    create_reading_list(name="Test List")
    create_user_preference(key="theme", value="darkly")

    return app.test_client()
