"""Wrapped routes: the yearly and monthly reading recaps.

The slides are drawn by ``wrapped.py``; which slides a recap has, and the PNG
cache they are served from, live in ``core/wrapped_slides.py``. The viewer and
the ZIP both start from the manifest, so they always hold the same slides.

Routes (``<period>`` is ``<year>`` or ``monthly/<year>/<month>``):
    GET /api/wrapped/years
    GET /api/wrapped/<period>                    -- stats as JSON
    GET /api/wrapped/<period>/manifest           -- the slides, in order
    GET /api/wrapped/<period>/slide/<id>.png     -- one slide
    GET /api/wrapped/<period>/download           -- every slide, as a ZIP
    GET /api/wrapped/<period>/image/<n>          -- legacy: slides by number

Every slide shows what the signed-in user read. Nothing here takes a user id.
"""
import io
import traceback
import zipfile
from datetime import datetime

from flask import Blueprint, Response, jsonify, request, url_for

from core import wrapped_slides
from core.app_logging import app_logger
from core.wrapped_slides import MONTHLY, YEARLY

wrapped_bp = Blueprint("wrapped", __name__)

# A year the calendar could plausibly hold; anything else is a typo or a probe
# and should not reach the renderer.
_MIN_YEAR, _MAX_YEAR = 1900, 2200


def _bad_period(year, month=None):
    """A 400 response when the period is out of range, else None."""
    if not _MIN_YEAR <= year <= _MAX_YEAR:
        return jsonify({"error": "Invalid year"}), 400
    if month is not None and not 1 <= month <= 12:
        return jsonify({"error": "Invalid month (1-12)"}), 400
    return None


def _slide_endpoint(scope):
    return "wrapped.api_monthly_wrapped_slide" if scope == MONTHLY else "wrapped.api_wrapped_slide"


def _manifest_response(scope, year, month=None):
    style = wrapped_slides.clean_style(request.args.get("style"))
    theme = wrapped_slides.user_wrapped_theme()
    try:
        slides = wrapped_slides.build_manifest(scope, year, month, theme, style)
    except Exception as e:
        app_logger.error(f"Error building wrapped manifest: {e}")
        return jsonify({"error": str(e)}), 500

    period = {"year": year, "month": month} if scope == MONTHLY else {"year": year}
    for slide in slides:
        query = {"style": style} if style != wrapped_slides.DEFAULT_STYLE else {}
        if slide["version"]:
            query["v"] = slide["version"]
        slide["url"] = url_for(_slide_endpoint(scope), slide_id=slide["id"], **period, **query)
    return jsonify({
        "scope": scope, "year": year, "month": month,
        "style": style, "theme": theme, "slides": slides,
    })


def _slide_response(scope, slide_id, year, month=None):
    spec = wrapped_slides.get_slide(scope, slide_id)
    if spec is None:
        return jsonify({"error": "Unknown slide"}), 404

    style = wrapped_slides.clean_style(request.args.get("style"))
    theme = wrapped_slides.user_wrapped_theme()
    try:
        image_bytes = wrapped_slides.render_slide(scope, slide_id, year, month, theme, style)
    except Exception as e:
        app_logger.error(f"Error generating wrapped image: {e}")
        app_logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500
    if not image_bytes:
        return jsonify({"error": "Nothing to show for this slide"}), 404

    response = Response(image_bytes, mimetype="image/png")
    # The URL in the manifest carries the slide's content key. While that key
    # is still the slide's, the bytes behind it cannot change, so the browser
    # may keep them; any other request must come back and ask. Private either
    # way: a slide is one user's reading history.
    spec_version = wrapped_slides.slide_version(
        spec, year, month, theme, style,
        wrapped_slides.period_fingerprint(year, month),
    )
    asked = request.args.get("v")
    if asked and spec_version and asked == spec_version:
        response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    else:
        response.headers["Cache-Control"] = "private, no-cache"
    return response


def _zip_response(scope, year, month=None):
    style = wrapped_slides.clean_style(request.args.get("style"))
    if scope == MONTHLY:
        folder = f"monthly_wrapped_{year}_{str(month).zfill(2)}"
    else:
        folder = f"wrapped_{year}"
    try:
        slides = wrapped_slides.render_all(scope, year, month, style=style)
        if not slides:
            return jsonify({"error": "Nothing was read in this period"}), 404

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for filename, image_bytes in slides:
                zf.writestr(f"{folder}/{filename}", image_bytes)
        return Response(
            zip_buffer.getvalue(),
            mimetype="application/zip",
            headers={"Content-Disposition": f"attachment; filename={folder}.zip"},
        )
    except Exception as e:
        app_logger.error(f"Error generating wrapped ZIP: {e}")
        return jsonify({"error": str(e)}), 500


