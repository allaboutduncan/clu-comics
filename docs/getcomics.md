# GetComics Integration

## Overview

GetComics (`models/getcomics.py`) provides search and download functionality for GetComics.org. It uses a sitemap-first architecture with a local scrape index to avoid hitting GetComics live during wanted-issue simulations.

## Architecture

### Components

- **Scrape Index**: SQLite table (`getcomics_urls`) storing scraped titles, download URLs, and metadata
- **Sitemap Index**: SQLite table (`getcomics_sitemap`) storing series-to-sitemap-URL mappings
- **Scoring Engine**: Pure functional scoring system for matching results to wanted issues

### Data Flow

1. **Indexing**: `scrape_sitemap.py` scrapes GetComics sitemaps and stores results in `getcomics_urls`
2. **Search**: `search_getcomics_for_issue()` queries the scrape index first, falls back to live search
3. **Scoring**: `score_getcomics_result()` evaluates how well a title matches a wanted issue

### Manual Search

The search modal on the Wanted and Series pages goes through the **same scorer**:
`/api/getcomics/search` (`routes/downloads.py`) calls `search_getcomics()`, then
`score_getcomics_result()` + `accept_result()` on every hit, and returns them
sorted best-first with a `score` and a `decision` per result — the response
shape `/api/usenet/search` and `/api/dcpp/search` already use. The modal shows
ACCEPT/FALLBACK results and collapses the rest behind a toggle.

Scoring needs the issue context (`series`, `issue`, `issue_year`). A bare `q`,
or a query the user has retyped to a different series, returns `scored: false`
and the site's own ordering — scoring against a stale context would reject
everything. Aliases are consulted when deciding whether the query still refers
to the series, because `series.html` seeds the query box with the first
configured alias rather than the canonical name.

## Scoring System

The scoring system (`score_comic()` in `models/getcomics.py`) evaluates how well a GetComics title matches a wanted issue.

### Decision Outcomes

| Decision | Score | Description |
|----------|-------|-------------|
| ACCEPT | >= 40 | Strong match - proceed with download |
| FALLBACK | 1-39 | Weak match - range pack containing target issue |
| REJECT | <= 0 | No match - skip result |

### Scoring Components

| Component | Points | Description |
|-----------|--------|-------------|
| Series match | +30 | Series name matches |
| Issue match | +30 | Issue number found with `#` prefix |
| Standalone issue | +20 | Issue number without `#` prefix |
| Year match | +20 | Year matches exactly |
| Title tightness | +15/-10 | Bonus/penalty for title closeness |
| Different series | -30 | Remaining text indicates different series |
| Arc sub-series | -30 | Story arc (dash notation) penalized |
| Variant sub-series | -30 | Publication variant without acceptance |
| Issue mismatch | -40 | Wrong issue number explicitly found |
| Wrong year | -20 | Year present but doesn't match |

### Range Pack Handling

| Scenario | Result | Score |
|----------|--------|-------|
| Same-series range ending on target | FALLBACK | 39 |
| Same-series range containing target | FALLBACK | 39 |
| Different-series range ending on target | REJECT | -100 |
| Different-series range containing target | REJECT | -100 |

**Rationale**: Same-series ranges (e.g., "Batman #1-12") contain the main series issues. Arc/different-series ranges (e.g., "Court of Owls #1-5") have their own internal numbering and are rejected.

### Sub-series Detection

1. **Variants**: Annual, TPB, Quarterly, etc. - penalized unless accepted via `SEARCH_VARIANTS`
2. **Arcs**: Dash notation (e.g., "Batman - Court of Owls") - penalized, arcs have separate numbering
3. **Sequels**: Season/Volume/Book/Part/Chapter keywords - treated as arc-type sub-series

Dash notation means any of `-`, `–` (en dash), `—` (em dash) or `:` — GetComics
titles use the en dash, and `_normalize_separators()` folds the rest onto it.

#### Worked example

Searching `Teenage Mutant Ninja Turtles 3 2024` returns four spin-offs sharing
the parent name, issue number and year:

| Title | Score | Why |
|-------|-------|-----|
| `Teenage Mutant Ninja Turtles #3 (2024)` | **95** | series +30, issue +30, year +20, tightness +15 |
| `Teenage Mutant Ninja Turtles – Nightwatcher #3 (2024)` | 10 | arc -30, no issue bonus, tightness -10 |
| `Teenage Mutant Ninja Turtles – Saturday Morning Adventures Vol. 3` | -20 | arc -30, no year |

