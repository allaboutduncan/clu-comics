import defusedxml.ElementTree as SafeET
import re
import os
from core.database import search_file_index, search_by_comic_metadata
from core.metadata_dates import years_in_path
from helpers.collection import series_names_compatible
from core.app_logging import app_logger
from cbz_ops.rename import apply_filename_cleanup, load_filename_cleanup_config

# ── Year policy ──────────────────────────────────────────────────────────────
#
# A reading list searches the WHOLE library for "series + issue number", with
# no folder to anchor it the way `helpers.collection.match_issues_to_collection`
# has one. For a character with several reboots sharing an issue number --
# Batman, Batwoman -- that question has many correct-looking answers, and
# before this the first alphabetical one won. "Batwoman (2026) #7" mapped to
# "Batwoman 007 (2012)"; "Batman (2025) #14" mapped to "Batman 014 (1942)".
#
# So a year that contradicts the issue REJECTS a candidate outright rather than
# merely scoring it down: an entry left unmatched shows as "Click to map" and
# invites a correction, whereas a wrong cover looks like success and is never
# noticed.

VERDICT_ISSUE = "issue"      # candidate carries the issue's own year
VERDICT_VOLUME = "volume"    # candidate carries the volume's start year
VERDICT_UNKNOWN = "unknown"  # candidate names no year at all
VERDICT_NONE = "none"        # candidate's year contradicts the issue -> reject

# Bonuses. An exact issue-year hit outranks a volume-year hit, which outranks
# the yearless case, mirroring `models.getcomics._score_year`.
VERDICT_SCORES = {
    VERDICT_ISSUE: 20,
    VERDICT_VOLUME: 10,
    VERDICT_UNKNOWN: 0,
    VERDICT_NONE: 0,
}


# ── Series identity ──────────────────────────────────────────────────────────
#
# A year cannot separate two series that started the same year. "Batman (2025)
# #14" matched "Absolute Batman 014 (2025)": same issue number, same volume
# year, and "Batman" is a substring of "Absolute Batman". Extra words in FRONT
# of the wanted name are a different series, and that has to be caught
# structurally -- never by blacklisting "absolute", which is an ordinary word
# in real issue titles ("Nightwing 117 - Absolute Power"), exactly the trap
# CLAUDE.md records against reusing VARIANT_TYPES for filename matching.

_LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)


def _normalise_series(name):
    """Lowercase, punctuation-flattened form used to compare series names."""
    text = (name or "").lower().replace(":", " ").replace("-", " ")
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def _starts_with_series(name, wanted):
    """Whether ``name`` begins with the whole word(s) of ``wanted``."""
    return bool(wanted) and (name == wanted or name.startswith(wanted + " "))


def filename_series_matches(filename, series):
    """True when ``filename`` begins with the series we want.

    The filename tier has no ComicInfo to consult, so position is the only
    structural signal available -- and it is a sound one: CLU's rename pattern
    puts the series first (``{series_name} {issue_number}``) and
    ``helpers.collection.generate_filename_pattern`` anchors there too.

    "Absolute Batman 014 (2025).cbz" *contains* "Batman 014" but does not start
    with "Batman", which is the whole difference between the series a reading
    list asked for and a different one that shares its issue number and year.

    A leading article is not a different series, so it may be dropped from
    either side: a list saying "Flash" still matches "The Flash 094.cbz", which
    is the same allowance the search-retry already makes by stripping the first
    word.
    """
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    name = _normalise_series(stem)
    wanted = _normalise_series(series)
    if not name or not wanted:
        return False

    bare_name = _LEADING_ARTICLE.sub("", name)
    for candidate in (wanted, _LEADING_ARTICLE.sub("", wanted)):
        if _starts_with_series(name, candidate) or _starts_with_series(bare_name, candidate):
            return True
    return False


