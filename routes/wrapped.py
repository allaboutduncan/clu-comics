"""Wrapped image routes: the yearly and monthly reading recaps.

Moved out of app.py unchanged apart from the blueprint, so they can be tested.
The slides themselves are rendered by ``wrapped.py``.

Routes:
    GET /api/wrapped/years
    GET /api/wrapped/<year>
    GET /api/wrapped/<year>/image/<1-3>
    GET /api/wrapped/<year>/download
    GET /api/wrapped/monthly/<year>/<month>
    GET /api/wrapped/monthly/<year>/<month>/image/<1|2>
    GET /api/wrapped/monthly/<year>/<month>/download
"""
import io
import traceback
import zipfile
from datetime import datetime

from flask import Blueprint, Response, jsonify

from core.app_logging import app_logger
from core.settings import get_setting

wrapped_bp = Blueprint("wrapped", __name__)


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


@wrapped_bp.route("/api/wrapped/<int:year>/image/<int:slide_num>")
def api_wrapped_image(year, slide_num):
    """Return individual wrapped slide as PNG image."""
    from wrapped import (
        generate_summary_slide,
        generate_most_read_series_slide,
        generate_series_highlights_slide,
    )

    # Get current theme from config
    theme = get_setting("BOOTSTRAP_THEME")

    try:
        if slide_num == 1:
            image_bytes = generate_summary_slide(year, theme)
        elif slide_num == 2:
            image_bytes = generate_most_read_series_slide(year, theme)
        elif slide_num == 3:
            image_bytes = generate_series_highlights_slide(year, theme)
        else:
            return jsonify({"error": "Invalid slide number (1-3)"}), 400

        return Response(image_bytes, mimetype="image/png")
    except Exception as e:
        app_logger.error(f"Error generating wrapped image: {e}")
        app_logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@wrapped_bp.route("/api/wrapped/<int:year>/download")
def api_wrapped_download(year):
    """Download all wrapped images as ZIP."""
    from wrapped import generate_all_wrapped_images

    theme = get_setting("BOOTSTRAP_THEME")

    try:
        slides = generate_all_wrapped_images(year, theme)

        # Create ZIP in memory
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for filename, image_bytes in slides:
                zf.writestr(f"wrapped_{year}/{filename}", image_bytes)

        zip_buffer.seek(0)

        return Response(
            zip_buffer.getvalue(),
            mimetype="application/zip",
            headers={"Content-Disposition": f"attachment; filename=wrapped_{year}.zip"},
        )
    except Exception as e:
        app_logger.error(f"Error generating wrapped ZIP: {e}")
        return jsonify({"error": str(e)}), 500


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


@wrapped_bp.route("/api/wrapped/monthly/<int:year>/<int:month>/image/<int:slide_num>")
def api_monthly_wrapped_image(year, month, slide_num):
    """Return a monthly wrapped slide as PNG image."""
    from wrapped import generate_monthly_recap_slide, generate_monthly_all_issues_slide

    if month < 1 or month > 12:
        return jsonify({"error": "Invalid month (1-12)"}), 400
    if slide_num not in (1, 2):
        return jsonify({"error": "Invalid slide number (1-2)"}), 400

    theme = get_setting("BOOTSTRAP_THEME")

    try:
        if slide_num == 1:
            image_bytes = generate_monthly_recap_slide(year, month, theme)
        else:
            image_bytes = generate_monthly_all_issues_slide(year, month, theme)
        return Response(image_bytes, mimetype="image/png")
    except Exception as e:
        app_logger.error(f"Error generating monthly wrapped image: {e}")
        app_logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@wrapped_bp.route("/api/wrapped/monthly/<int:year>/<int:month>/download")
def api_monthly_wrapped_download(year, month):
    """Download all monthly wrapped images as ZIP."""
    from wrapped import generate_all_monthly_wrapped, MONTH_NAMES

    if month < 1 or month > 12:
        return jsonify({"error": "Invalid month (1-12)"}), 400

    theme = get_setting("BOOTSTRAP_THEME")

    try:
        slides = generate_all_monthly_wrapped(year, month, theme)
        month_str = str(month).zfill(2)

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for filename, image_bytes in slides:
                zf.writestr(
                    f"monthly_wrapped_{year}_{month_str}/{filename}", image_bytes
                )

        zip_buffer.seek(0)

        return Response(
            zip_buffer.getvalue(),
            mimetype="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename=monthly_wrapped_{year}_{month_str}.zip"
            },
        )
    except Exception as e:
        app_logger.error(f"Error generating monthly wrapped ZIP: {e}")
        return jsonify({"error": str(e)}), 500
