"""
Root test configuration and fixtures.

Sets up environment variables BEFORE any app modules are imported,
to prevent import-time side effects (log file creation, config loading, etc.)
from writing to production paths.
"""
import os
import sys
import tempfile
import logging

# ---------------------------------------------------------------------------
# Environment setup (runs at import time, before any test module loads)
# ---------------------------------------------------------------------------
# Create a temp dir for config/logs that persists for the test session
_TEST_CONFIG_DIR = tempfile.mkdtemp(prefix="clu_test_config_")
os.environ["CONFIG_DIR"] = _TEST_CONFIG_DIR

# Ensure the project root is on sys.path so imports work
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Now we can safely import pytest (fixtures below)
# ---------------------------------------------------------------------------
import pytest
import zipfile
import io
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Fixture: Temporary directories
# ---------------------------------------------------------------------------
@pytest.fixture
def tmp_data_dir(tmp_path):
    """Simulated /data mount with sample directory structure."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture
def tmp_downloads_dir(tmp_path):
    """Simulated /downloads mount with temp/ and processed/ subdirs."""
    dl_dir = tmp_path / "downloads"
    (dl_dir / "temp").mkdir(parents=True)
    (dl_dir / "processed").mkdir(parents=True)
    return dl_dir


@pytest.fixture
def tmp_config_dir(tmp_path):
    """Temporary config directory for a single test."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return config_dir


# ---------------------------------------------------------------------------
# Fixture: Database
# ---------------------------------------------------------------------------
@pytest.fixture
def db_path(tmp_path):
    """Path to a temporary SQLite database file."""
    return str(tmp_path / "test_comic_utils.db")


@pytest.fixture
def db_connection(db_path):
    """
    Create a fresh SQLite database with the full CLU schema.
    Patches get_db_path() so all database.py functions use this test DB.
    """
    with patch("database.get_db_path", return_value=db_path):
        from database import init_db, get_db_connection

        init_db()
        conn = get_db_connection()
        yield conn
        conn.close()


# ---------------------------------------------------------------------------
# Fixture: Sample CBZ creation
# ---------------------------------------------------------------------------
@pytest.fixture
def create_cbz(tmp_path):
    """
    Factory fixture to create minimal CBZ files for testing.

    Usage:
        path = create_cbz("test.cbz", num_images=3, comicinfo_xml="<ComicInfo>...</ComicInfo>")
    """
    def _create_cbz(filename="test.cbz", num_images=3, comicinfo_xml=None):
        from PIL import Image

        cbz_path = tmp_path / filename
        with zipfile.ZipFile(str(cbz_path), "w") as zf:
            for i in range(num_images):
                img = Image.new("RGB", (100, 150), color=(i * 50, 100, 200))
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                zf.writestr(f"page_{i:03d}.png", buf.getvalue())
            if comicinfo_xml:
                zf.writestr("ComicInfo.xml", comicinfo_xml)
        return str(cbz_path)

    return _create_cbz


# ---------------------------------------------------------------------------
# Fixture: Logging suppression (autouse)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _suppress_app_logging():
    """Redirect app_logger to a NullHandler to avoid file I/O in tests."""
    try:
        from app_logging import app_logger

        # Store original handlers and replace with NullHandler
        original_handlers = app_logger.handlers[:]
        app_logger.handlers = [logging.NullHandler()]
        yield
        app_logger.handlers = original_handlers
    except ImportError:
        yield