def _legacy_image(scope, slide_num, year, month=None):
    """``/image/<n>``: slides by position, as the pages addressed them before
    slide ids. Kept for one release so a cached page keeps working."""
    numbers = wrapped_slides.LEGACY_NUMBERS[scope]
    slide_id = numbers.get(slide_num)
    if slide_id is None:
        return jsonify({"error": f"Invalid slide number (1-{len(numbers)})"}), 400
    return _slide_response(scope, slide_id, year, month)


#########################
#   Yearly Wrapped      #
#########################


@wrapped_bp.route("/api/wrapped/years")
def api_wrapped_years():
    """Get list of years with reading data. Defaults to current year if none found."""
    from wrapped import get_years_with_reading_data

    try:
        years = get_years_with_reading_data()
    except Exception as e:
        app_logger.error(f"Error getting wrapped years: {e}")
        years = []

    # Default to current year if no data found
    if not years:
        years = [datetime.now().year]

    return jsonify({"years": years})


@wrapped_bp.route("/api/wrapped/<int:year>")
def api_wrapped_data(year):
    """Return wrapped stats as JSON."""
    from wrapped import get_all_wrapped_stats

    try:
        stats = get_all_wrapped_stats(year)
        return jsonify(stats)
    except Exception as e:
        app_logger.error(f"Error getting wrapped stats: {e}")
        return jsonify({"error": str(e)}), 500


@wrapped_bp.route("/api/wrapped/<int:year>/manifest")
def api_wrapped_manifest(year):
    """The slides this year's recap has, in order, each with its URL."""
    return _bad_period(year) or _manifest_response(YEARLY, year)


@wrapped_bp.route("/api/wrapped/<int:year>/slide/<slide_id>.png")
def api_wrapped_slide(year, slide_id):
    """One yearly slide as a PNG."""
    return _bad_period(year) or _slide_response(YEARLY, slide_id, year)


@wrapped_bp.route("/api/wrapped/<int:year>/image/<int:slide_num>")
def api_wrapped_image(year, slide_num):
    """Legacy: a yearly slide by number (1-3)."""
    return _bad_period(year) or _legacy_image(YEARLY, slide_num, year)


@wrapped_bp.route("/api/wrapped/<int:year>/download")
def api_wrapped_download(year):
    """Download all wrapped images as ZIP."""
    return _bad_period(year) or _zip_response(YEARLY, year)


##########################
#   Monthly Wrapped      #
##########################


@wrapped_bp.route("/api/wrapped/monthly/<int:year>/<int:month>")
def api_monthly_wrapped_data(year, month):
    """Return monthly wrapped stats as JSON."""
    from wrapped import get_monthly_wrapped_stats

    if month < 1 or month > 12:
        return jsonify({"error": "Invalid month (1-12)"}), 400
    try:
        stats = get_monthly_wrapped_stats(year, month)
        return jsonify(stats)
    except Exception as e:
        app_logger.error(f"Error getting monthly wrapped stats: {e}")
        return jsonify({"error": str(e)}), 500


@wrapped_bp.route("/api/wrapped/monthly/<int:year>/<int:month>/manifest")
def api_monthly_wrapped_manifest(year, month):
    """The slides this month's recap has, in order, each with its URL."""
    return _bad_period(year, month) or _manifest_response(MONTHLY, year, month)


@wrapped_bp.route("/api/wrapped/monthly/<int:year>/<int:month>/slide/<slide_id>.png")
def api_monthly_wrapped_slide(year, month, slide_id):
    """One monthly slide as a PNG."""
    return _bad_period(year, month) or _slide_response(MONTHLY, slide_id, year, month)


@wrapped_bp.route("/api/wrapped/monthly/<int:year>/<int:month>/image/<int:slide_num>")
def api_monthly_wrapped_image(year, month, slide_num):
    """Legacy: a monthly slide by number (1-2)."""
    return _bad_period(year, month) or _legacy_image(MONTHLY, slide_num, year, month)


@wrapped_bp.route("/api/wrapped/monthly/<int:year>/<int:month>/download")
def api_monthly_wrapped_download(year, month):
    """Download all monthly wrapped images as ZIP."""
    return _bad_period(year, month) or _zip_response(MONTHLY, year, month)
