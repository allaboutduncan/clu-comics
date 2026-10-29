"""The one set of page sizes, shared by every paged list in CLU.

Every "Per page" select offers exactly PAGE_SIZES (partials/filters.html
``per_page_select`` renders them), and every server endpoint snaps what it is
sent onto the same set. The sizes fill whole rows of the cover grid: each one
divides by 2, 3, 4 and 6 columns, and the three larger ones by 5 as well.

The lists used to offer three sets (20/50/100/250 sending 21/49/98/252 on
Collection, 24/50/100/250 on Library and Pull List, 20/40/80/160/300 on
Reading Lists). A size saved under an old set (cookie or localStorage) is
snapped to the nearest size here instead of being thrown away, so nobody's
choice resets to the default. CLU.snapPerPage in static/js/clu-pager.js
does the same for sizes only the browser remembers.
"""

PAGE_SIZES = (24, 60, 120, 240)

# Collection opens a folder's covers and generates thumbnails for what it shows,
# so it starts on the small page; every other list starts on 60.
DEFAULT_PER_PAGE = 60
COLLECTION_DEFAULT_PER_PAGE = 24


def snap_per_page(value, default=DEFAULT_PER_PAGE):
    """``value`` as one of PAGE_SIZES: the nearest one (ties go to the smaller),
    or ``default`` when it is missing or not a positive number."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return min(PAGE_SIZES, key=lambda size: (abs(size - n), size))


def per_page_from(*candidates, default=DEFAULT_PER_PAGE):
    """The first usable candidate (URL value, then cookie...), snapped.

    A junk or non-positive candidate falls through to the next one, as
    per_page_arg does, so ``?per_page=abc`` still shows the size the user
    last picked rather than the default.
    """
    for value in candidates:
        snapped = snap_per_page(value, None)
        if snapped is not None:
            return snapped
    return default


# Exact-match variant from #681, kept for callers that offer their own size
# set and want an unoffered size ignored rather than snapped.
def _offered_size(value, sizes):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n in sizes else None


def per_page_arg(args, cookies, cookie_name, sizes, default):
    """The page size to render: ``?per_page=`` if the page offers it, else the
    size remembered in ``cookie_name``, else ``default``.

    A junk or unoffered ``?per_page`` falls through to the cookie rather than
    straight to the default, so ``?per_page=abc`` still shows the size the
    user last picked.
    """
    return (_offered_size(args.get("per_page"), sizes)
            or _offered_size(cookies.get(cookie_name), sizes)
            or default)
