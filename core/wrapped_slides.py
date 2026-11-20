"""Wrapped slides: one registry, one manifest, one cache.

``wrapped.py`` draws the slides. This module decides **which** slides a recap
has, in what order, and serves each one as a cached PNG, so the story viewer
and the "Download All" ZIP are built from the same list and cannot disagree.
Before it, the yearly ZIP held four slides while the viewer showed three, each
route named its slides by position, and every request re-ran every query and
re-drew every image.

Things that look arbitrary and are not:

- **A slide is addressed by id, never by number.** A recap with nothing to say
  on a slide leaves it out (``has_data``), which shifts every later position.
- **The cache key is a fingerprint of what the slide is drawn from** -- the
  user's read rows for the period, the theme and style, and
  ``RENDERER_VERSION``. Nothing has to remember to invalidate it: marking a
  comic read, hiding one, renaming a folder or changing theme all change the
  key. Bump ``RENDERER_VERSION`` when a slide's drawing changes.
- **The user id comes from the session, never from the URL.** Slides show what
  someone read; the directory and the key are both per user.
- **Writes go through a temp file and ``os.replace``**, the thumbnail cache's
  rule: a half-written PNG must never be served, and ``replace`` needs
  permission on the directory, not on a file an earlier root-owned run left.
- **One render per key at a time.** Pillow is CPU-bound and the app runs one
  gunicorn worker; a viewer that asks for a slide twice (preload plus display)
  must not draw it twice. Relies on that single worker, like the thumbnail
  in-flight guard.
- Old files are pruned on write, in the user's own directory only.
"""
import hashlib
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable

from core import wrapped_styles
from core.app_logging import app_logger
from core.config import get_cache_dir
from core.database import current_user_id, db_conn
from core.user_time import DISPLAY_FORMAT, local_period_bounds_utc, sql_offset_modifier

# Bump when any slide's drawing changes, so cached PNGs are redrawn.
RENDERER_VERSION = 5

YEARLY = wrapped_styles.YEARLY
MONTHLY = wrapped_styles.MONTHLY

# The user_settings / user_preferences key for the chosen style. Blank means
# "each recap's own default" (core.wrapped_styles.DEFAULTS).
STYLE_SETTING = "wrapped_style"

CACHE_SUBDIR = "wrapped"
MAX_CACHE_AGE_SECONDS = 30 * 24 * 3600


@dataclass(frozen=True)
class SlideSpec:
    id: str
    title: str
    filename: str
    scope: str
    # Name of the generator, looked up at call time so tests can patch it and
    # so importing this module does not import Pillow.
    generator: str
    has_data: Callable[[dict], bool]
    # The module the generator lives in: wrapped.py for the original slides,
    # wrapped_story.py for the ones drawn from the period's story.
    module: str = "wrapped"


def _any_reads(stats):
    return (stats.get("total_read") or 0) > 0


def _story(check_name):
    """A ``has_data`` that asks wrapped_story whether the period's story has
    what a slide needs. The story (models.reading_insights.period_story) is
    what those slides are drawn from, so the check and the drawing cannot
    disagree."""
    def check(stats):
        import wrapped_story

        story = stats.get("story")
        return bool(story) and getattr(wrapped_story, check_name)(story)
    return check


# In the order a recap is told: the headline, how the year went, what and who
# was read, when, what kind of reader that makes you, and everything at the end.
SLIDES = (
    SlideSpec("summary", "Your Year", "01_summary.png", YEARLY,
              "generate_summary_slide", _any_reads),
    SlideSpec("goal", "Your Reading Goal", "02_goal.png", YEARLY,
              "generate_goal_slide", _story("has_goal"), "wrapped_story"),
    SlideSpec("streaks", "Your Streaks", "03_streaks.png", YEARLY,
              "generate_streaks_slide", _story("has_personality"), "wrapped_story"),
    SlideSpec("time-pages", "Time Well Spent", "04_time_and_pages.png", YEARLY,
              "generate_time_pages_slide", _story("has_time"), "wrapped_story"),
    SlideSpec("most-read-series", "Most Read Series", "05_most_read_series.png", YEARLY,
              "generate_most_read_series_slide", _any_reads),
    SlideSpec("series-highlights", "Top Series", "06_series_highlights.png", YEARLY,
              "generate_series_highlights_slide", _any_reads),
    SlideSpec("creators", "Your Creators", "07_creators.png", YEARLY,
              "generate_creators_slide", _story("has_creators"), "wrapped_story"),
    SlideSpec("characters", "Your Universe", "08_characters.png", YEARLY,
              "generate_characters_slide", _story("has_characters"), "wrapped_story"),
    SlideSpec("reading-clock", "Your Reading Clock", "09_reading_clock.png", YEARLY,
              "generate_clock_slide", _story("has_clock"), "wrapped_story"),
    SlideSpec("personality", "Your Reading Personality", "10_personality.png", YEARLY,
              "generate_personality_slide", _story("has_personality"), "wrapped_story"),
    SlideSpec("books-grid", "Everything You Read", "11_books_grid.png", YEARLY,
              "generate_books_grid_slide", _any_reads),
    SlideSpec("recap", "Monthly Recap", "01_monthly_recap.png", MONTHLY,
              "generate_monthly_recap_slide", _any_reads),
    SlideSpec("numbers", "The Month in Numbers", "02_month_in_numbers.png", MONTHLY,
              "generate_monthly_numbers_slide", _story("has_personality"), "wrapped_story"),
    SlideSpec("creators", "Who You Read", "03_who_you_read.png", MONTHLY,
              "generate_monthly_creators_slide", _story("has_creators"), "wrapped_story"),
    SlideSpec("reading-clock", "Your Reading Clock", "04_reading_clock.png", MONTHLY,
              "generate_monthly_clock_slide", _story("has_clock"), "wrapped_story"),
    SlideSpec("all-issues", "Everything You Read", "05_all_issues.png", MONTHLY,
              "generate_monthly_all_issues_slide", _any_reads),
)

