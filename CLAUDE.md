# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Comic Library Utilities (CLU) is a Flask-based web application for managing comic book collections. It provides bulk operations for CBZ/CBR files, metadata editing, file renaming, format conversion, and folder monitoring. Designed to run in Docker, it integrates with comic databases (GCD, ComicVine, Metron) for metadata enrichment.

## Development Commands

```bash
# Run locally (development)
python app.py

# Run with Docker
docker build -t comic-utils .
docker run -p 5577:5577 -v /path/to/comics:/data -v /path/to/downloads:/downloads comic-utils

# Verify Python syntax
python -m py_compile <filename.py>

# Production server (used in Docker)
gunicorn -w 1 --threads 8 -b 0.0.0.0:5577 --timeout 120 app:app
```

## Architecture

### Core Application Flow
- **`api.py`**: Creates the Flask app instance and handles download queue/remote downloads
- **`app.py`**: Main application - imports Flask app from `api.py`, registers blueprints, defines all routes and API endpoints
- **`monitor.py`**: Standalone file watcher for folder monitoring (runs when `MONITOR=yes`)

### Core Modules (`core/`)
| Module | Purpose |
|--------|---------|
| `core/config.py` | ConfigParser-based settings from `/config/config.ini` |
| `core/database.py` | SQLite database (`comic_utils.db`) for caching, file index, reading history |
| `core/comicinfo.py` | ComicInfo.xml parsing and generation |
| `core/app_logging.py` | Centralized logging — `app_logger` and `monitor_logger`, log files in `CONFIG_DIR/logs` |
| `core/app_state.py` | Global state — APScheduler instance, wanted-issues refresh state, data-dir stats cache |
| `core/file_watcher.py` | DebouncedFileHandler for `/data` monitoring — detects changes, queues metadata scanning |
| `core/metadata_scanner.py` | Background worker scanning ComicInfo.xml — priority queue, updates file_index with metadata |
| `core/memory_utils.py` | Memory monitoring — tracks usage, triggers cleanup at thresholds, `memory_context()` manager |
| `core/version.py` | Single `__version__` string |

### Other Root Modules
| Module | Purpose |
|--------|---------|
| `rename.py` | Comic file renaming with regex patterns for volume/issue extraction |
| `edit.py` | CBZ editing - image manipulation, file reordering, cropping |
| `convert.py` | CBR to CBZ conversion using `unar` |
| `wrapped.py` | Yearly reading stats image generation (Spotify Wrapped style) |
| `helpers.py` | Utility functions — `is_hidden()`, `safe_image_open()`, `create_thumbnail_streaming()`, ZIP/RAR extraction |
| `recommendations.py` | AI-powered recommendations via OpenAI/Anthropic APIs |

### Models
| Module | Purpose |
|--------|---------|
| `models/metron.py` | Metron API via Mokkari — search, metadata fetch, rate-limit retry, scrobble |
| `models/comicvine.py` | ComicVine API via Simyan — volume/issue search, metadata mapping |
| `models/gcd.py` | Grand Comics Database — MySQL queries, fuzzy title matching |
| `models/komga.py` | Komga media server REST client — reading history, in-progress books |
| `models/getcomics.py` | GetComics.org scraper — cloudscraper-based search and download |
| `models/mega.py` | MEGA download support — URL parsing, AES-256 decryption |
| `models/stats.py` | Library statistics — file counts, disk usage, read stats (cached) |
| `models/timeline.py` | Reading timeline — groups history by date, filters by year/month |
| `models/cbl.py` | CBL (Comic Book List) XML parser — matches entries to collection files |
| `models/issue.py` | Data classes — `IssueObj` and `SeriesObj` for unified data representation |
| `models/update_xml.py` | Batch ComicInfo.xml field updater across CBZ files |
| `models/providers/` | Unified provider system — `BaseProvider` ABC, registry, adapters for Metron/ComicVine/GCD/AniList/MangaDex/Bedetheque |

### CBZ Operations
| Module | Purpose |
|--------|---------|
| `cbz_ops/add.py` | Insert blank images into CBZ files |
| `cbz_ops/delete.py` | Delete CBZ files from filesystem |
| `cbz_ops/convert.py` | CBR→CBZ conversion using `unar` |
| `cbz_ops/single_file.py` | Single RAR→CBZ conversion with progress reporting |
| `cbz_ops/edit.py` | CBZ editing — crop, reorder, extract covers |
| `cbz_ops/crop.py` | Cover image cropping — left/center/right/freeform with blur |
| `cbz_ops/remove.py` | Remove specific images from CBZ files |
| `cbz_ops/enhance_single.py` | Single image enhancement — contrast, brightness, blur |
| `cbz_ops/enhance_dir.py` | Batch directory image enhancement |
| `cbz_ops/rebuild.py` | Rebuild CBZ structure — normalize filenames, reorder images |
| `cbz_ops/pdf.py` | PDF→CBZ conversion via pdf2image |
| `cbz_ops/rename.py` | Comic file renaming with regex pattern matching |

### Routes
| Module | Purpose |
|--------|---------|
| `routes/downloads.py` | GetComics search/download, auto-download schedules, weekly packs |
| `routes/files.py` | File ops — rename, delete, move, crop, combine CBZ, upload, cleanup |
| `routes/collection.py` | File browsing — directory listing, search, thumbnails, metadata browse |
| `routes/metadata.py` | ComicInfo.xml management — provider search, batch processing, field updates |
| `routes/series.py` | Releases/Wanted/Pull List — series sync, mapping, subscriptions |

