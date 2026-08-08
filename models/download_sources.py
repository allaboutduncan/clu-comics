"""
Download source priority and shared search helpers.

CLU can acquire an issue from three sources — GetComics (CLU downloads over
HTTP itself), Usenet (submit an NZB to SABnzbd/NZBGet) and DC++ (queue a hub
result in AirDC++). The user orders them on the Download Clients settings tab;
that order is stored as a JSON list under the ``download_source_priority``
preference and decides which source the scheduled auto-download tries first
and how the manual search modal stacks its result sections.

This module owns the ordering so nothing has to reason about it pairwise.
``models/usenet.py`` re-exports :func:`get_source_priority` for backwards
compatibility with its existing callers.
"""

# Every source CLU knows about, in the order used when the user has no saved
# preference. GetComics first preserves the pre-Usenet/pre-DC++ behaviour.
KNOWN_SOURCES = ("getcomics", "usenet", "dcpp")

_DEFAULT_PRIORITY = ["getcomics"]


def get_source_priority() -> list:
    """Return the ordered download-source list (default GetComics only).

    Stored as a JSON list under the ``download_source_priority`` preference.
    An unset or unparseable preference falls back to GetComics alone, which
    keeps a fresh install behaving as it always has.
    """
    import json

    try:
        from core.database import get_user_preference

        raw = get_user_preference("download_source_priority", default=None)
        if not raw:
            return list(_DEFAULT_PRIORITY)
        value = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(value, list) and value:
            return [str(s) for s in value]
    except Exception:
        pass
    return list(_DEFAULT_PRIORITY)


def source_rank(name: str) -> int:
    """Return the priority index of a source; unlisted sources sort last."""
    order = get_source_priority()
    return order.index(name) if name in order else 999


def source_enabled(name: str) -> bool:
    """True if the user has this source in their priority list."""
    return name in get_source_priority()


def ordered_sources(names=None) -> list:
    """Return ``names`` (default: all known sources) in priority order.

    Sources the user has not enabled are dropped. Ties — which only happen
    among unlisted sources — keep :data:`KNOWN_SOURCES` order for stability.
    """
    candidates = list(names) if names is not None else list(KNOWN_SOURCES)
    order = get_source_priority()
    return sorted((n for n in candidates if n in order), key=order.index)


def get_external_sources() -> list:
    """Return the enabled, configured non-GetComics sources in priority order.

    Each entry is ``(name, label, try_download_for_issue)``. Every function
    shares the signature and return contract of
    ``models.usenet.try_download_for_issue``, so the auto-download loop can
    call them interchangeably.

    GetComics is excluded because it is not a pluggable client — it lives
    inline in the download loop — so it is handled by
    :func:`split_around_getcomics` instead.
    """
    from models.dcpp import dcpp_enabled_and_configured
    from models.dcpp import try_download_for_issue as dcpp_try
    from models.usenet import try_download_for_issue as usenet_try
    from models.usenet import usenet_enabled_and_configured

    candidates = {
        "usenet": ("Usenet", usenet_enabled_and_configured, usenet_try),
        "dcpp": ("DC++", dcpp_enabled_and_configured, dcpp_try),
    }

    out = []
    for name in ordered_sources(candidates.keys()):
        label, is_on, try_fn = candidates[name]
        try:
            if is_on():
                out.append((name, label, try_fn))
        except Exception:
            continue  # a misconfigured source must not break the whole run
    return out


def split_around_getcomics(sources=None):
    """Split external sources into those tried before vs after GetComics.

    Sources the user ranked above GetComics are attempted first and skip
    GetComics on a hit; the rest only run when GetComics queued nothing.
    """
    sources = get_external_sources() if sources is None else sources
    gc_rank = source_rank("getcomics")
    # Sort here rather than trusting the caller: each half must be attempted in
    # priority order, and an explicitly-passed list may be in any order.
    ranked = sorted(sources, key=lambda s: source_rank(s[0]))
    pre = [s for s in ranked if source_rank(s[0]) < gc_rank]
    post = [s for s in ranked if source_rank(s[0]) > gc_rank]
    return pre, post


def build_queries(series_name, issue_num) -> list:
    """Build search query variants for a series/issue.

    Releases commonly zero-pad the issue number (e.g. "004", "#4"), so we try
    the raw number plus 2- and 3-digit padded forms. Shared by the Usenet and
    DC++ searches; order matters only for logging, since callers de-duplicate.
    """
    s = str(issue_num or "").strip()
    variants = []
    if s:
        variants.append(s)
        if s.isdigit():
            n = int(s)
            for width in (2, 3):
                p = str(n).zfill(width)
                if p not in variants:
                    variants.append(p)
    else:
        variants.append("")
    base = series_name.strip()
    return [f"{base} {v}".strip() for v in variants]