# The numbered routes that predate slide ids (/image/<n>), kept for one release.
LEGACY_NUMBERS = {
    YEARLY: {1: "summary", 2: "most-read-series", 3: "series-highlights"},
    MONTHLY: {1: "recap", 2: "all-issues"},
}


def slides_for(scope):
    return [spec for spec in SLIDES if spec.scope == scope]


def get_slide(scope, slide_id):
    for spec in SLIDES:
        if spec.scope == scope and spec.id == slide_id:
            return spec
    return None


def user_wrapped_style(scope):
    """The style this user's recaps of *scope* are drawn in.

    Their own choice if they made one and it suits the scope, else the site's,
    else the scope's default -- so Monthly Wrapped keeps its comic-panel look
    until someone picks another.
    """
    from core.database import get_user_setting

    try:
        stored = get_user_setting(STYLE_SETTING, default="")
    except Exception:
        stored = ""
    return wrapped_styles.resolve_style(stored, scope)


def clean_style(style, scope):
    """The style to draw *scope* in: *style* when it is given and valid for
    the scope, else the user's saved one."""
    if style and wrapped_styles.get_style(style) is not None \
            and scope in wrapped_styles.get_style(style).scopes:
        return style
    return user_wrapped_style(scope)


def style_choices(scope):
    """``[{id, label}]`` for the viewer's picker."""
    return [{"id": s.id, "label": s.label} for s in wrapped_styles.styles_for(scope)]


def user_wrapped_theme():
    """The theme a recap is drawn in: the user's own, else the site's.

    The routes used to read the site setting only, so someone who had picked
    their own theme got slides in the owner's.
    """
    from core.database import get_user_setting
    from core.themes import is_valid_theme

    try:
        theme = get_user_setting("bootstrap_theme", default="default")
    except Exception:
        theme = None
    return theme if theme and is_valid_theme(theme) else "default"


def period_stats(scope, year, month=None):
    """The stats a scope's ``has_data`` checks read."""
    import wrapped

    from models.reading_insights import period_story

    if scope == MONTHLY:
        stats = dict(wrapped.get_monthly_wrapped_stats(year, month))
    else:
        stats = dict(wrapped.get_all_wrapped_stats(year))
    stats["story"] = period_story(scope, year, month)
    return stats


def period_fingerprint(year, month=None, user_id=None):
    """A digest of the reads a recap for this period is drawn from.

    Covers what changes a slide: a read added or removed, hidden or shown,
    re-dated, moved to another path (covers are looked up by path), or
    re-tagged. Returns None when the database cannot be read, which the caller
    treats as "do not cache" -- a slide must never be filed under a key that
    does not describe it.
    """
    uid = current_user_id() if user_id is None else user_id
    start, end = local_period_bounds_utc(year, month)
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"{uid}|{year}|{month}|{sql_offset_modifier()}".encode())
    try:
        with db_conn() as conn:
            if not conn:
                return None
            rows = conn.execute(
                "SELECT issue_path, read_at, hide, page_count, time_spent, publisher,"
                " writer, penciller, characters"
                " FROM issues_read"
                " WHERE user_id = ? AND datetime(read_at) >= ? AND datetime(read_at) < ?"
                " ORDER BY issue_path",
                (uid, start.strftime(DISPLAY_FORMAT), end.strftime(DISPLAY_FORMAT)),
            ).fetchall()
    except Exception as e:
        app_logger.debug(f"Wrapped fingerprint unavailable: {e}")
        return None
    for row in rows:
        digest.update(repr(tuple(row)).encode("utf-8", "replace"))
    return digest.hexdigest()