The spin-offs score 10 rather than 40 because `_score_series_match()` marks the
dash subtitle as an `arc`, which both costs -30 and clears `allow_issue_match`
— so the +30 issue bonus never applies even though `#3` is present and correct.
That gate is what separates a spin-off from the series it spun off from; a
substring heuristic cannot, since all five titles contain the series name, the
issue number and the year.

### Configurable Keywords

Settings in `config.ini` under `[SETTINGS]`:

| Setting | Default | Purpose |
|---------|---------|---------|
| `VARIANT_TYPES` | annual,quarterly,tpB,oneshot,... | Publication format keywords |
| `PUBLICATION_TYPES` | annual,quarterly | Series type keywords |
| `SEQUEL_KEYWORDS` | season,volume,book,part,chapter | Volume/sequel detection |
| `CROSSOVER_KEYWORDS` | meets,vs,versus,x-over,crossover | Crossover detection |

## Scrape Index Schema

```sql
CREATE TABLE getcomics_urls (
    url TEXT PRIMARY KEY,           -- entry URL (base_url#slug for multi-entry)
    full_url TEXT,                  -- page URL for scraping
    series_norm TEXT,               -- normalized series name
    url_slug TEXT,                  -- URL slug from sitemap
    series_norm_norm TEXT,          -- doubly-normalized series name
    title TEXT,                     -- raw title from page
    issue_num TEXT,                 -- parsed issue number
    issue_range TEXT,                -- parsed issue range (e.g., "1-5")
    year INTEGER,                   -- parsed publication year
    volume INTEGER,                 -- parsed volume number
    is_annual INTEGER,               -- boolean flag
    is_bulk_pack INTEGER,            -- boolean flag
    is_multi_series INTEGER,         -- boolean flag
    format_variants TEXT,            -- comma-separated variants
    download_url TEXT,             -- primary download URL
    lastmod TEXT,                   -- last modified from sitemap
    indexed_at TEXT,                -- timestamp added to index
    search_aliases TEXT,            -- series aliases for matching
    scrape_status TEXT,             -- 'success' or 'empty'
    scrape_attempts INTEGER,         -- number of scrape attempts
    last_scrape_attempt TEXT,       -- timestamp of last attempt
    url_last_modified TEXT          -- URL last modified header
);
```

### Entry URL Format

- **Single entry pages**: `url = full_url` (e.g., `https://getcomics.org/...`)
- **Multi-entry pages**: `url = full_url#slug` where slug is derived from the title (e.g., `https://getcomics.org/...#top-10-beyond-the-farthest-precinct`)

This allows multiple entries from the same page to be stored separately while sharing the same source URL.

## Sitemap Index Schema

```sql
CREATE TABLE getcomics_sitemap (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_norm TEXT NOT NULL,      -- normalized series name
    url_slug TEXT,                  -- URL slug component
    full_url TEXT NOT NULL,         -- sitemap URL
    lastmod TEXT,                   -- last modified from sitemap
    UNIQUE(series_norm, url_slug)
);
```

The sitemap index maps series names to their GetComics sitemap URLs for pre-filtering searches.

## Key Functions

### `score_getcomics_result(result_title, series_name, issue_number, year, ...) -> tuple`
The public entry point every caller uses — auto-download (`app.py`), the dry-run
simulation and the manual search route (`routes/downloads.py`), Usenet
(`models/usenet.py`) and DC++ (`models/dcpp.py`). Scores the title once per
series alias and keeps the best, returning `(score, range_contains_target,
series_match)`. Pair it with `accept_result()` to get ACCEPT/FALLBACK/REJECT.

### `score_comic(result_title, search) -> ComicScore`
Pure functional scoring composition behind `score_getcomics_result`. Returns `ComicScore` with score, series_match, sub_series_type, variant_accepted, and remaining analysis.

### `search_getcomics_for_issue(series_name, issue_num, ...)`
Main search function. Queries scrape index first, then falls back to live GetComics search. Returns list of matched results.

### `_scrape_url_for_index(url, ...)`
Scrapes a GetComics page and stores ALL entries in the scrape index. Handles single pages, listing pages, and Top-10 collection pages.

### `lookup_series_urls(series_name) -> list[dict]`
Looks up sitemap URLs for a series from the sitemap index.
