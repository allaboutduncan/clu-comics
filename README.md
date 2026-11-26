# Comic Library Utilities (CLU)

**A self-hosted comic library manager for Docker — bulk CBR to CBZ conversion, a full CBZ editor, ComicInfo.xml metadata from Metron and ComicVine, a built-in web reader, and download automation.**

![Docker Pulls](https://img.shields.io/docker/pulls/allaboutduncan/comic-utils-web)
![GitHub Release](https://img.shields.io/github/v/release/allaboutduncan/clu-comics)
![GitHub commits since latest release](https://img.shields.io/github/commits-since/allaboutduncan/clu-comics/latest)
![License](https://img.shields.io/github/license/allaboutduncan/clu-comics)

[![Join our Discord](https://img.shields.io/discord/1384271933327020113?label=CLU%20Discord&logo=discord&style=for-the-badge)](https://discord.gg/ndDhpvrgBa)

![Comic Library Utilities (CLU) logo](images/clu-logo-360.png "Comic Library Utilities")

Comic Library Utilities (CLU) is a self-hosted web app for organizing, repairing and reading a digital comic collection. It runs in Docker, points at the folders your comics already live in, and gives you the bulk tools you would otherwise write scripts for: convert CBR to CBZ across an entire library, convert PDF to CBZ, rename thousands of files to a consistent pattern, edit the pages inside a CBZ from the browser, write ComicInfo.xml metadata from Metron, ComicVine, GCD, AniList or MangaDex, and track weekly comic releases with a pull list. Everything happens through the web UI, so you can maintain a remote comic book collection without shell access to the server.

**[Documentation](https://clucomics.org)** · **[Full feature list](FEATURES.md)** · **[FAQ](#frequently-asked-questions)** · **[Discord](https://discord.gg/ndDhpvrgBa)** · **[Docker Hub](https://hub.docker.com/r/allaboutduncan/comic-utils-web)**

![CLU publisher page showing a grid of comic series covers grouped by publisher](images/header.png "CLU Publisher page — browse a digital comic collection by publisher")

## Why CLU Exists

This started as a set of utilities built while moving a 70,000+ issue comic library to [Komga](https://komga.org/). It has since grown into a stand-alone comic library manager: run it alongside Komga or Kavita as the maintenance and metadata layer, or run it on its own and read in the browser.

## What CLU Does

- **[Convert CBR to CBZ in bulk](FEATURES.md#directory-operations)** — convert a folder, or an entire library, from RAR-based CBR to CBZ. Also converts PDF to CBZ with a JPEG or WebP output choice.
- **[Edit CBZ files in the browser](FEATURES.md#single-file--cbz-operations)** — a full GUI editor: rename and drag-reorder pages, add or delete images, crop covers, split a multi-issue CBZ into single issues, combine files, enhance image contrast and brightness.
- **[Clean up and rename files](FEATURES.md#directory-operations)** — bulk rename with custom patterns, remove text from filenames, zero-pad issue numbers, strip junk files and `__MACOSX` folders, and run a missing-issue check against a series.
- **[Comic metadata editing](FEATURES.md#metadata--comicinfoxml)** — read and write ComicInfo.xml using Metron, ComicVine, Grand Comics Database, AniList, MangaDex, MangaUpdates and Bedetheque. Bulk tag a whole directory, review changes before they apply, and revert them from a history log.
- **[Source Wall](FEATURES.md#metadata--comicinfoxml)** — a spreadsheet-style view for reviewing and correcting metadata across many issues at once.
- **[Browse and read your collection](FEATURES.md#library-browsing--collection-views)** — paginated library browsing, per-series pages, folder thumbnails, favorites, and a built-in page-by-page reader that remembers where you left off.
- **[Reading lists](FEATURES.md#reading-lists)** — import CBL files or story arcs from Metron and ComicVine, map entries to files you already own, and see what is missing.
- **[Pull list and weekly comic releases](FEATURES.md#pull-list-releases--wanted)** — subscribe to series, track new weekly releases, and keep a Wanted list of issues you do not have yet.
- **[Comic downloads](FEATURES.md#downloads)** — search and download from Direct Download Source, Usenet via Newznab indexers with SABnzbd or NZBGet, and DC++/AirDC++, with configurable source priority and a browser extension for one-click sends.
- **[Folder monitoring](FEATURES.md#folder-monitoring)** — watch a downloads folder and automatically unpack, convert to CBZ, rename and file new comics into your library.
- **[Insights, Timeline and CLU Wrapped](FEATURES.md#insights-timeline--clu-wrapped)** — collection size and reading stats, a full timeline of what you have read, and shareable year-in-review images.
- **[Multi-user with per-folder permissions](FEATURES.md#multi-user-accounts--permissions)** — Reader, Clerk and Store Owner roles, per-library and per-folder access grants, and per-user reading history. A single-user install with no password needs no login; once you set an owner password, CLU asks for it.
- **[API and OPDS](FEATURES.md#api--integrations)** — a token-authenticated `/api/v1` for companion apps, self-documenting at `/api/v1/docs`, plus OPDS feeds for comic reader apps and optional Komga and Kavita reading sync.

See **[FEATURES.md](FEATURES.md)** for the complete breakdown.

## Installation

CLU is distributed as a Docker image: [`allaboutduncan/comic-utils-web`](https://hub.docker.com/r/allaboutduncan/comic-utils-web).

### Quick start

```bash
docker run -d \
  --name clu \
  -p 5577:5577 \
  -v clu-config:/config \
  -v /path/to/cache:/cache \
  -v /path/to/comics:/data \
  allaboutduncan/comic-utils-web:latest
```

Then open `http://localhost:5577`.

### Docker Compose (recommended)

Copy the following and edit the environment variables and volume paths.

```yaml
services:
    comic-utils:
        image: allaboutduncan/comic-utils-web:latest

        container_name: clu
        logging:
            driver: "json-file"
            options:
                max-size: '20m'  # Reduce log size to 20MB
                max-file: '3'     # Keep only 3 rotated files
        restart: always
        ports:
            - '5577:5577'
        volumes:
            - 'config-volume:/config' # Maps to a Docker Volume for Database Storage and Backups
            - "/path/to/local/cache:/cache" # Maps to local folder for thumbnail cache
            ## update the line below to map to your library.
            ## Map your first/main library to /data
            - "/e/Comics:/data"
            ## Map additional libraries and add them in the settings of the app
            - "/e/Manga:/manga"
            - "/f/Magazines:/magazines"
            ## Additional folder if you want to use Folder Monitoring.
            - "/f/Downloads:/downloads"
        environment:
            - FLASK_ENV=production
            ## Set the User ID (PUID) and Group ID (PGID) for the container.
            ## This is often needed to resolve permission issues, especially on systems like Unraid
            ## where a specific user/group owns the files.
            ## For Unraid, PUID is typically 99 (user 'nobody') and PGID is typically 100 (group 'users').
            ## For Windows/WSL, set these to match your Windows user ID (see docs/WINDOWS_WSL_SETUP.md)
            - PUID=99
            - PGID=100
            ## File creation mask. 000 -> world-writable folders (drwxrwsrwx) and files
            ## (-rw-rw-rw-). Use 002 for group-writable (775/664) or 022 for owner-only writes.
            - UMASK=000
            ## Only if a reverse proxy rewrites the Host header and changes in the UI fail
            ## with "Cross-site request refused" (see "Running behind a reverse proxy").
            # - CLU_TRUSTED_ORIGINS=https://comics.example.com
            ## Only if another service on this container's network already uses
            ## 127.0.0.1:18191 (see "Built-in FlareSolverr").
            # - FLARESOLVERR_PORT=18191
volumes:
  config-volume: # Now required for Database Storage and Backups
```

__Update your Docker Compose:__ Mapping the `/config` directory is required to ensure config settings and the database persist across updates.

__First install:__ visit the config page and confirm everything is set up the way you want it, then:

* Save your Config settings
* Click the Restart App button

Windows and WSL users hitting file permission errors should read **[docs/WINDOWS_WSL_SETUP.md](docs/WINDOWS_WSL_SETUP.md)**.

### Mapping your library volumes

Map your main library to `/data`, the default library path on a new install. Any other path works too (for example `/comics`): map it, then add it or point the default library at it under Settings → Libraries. Additional libraries are mapped and added the same way. Full install walkthrough: **[clucomics.org quickstart](https://clucomics.org/getting-started/quickstart/)**.

### Running behind a reverse proxy

CLU honours `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host` and `X-Forwarded-Prefix` (one proxy hop), so it works on its own hostname or on a subpath such as `https://example.com/clu/`.

To protect against cross-site request forgery, CLU refuses any POST, PUT, PATCH or DELETE that a browser reports as coming from another site. It accepts a request when the browser says it is same-origin, or when the `Origin` (or `Referer`) host matches the `Host` header or an `X-Forwarded-Host` value. Clients that send none of these headers (scripts, curl, OPDS readers, API tokens) are not affected. Most proxies (Nginx Proxy Manager, Traefik, Caddy, SWAG, and Nginx with `proxy_set_header Host $host;`) need nothing extra.

If a proxy rewrites `Host` to the container's address and sends no `X-Forwarded-Host`, saving or changing anything in the UI fails with *"Cross-site request refused"*. Fix the proxy to pass the original host, or list your public address in `CLU_TRUSTED_ORIGINS` (comma-separated origins or `host:port`) and restart the container:

```yaml
        environment:
            - CLU_TRUSTED_ORIGINS=https://comics.example.com
```

### Built-in FlareSolverr

Some GetComics mirrors answer with a Cloudflare "Just a moment…" check that a normal download cannot pass. The Docker image ships its own [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) (a headless Chromium) for that, so there is no second container to install.

* It starts on the first challenge and stops after 10 idle minutes. While it is up it uses a few hundred MB of memory.
* It only fetches the clearance cookies; CLU then downloads the file itself. A check it cannot pass still ends as a manual-download link.
* Turn it off, or test it, under **Settings → Download and API → Pass Cloudflare challenges (FlareSolverr)**.
* It listens on `127.0.0.1:18191` inside the container and is never published. The port is deliberately not FlareSolverr's usual `8191`.

| Environment variable | Default | What it does |
| --- | --- | --- |
| `FLARESOLVERR_PORT` | `18191` | Loopback port of the built-in solver |
| `FLARESOLVERR_LOG_LEVEL` | `info` | Log level of the solver process |

**Sharing a network with other containers.** With `network_mode: service:<vpn>` (gluetun and similar), every container behind the VPN shares one `127.0.0.1`, so two services cannot use the same port. CLU up to 7.2.1 used `8191`, which clashed with a standalone FlareSolverr run for Prowlarr in the same namespace: whichever started second failed with `Address already in use`. Later versions use `18191`, so the two run side by side. If something else already holds `18191`, the app log says so; set `FLARESOLVERR_PORT` to a free port and restart the container.

To have CLU use a standalone FlareSolverr in the same network namespace instead of starting its own, set `FLARESOLVERR_PORT` to that solver's port (`8191`). CLU reuses a FlareSolverr that is already answering on its port. This needs the two to share a network namespace, and the solver must leave through the same public IP as CLU, because Cloudflare ties the clearance to the address that earned it.

## Integrations

| Integration | What it is used for |
| --- | --- |
| [Metron](https://metron.cloud/) | Comic metadata, series, story arcs, weekly releases |
| [ComicVine](https://comicvine.gamespot.com/api/) | Comic metadata via API, or a [local offline database](https://clucomics.org/features/local-databases/comicvine/) |
| [Grand Comics Database](https://clucomics.org/features/local-databases/gcd/) | Optional local SQLite metadata database |
| [INDUCKS](docs/inducks-provider.md) | Optional local SQLite database of Disney comics worldwide |
| AniList / MangaDex / MangaUpdates | Manga metadata |
| Bedetheque | Bandes dessinées (European comics) metadata |
| [Komga](https://komga.org/) | Optional reading-history sync |
| [Kavita](https://www.kavitareader.com/) | Optional reading-history sync, per user (Kavita 0.9 or newer) |
| [Direct Download Source](https://clucomics.org/features/file-downloads/) | Comic download search and grabbing |
| [SABnzbd / NZBGet](https://clucomics.org/features/usenet/setup/) | Usenet download clients |
| [Newznab indexers](https://clucomics.org/features/usenet/indexers/) | Usenet search |
| AirDC++ | DC++ download source |
| MEGA / Pixeldrain | Supported download mirrors |
| OpenAI / Anthropic / Gemini | Optional AI-powered reading recommendations |
| OPDS | Feeds for third-party comic reader apps |

## How CLU Compares

CLU is not a replacement for a comic server — it is the layer underneath one.

| Tool | What it is for | With CLU |
| --- | --- | --- |
| **Komga / Kavita** | Serving and reading your library | CLU maintains the files and metadata they read; CLU can also sync reading history from Komga and Kavita |
| **Mylar3** | Comic downloading and series tracking | Overlapping — CLU covers pull list, wanted issues and downloads, plus file and metadata tools Mylar does not have |
| **ComicTagger** | Tagging individual comic files | CLU does bulk tagging across a whole library, with review and revert |
| **CLU** | Bulk file operations, CBZ editing, metadata, downloads, browsing and reading | — |

Plenty of people run CLU *and* Komga against the same folders. That is the intended setup.

## Frequently Asked Questions

### How do I convert CBR to CBZ in bulk?

Point CLU at a directory and run **Convert Directory**, or run **Convert Entire Library to CBZ** to process everything at once. Conversion uses `unar`, so it handles RAR-based CBR files that many tools choke on. See [Convert Directory](https://clucomics.org/features/directory-features/convert/).

### Can I convert PDF to CBZ?

Yes. CLU converts PDF comics to CBZ and lets you choose JPEG or WebP for the extracted pages. See [Convert PDF to CBZ](https://clucomics.org/features/directory-features/pdf/).

### Can CLU catch a wrong metadata match?

Optionally, yes. The [metadata date check](docs/metadata-date-check.md) compares the matched
issue's date against the year in the filename and flags a match that disagrees wildly — the case
where the series matched is the wrong one and the issue number resolved anyway. Off by default,
with a log-only mode so you can see what it would do before it does anything.

### How do I find damaged comics?

The [Library Health](docs/library-health.md) page collects every file an operation could not read —
a truncated download, a bad checksum, a RAR wearing a `.cbz` name — with the cause in plain
English and the action most likely to help. It does not scan your library; a file appears because
something tried to open it and failed. You can search your download sources for a replacement from
the row, and CLU files it straight onto the damaged file, keeping the old copy in the Trash.

### Does CLU edit ComicInfo.xml?

Yes — CLU is a full comic metadata editor. It reads and writes ComicInfo.xml, creates it when missing, and can bulk-update fields across an entire directory. Metadata can be pulled from Metron, ComicVine, GCD, AniList, MangaDex, MangaUpdates or Bedetheque, reviewed before it is applied, and reverted afterwards from the metadata history.

### Does CLU work with Komga or Kavita?

Yes. CLU operates directly on the files on disk, so anything you fix or tag in CLU shows up in Komga or Kavita after a scan. CLU can also sync reading history from Komga, and from Kavita for each CLU user who links their own Kavita auth key under My Account.

### Can multiple people use one CLU install?

Yes. CLU supports multiple accounts across three roles — Reader, Clerk and Store Owner — with per-library and per-folder access grants. Reading history, favorites and reading lists are kept separate per user. A single-user install where no password has been set requires no login, and anyone who can reach it is the owner.

**A password means login.** If you set an owner password, CLU asks for it, even when yours is the only account. OPDS reading apps then need your username and password, and `/api/v1` clients need an API token (My Account → API Tokens). Forgot the owner password? See [Multi-User Accounts & Permissions](FEATURES.md#multi-user-accounts--permissions) for the recovery steps.

### Is there a mobile app or OPDS support?

CLU serves OPDS feeds that work with standard comic reader apps, and exposes a token-authenticated `/api/v1` for companion apps, documented in-app at `/api/v1/docs`.

### Can I read comics directly in CLU?

Yes. CLU has a built-in browser-based reader with page-by-page navigation that remembers your position, plus Continue Reading and On the Stack views.

### Does CLU need an internet connection?

Only for metadata lookups, downloads and recommendations. All file operations — conversion, renaming, CBZ editing, browsing and reading — work entirely offline. GCD and ComicVine can also run from local databases.

### Do I need shell access to my server to use CLU?

No — that is the point. Every operation runs through the web UI, so you can maintain a remote comic collection without SSH.

### What file formats does CLU support?

CBZ, CBR, PDF, and the plain ZIP and RAR archives that comics are often distributed in. CBZ is the working format everything converts to.

### My log says "the inotify queue overflowed". What should I do?

Usually nothing. It means files changed in your library faster than Linux could report them — a large import or an rsync, typically. CLU logs it once and runs a file index sync about a minute later to pick up whatever was missed.

If it happens often on a very large library, raise the kernel's queue size on the **host** (it is not a container setting) and restart the CLU container, because the value is read when the watcher starts:

```bash
sudo sysctl fs.inotify.max_queued_events=262144
```

Add `fs.inotify.max_queued_events=262144` to `/etc/sysctl.conf` (or a file in `/etc/sysctl.d/`) to keep it across reboots.

### Where is the full documentation?

At **[clucomics.org](https://clucomics.org)**, including a [setup walkthrough](https://clucomics.org/getting-started/setup-walkthrough/) and a page for every feature.

## Screenshots

![CLU Insights page showing comic collection size, publisher breakdown and reading statistics](images/insights.png "Insights — digital comic collection statistics")

![CLU Timeline showing a chronological history of comics read by date](images/timeline.png "Timeline — comic reading history")

![CLU weekly pull list showing new comic releases with covers and download status](images/weekly.png "Pull List — weekly comic releases")

![CLU browser extension sending a comic download to the app](images/chrome_promo.png "Browser extension — send comic downloads to CLU")

## Community & Contributing

- **[Discord](https://discord.gg/ndDhpvrgBa)** — questions, support and feature discussion
- **[Documentation](https://clucomics.org)** — full install and feature docs
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — how to contribute code, including adding new metadata providers
- **[SECURITY.md](SECURITY.md)** — reporting a security issue
- **[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)** — community expectations
- **[Issues](https://github.com/allaboutduncan/clu-comics/issues)** — bug reports and enhancement requests

## License

Licensed under the **[GNU General Public License v3.0](LICENSE)**.

## Say Thanks

If you enjoyed this, want to say thanks or want to encourage updates and enhancements, feel free to [!["Buy Me A Coffee"](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/allaboutduncan)