def slide_version(spec, year, month, theme, style, fingerprint):
    """The cache key for one slide, or None when it cannot be cached."""
    if fingerprint is None:
        return None
    text = "|".join(str(part) for part in (
        RENDERER_VERSION, spec.scope, spec.id, year, month, theme, style, fingerprint))
    return hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()


def build_manifest(scope, year, month=None, theme=None, style=None, user_id=None):
    """The slides this recap has, in order.

    Each item: ``{id, title, filename, version}``. ``version`` is the cache key
    (None if the slide cannot be cached); routes turn it into a ``?v=`` so a
    browser may keep the image for as long as it stays the same.
    """
    theme = theme or user_wrapped_theme()
    style = clean_style(style, scope)
    stats = period_stats(scope, year, month)
    fingerprint = period_fingerprint(year, month, user_id)
    return [
        {
            "id": spec.id,
            "title": spec.title,
            "filename": spec.filename,
            "version": slide_version(spec, year, month, theme, style, fingerprint),
        }
        for spec in slides_for(scope)
        if spec.has_data(stats)
    ]


# --- cache -------------------------------------------------------------------

_inflight_guard = threading.Lock()
_inflight = {}


def _cache_dir(user_id):
    return os.path.join(get_cache_dir(), CACHE_SUBDIR, str(int(user_id)))


def _cache_path(user_id, version):
    return os.path.join(_cache_dir(user_id), f"{version}.png")


def _read_cached(path):
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    # Count a hit as use, so pruning is by last use rather than by creation.
    try:
        os.utime(path, None)
    except OSError:
        pass
    return data or None


def _prune(directory, now=None):
    """Drop slides nobody has asked for in ``MAX_CACHE_AGE_SECONDS``."""
    cutoff = (time.time() if now is None else now) - MAX_CACHE_AGE_SECONDS
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return 0
    removed = 0
    for entry in entries:
        try:
            if entry.is_file() and entry.name.endswith((".png", ".tmp")) \
                    and entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
                removed += 1
        except OSError:
            continue
    return removed


def _write_cached(user_id, version, data):
    directory = _cache_dir(user_id)
    try:
        os.makedirs(directory, exist_ok=True)
        temp = os.path.join(directory, f".{version}.{os.getpid()}.{threading.get_ident()}.tmp")
        with open(temp, "wb") as fh:
            fh.write(data)
        os.replace(temp, _cache_path(user_id, version))
        _prune(directory)
    except OSError as e:
        # An unwritable cache costs a re-render next time, nothing else.
        app_logger.debug(f"Wrapped slide not cached: {e}")


def _draw(spec, year, month, theme, style):
    import importlib

    import wrapped

    generator = getattr(importlib.import_module(spec.module), spec.generator)
    with wrapped.use_style(style):
        if spec.scope == MONTHLY:
            return generator(year, month, theme)
        return generator(year, theme)


def render_slide(scope, slide_id, year, month=None, theme=None, style=None, user_id=None):
    """PNG bytes for one slide, from the cache when it is current.

    Returns None when the slide does not exist or has nothing to draw.
    """
    spec = get_slide(scope, slide_id)
    if spec is None:
        return None
    uid = current_user_id() if user_id is None else user_id
    theme = theme or user_wrapped_theme()
    style = clean_style(style, scope)
    version = slide_version(spec, year, month, theme, style,
                            period_fingerprint(year, month, uid))
    if version is None:
        return _draw(spec, year, month, theme, style)

    path = _cache_path(uid, version)
    cached = _read_cached(path)
    if cached:
        return cached

    with _inflight_guard:
        lock = _inflight.setdefault(version, threading.Lock())
    try:
        with lock:
            # Whoever held the lock may have just written it.
            cached = _read_cached(path)
            if cached:
                return cached
            data = _draw(spec, year, month, theme, style)
            if data:
                _write_cached(uid, version, data)
            return data
    finally:
        with _inflight_guard:
            _inflight.pop(version, None)


def render_all(scope, year, month=None, theme=None, style=None, user_id=None):
    """``[(filename, bytes)]`` for every slide in the manifest, for the ZIP."""
    theme = theme or user_wrapped_theme()
    style = clean_style(style, scope)
    slides = []
    for item in build_manifest(scope, year, month, theme, style, user_id):
        data = render_slide(scope, item["id"], year, month, theme, style, user_id)
        if data:
            slides.append((item["filename"], data))
    return slides
