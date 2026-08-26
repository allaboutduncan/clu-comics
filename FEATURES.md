# CLU Features

A complete reference for **Comic Library Utilities (CLU)** — the self-hosted comic library manager for CBZ, CBR and PDF collections. For install steps see the [README](README.md); for step-by-step guides see [clucomics.org](https://clucomics.org).

**Contents**

- [Library Browsing & Collection Views](#library-browsing--collection-views)
- [Built-In Reader & Reading Tracking](#built-in-reader--reading-tracking)
- [Reading Lists](#reading-lists)
- [Directory Operations](#directory-operations)
- [Single-File & CBZ Operations](#single-file--cbz-operations)
- [Metadata & ComicInfo.xml](#metadata--comicinfoxml)
- [Pull List, Releases & Wanted](#pull-list-releases--wanted)
- [Downloads](#downloads)
- [File Management](#file-management)
- [Folder Monitoring](#folder-monitoring)
- [Insights, Timeline & CLU Wrapped](#insights-timeline--clu-wrapped)
- [AI Recommendations](#ai-recommendations)
- [Multi-User Accounts & Permissions](#multi-user-accounts--permissions)
- [API & Integrations](#api--integrations)
- [Administration](#administration)

---

## Library Browsing & Collection Views

Browse a digital comic collection the way you would in a comic server, without leaving CLU.

- **Browse Library** — paginated grid of your collection with server-side paging, a per-page setting that persists, and a card size picker.
- **Publisher and series pages** — drill from publisher, to series, to individual issues, with per-issue actions available inline.
- **Metadata browser** — browse the collection by the metadata in your ComicInfo.xml files rather than by folder structure.
- **Folder thumbnails** — auto-generated cover thumbnails for folders, cached to a dedicated `/cache` volume.
- **Favorites** — mark publishers, series and individual issues as favorites.
- **Want to Read / On the Stack** — queue up what you plan to read next, straight from a folder or series.
- **Search** — search across the indexed library by filename and metadata.

📖 [Collection docs](https://clucomics.org/features/collection/) · [Publishers](https://clucomics.org/features/collection/publishers/) · [Series](https://clucomics.org/features/collection/series/) · [Issues](https://clucomics.org/features/collection/issues/)

## Built-In Reader & Reading Tracking

CLU includes a browser-based comic reader, so a CLU install is usable on its own without a separate comic server.

- **Page-by-page web reader** for CBZ files.
- **Resume where you left off** — reading positions are saved per user and restored when you reopen an issue.
- **Mark as read** and automatic completion tracking.
- **Continue Reading** and **On the Stack** views surface what is in progress.
- Reading history feeds the [Insights, Timeline and Wrapped](#insights-timeline--clu-wrapped) features.

📖 [Reading docs](https://clucomics.org/features/collection/reading/)

## Reading Lists

Track curated runs, story arcs and crossovers, and see which issues you already own.

- **Import CBL files** (the Comic Book List format used by ComicRack and friends).
- **Bulk import from a GitHub tree** — pull in an entire collection of reading-order lists at once.
- **Import story arcs from Metron and ComicVine** by arc.
- **Create lists manually**, tag them, and drag-and-drop to reorder entries.
- **Map entries to local files** so a list shows what you have and what is missing.
- **Export**, bulk delete, per-list thumbnails, and scheduled sync to keep imported lists current.

📖 [Reading lists docs](https://clucomics.org/features/collection/reading-lists/)

## Directory Operations

Bulk operations that run across a whole folder — the core of what CLU was built for.

- **Convert CBR to CBZ** — batch convert RAR-based comics using `unar`, which handles archives many Python-only tools fail on.
- **Convert Entire Library to CBZ** — one operation across every mapped library.
- **Convert PDF to CBZ** — extract PDF pages into a CBZ, with a JPEG or WebP output choice.
- **Rename All Files** — apply a consistent naming pattern with regex-based volume and issue extraction, configurable issue-number zero-padding, and space/special-character replacement.
- **Smart Rename** with exclude terms for text you never want carried into filenames.
- **Clean Files** — strip `__MACOSX` folders, remove leading `.`, `_` and `._` characters, drop unwanted file extensions, and normalize page filenames to zero-padded numbering.
- **Rebuild All Files** — rewrite archive structure so every file in a directory is consistent.
- **Missing Issue Check** — find gaps in a series based on the issues present on disk.
- **Enhance Images** — batch contrast, brightness and sharpening adjustments.
- **Update ComicInfo.xml** — batch field updates across every file in a directory.

📖 [Directory features docs](https://clucomics.org/features/directory-features/) · [Convert](https://clucomics.org/features/directory-features/convert/) · [Rename](https://clucomics.org/features/directory-features/rename/) · [PDF to CBZ](https://clucomics.org/features/directory-features/pdf/) · [Missing issues](https://clucomics.org/features/directory-features/missing/)

## Single-File & CBZ Operations

A full CBZ editor in the browser — no unzipping, no image editor, no re-zipping.

- **Edit CBZ** — rename pages, drag-and-drop to reorder, add images, delete images, all in a GUI.
- **Crop cover** — left, center, right or freeform crop, with an optional blurred fill.
- **Remove first image** — drop scan-group covers and ads in one click.
- **Add blank image** at the end of a file.
- **Split file** — break a multi-issue CBZ (a trade or a pack) into individual issues.
- **Combine CBZ** — merge multiple files into one.
- **Single-file rebuild / convert** — CBR to CBZ for one file with live progress reporting.
- **Enhance images** — contrast, brightness and blur adjustments for a single file.
- **Read date** — set or correct an issue's read date.
- **Delete file**, with a trash can and restore manifest so deletions are recoverable.

📖 [Single-file features docs](https://clucomics.org/features/single-file-features/) · [Edit CBZ](https://clucomics.org/features/single-file-features/edit/) · [Crop cover](https://clucomics.org/features/single-file-features/crop/) · [Split file](https://clucomics.org/features/file-management/split/)

## Metadata & ComicInfo.xml

CLU is a comic metadata editor as well as a file tool. Metadata is written to `ComicInfo.xml` inside each CBZ, which is what Komga, Kavita, ComicRack and most readers read.

**Supported metadata providers**

| Provider | Notes |
| --- | --- |
| **Metron** | Primary comic metadata source, including series, story arcs and weekly releases |
| **ComicVine** | Via the ComicVine API, or from a local offline SQLite database |
| **Grand Comics Database (GCD)** | Optional local SQLite database with fuzzy title matching |
| **GCD API** | Hosted GCD access |
| **AniList** | Manga metadata |
| **MangaDex** | Manga metadata |
| **MangaUpdates** | Manga metadata |
| **Bedetheque** | Bandes dessinées / European comics metadata |

**Metadata tooling**

- **Source Wall** — a spreadsheet-style bulk review and edit surface. Stage pending edits across many issues, see what changed, and commit when you are happy. Can rename files after a successful fetch.
- **Bulk metadata** — run a provider match across a directory, review the proposed changes in a modal, then apply.
- **Metadata history with bulk revert** — every batch is logged and can be rolled back.
- **Create ComicInfo.xml when missing**, and a missing-XML view with rescan.
- **Batch field updates** — set or clear specific ComicInfo fields across many files in parallel.
- **Write from database** — push metadata already indexed in CLU back into the files.
- **Creator credit backfill** — Metron often finishes an issue record hours after the comic ships, so a file tagged on release morning can land with no creators at all. CLU re-checks recently tagged files after each series sync (or on demand from Schedules) and fills the credits in, keeping the tags the file already has.
- Provider-ID stripping from credits, and clickable Writer / Penciller / Character links in the issue info modal.

📖 [Metadata provider settings](https://clucomics.org/features/app-settings/metadata/) · [Source Wall](https://clucomics.org/features/collection/source-wall/) · [ComicInfo.xml options](https://clucomics.org/features/file-management/comicinfo/) · [Local databases](https://clucomics.org/features/local-databases/)

## Pull List, Releases & Wanted

Track ongoing series and new weekly comic releases.

- **Pull List** — subscribe to series, with status color-coding, filtering, a Monitored flag with bulk multi-select toggling, and export/import.
- **Releases** — weekly comic releases from Metron, with a publisher filter.
- **Wanted** — issues you are missing, reconciled against what is actually on disk, with per-issue delete and re-mark-wanted.
- **Weekly Packs** — track and grab weekly release packs, with tri-state availability and a scheduler that backfills oldest-first.
- **Series Search** — find missing issues of series you already own.
- **Publishers** — publisher list synced from Metron, with filtering.
- **Automap (Scan Library)** — match your existing folders to series automatically using sidecar files, with Mylar-compatible `series.json` support. Ended series default to Monitor off.

📖 [Pull list docs](https://clucomics.org/features/pull-list/) · [Releases](https://clucomics.org/features/pull-list/releases/) · [Wanted](https://clucomics.org/features/pull-list/wanted/) · [Weekly packs](https://clucomics.org/features/pull-list/weekly/) · [Automap](https://clucomics.org/features/pull-list/automap/)

## Downloads

CLU can search and retrieve comics from several sources and file the results into your library. Source order is configurable, so CLU tries your preferred source first.

- **GetComics.org** — search and download, with a tuned scoring system that decides whether a search result really matches the issue you want (accept / fallback / reject), user-defined series aliases, and Cloudflare challenge handling. A dry-run simulation tool lets you test scoring against your Wanted list before enabling automation.
- **Usenet** — Newznab-compatible indexers with **SABnzbd** and **NZBGet** as download clients. The Status page shows live stage (downloading, verifying, repairing, extracting, moving), percentage and bytes.
- **DC++ / AirDC++** — a third source, which survives a CLU restart mid-transfer.
- **MEGA** and **Pixeldrain** mirrors are supported for links that use them.
- **Download source priority** — configure which source is tried first.
- **Browser extension** — a "Send to CLU" extension for one-click sends from a browser.
- **Auto-download schedules** — run searches on a schedule against your Wanted list and pull list.
- **Status page** — live progress, plus Recent Downloads reconciled against real state with Interrupted, Cancelled and Failed badges.

📖 [File downloads docs](https://clucomics.org/features/file-downloads/) · [Usenet setup](https://clucomics.org/features/usenet/setup/) · [Indexers](https://clucomics.org/features/usenet/indexers/) · [Source priority](https://clucomics.org/features/usenet/source-priority/) · [Browser extension](https://clucomics.org/features/file-downloads/setup/)

## File Management

A file manager built for comic libraries, usable entirely over the web.

- **Source and destination browsing** side by side.
- **Drag and drop** to move files and directories between them.
- **Rename** directories and files, including bulk rename across a directory and "remove text from all filenames".
- **Delete** directories or files, with the trash can and restore manifest.
- **Upload** files, including PDFs, directly into a library.
- **Cleanup** — remove empty folders left behind after moves.
- Permission handling for `PUID`, `PGID` and `UMASK`, so files created by CLU stay readable by your comic server.

📖 [File management docs](https://clucomics.org/features/file-management/) · [Move](https://clucomics.org/features/file-management/move/) · [Rename](https://clucomics.org/features/file-management/rename/)

## Folder Monitoring

Point CLU at a downloads folder and let it file new comics automatically.

- **Auto-rename** new arrivals using your naming pattern.
- **Auto-convert to CBZ** as files land.
- **Auto-unpack** archives, including hybrid and multipart releases.
- **Process sub-directories** and move them into the target library.
- **Custom naming patterns** for the destination.
- **Empty-folder cleanup** in the target after a move.
- A debounced file watcher feeds a background metadata scanner that keeps the file index current.

Enable with `MONITOR=yes` in your Docker environment.

📖 [Folder monitoring docs](https://clucomics.org/features/folder-monitoring/) · [Setup](https://clucomics.org/features/folder-monitoring/setup/)

## Insights, Timeline & CLU Wrapped

- **Insights** — collection size, file counts, disk usage, publisher and series breakdowns, reading stats by year, and favorite writers, artists and characters.
- **Timeline** — a full chronological history of everything you have read, filterable by year and month.
- **CLU Wrapped** — Spotify-Wrapped-style yearly *and* monthly stat images, generated as downloadable slides you can share.
- **Insights API** — exposes the same numbers for dashboards such as [gethomepage.dev](https://gethomepage.dev/), with per-user bearer-token support.

📖 [Insights docs](https://clucomics.org/features/insights/) · [Timeline](https://clucomics.org/features/insights/timeline/) · [Insights API](https://clucomics.org/features/insights/api/)

## AI Recommendations

Optional AI-powered reading recommendations based on your library and reading history.

- Supports **OpenAI**, **Anthropic** and **Google Gemini** (via its OpenAI-compatible endpoint).
- Configured under **Settings → Personalization → Recommendation Service**.
- Entirely optional — CLU works fully without an AI key.

📖 [Personalization settings](https://clucomics.org/features/app-settings/personalization/)

## Multi-User Accounts & Permissions

- **Three roles** — Reader, Clerk and Store Owner — enforced by a central path-and-method policy, so mutating requests default to Clerk and reads to Reader.
- **Single-user installs need no login.** CLU runs in implicit-owner mode by default; multi-user activates the moment you create a second account. Existing `CLU_USERNAME` / `CLU_PASSWORD` credentials are migrated into a hashed owner account.
- **Per-library and per-folder access grants** with nested inheritance and default-deny for new accounts. Enforced at listing *and* path-resolution level, so a hand-typed URL to a folder a user cannot see returns 403 — across browse, search, thumbnails, covers, the reader, downloads, the metadata browser, OPDS and the token API.
- **Per-user data isolation** — reading history, reading positions, Want to Read, On the Stack, favorites, reading lists, Insights, Timeline and Wrapped are all scoped to the individual user.
- **Per-user theme and dashboard layout.**

## API & Integrations

- **`/api/v1`** — a token-authenticated external API for companion reading apps: publishers, series, issues, favorites, to-read, recent, issue detail, cover, download, reading progress (including a `progress/since` delta sync) and mark-read.
- **`/api/v1/docs`** — the API documents itself in-app.
- **Per-user API tokens** — created and revoked from My Account, stored hashed and shown only once.
- **OPDS feeds** — `/opds`, `/opds/browse` and `/opds/to-read` for third-party comic reader apps, with HTTP Basic Auth in multi-user mode and library scoping per user.
- **Komga reading sync** — pull reading history and in-progress books from a [Komga](https://komga.org/) server.
- **`/health`** — a health endpoint for container orchestration and uptime monitoring.

## Administration

- **Schedules** — scheduled jobs for file-index rebuilds, reading-list sync, GetComics runs and the scrape index.
- **Database tools** — backup, restore and download the SQLite database from the UI, with corruption detection and a salvage tool. The database lives in `/config`, so it persists across container updates.
- **Logs** — an in-app log viewer with a polled tail, plus separate application and folder-monitoring logs.
- **Debug package export** — bundle logs and config with secrets redacted, for bug reports.
- **Active Operations indicator** in the navbar, plus a version-update badge and an in-app Restart button.
- **26 Bootswatch themes** and a configurable dashboard layout editor.

📖 [App settings docs](https://clucomics.org/features/app-settings/) · [Database](https://clucomics.org/features/app-settings/database/) · [Schedules](https://clucomics.org/features/app-settings/schedules/) · [Logs](https://clucomics.org/features/app-settings/logs/)

---

**Related:** [README](README.md) · [Full documentation](https://clucomics.org) · [FAQ](https://clucomics.org/frequently-asked-questions/) · [Discord](https://discord.gg/ndDhpvrgBa)
