"""
Version information for Comic Library Utilities (CLU)
"""

__version__ = "7.0"

# How CLU identifies itself to metadata providers that ask clients not to
# pretend to be a browser (Metron, MangaDex, MangaUpdates, Bedetheque,
# AniList). Providers fronted by a Cloudflare bot check that 403s non-browser
# agents (ComicVine, comics.org) use models.providers.base's
# DEFAULT_PROVIDER_USER_AGENT instead.
CLU_USER_AGENT = f"CLU/{__version__}"