### Test Organization
```
tests/
├── unit/          # Pure logic, no external deps
├── mocked/        # External APIs mocked
├── integration/   # Real SQLite database
├── routes/        # Flask route/endpoint tests
└── factories/     # Test data factories
```

### Blueprints
- `favorites_bp` (routes/favorites.py): Reading list/favorites functionality
- `opds_bp` (routes/opds.py): OPDS feed for comic readers
- `reading_lists_bp` (routes/reading_lists.py): Reading list management
- `downloads_bp` (routes/downloads.py): GetComics search and downloads
- `files_bp` (routes/files.py): File operations
- `collection_bp` (routes/collection.py): Collection browsing
- `metadata_bp` (routes/metadata.py): Metadata management
- `series_bp` (routes/series.py): Series and releases

### Data Flow
1. Comics stored in `/data` (mounted volume)
2. Downloads go to `/downloads/temp` then processed to `/downloads/processed`
3. SQLite database in `CACHE_DIR` (default `/cache`)
4. Config persisted in `/config/config.ini`

### Frontend
- Jinja2 templates in `templates/`
- Bootswatch themes (26 themes supported)
- Bootstrap 5 with custom CSS in `static/css/`

## Configuration

Settings in `core/config.py` define defaults merged with `/config/config.ini`. Key settings:
- `WATCH`/`TARGET`: Folder monitoring paths
- `AUTOCONVERT`: Auto CBR-to-CBZ conversion
- `BOOTSTRAP_THEME`: UI theme name
- API keys: `COMICVINE_API_KEY`, `PIXELDRAIN_API_KEY`, `METRON_USERNAME/PASSWORD`

## File Processing Pipeline

CBZ processing in `edit.py` (`process_cbz_file`):
1. Delete `_MACOSX` folders
2. Remove prefix characters (`.`, `_`, `._`) from filenames
3. Skip/delete files based on configured extensions
4. Normalize image filenames with zero-padded numbering

## GetComics Search Scoring System

The GetComics download detection uses a scoring system in `models/getcomics.py` (`score_getcomics_result`) to match search results against wanted issues.

### Scoring Components

| Component | Points | Description |
|-----------|--------|-------------|
| Series match | +30 | Series name matches |
| Issue match | +30 | Issue number found explicitly (e.g., `#1`) |
| Standalone issue | +20 | Issue number found without `#` prefix |
| Year match | +20 | Year matches exactly |
| Title tightness | +15/-10 | Bonus for title closely matching series |
| Different series | -30 | Remaining text indicates different series |
| Arc sub-series | -30 | Story arc sub-series (not variant) |
| Variant sub-series | -30 | Publication variant without acceptance |
| Issue mismatch | -40 | Explicit issue number found but wrong |
| Wrong year | -20 | Year present but doesn't match |
| Range ends on target | -100 | Range pack ending on target issue |

### Variant Keywords

Variants are publication types that can be optionally accepted via `SEARCH_VARIANTS` config:

```
annual, quarterly, tpB, oneshot, one-shot, o.s., os, OS,
trade paperback, trade-paperback, omni, omnibus, omb,
hardcover, deluxe, prestige, gallery, absolute
```

### Sub-series Detection

1. **Variants** (Annual, TPB, Quarterly, etc.): Publication variants, penalized unless the variant keyword is in `SEARCH_VARIANTS` config
2. **Arcs** (Batman - Court of Owls): Story arcs with dash notation, always penalized - arc issue numbering is different from main series
3. **Different Series** (Batman Inc, Flash Gordon): Series with remaining text that isn't variant or arc, penalized

### "The" Prefix Handling

The swap logic allows matching "The Flash" with "Flash" for series flexibility. However, if a search uses "The " prefix and the result doesn't (or vice versa), it's treated as a different series to prevent false matches.

### Decision Thresholds

- `ACCEPT`: Score >= 40, strong match
- `FALLBACK`: Score positive but < 40, range pack containing target issue
- `REJECT`: Score <= 0 or explicitly disqualified

### Edge Cases

- Range packs (e.g., `#1-5`) containing target issue are accepted as FALLBACK
- Different arcs (e.g., "Court of Owls" vs "Darkest Knight") do NOT match
- Series with "The " prefix are treated as different from same series without

## Docker Environment

- Base: `python:3.11-slim-bookworm`
- Uses `tini` as PID 1, `gosu` for user switching
- Playwright/Chromium for web scraping features
- `entrypoint.sh` handles PUID/PGID permissions

## Key Patterns

### Logging
Use `app_logger` from `core/app_logging.py` for application logs, `monitor_logger` for folder monitoring.

### Database Access
```python
from core.database import get_db_connection
conn = get_db_connection()
# Always use WAL mode - concurrent reads supported
```

### Image Processing
Use `helpers.py` functions: `safe_image_open()`, `create_thumbnail_streaming()` for memory-safe PIL operations.

## Project Rules

- Every new route in `routes/` must have a corresponding test in `tests/routes/`.
- Any modification to `cbz_ops/` or file operations must include a pytest fixture check.
- **Verification:** Before finishing any task, run `pytest` and ensure 100% pass rate.
- **Maintenance:** If a feature is updated, the corresponding test file MUST be updated in the same PR.
