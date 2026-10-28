# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

**Primary: the library maintainer.** A self-hoster (often Unraid or a homelab Docker host) responsible for a large digital comic library, from thousands up to 70,000+ issues. They come to CLU to do jobs they would otherwise script: convert, rename and repair files, write ComicInfo.xml metadata, triage problem files, track releases, and automate downloads into the library. When a design decision trades the maintainer against another audience, the maintainer wins.

**Secondary: readers.** Household or friends with Reader accounts (multi-user mode) who browse the collection, read in the built-in reader, and keep reading lists, Want to Read and On the Stack. Many installs are single-user, where maintainer and reader are the same person.

Roles in the product: **Reader**, **Clerk** (can change things) and **Store Owner** (admin). Single-user installs run in implicit-owner mode with no login.

## Product Purpose

CLU is a self-hosted web app for organizing, repairing and reading a digital comic collection. It points at the folders comics already live in and does bulk maintenance through the browser, so a remote collection can be maintained without shell access. Success is a library that is consistently named, correctly tagged, complete where the owner wants it complete, and kept that way with little ongoing effort.

## Positioning

CLU is the maintenance and metadata layer underneath a comic server, not a replacement for one. It runs against the same folders as Komga or Kavita, fixes the files and ComicInfo.xml they read, and can also stand alone with its own browser and reader. Its differentiator is bulk, library-wide operations with review and revert (tagging, renaming, conversion, health scanning), which neither comic servers nor single-file taggers like ComicTagger do.

## Operating Context

- Runs in Docker behind a reverse proxy, possibly on a subpath (`ProxyFix(x_prefix=1)`); libraries are mounted volumes, downloads flow through WATCH → TARGET folders into a library.
- Used on **desktop browsers** (bulk and admin work), **tablets** (browsing and reading) and **phones** (quick checks: pull list, wanted, downloads, status). All three are confirmed use contexts.
- Long-running background work (folder monitor, scheduled jobs, health scans, download queues) is surfaced through status pages, an Active Operations indicator, toasts and notifications.
- External integrations: Metron, ComicVine, GCD, INDUCKS, AniList, MangaDex, MangaUpdates, Bedetheque, Komga, SABnzbd/NZBGet, Newznab, AirDC++, OPDS clients, a browser extension (`extension/`), and a token API (`/api/v1`) for companion apps.

## Capabilities and Constraints

- Capabilities are catalogued in `FEATURES.md`: library browsing, built-in reader, reading lists, directory and CBZ operations, metadata and Source Wall, pull list / releases / Wanted, downloads, folder monitoring, Insights / Timeline / CLU Wrapped, AI recommendations, multi-user permissions, admin tools.
- Stack: Flask + Jinja templates, Bootstrap 5.3.8, vanilla JS under `static/js/`. No frontend build step or HMR dev server.
- **Themes are a hard constraint.** Every screen must work on all 26 Bootswatch themes and the local themes (Gruvbox Dark, Catppuccin Latte/Frappé/Macchiato/Mocha), light and dark, by reading Bootstrap/theme variables rather than hardcoded colours. Theme is per-user.
- Never use native `alert()` / `confirm()` / `prompt()`; use Bootstrap modals and `CLU.showToast` toasts.
- Static URLs go through `url_for` (subpath deploys).
- Terminology to keep: Pull List, Wanted, Want to Read, On the Stack, Source Wall, Problem Files, CLU Wrapped, Reader / Clerk / Store Owner.

## Brand Commitments

- Name: **Comic Library Utilities (CLU)**. Logo at `images/clu-logo-360.png`.
- Voice (observed in README and FEATURES.md): plain, practical, specific about what a feature does and what it will not do (e.g. "nothing is deleted without you choosing which copy to keep"). Comic-shop vocabulary for roles and lists (Store Owner, Clerk, Pull List, On the Stack).

## Evidence on Hand

- `README.md`, `FEATURES.md`, `docs/` and the documentation site https://clucomics.org.
- Screenshots: `images/header.png` (publisher page) and others under `images/` and `readme/`.
- Community: Discord, Docker Hub pulls badge. The 70,000+ issue origin library is the project's stated origin story.
- No testimonials, user counts or benchmarks are recorded; do not invent them.

## Product Principles

1. **The maintainer's time is the scarce resource.** Bulk, library-scale operations come first; per-item work should never be the only path.
2. **Safe by default, reversible where possible.** Show what will change before it changes, keep history, and never destroy files or folders the user did not choose.
3. **Work alongside, not instead of.** Stay compatible with comic servers and standard formats (CBZ, ComicInfo.xml, OPDS) rather than locking data into CLU.
4. **Every theme, every device.** A surface is not done until it works across the theme set and on desktop, tablet and phone.
5. **Honest status.** Background work is visible, failures are reported clearly, and nothing silently no-ops.