def year_verdict(candidate_years, volume_year=None, issue_years=None):
    """Whether a candidate file's years are consistent with the wanted issue.

    ``candidate_years``  every plausible year on the candidate -- its filename,
                         its folders, and its ComicInfo Year/Volume.
    ``volume_year``      the year the series began (Metron ``year_began``, or a
                         CBL ``Volume`` attribute when year-shaped).
    ``issue_years``      years this specific issue could bear. A *set*, because
                         Metron gives both ``cover_date`` and ``store_date``
                         and an issue shipping in November under a January
                         cover date is honestly labelled either way. Comparing
                         against one of them alone would reject the other.

    Returns one of the VERDICT_* constants; ``VERDICT_NONE`` means skip this
    candidate entirely.

    The rules, and why each is shaped the way it is:

    * **No year anywhere -> UNKNOWN, allowed.** Plenty of libraries have no
      year in the path and no ComicInfo (nothing tags CBR at all). Rejecting
      those would break matching that works today, so a yearless candidate is
      let through unchanged -- it simply earns no bonus.

    * **A known issue year is authoritative.** When we know when the issue came
      out, a candidate must carry that year -- or the volume's start year,
      which is what the very common ``/Batman (2025)/Batman 014.cbz`` layout
      puts in the folder while leaving the file yearless. Anything else is a
      different volume. Note the volume clause here is exact equality, NOT the
      floor below: without that, hunting the 1942 volume would happily accept a
      2026 file, since 2026 clears a 1942 floor.

    * **A volume year alone is a FLOOR, not a window.** An issue cannot predate
      its own volume, so an earlier year rejects -- which is precisely what
      kills the two bugs above. A *later* year must not, because a library
      filename usually carries the ISSUE year: issue #40 of a 2015 volume is
      honestly named "(2024)", nine years out, and a +/-1 window like the one
      in ``models.getcomics._score_year`` would throw it away. That window is
      right there and wrong here -- a GetComics post title names the volume,
      a filename on disk names the issue. One year of slack absorbs a volume
      whose first issues shipped the previous December.

    * **Any single matching year is enough.** Never all of them: a comic at
      ``/Batman (2025)/Batman 014 (2026).cbz`` legitimately carries the volume
      year in its folder and the issue year in its name, and an unrelated year
      elsewhere in the path (``/data/2024 Backups/...``) must never be able to
      force a rejection.
    """
    candidate_years = {y for y in (candidate_years or set()) if y}
    if not candidate_years:
        return VERDICT_UNKNOWN

    issue_years = {y for y in (issue_years or set()) if y}

    if issue_years:
        if candidate_years & issue_years:
            return VERDICT_ISSUE
        if volume_year and volume_year in candidate_years:
            return VERDICT_VOLUME
        return VERDICT_NONE

    if volume_year:
        if any(year >= volume_year - 1 for year in candidate_years):
            return VERDICT_VOLUME
        return VERDICT_NONE

    return VERDICT_UNKNOWN


