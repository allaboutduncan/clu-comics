"""Page-size choice for the server-paged lists (/pull-list, /reading-lists)."""


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