def _as_year(value):
    """``value`` as a four-digit year, or None if it isn't one.

    Guards the slot confusion this module used to suffer from: Metron hands us
    ``series.volume`` (an ordinal -- 1, 2, 3) and ComicVine hands us a volume
    *id* (five digits), and both used to land in a parameter the CBL format
    fills with a year.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not re.fullmatch(r"(?:19|20)\d{2}", text):
        return None
    return int(text)


def _legacy_series(series):
    """The series as the pre-character-map matcher cleaned it: ':' -> ' -',
    other punctuation dropped. Kept as a fallback for files named under the
    old rules or by hand."""
    cleaned = re.sub(r'[^\w\s-]', '', (series or '').replace(':', ' -'))
    return ' '.join(cleaned.split())


def format_search_term(rename_pattern, series, number, volume, year, cleanup_cfg=None):
    """The rename pattern filled with what a reading-list entry knows, cleaned
    the way the renamer cleans a filename, so it is a substring of the file
    the renamer produced (#588).

    Tokens the entry cannot fill are dropped along with the separators they
    leave dangling, exactly as ``cbz_ops.rename.apply_custom_pattern`` does:
    "({issue_month_M}, {issue_year})" must vanish, not leave "(, )" behind.
    """
    padded_number = number.zfill(3) if number else ''

    search_term = rename_pattern or '{series_name} {issue_number}'
    search_term = search_term.replace('{series_name}', series or '')
    search_term = search_term.replace('{series}', series or '')
    search_term = search_term.replace('{issue_number}', padded_number)
    search_term = search_term.replace('{issue}', padded_number)
    search_term = search_term.replace('{volume}', volume or '')
    search_term = search_term.replace('{volume_year}', year or '')
    search_term = search_term.replace('{year}', year or '')
    search_term = search_term.replace('{start_year}', volume or year or '')

    search_term = re.sub(r'\{[^}]+\}', '', search_term)
    search_term = re.sub(r'\s+', ' ', search_term).strip()
    search_term = re.sub(r'\(\s*[-,]\s*', '(', search_term)
    search_term = re.sub(r'\s*[-,]\s*\)', ')', search_term)
    search_term = re.sub(r'\s*\(\s*\)', '', search_term).strip()
    search_term = re.sub(r'\s*-\s*(?=\(|$)', ' ', search_term).strip()

    if cleanup_cfg is None:
        cleanup_cfg = load_filename_cleanup_config()
    return apply_filename_cleanup(search_term, cleanup_cfg)


class CBLLoader:
    def __init__(self, file_content, filename=None, rename_pattern=None):
        self.root = SafeET.fromstring(file_content)
        self.books = []
        self.name = self.root.find('Name').text if self.root.find('Name') is not None else "Unknown Reading List"
        self.publisher = self._extract_publisher(filename)
        self.rename_pattern = rename_pattern or '{series_name} {issue_number}'
        # {metron issue id: [path, ...]} prefetched for a whole list at once.
        # find_files_by_metron_ids batches at 500 ids; resolving one id per
        # entry would open a connection per issue for no benefit.
        self.metron_id_map = {}
        # Loaded on first use: one read per loader, not per entry.
        self._cleanup_cfg = None

    def _cleanup(self):
        if self._cleanup_cfg is None:
            self._cleanup_cfg = load_filename_cleanup_config()
        return self._cleanup_cfg

    def prefetch_metron_ids(self, issue_ids):
        """Resolve a whole list's Metron issue ids to files in one pass."""
        wanted = [i for i in (issue_ids or []) if i]
        if not wanted:
            return
        try:
            from core.database import find_files_by_metron_ids
            self.metron_id_map = find_files_by_metron_ids(wanted) or {}
        except Exception as e:
            app_logger.debug(f"Metron id prefetch failed: {e}")
            self.metron_id_map = {}

    def _extract_publisher(self, filename):
        """Extract publisher from CBL filename like '[Marvel] (2021-09) Inferno.cbl'"""
        if not filename:
            return None
        match = re.search(r'\[([^\]]+)\]', filename)
        return match.group(1) if match else None

    def parse(self):
        """Parse the CBL content and extract books with matching (legacy method)."""
        entries = self.parse_entries()
        for entry in entries:
            entry['matched_file_path'] = self.match_file(
                entry['series'], entry['issue_number'], entry['volume'], entry['year']
            )
            self.books.append(entry)
        return self.books

    @staticmethod
    def entry_year_hints(entry):
        """The (volume_year, issue_years) a stored entry implies.

        A CBL Book carries ``Volume`` (the year the run began) and ``Year``
        (when this issue came out); ``issue_year`` is CLU's own column, set by
        importers that know the issue's cover/store date. Prefer it, and fall
        back to the CBL reading so a list imported before that column existed
        still gets a year to match on.
        """
        volume_year = _as_year(entry.get('volume'))
        issue_years = {y for y in (_as_year(entry.get('issue_year')),
                                   _as_year(entry.get('year'))) if y}
        if not volume_year:
            # Metron writes the volume start year into `year`; when there is no
            # separate issue year it is the only hint available.
            volume_year = _as_year(entry.get('year'))
        return volume_year, issue_years

    def parse_entries(self):
        """Parse the CBL content and extract book entries WITHOUT matching."""
        books_elem = self.root.find('Books')
        if books_elem is None:
            return []

        entries = []
        for book in books_elem.findall('Book'):
            entries.append({
                'series': book.get('Series'),
                'issue_number': book.get('Number'),
                'volume': book.get('Volume'),
                'year': book.get('Year'),
                'matched_file_path': None
            })
        return entries

    def _format_search_term(self, series, number, volume, year):
        """Format search term using the rename pattern."""
        return format_search_term(self.rename_pattern, series, number, volume,
                                  year, self._cleanup())

    def _year_hints(self, volume, year, volume_year=None, issue_years=None):
        """Normalise the caller's year vocabulary into (volume_year, issue_years).

        Explicit keywords win. The positional ``volume``/``year`` fall back to
        the CBL file's meaning -- ``Volume`` is the year the run began, ``Year``
        is when this issue came out -- which is what every existing caller and
        test assumes, and what keeps this signature backward compatible.
        """
        if volume_year is None:
            volume_year = _as_year(volume)
        else:
            volume_year = _as_year(volume_year)

        resolved = {y for y in (_as_year(y) for y in (issue_years or ())) if y}
        if not resolved:
            fallback = _as_year(year)
            if fallback:
                resolved = {fallback}
        return volume_year, resolved

    def _candidate_years(self, path, number, ci_year=None, ci_volume=None):
        """Every plausible year attached to a candidate file.

        The path is scanned whole -- a volume year lives in a folder at least
        as often as in a filename. ComicInfo contributes too: ``Year`` is the
        issue's own year, and ``Volume`` is a volume start year in most
        taggers but an ordinal in some, so it only counts when year-shaped.
        """
        years = years_in_path(path, issue_number=number)
        for value in (ci_year, ci_volume):
            found = _as_year(value)
            if found:
                years.add(found)
        return years

    def match_file(self, series, number, volume=None, year=None, *,
                   volume_year=None, issue_years=None, metron_id=None):
        """Attempt to match a book to a file in the library.

        Strategy:
        0. Exact identity via the Metron issue id, when the caller has one
        1. Metadata-first matching using ComicInfo.xml fields
        2. Fall back to filename pattern matching

        ``volume_year``/``issue_years`` let a caller state which year is which
        instead of relying on the CBL positional convention -- see
        ``_year_hints``. ``issue_years`` is a set; see ``year_verdict``.
        """
        if not series or not number:
            return None

        if metron_id:
            exact = self._match_by_metron_id(metron_id)
            if exact:
                return exact

        volume_year, issue_years = self._year_hints(
            volume, year, volume_year, issue_years
        )

        match = self._match_by_metadata(series, number, volume, year,
                                        volume_year, issue_years)
        if match:
            return match
        return self._match_by_filename(series, number, volume, year,
                                       volume_year, issue_years)

    def _match_by_metron_id(self, metron_id):
        """Exact match on the Metron issue id recorded in ComicInfo.

        Identity, not a heuristic: ``file_index.ci_metronid`` holds the very id
        the reading list is built from, so when it hits there is nothing to
        score. Only reachable for files tagged from Metron, hence a tier above
        the guesswork rather than a replacement for it.
        """
        try:
            key = int(metron_id)
        except (TypeError, ValueError):
            return None

        if self.metron_id_map:
            paths = self.metron_id_map.get(key)
            return sorted(paths)[0] if paths else None

        try:
            from core.database import find_files_by_metron_ids
            found = find_files_by_metron_ids([key])
        except Exception as e:
            app_logger.debug(f"Metron id lookup failed for {metron_id}: {e}")
            return None
        paths = found.get(key)
        return sorted(paths)[0] if paths else None

    def _match_by_metadata(self, series, number, volume, year,
                           volume_year=None, issue_years=None):
        """Match using ComicInfo.xml metadata columns in file_index."""
        # The candidate set is series + issue number only, so for a character
        # with many volumes it is mostly wrong answers. It used to be capped at
        # 20 rows ordered by name, which could truncate the RIGHT file out
        # before scoring ever saw it; widen it whenever we have a year to
        # discriminate with.
        limit = 200 if (volume_year or issue_years) else 20
        results = search_by_comic_metadata(
            series, number, volume=volume, year=year,
            publisher=self.publisher, limit=limit
        )

        if not results:
            return None

        best_match = None
        best_score = 0

        for res in results:
            path = (res.get('path') or '')

            candidate_years = self._candidate_years(
                path, number,
                ci_year=res.get('ci_year'), ci_volume=res.get('ci_volume')
            )
            verdict = year_verdict(candidate_years, volume_year, issue_years)
            if verdict == VERDICT_NONE:
                # Wrong volume. Skipping beats scoring it down: with nothing
                # else in the library this entry stays unmatched and says so.
                continue

            score = 10  # Base score for series + number match
            score += VERDICT_SCORES[verdict]

            ci_series = (res.get('ci_series') or '').lower()
            ci_volume = res.get('ci_volume') or ''
            ci_publisher = (res.get('ci_publisher') or '').lower()
            path_lower = path.lower().replace('\\', '/')

            # Normalize colons and dashes for comparison so
            # "Batman: Legends" / "Batman - Legends" match "Batman Legends"
            ci_series_norm = ' '.join(ci_series.replace(':', ' ').replace('-', ' ').split())
            series_norm = ' '.join(series.lower().replace(':', ' ').replace('-', ' ').split())

            # Series identity. A plain substring test let "Absolute Batman"
            # satisfy "Batman"; series_names_compatible is the same asymmetric
            # rule the wanted-issue matcher already uses -- a shorter name is a
            # library spelling a fuller one, a LONGER one has to earn it.
            if not series_names_compatible(ci_series_norm, series_norm):
                continue

            # Exact series name match (dash-normalized)
            if ci_series_norm == series_norm:
                score += 15
            else:
                score += 5

            # Volume match. Only meaningful when both sides are the same KIND
            # of value -- ComicInfo Volume is a start year in most taggers and
            # an ordinal in others, so compare literally and let the year
            # verdict above carry the year half.
            if volume and ci_volume and str(ci_volume) == str(volume):
                score += 15

            # Publisher match via metadata
            if self.publisher and ci_publisher and self.publisher.lower() in ci_publisher:
                score += 10
            # Publisher match via path
            elif self.publisher and self.publisher.lower() in path_lower:
                score += 5

            if score > best_score:
                best_score = score
                best_match = res['path']

        return best_match

    def _match_by_filename(self, series, number, volume, year,
                           volume_year=None, issue_years=None):
        """Match using filename patterns and path scoring (fallback)."""
        cfg = self._cleanup()
        # Names as the renamer writes them, through the user's character map.
        named_series = apply_filename_cleanup(series, cfg)
        padded_3 = number.zfill(3)  # "18" -> "018"

        # Build search patterns - prioritize pattern-based search
        results = []
        search_patterns = []

        # First try the rename pattern format (most likely to match)
        pattern_search = self._format_search_term(series, number, volume, year)
        if pattern_search:
            search_patterns.append(pattern_search)

        # Fallback patterns
        search_patterns.extend(apply_filename_cleanup(p, cfg) for p in (
            f"{series} {padded_3}",         # "Avengers 018"
            f"{series} {number}",           # "Avengers 18"
            f"{series} #{padded_3}",        # "Avengers #018"
            f"{series} #{number}",          # "Avengers #18"
        ))

        # Files named under the old rules (':' -> ' -') or by hand
        legacy_series = _legacy_series(series)
        search_patterns.extend([
            f"{legacy_series} {padded_3}",
            f"{legacy_series} {number}",
        ])

        # Dash-stripped patterns for cases like "Batman - Legends" -> "Batman Legends"
        no_dash_series = ' '.join(legacy_series.replace('-', ' ').split())
        if no_dash_series != legacy_series:
            search_patterns.extend([
                f"{no_dash_series} {padded_3}",
                f"{no_dash_series} {number}",
            ])

        # Remove duplicates while preserving order
        search_patterns = list(dict.fromkeys(search_patterns))

        for pattern in search_patterns:
            results = search_file_index(pattern, limit=20)
            if results:
                break

        # If no results, try without first word (e.g., "The Flash" -> "Flash")
        if not results:
            words = named_series.split()
            if len(words) > 1:
                alt_series = ' '.join(words[1:])
                alt_patterns = [
                    f"{alt_series} {padded_3}",
                    f"{alt_series} {number}",
                ]
                for pattern in alt_patterns:
                    results = search_file_index(pattern, limit=20)
                    if results:
                        break

        if not results:
            # Try looser search with just series
            for loose in dict.fromkeys((named_series, legacy_series)):
                results = search_file_index(loose, limit=100)
                if results:
                    break

        if not results:
            return None

        # Score and rank results - MUST have issue number match
        best_match = None
        best_score = 0

        # Only a four-digit value is a year. Metron hands us series.volume (an
        # ordinal -- 1, 2, 3) and ComicVine a volume id, and testing those
        # against the path used to award a bogus bonus to any path containing
        # the digit.
        volume_token = _as_year(volume)

        for res in results:
            path = res['path'].lower().replace('\\', '/')
            filename = res['name'].lower()

            # REQUIRED: Check issue number in filename (avoid matching #1 in #10, or 19 in 2019)
            padded_2 = number.zfill(2)
            padded_3 = number.zfill(3)
            issue_pattern = rf'(?:^|[^\d])#?\s*(?:0*{re.escape(number)}|{re.escape(padded_2)}|{re.escape(padded_3)})(?:[^\d]|$)'
            if not re.search(issue_pattern, filename):
                continue  # Skip files that don't have the correct issue number

            # search_file_index matches on the NAME only and returns no
            # ComicInfo, so the path is the one place a year can be found here.
            verdict = year_verdict(
                self._candidate_years(res['path'], number),
                volume_year, issue_years
            )
            if verdict == VERDICT_NONE:
                continue  # Different volume -- see year_verdict.

            score = 10  # Base score for having correct issue number
            score += VERDICT_SCORES[verdict]

            # Check publisher in path
            if self.publisher and self.publisher.lower() in path:
                score += 10

            # Series must START the filename, not merely appear in the path:
            # "Absolute Batman 014" contains "Batman" but is another series.
            if not filename_series_matches(res['name'], series):
                continue
            score += 5

            # Check volume - two possible formats:
            if volume_token:
                # Format 1: /vVolume/ subfolder (e.g., /v2021/)
                if f"/v{volume_token}/" in path:
                    score += 15
                # Format 2: Series (Volume) folder (e.g., /Inferno (2021)/)
                elif f"({volume_token})" in path:
                    score += 15

            if score > best_score:
                best_score = score
                best_match = res['path']

        return best_match
