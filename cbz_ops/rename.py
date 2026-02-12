import sys
import os
import re
import configparser
from app_logging import app_logger
from helpers import is_hidden
from config import config

# -------------------------------------------------------------------
#  Pattern for Volume + Issue, e.g.:
#   "Comic Name v3 051 (2018) (DCP-Scan Final).cbz"
#   "2000AD v1 1795 (2018).cbz" (4-digit issue numbers for 2000AD)
#   Group(1) => "Comic Name"
#   Group(2) => "v3"
#   Group(3) => "051" or "1795"
#   Group(4) => " (2018) (DCP-Scan Final)"
#   Group(5) => ".cbz"
# -------------------------------------------------------------------
VOLUME_ISSUE_PATTERN = re.compile(
    r'^(.*?)\s+(v\d{1,3})\s+(\d{1,4})(.*)(\.\w+)$',
    re.IGNORECASE
)

# -------------------------------------------------------------------
# Pattern for Volume + Subtitle (no issue number), e.g.:
#   "Infinity 8 v03 - The Gospel According to Emma (2019).cbr"
#   Group(1) => "Infinity 8"
#   Group(2) => "v03"
#   Group(3) => " - The Gospel According to Emma (2019)"
#   Group(4) => ".cbr"
# -------------------------------------------------------------------
VOLUME_SUBTITLE_PATTERN = re.compile(
    r'^(.*?)\s+(v\d{1,3})\s+(-\s*[^-]+.*?)(\.\w+)$',
    re.IGNORECASE
)

# -------------------------------------------------------------------
# Pattern for just "Title YEAR anything.ext"
# e.g. "Hulk vs. The Marvel Universe 2008 Digital4K.cbz" → "Hulk vs. The Marvel Universe (2008).cbz"
# -------------------------------------------------------------------
TITLE_YEAR_PATTERN = re.compile(
    r'^(.*?)\b((?:19|20)\d{2})\b(.*)(\.\w+)$',
    re.IGNORECASE
)

# -------------------------------------------------------------------
# Pattern for explicit hash‐issue notation, e.g.:
#   "Title 2 #10 (2018).cbz"
#   "2000AD #1795 (2018).cbz" (4-digit issue numbers for 2000AD)
#   Group(1) ⇒ "Title 2"
#   Group(2) ⇒ "10" or "1795"
#   Group(3) ⇒ " (2018)"
#   Group(4) ⇒ ".cbz"
# -------------------------------------------------------------------
ISSUE_HASH_PATTERN = re.compile(
    r'^(.*?)\s*#\s*(\d{1,4})\b(.*)(\.\w+)$',
    re.IGNORECASE
)

# -------------------------------------------------------------------
# Original ISSUE_PATTERN:
#   Title + space + (v## or up to 4 digits) + (middle) + extension
#   e.g. "Comic Name 051 (2018).cbz"  or  "Comic Name v3 (2022).cbr"
#   e.g. "2000AD 1795.cbz" (4-digit issue numbers for 2000AD)
# -------------------------------------------------------------------
ISSUE_PATTERN = re.compile(
    r'^(.*?)\s+((?:v\d{1,3})|(?:\d{1,4}))\b(.*)(\.\w+)$',
    re.IGNORECASE
)

# -------------------------------------------------------------------
# New pattern for cases where the issue number comes after the year.
# e.g. "
#  (digital) (Colecionadores.GO).cbz"
# e.g. "2000AD (2018) #1795 (digital).cbz" (4-digit issue numbers for 2000AD)
#   Group(1) => Title (e.g. "Spider-Man 2099")
#   Group(2) => Year (e.g. "1992")
#   Group(3) => Issue number (e.g. "#44" or "#1795")
#   Group(4) => Extra text (ignored)
#   Group(5) => Extension (e.g. ".cbz")
# -------------------------------------------------------------------
ISSUE_AFTER_YEAR_PATTERN = re.compile(
    r'^(.*?)\s*\((\d{4})\)\s*(#\d{1,4})(.*)(\.\w+)$',
    re.IGNORECASE
)

# -------------------------------------------------------------------
# Pattern for series-number + issue-number with no “v” or “#”
# e.g. "Injustice 2 001 (2018).cbz"
#   Group(1) ⇒ "Injustice"
#   Group(2) ⇒ "2"
#   Group(3) ⇒ "001"
#   Group(4) ⇒ " (2018)"
#   Group(5) ⇒ ".cbz"
# -------------------------------------------------------------------
SERIES_ISSUE_PATTERN = re.compile(
    r'^(.*?)\s+(\d{1,3})\s+(\d{1,3})\b(.*)(\.\w+)$',
    re.IGNORECASE
)

# -------------------------------------------------------------------
# Fallback for Title (YYYY) anything .ext
# e.g. "Comic Name (2018) some extra.cbz" -> "Comic Name (2018).cbz"
# -------------------------------------------------------------------
FALLBACK_PATTERN = re.compile(
    r'^(.*?)\((\d{4})\)(.*)(\.\w+)$',
    re.IGNORECASE
)

# -------------------------------------------------------------------
# Pattern for issue number + year in parentheses, e.g.:
#   "Leonard Nimoy's Primortals (00 1996).cbz"
#   Group(1) => "Leonard Nimoy's Primortals"
#   Group(2) => "00"
#   Group(3) => "1996"
#   Group(4) => ".cbz"
# -------------------------------------------------------------------
ISSUE_YEAR_PARENTHESES_PATTERN = re.compile(
    r'^(.*?)\s*\((\d{1,3})\s+(\d{4})\)(.*)(\.\w+)$',
    re.IGNORECASE
)

# -------------------------------------------------------------------
# Pattern for Title, YYYY-MM-DD (NN) format, e.g.:
#   "Justice League Europe, 1990-02-00 ( 13) (digital) (OkC.O.M.P.U.T.O.-Novus-HD).cbz"
#   "Blue Devil, 1984-04-00 (_01) (digital) (Glorith-Novus-HD).cbz"
#   Group(1) => "Justice League Europe" or "Blue Devil"
#   Group(2) => "1990" or "1984"
#   Group(3) => "13" or "_01"
#   Group(4) => " (digital) (OkC.O.M.P.U.T.O.-Novus-HD)" or " (digital) (Glorith-Novus-HD)"
#   Group(5) => ".cbz"
# -------------------------------------------------------------------
TITLE_COMMA_YEAR_ISSUE_PATTERN = re.compile(
    r'^(.*?),\s*(\d{4})-\d{2}-\d{2}\s*\(\s*(_?\d{1,3})\s*\)(.*)(\.\w+)$',
    re.IGNORECASE
)

# -------------------------------------------------------------------
# Pattern for Title, YYYY-MM-DD (#NN) format, e.g.:
#   "Legion of Super-Heroes, 1985-07-00 (#14) (digital) (Glorith-Novus-HD).cbz"
#   Group(1) => "Legion of Super-Heroes"
#   Group(2) => "1985"
#   Group(3) => "#14"
#   Group(4) => " (digital) (Glorith-Novus-HD)"
#   Group(5) => ".cbz"
# -------------------------------------------------------------------
TITLE_COMMA_YEAR_HASH_ISSUE_PATTERN = re.compile(
    r'^(.*?),\s*(\d{4})-\d{2}-\d{2}\s*\(\s*(#\d{1,4})\s*\)(.*)(\.\w+)$',
    re.IGNORECASE
)

# -------------------------------------------------------------------
# Pattern for YYYYMM Series Name v# ### format, e.g.:
#   "199309 Hokum & Hex v1 001.cbz"
#   Group(1) => "199309" (year + month)
#   Group(2) => "Hokum & Hex" (series name)
#   Group(3) => "v1" (volume)
#   Group(4) => "001" (issue number)
#   Group(5) => ".cbz" (extension)
# -------------------------------------------------------------------
YEAR_MONTH_SERIES_VOLUME_ISSUE_PATTERN = re.compile(
    r'^(\d{6})\s+(.*?)\s+(v\d{1,3})\s+(\d{1,4})(\.\w+)$',
    re.IGNORECASE
)

# -------------------------------------------------------------------
# Pattern for Series Name YYYY-MM ( NN) (YYYY) format, e.g.:
#   "Mister Miracle 1989-08 ( 08) (1989) (Digital) (Shadowcat-Empire).cbz"
#   Group(1) => "Mister Miracle" (series name)
#   Group(2) => "1989" (year from YYYY-MM)
#   Group(3) => "08" (issue number)
#   Group(4) => " (Digital) (Shadowcat-Empire)" (extra info)
#   Group(5) => ".cbz" (extension)
# -------------------------------------------------------------------
SERIES_YEAR_MONTH_ISSUE_PATTERN = re.compile(
    r'^(.*?)\s+(\d{4})-\d{2}\s*\(\s*(\d{1,3})\s*\)\s*\(\d{4}\)(.*)(\.\w+)$',
    re.IGNORECASE
)

# -------------------------------------------------------------------
# Pattern for Series Name YYYY-MM-DD ( NN) (digital) format, e.g.:
#   "Mister Miracle 1990-09-18 ( 21) (digital) (Glorith-Novus-HD).cbz"
#   Group(1) => "Mister Miracle" (series name)
#   Group(2) => "1990" (year from YYYY-MM-DD)
#   Group(3) => "21" (issue number)
#   Group(4) => " (digital) (Glorith-Novus-HD)" (extra info)
#   Group(5) => ".cbz" (extension)
# -------------------------------------------------------------------
SERIES_YEAR_MONTH_DAY_ISSUE_PATTERN = re.compile(
    r'^(.*?)\s+(\d{4})-\d{2}-\d{2}\s*\(\s*(\d{1,3})\s*\)(.*)(\.\w+)$',
    re.IGNORECASE
)

# Pattern: "Series (YYYY-MM) ### (...)" → extract series, year, issue
SERIES_YEAR_MONTH_SIMPLE_PATTERN = re.compile(
    r'^(.*?)\s*\((\d{4})-\d{2}\)\s+(\d{1,4})(.*)(\.\w+)$',
    re.IGNORECASE
)

# Pattern: "Series International/Annual v# ### (YYYY)" → extract series, volume, year, issue
SERIES_INTERNATIONAL_ANNUAL_PATTERN = re.compile(
    r'^(.*?(?:International|Annual).*?)\s+(v\d+\s+)?(Annual\s+)?(\d{1,4})\s*\((\d{4})\)(.*)(\.\w+)$',
    re.IGNORECASE
)

# Pattern: "Series # ### (YYYY)" → extract series with number, year, issue
SERIES_NUMBER_ISSUE_YEAR_PATTERN = re.compile(
    r'^(.*\s+\d+)(?!\s+v\d)\s+(\d{1,4})\s*\((\d{4})\)(.*)(\.\w+)$',
    re.IGNORECASE
)

# ====== BEGIN: Rule Engine Helpers ======
def smart_title_case(text: str) -> str:
    """
    Convert text to title case with special handling for articles and conjunctions.
    Rules:
    - Always capitalize the first word
    - Don't capitalize: a, an, and, of, if, the (unless they're the first word)
    - Capitalize all other words
    """
    if not text:
        return text

    # Words that should stay lowercase (unless first word)
    lowercase_words = {'a', 'an', 'and', 'of', 'if', 'the'}

    words = text.split()
    if not words:
        return text

    result = []
    for i, word in enumerate(words):
        # Always capitalize the first word
        if i == 0:
            result.append(word.capitalize())
        # Check if word (lowercase) is in our exceptions list
        elif word.lower() in lowercase_words:
            result.append(word.lower())
        # Capitalize all other words
        else:
            result.append(word.capitalize())

    return ' '.join(result)

def _apply_filters(val: str, filters: list[str]) -> str:
    for f in filters:
        if f == "digits":
            val = re.sub(r'\D+', '', val or '')
        elif f == "year4":
            # Extract first 4 digits (year from YYYYMM)
            val = re.sub(r'\D+', '', val or '')
            val = val[:4] if len(val) >= 4 else val
        elif f == "pad3":
            val = f"{int(val):03d}" if val else val
        elif f == "pad4":
            val = f"{int(val):04d}" if val else val
        elif f == "upper":
            val = (val or '').upper()
        elif f == "lower":
            val = (val or '').lower()
        elif f == "title":
            val = smart_title_case(val or '')
    return val

def _format_from_groups(fmt: str, groups: dict[str, str]) -> str:
    def repl(m):
        spec = m.group(1)  # e.g. issue|digits|pad3
        parts = spec.split("|")
        key, filters = parts[0], parts[1:]
        val = groups.get(key, "")
        return _apply_filters(val, filters)
    return re.sub(r"\{([^{}]+)\}", repl, fmt).strip()

def try_rule_engine(filename: str, cfg_path="/config/rename_rules.ini"):
    # Split ext safely in case rules don't capture it
    m = re.match(r"^(.*)(\.\w+)$", filename)
    if not m:
        base, ext = filename, ""
    else:
        base, ext = m.group(1), m.group(2)

    if not os.path.exists(cfg_path):
        return None

    cp = configparser.ConfigParser()
    try:
        cp.read(cfg_path)
    except Exception:
        return None

    if "RENAME" not in cp:
        return None

    rules = []
    for key in cp["RENAME"]:
        if key.endswith(".pattern"):
            name = key[:-8]  # strip ".pattern"
            pattern = cp["RENAME"][key]
            output  = cp["RENAME"].get(f"{name}.output", "{series} {issue|pad3} ({year}){ext}")
            prio    = int(cp["RENAME"].get(f"{name}.priority", "100"))
            try:
                rx = re.compile(pattern, re.IGNORECASE)
            except re.error:
                continue
            rules.append((prio, name, rx, output))

    rules.sort(reverse=True)  # highest prio first

    for prio, name, rx, outfmt in rules:
        m = rx.match(filename) or rx.match(base)
        if not m:
            continue
        app_logger.info(f"Rule {name} (priority {prio}) matched: {filename}")
        g = m.groupdict()

        # ensure ext available
        g.setdefault("ext", g.get("extension", ext))

        # normalize common fields
        if "series" in g:
            g["series"] = smart_title_case(g["series"].replace("_", " ").strip())
        if "series_name" in g and "series" not in g:
            g["series"] = smart_title_case(g["series_name"].replace("_", " ").strip())

        # allow deriving year from YYYYMM token if present
        if "yearmonth" in g and not g.get("year"):
            ym_digits = re.sub(r"\D+", "", g["yearmonth"] or "")
            if len(ym_digits) >= 4:
                g["year"] = ym_digits[:4]

        new_name = _format_from_groups(outfmt, g)
        ext = g.get("ext", "")
        if ext and not new_name.endswith(ext):
            new_name += ext
        return new_name

    return None
# ====== END: Rule Engine Helpers ======


# -------------------------------------------------------------------
# Custom Rename Pattern Support
# -------------------------------------------------------------------

def norm_issue(s):
    s = re.sub(r'\D+', '', s or '')
    return f"{int(s):03d}" if s else ''

def clean_final_filename(filename):
    """
    Final cleanup of filename to remove empty parentheses and extra spaces.
    """
    if not filename:
        return filename
    # Remove empty parentheses and space before them
    filename = re.sub(r'\s*\(\s*\)', '', filename).strip()
    # Clean up any double spaces
    filename = re.sub(r'\s+', ' ', filename).strip()
    return filename

def load_custom_rename_config():
    """
    Load custom rename pattern configuration from the config module.
    Returns tuple: (enabled, pattern)
    """
    try:
        # Ensure config is loaded
        if not config.sections():
            from config import load_config
            load_config()

        # Use the centralized config module which auto-reloads from config.ini
        # config is a ConfigParser object, not a dict
        if "SETTINGS" in config:
            enabled = config.getboolean("SETTINGS", "ENABLE_CUSTOM_RENAME", fallback=False)
            pattern = config.get("SETTINGS", "CUSTOM_RENAME_PATTERN", fallback="")
            app_logger.info(f"Loaded custom rename config: enabled={enabled}, pattern={pattern}")
            return enabled, pattern
        else:
            app_logger.warning("SETTINGS section not found in config")
            return False, ""
    except Exception as e:
        app_logger.warning(f"Failed to load custom rename config: {e}")
        import traceback
        app_logger.warning(traceback.format_exc())
        return False, ""


def extract_comic_values(filename):
    """
    Extract comic values from filename using existing regex patterns.
    Returns a dictionary with keys: series_name, volume_number, year, issue_number
    Missing values will be empty strings.
    """
    values = {
        'series_name': '',
        'volume_number': '',
        'year': '',
        'issue_number': ''
    }

    # Handle "Series (YYYY) Volume ## Issue ###" format
    # e.g., "Top 10 (1999) Volume 01 Issue 010.cbz"
    volume_issue_keyword_match = re.match(
        r'^(?P<series>.*?)\s*\((?P<year>\d{4})\)\s*Volume\s*(?P<volume>\d+)\s*Issue\s*(?P<issue>\d+).*?(?P<ext>\.\w+)?$',
        filename,
        re.IGNORECASE
    )
    if volume_issue_keyword_match:
        series_name = volume_issue_keyword_match.group('series')
        volume_num = volume_issue_keyword_match.group('volume')
        issue_num = volume_issue_keyword_match.group('issue')
        year = volume_issue_keyword_match.group('year')

        values['series_name'] = smart_title_case(series_name.strip())
        values['volume_number'] = f"v{int(volume_num):02d}"
        values['issue_number'] = f"{int(issue_num):03d}"
        values['year'] = year
        app_logger.info(f"Matched Volume/Issue keyword pattern: series={values['series_name']}, volume={values['volume_number']}, issue={values['issue_number']}, year={values['year']}")
        return values

    # Handle "Series (YYYY) Issue ###" format (no Volume keyword)
    # e.g., "The Amazing Spider-Man (2018) Issue 080.BEY.cbz"
    # e.g., "The Amazing Spider-Man (1999) Issue 700.1.cbz"
    issue_keyword_match = re.match(
        r'^(?P<series>.*?)\s*\((?P<year>\d{4})\)\s*[Ii]ssue\s*(?P<issue>\d+(?:\.\w+)?)(?P<extra>.*?)(?P<ext>\.\w+)$',
        filename,
        re.IGNORECASE
    )
    if issue_keyword_match:
        series_name = issue_keyword_match.group('series')
        issue_num = issue_keyword_match.group('issue')
        year = issue_keyword_match.group('year')

        values['series_name'] = smart_title_case(series_name.strip())
        if '.' in issue_num:
            parts = issue_num.split('.', 1)
            values['issue_number'] = f"{int(parts[0]):03d}.{parts[1]}"
        else:
            values['issue_number'] = f"{int(issue_num):03d}"
        values['year'] = year
        app_logger.info(f"Matched Issue keyword pattern: series={values['series_name']}, issue={values['issue_number']}, year={values['year']}")
        return values

    # NEW: Handle "Series_###_YYYY_ExtraInfo.ext" format with underscores
    # e.g., "Batman_-_Superman_-_Worlds_Finest_045_2025_Webrip_The_Last_Kryptonian-DCP.cbr"
    underscore_series_issue_year_match = re.match(
        r'^(?P<series>.+?)_(?P<issue>\d{3,4})_(?P<year>\d{4})_.*?(?P<ext>\.\w+)$',
        filename,
        re.IGNORECASE
    )
    if underscore_series_issue_year_match:
        series_name = underscore_series_issue_year_match.group('series')
        issue_num = underscore_series_issue_year_match.group('issue')
        year = underscore_series_issue_year_match.group('year')

        # Replace _-_ with space-hyphen-space, then replace remaining underscores with spaces
        clean_series = series_name.replace('_-_', ' - ').replace('_', ' ')
        values['series_name'] = smart_title_case(clean_series.strip())
        values['issue_number'] = f"{int(issue_num):03d}"  # Zero-pad to 3 digits
        values['year'] = year
        app_logger.info(f"Matched underscore pattern: series={values['series_name']}, issue={values['issue_number']}, year={values['year']}")
        return values

    # First, try to match "Series ### extra_info YYYY" format (e.g., "Batman 046 52p ctc 04-05 1948.cbz")
    series_issue_extra_year_match = re.match(
        r'^(?P<series>.*?)\s+(?P<issue>\d{1,4})\s+.*?\s+(?P<year>\d{4})(?P<ext>\.\w+)?$',
        filename,
        re.IGNORECASE
    )
    if series_issue_extra_year_match:
        series_name = series_issue_extra_year_match.group('series')
        issue_num = series_issue_extra_year_match.group('issue')
        year = series_issue_extra_year_match.group('year')

        values['series_name'] = smart_title_case(series_name.replace('_', ' ').strip())
        values['issue_number'] = f"{int(issue_num):03d}"  # Zero-pad to 3 digits
        values['year'] = year
        return values

    # Next, try to match International/Annual patterns (highest priority)
    international_annual_match = SERIES_INTERNATIONAL_ANNUAL_PATTERN.match(filename)
    if international_annual_match:
        series_name, volume, annual, issue_num, year, extra, extension = international_annual_match.groups()

        values['series_name'] = smart_title_case(series_name.replace('_', ' ').strip())
        values['volume_number'] = (volume or '').strip()
        values['year'] = year
        values['issue_number'] = f"{int(issue_num):03d}"  # Zero-pad to 3 digits
        return values

    # Try to match Series # ### (YYYY) format (e.g., "Lady Killer 2 001 (2016)")
    series_number_issue_year_match = SERIES_NUMBER_ISSUE_YEAR_PATTERN.match(filename)
    if series_number_issue_year_match:
        series_name, issue_num, year, extra, extension = series_number_issue_year_match.groups()

        values['series_name'] = smart_title_case(series_name.replace('_', ' ').strip())
        values['year'] = year
        values['issue_number'] = f"{int(issue_num):03d}"  # Zero-pad to 3 digits
        return values

    # Try to match "Series v# ### (YYYYMM)" format (e.g., "Astonishing v1 063 (195708).cbz")
    series_volume_issue_yearmonth_match = re.match(
        r'^(.*?)\s+(v\d{1,3})\s+(\d{1,4})\s+\((\d{6})\).*(\.\w+)?$',
        filename,
        re.IGNORECASE
    )
    if series_volume_issue_yearmonth_match:
        series_name, volume, issue_num, yearmonth, extension = series_volume_issue_yearmonth_match.groups()
        values['series_name'] = smart_title_case(series_name.replace('_', ' ').strip())
        values['volume_number'] = volume.strip()
        values['issue_number'] = f"{int(issue_num):03d}"  # Zero-pad to 3 digits
        values['year'] = yearmonth[:4]  # Extract YYYY from YYYYMM
        return values

    # Try to match "YYYYMM Series ### ()" format (e.g., "195200 Astonishing 011 ().cbz")
    yearmonth_series_issue_empty_match = re.match(
        r'^(\d{6})\s+(.*?)\s+(\d{1,4})\s+\(\s*\).*(\.\w+)?$',
        filename,
        re.IGNORECASE
    )
    if yearmonth_series_issue_empty_match:
        yearmonth, series_name, issue_num, extension = yearmonth_series_issue_empty_match.groups()
        values['series_name'] = smart_title_case(series_name.replace('_', ' ').strip())
        values['issue_number'] = f"{int(issue_num):03d}"  # Zero-pad to 3 digits
        values['year'] = yearmonth[:4]  # Extract YYYY from YYYYMM
        return values

    # Try to match "YYYYMM Series ###" format without parentheses (e.g., "195203 Astonishing 010.cbz")
    yearmonth_series_issue_match = re.match(
        r'^(\d{6})\s+(.*?)\s+(\d{1,4})(\.\w+)?$',
        filename,
        re.IGNORECASE
    )
    if yearmonth_series_issue_match:
        yearmonth, series_name, issue_num, extension = yearmonth_series_issue_match.groups()
        # Make sure the series name doesn't look like just numbers or a pattern indicator
        if series_name and not re.match(r'^\d+$', series_name):
            values['series_name'] = smart_title_case(series_name.replace('_', ' ').strip())
            values['issue_number'] = f"{int(issue_num):03d}"  # Zero-pad to 3 digits
            values['year'] = yearmonth[:4]  # Extract YYYY from YYYYMM
            return values

    # Try to match the YYYYMM Series Name v# ### format (e.g., "199309 Hokum & Hex v1 001.cbz")
    year_month_series_volume_issue_match = YEAR_MONTH_SERIES_VOLUME_ISSUE_PATTERN.match(filename)
    if year_month_series_volume_issue_match:
        year_month, series_name, volume, issue_num, extension = year_month_series_volume_issue_match.groups()

        # Extract year from the first 4 digits of year_month
        values['year'] = year_month[:4]
        values['series_name'] = smart_title_case(series_name.replace('_', ' ').strip())
        values['volume_number'] = volume.strip()
        values['issue_number'] = f"{int(issue_num):03d}"  # Zero-pad to 3 digits
        return values

    # Try to match the Series Name YYYY-MM ( NN) (YYYY) format
    series_year_month_issue_match = SERIES_YEAR_MONTH_ISSUE_PATTERN.match(filename)
    if series_year_month_issue_match:
        series_name, year, issue_num, extra, extension = series_year_month_issue_match.groups()

        values['series_name'] = smart_title_case(series_name.replace('_', ' ').strip())
        values['year'] = year
        values['issue_number'] = f"{int(issue_num):03d}"  # Zero-pad to 3 digits
        return values

    # Try to match the Series Name YYYY-MM-DD ( NN) (digital) format
    series_year_month_day_issue_match = SERIES_YEAR_MONTH_DAY_ISSUE_PATTERN.match(filename)
    if series_year_month_day_issue_match:
        series_name, year, issue_num, extra, extension = series_year_month_day_issue_match.groups()

        values['series_name'] = smart_title_case(series_name.replace('_', ' ').strip())
        values['year'] = year
        values['issue_number'] = f"{int(issue_num):03d}"  # Zero-pad to 3 digits
        return values

    # Try to match the Series (YYYY-MM) ### format
    series_year_month_simple_match = SERIES_YEAR_MONTH_SIMPLE_PATTERN.match(filename)
    if series_year_month_simple_match:
        series_name, year, issue_num, extra, extension = series_year_month_simple_match.groups()

        values['series_name'] = smart_title_case(series_name.replace('_', ' ').strip())
        values['year'] = year
        values['issue_number'] = f"{int(issue_num):03d}"  # Zero-pad to 3 digits
        return values

    # Try to match the Title, YYYY-MM-DD (#NN) format
    title_comma_year_hash_issue_match = TITLE_COMMA_YEAR_HASH_ISSUE_PATTERN.match(filename)
    if title_comma_year_hash_issue_match:
        raw_title, year, hash_issue_num, extra, extension = title_comma_year_hash_issue_match.groups()
        values['year'] = year
        values['series_name'] = smart_title_case(raw_title.replace('_', ' ').strip())
        # Remove the # prefix and zero-pad to 3 digits
        issue_num = hash_issue_num[1:]  # Remove the # prefix
        values['issue_number'] = f"{int(issue_num):03d}"
        return values

    # Try to match the Title, YYYY-MM-DD (NN) format
    title_comma_year_issue_match = TITLE_COMMA_YEAR_ISSUE_PATTERN.match(filename)
    if title_comma_year_issue_match:
        raw_title, year, issue_num, extra, extension = title_comma_year_issue_match.groups()
        values['year'] = year
        values['series_name'] = smart_title_case(raw_title.replace('_', ' ').strip())
        # Handle issue numbers that may have underscore prefixes and zero-pad to 3 digits
        if issue_num.startswith('_'):
            numeric_part = issue_num[1:]  # Remove the underscore
            values['issue_number'] = f"{int(numeric_part):03d}"
        else:
            values['issue_number'] = f"{int(issue_num):03d}"
        return values
    
    # Extract year from parentheses (most reliable fallback)
    year_match = re.search(r'\((\d{4})\)', filename)
    if year_match:
        values['year'] = year_match.group(1)
    
    # Try to extract issue number from various patterns
    # Look for patterns like "v2 044", "#044", "044", etc.
    issue_patterns = [
        r'v\d{1,3}\s+(\d{1,4})',  # v2 044
        r'#(\d{1,4})',             # #044
        r'\b(\d{1,4})\s*\(',       # 044 (
        r'\b(\d{1,4})\s*\[',       # 044 [
        r'\b(\d{1,4})\s*$',        # 044 at end
    ]
    
    for pattern in issue_patterns:
        match = re.search(pattern, filename)
        if match:
            # Zero-pad the issue number to 3 digits
            issue_num = match.group(1)
            values['issue_number'] = f"{int(issue_num):03d}"
            break
    
    # If no issue number found with patterns, try to find any 4-digit number that's not a year
    if not values['issue_number']:
        # Look for 4-digit numbers that aren't years (not in parentheses)
        all_numbers = re.findall(r'\b(\d{4})\b', filename)
        for num in all_numbers:
            # Check if this number is not in parentheses (i.e., not a year)
            num_pos = filename.find(num)
            # Look backwards and forwards to see if it's in parentheses
            before = filename[:num_pos]
            after = filename[num_pos + 4:]
            
            # If it's not surrounded by parentheses, it might be an issue number
            if not (before.rstrip().endswith('(') and after.lstrip().startswith(')')):
                # Zero-pad the issue number to 3 digits
                values['issue_number'] = f"{int(num):03d}"
                break
    
    # Extract series name (everything before the issue number)
    if values['issue_number']:
        # Find the position of the issue number
        issue_pos = filename.find(values['issue_number'])
        if issue_pos > 0:
            series_part = filename[:issue_pos].strip()
            # Clean up the series name
            series_part = re.sub(r'[\(\)\[\]]', '', series_part).strip()
            values['series_name'] = series_part
        else:
            # If we can't find the issue number position, try a different approach
            # Look for the pattern: "Series Name ### (YYYY)"
            series_pattern = r'^(.*?)\s+\d{1,4}\s*\(\d{4}\)'
            series_match = re.match(series_pattern, filename)
            if series_match:
                values['series_name'] = series_match.group(1).strip()
    
    # Extract volume number (look for v1, v2, etc.)
    volume_match = re.search(r'\b(v\d{1,3})\b', filename)
    if volume_match:
        values['volume_number'] = volume_match.group(1)
    
    return values


def apply_custom_pattern(values, pattern):
    """
    Apply custom rename pattern with extracted values.
    Returns the new filename (without extension).
    """
    if not pattern:
        return ""

    # Validate that we have the required fields
    series_name = values.get('series_name', '').strip()
    issue_number = values.get('issue_number', '').strip()

    if not series_name:
        app_logger.warning(f"Missing series_name in extracted values: {values}")
        return ""

    if not issue_number:
        app_logger.warning(f"Missing issue_number in extracted values: {values}")
        return ""

    result = pattern

    # Replace variables with extracted values
    result = result.replace('{series_name}', series_name)
    result = result.replace('{volume_number}', values.get('volume_number', ''))
    result = result.replace('{year}', values.get('year', ''))
    result = result.replace('{issue_number}', issue_number)

    # Clean up extra spaces and trim
    result = re.sub(r'\s+', ' ', result).strip()

    # Remove empty parentheses and space before them
    result = re.sub(r'\s*\(\s*\)', '', result).strip()

    return result


def validate_custom_pattern(pattern):
    """
    Basic validation of custom rename pattern.
    Returns True if valid, False otherwise.
    """
    if not pattern:
        return True  # Empty pattern is valid (will use default logic)
    
    # Check for valid variable syntax
    valid_variables = ['{series_name}', '{volume_number}', '{year}', '{issue_number}']
    
    # Check if pattern contains only valid variables and other characters
    # This is a simple validation - we could make it more sophisticated
    return True  # For now, accept any pattern


def parentheses_replacer(match):
    """
    Process a parentheses group:
      - If it contains a 4-digit year, return just that year in parentheses.
      - Otherwise, remove the entire parentheses group.
    """
    # Strip the outer parentheses
    inner_text = match.group(0)[1:-1]
    # Look for a 4-digit year
    year_match = re.search(r'\b\d{4}\b', inner_text)
    if year_match:
        year = year_match.group(0)
        return f"({year})"
    return ''


def clean_parentheses_content(filename):
    """
    Enhanced parentheses cleaning that:
    1) Keeps parentheses containing 4-digit years
    2) Removes all other parentheses content
    3) If a 4-digit year parentheses exists, removes any parentheses content that comes after it
    """
    # First, extract the file extension to preserve it
    last_dot_pos = filename.rfind('.')
    if last_dot_pos == -1:
        # No file extension found
        base_name = filename
        extension = ''
    else:
        base_name = filename[:last_dot_pos]
        extension = filename[last_dot_pos:]
    
    # Find all parentheses groups in the base name
    parentheses_groups = list(re.finditer(r'\([^)]*\)', base_name))
    
    if not parentheses_groups:
        return filename
    
    # Find the first parentheses group with a 4-digit year
    first_year_parentheses = None
    for match in parentheses_groups:
        inner_text = match.group(0)[1:-1]  # Remove outer parentheses
        if re.search(r'\b\d{4}\b', inner_text):
            first_year_parentheses = match
            break
    
    # If we found a 4-digit year parentheses, remove everything after it
    if first_year_parentheses:
        # Keep everything up to and including the first year parentheses
        year_end_pos = first_year_parentheses.end()
        base_name = base_name[:year_end_pos]
        
        # Now clean up any remaining parentheses content before the year parentheses
        before_year = base_name[:first_year_parentheses.start()]
        after_year = base_name[first_year_parentheses.start():]
        
        # Clean the part before the year parentheses
        cleaned_before = re.sub(r'\([^)]*\)', parentheses_replacer, before_year)
        
        # Combine cleaned before + year parentheses
        base_name = cleaned_before + after_year
    else:
        # No 4-digit year found, remove all parentheses content
        base_name = re.sub(r'\([^)]*\)', '', base_name)
    
    # Clean up any extra spaces that might result
    base_name = re.sub(r'\s+', ' ', base_name).strip()
    
    # Reattach the file extension
    return base_name + extension


def clean_filename_pre(filename):
    """
    Pre-process the filename to:
      1) Remove anything in [brackets].
      2) Process parentheses:
         - If a 4-digit year is present, keep only that year.
         - Otherwise, remove the parentheses entirely.
      3) Handle dash-separated numbers:
         - Replace patterns like 'YYYY-XX' or 'YYYY-YYYY' with 'YYYY'.
         - Remove any other dash-separated numbers (e.g. '01-05').
      4) Remove " - Issue" from the filename.
    """
    filename = filename.replace('_', ' ')

    # 1) Remove bracketed text [ ... ]
    filename = re.sub(r'\[.*?\]', '', filename)

    # 2) Process parentheses using the enhanced helper
    filename = clean_parentheses_content(filename)

    # 3a) Replace 4-digit–dash–2-digit (e.g. "2018-04") with the 4-digit year.
    filename = re.sub(r'\b(\d{4})-\d{2}\b', r'\1', filename)
    # 3b) Replace 4-digit–dash–4-digit (e.g. "1989-1990") with the first 4-digit year.
    filename = re.sub(r'\b(\d{4})-\d{4}\b', r'\1', filename)
    # 3c) Remove any other dash-separated numbers (e.g. "01-05")
    filename = re.sub(r'\b\d+(?:-\d+)+\b', '', filename)

    # 4) Remove " - Issue" from the filename
    filename = re.sub(r'\s*-\s*Issue\b', '', filename, flags=re.IGNORECASE)

    # Trim extra spaces that might result
    filename = re.sub(r'\s+', ' ', filename).strip()

    return filename


def clean_directory_name(directory_name):
    """
    Pre-process the directory name using the same rules as the filename:
      1) Remove anything in [brackets].
      2) Remove parentheses that don't contain a 4-digit year.
      3) If a parentheses contains a 4-digit year followed by -XX (month),
         remove that -XX piece (e.g. "2023-04" -> "2023").
      4) Remove " - Issue" from the directory name.
    """
    return clean_filename_pre(directory_name)


def get_unique_filepath(file_path):
    """
    Generate a unique filepath by appending (1), (2), etc. if the file already exists.
    This prevents files from being overwritten during rename operations.

    Args:
        file_path: The desired target path for the file

    Returns:
        A unique filepath that doesn't exist yet
    """
    if not os.path.exists(file_path):
        return file_path

    # Split the path into directory, filename, and extension
    directory = os.path.dirname(file_path)
    filename = os.path.basename(file_path)
    name, ext = os.path.splitext(filename)

    # Keep incrementing the counter until we find a unique name
    counter = 1
    while True:
        new_name = f"{name} ({counter}){ext}"
        new_path = os.path.join(directory, new_name)
        if not os.path.exists(new_path):
            return new_path
        counter += 1


def get_renamed_filename(filename):
    """
    Given a single filename (no directory path):
      1) Check if custom rename pattern is enabled and try to apply it
      2) If custom rename fails or is disabled, check for special case: issue number + year in parentheses (e.g. "Title (00 1996).ext")
      3) Special case: Title, YYYY-MM-DD (NN) format
      4) Special case: Title, YYYY-MM-DD (#NN) format (e.g. "Legion of Super-Heroes, 1985-07-00 (#14)")
      5) Special case: ISSUE number AFTER YEAR pattern
      6) Special case: YYYYMM Series Name v# ### format (e.g. "199309 Hokum & Hex v1 001.cbz")
      7) Pre-clean the filename by removing bracketed text,
         processing parentheses (keeping only 4-digit years),
         and removing dash-separated numbers.
      8) Try VOLUME_ISSUE_PATTERN first (e.g. "Title v3 051 (2018).ext").
      9) If it fails, try the single ISSUE_PATTERN.
      10) Next, try ISSUE_AFTER_YEAR_PATTERN for cases where the issue number follows the year.
      11) If that fails, try FALLBACK_PATTERN for just (YYYY).
      12) If none match, return None.
    """
    app_logger.info(f"Attempting to rename filename: {filename}")

    # ==========================================================
    # 0) Check for custom rename pattern (BEFORE all other logic)
    # ==========================================================
    try:
        custom_enabled, custom_pattern = load_custom_rename_config()
        if custom_enabled and custom_pattern:
            app_logger.info(f"Custom rename pattern enabled: {custom_pattern}")
            
            # Extract comic values from the filename
            comic_values = extract_comic_values(filename)
            app_logger.info(f"Extracted comic values: {comic_values}")
            
            # Apply custom pattern
            custom_result = apply_custom_pattern(comic_values, custom_pattern)
            if custom_result:
                # Get file extension
                last_dot_pos = filename.rfind('.')
                extension = filename[last_dot_pos:] if last_dot_pos != -1 else ''
                
                # Check if this would create a duplicate filename by looking for unique identifiers
                # in the original filename that we can preserve
                base_filename = custom_result
                
                # Don't preserve scanner/source information - it's not needed for uniqueness
                # The custom pattern already provides the essential information (series, issue, year)
                # Scanner info like (Glorith-Novus-HD), (Other-Source), etc. should be removed
                base_filename = custom_result
                
                new_filename = base_filename + extension
                new_filename = clean_final_filename(new_filename)
                app_logger.info(f"Custom rename result: {filename} -> {new_filename}")
                return new_filename
            else:
                app_logger.info("Custom rename pattern failed, falling back to default logic")
        else:
            app_logger.info("Custom rename pattern disabled or not configured, using default logic")
    except Exception as e:
        app_logger.warning(f"Error in custom rename logic: {e}, falling back to default logic")

    # Try declarative rule engine (hot-patchable via /config/rename_rules.ini)
    # This runs AFTER custom rename pattern check, so custom pattern takes precedence
    rule_name = try_rule_engine(filename, "config/rename_rules.ini")
    if rule_name:
        return clean_final_filename(rule_name)

    # ==========================================================
    # 1) Special case: Issue number + year in parentheses (BEFORE pre-cleaning)
    #    e.g. "Leonard Nimoy's Primortals (00 1996).cbz"
    # ==========================================================
    issue_year_paren_match = ISSUE_YEAR_PARENTHESES_PATTERN.match(filename)
    if issue_year_paren_match:
        app_logger.info(f"Matched ISSUE_YEAR_PARENTHESES_PATTERN for: {filename}")
        raw_title, issue_num, year, extra, extension = issue_year_paren_match.groups()
        clean_title = smart_title_case(raw_title.replace('_', ' ').strip())
        final_issue = norm_issue(issue_num)
        new_filename = f"{clean_title} {final_issue} ({year}){extension}"
        return clean_final_filename(new_filename)

    # ==========================================================
    # 2) Special case: Title, YYYY-MM-DD (NN) format (BEFORE pre-cleaning)
    #    e.g. "Justice League Europe, 1990-02-00 ( 13) (digital) (OkC.O.M.P.U.T.O.-Novus-HD).cbz"
    #    e.g. "Blue Devil, 1984-04-00 (_01) (digital) (Glorith-Novus-HD).cbz"
    # ==========================================================
    title_comma_year_issue_match = TITLE_COMMA_YEAR_ISSUE_PATTERN.match(filename)
    if title_comma_year_issue_match:
        app_logger.info(f"Matched TITLE_COMMA_YEAR_ISSUE_PATTERN for: {filename}")
        raw_title, year, issue_num, extra, extension = title_comma_year_issue_match.groups()
        clean_title = smart_title_case(raw_title.replace('_', ' ').strip())

        # Handle issue numbers that may have underscore prefixes
        if issue_num.startswith('_'):
            # Remove underscore and zero-pad the numeric part
            numeric_part = issue_num[1:]  # Remove the underscore
            final_issue = norm_issue(issue_num)
        else:
            # Regular numeric issue number
            final_issue = norm_issue(issue_num)

        new_filename = f"{clean_title} {final_issue} ({year}){extension}"
        return clean_final_filename(new_filename)

    # ==========================================================
    # 3) Special case: Title, YYYY-MM-DD (#NN) format (BEFORE pre-cleaning)
    #    e.g. "Legion of Super-Heroes, 1985-07-00 (#14) (digital) (Glorith-Novus-HD).cbz"
    # ==========================================================
    title_comma_year_hash_issue_match = TITLE_COMMA_YEAR_HASH_ISSUE_PATTERN.match(filename)
    if title_comma_year_hash_issue_match:
        app_logger.info(f"Matched TITLE_COMMA_YEAR_HASH_ISSUE_PATTERN for: {filename}")
        raw_title, year, hash_issue_num, extra, extension = title_comma_year_hash_issue_match.groups()
        clean_title = smart_title_case(raw_title.replace('_', ' ').strip())

        # Remove the # from the issue number and zero-pad
        issue_num = hash_issue_num[1:]  # Remove the # prefix
        final_issue = norm_issue(issue_num)

        new_filename = f"{clean_title} {final_issue} ({year}){extension}"
        return clean_final_filename(new_filename)

    # ==========================================================
    # 4) Special case: ISSUE number AFTER YEAR pattern (BEFORE pre-cleaning)
    #    e.g. "Spider-Man 2099 (1992) #44 (digital) (Colecionadores.GO).cbz"
    #    e.g. "2000AD (2018) #1795.cbz"
    # ==========================================================
    issue_after_year_match = ISSUE_AFTER_YEAR_PATTERN.match(filename)
    if issue_after_year_match:
        app_logger.info(f"Matched ISSUE_AFTER_YEAR_PATTERN for: {filename}")
        raw_title, year, issue, extra, extension = issue_after_year_match.groups()
        clean_title = smart_title_case(raw_title.replace('_', ' ').strip())
        # Remove the # from the issue number and zero-pad
        issue_num = issue[1:]  # Remove the # prefix
        final_issue = norm_issue(issue_num)
        new_filename = f"{clean_title} {final_issue} ({year}){extension}"
        return clean_final_filename(new_filename)

    # ==========================================================
    # 5) Special case: YYYYMM Series Name v# ### format (BEFORE pre-cleaning)
    #    e.g. "199309 Hokum & Hex v1 001.cbz"
    # ==========================================================
    year_month_series_volume_issue_match = YEAR_MONTH_SERIES_VOLUME_ISSUE_PATTERN.match(filename)
    if year_month_series_volume_issue_match:
        app_logger.info(f"Matched YEAR_MONTH_SERIES_VOLUME_ISSUE_PATTERN for: {filename}")
        year_month, series_name, volume, issue_num, extension = year_month_series_volume_issue_match.groups()

        # Extract year from the first 4 digits of year_month
        year = year_month[:4]

        # Clean the series name: underscores -> spaces, then strip, then title case
        clean_series = smart_title_case(series_name.replace('_', ' ').strip())

        # Keep volume as-is (e.g., "v1")
        final_volume = volume.strip()

        # Zero-pad the issue number
        final_issue = norm_issue(issue_num)

        new_filename = f"{clean_series} {final_volume} {final_issue} ({year}){extension}"
        return clean_final_filename(new_filename)

    # ==========================================================
    # 6) Special case: Series Name YYYY-MM ( NN) (YYYY) format (BEFORE pre-cleaning)
    #    e.g. "Mister Miracle 1989-08 ( 08) (1989) (Digital) (Shadowcat-Empire).cbz"
    # ==========================================================
    series_year_month_issue_match = SERIES_YEAR_MONTH_ISSUE_PATTERN.match(filename)
    if series_year_month_issue_match:
        app_logger.info(f"Matched SERIES_YEAR_MONTH_ISSUE_PATTERN for: {filename}")
        series_name, year, issue_num, extra, extension = series_year_month_issue_match.groups()

        # Clean the series name: underscores -> spaces, then strip, then title case
        clean_series = smart_title_case(series_name.replace('_', ' ').strip())

        # Zero-pad the issue number
        final_issue = norm_issue(issue_num)

        new_filename = f"{clean_series} {final_issue} ({year}){extension}"
        return clean_final_filename(new_filename)

    # ==========================================================
    # 7) Special case: Series Name YYYY-MM-DD ( NN) (digital) format (BEFORE pre-cleaning)
    #    e.g. "Mister Miracle 1990-09-18 ( 21) (digital) (Glorith-Novus-HD).cbz"
    # ==========================================================
    series_year_month_day_issue_match = SERIES_YEAR_MONTH_DAY_ISSUE_PATTERN.match(filename)
    if series_year_month_day_issue_match:
        app_logger.info(f"Matched SERIES_YEAR_MONTH_DAY_ISSUE_PATTERN for: {filename}")
        series_name, year, issue_num, extra, extension = series_year_month_day_issue_match.groups()

        # Clean the series name: underscores -> spaces, then strip, then title case
        clean_series = smart_title_case(series_name.replace('_', ' ').strip())

        # Zero-pad the issue number
        final_issue = norm_issue(issue_num)

        new_filename = f"{clean_series} {final_issue} ({year}){extension}"
        return clean_final_filename(new_filename)

    # Pre-processing step
    cleaned_filename = clean_filename_pre(filename)

    # ==========================================================
    # 5) VOLUME + ISSUE pattern (e.g. "Comic Name v3 051 (2018).ext")
    # ==========================================================
    vol_issue_match = VOLUME_ISSUE_PATTERN.match(cleaned_filename)
    if vol_issue_match:
        app_logger.info(f"Matched VOLUME_ISSUE_PATTERN for: {cleaned_filename}")
        raw_title, volume_part, issue_part, middle, extension = vol_issue_match.groups()

        # Clean the title: underscores -> spaces, then strip, then title case
        clean_title = smart_title_case(raw_title.replace('_', ' ').strip())

        # volume_part (e.g. "v3") - keep as-is
        final_volume = volume_part.strip()

        # If issue_part starts with 'v', keep as-is, else zero-pad numeric
        if issue_part.lower().startswith('v'):
            final_issue = issue_part
        else:
            final_issue = f"{int(issue_part):03d}"  # zero-pad if numeric

        # Look for the first 4-digit year in `middle`
        found_year = None
        paren_groups = re.findall(r'\(([^)]*)\)', middle)
        for group_text in paren_groups:
            year_match = re.search(r'\b(\d{4})\b', group_text)
            if year_match:
                found_year = year_match.group(1)
                break

        if found_year:
            new_filename = f"{clean_title} {final_volume} {final_issue} ({found_year}){extension}"
        else:
            new_filename = f"{clean_title} {final_volume} {final_issue}{extension}"

        return clean_final_filename(new_filename)

    # ==========================================================
    # 6) Hash‐issue pattern (explicit "#NNN"): catch before bare digits
    #    e.g. "Injustice 2 #1 (2018).cbz"
    # ==========================================================
    hash_match = ISSUE_HASH_PATTERN.match(cleaned_filename)
    if hash_match:
        app_logger.info(f"Matched ISSUE_HASH_PATTERN for: {cleaned_filename}")
        raw_title, issue_num, middle, extension = hash_match.groups()

        clean_title = smart_title_case(raw_title.replace('_', ' ').strip())
        final_issue = norm_issue(issue_num)

        # Try to pull a year out of any parentheses in `middle`
        found_year = None
        for group_text in re.findall(r'\(([^)]*)\)', middle):
            if year := re.search(r'\b(\d{4})\b', group_text):
                found_year = year.group(1)
                break

        if found_year:
            new_filename = f"{clean_title} {final_issue} ({found_year}){extension}"
        else:
            new_filename = f"{clean_title} {final_issue}{extension}"

        return clean_final_filename(new_filename)

    # ==========================================================
    # 7) VOLUME + SUBTITLE pattern (e.g. "Infinity 8 v03 - The Gospel According to Emma (2019).cbr")
    # ==========================================================
    vol_subtitle_match = VOLUME_SUBTITLE_PATTERN.match(cleaned_filename)
    if vol_subtitle_match:
        app_logger.info(f"Matched VOLUME_SUBTITLE_PATTERN for: {cleaned_filename}")
        raw_title, volume_part, subtitle_part, extension = vol_subtitle_match.groups()

        # Clean the title: underscores -> spaces, then strip, then title case
        clean_title = smart_title_case(raw_title.replace('_', ' ').strip())

        # volume_part (e.g. "v03") - keep as-is
        final_volume = volume_part.strip()

        # Extract year from subtitle and clean it up
        found_year = None
        clean_subtitle = subtitle_part.strip()

        # Look for a 4-digit year in parentheses
        year_match = re.search(r'\((\d{4})\)', subtitle_part)
        if year_match:
            found_year = year_match.group(1)
            # Remove everything after the year parentheses, but keep the subtitle clean
            clean_subtitle = subtitle_part[:year_match.start()].strip()
            # Also remove any trailing parentheses that might be left
            clean_subtitle = re.sub(r'\s*\([^)]*\)\s*$', '', clean_subtitle).strip()

        if found_year:
            new_filename = f"{clean_title} {final_volume} {clean_subtitle} ({found_year}){extension}"
        else:
            new_filename = f"{clean_title} {final_volume} {clean_subtitle}{extension}"

        return clean_final_filename(new_filename)

    # ==========================================================
    # 4) Series-number + issue-number (no “v”, no “#”)
    #    e.g. "Injustice 2 001 (2018).cbz"
    # ==========================================================
    series_match = SERIES_ISSUE_PATTERN.match(cleaned_filename)
    if series_match:
        app_logger.info(f"Matched SERIES_ISSUE_PATTERN for: {cleaned_filename}")
        raw_title, series_num, issue_num, middle, extension = series_match.groups()

        # Keep the series number in the title, apply title case
        clean_title = smart_title_case(f"{raw_title.replace('_', ' ').strip()} {series_num}")
        final_issue = norm_issue(issue_num)

        # Pull out a 4-digit year if present
        found_year = None
        for grp in re.findall(r'\(([^)]*)\)', middle):
            if ym := re.search(r'\b(\d{4})\b', grp):
                found_year = ym.group(1)
                break

        if found_year:
            return clean_final_filename(f"{clean_title} {final_issue} ({found_year}){extension}")
        return clean_final_filename(f"{clean_title} {final_issue}{extension}")

    # ==========================================================
    # 9) Single ISSUE pattern (no separate "volume" token)
    #    e.g. "Comic Name 051 (2018).cbz" or "Comic Name v3 (2018).cbz"
    # ==========================================================
    issue_match = ISSUE_PATTERN.match(cleaned_filename)
    if issue_match:
        app_logger.info(f"Matched ISSUE_PATTERN for: {cleaned_filename}")
        raw_title, issue_part, middle, extension = issue_match.groups()

        # Clean the title: underscores -> spaces, then strip, then title case
        clean_title = smart_title_case(raw_title.replace('_', ' ').strip())

        # If issue_part starts with 'v', keep "vXX" as-is, else zero-pad
        if issue_part.lower().startswith('v'):
            final_issue = issue_part  # e.g. 'v01'
        else:
            final_issue = f"{int(issue_part):03d}"  # e.g. 1 -> 001

        # Attempt to find a 4-digit year in `middle`
        found_year = None
        paren_groups = re.findall(r'\(([^)]*)\)', middle)
        for group_text in paren_groups:
            year_match = re.search(r'\b(\d{4})\b', group_text)
            if year_match:
                found_year = year_match.group(1)
                break

        if found_year:
            new_filename = f"{clean_title} {final_issue} ({found_year}){extension}"
        else:
            new_filename = f"{clean_title} {final_issue}{extension}"

        return clean_final_filename(new_filename)



    # ==========================================================
    # 10) Title with just YEAR (no volume or issue)
    #     e.g. "Hulk vs. The Marvel Universe 2008 Digital.cbz"
    # ==========================================================
    title_year_match = TITLE_YEAR_PATTERN.match(cleaned_filename)
    if title_year_match:
        app_logger.info(f"Matched TITLE_YEAR_PATTERN for: {cleaned_filename}")
        raw_title, found_year, _, extension = title_year_match.groups()
        clean_title = smart_title_case(raw_title.replace('_', ' ').strip())
        # Remove any trailing opening parenthesis that might have been captured
        clean_title = clean_title.rstrip(' (')
        return clean_final_filename(f"{clean_title} ({found_year}){extension}")

    # ==========================================================
    # 11) Fallback: Title (YYYY) anything .ext
    #    e.g. "Comic Name (2018) some extra.cbz" -> "Comic Name (2018).cbz"
    # ==========================================================
    fallback_match = FALLBACK_PATTERN.match(cleaned_filename)
    if fallback_match:
        app_logger.info(f"Matched FALLBACK_PATTERN for: {cleaned_filename}")
        raw_title, found_year, _, extension = fallback_match.groups()
        clean_title = smart_title_case(raw_title.replace('_', ' ').strip())
        new_filename = f"{clean_title} ({found_year}){extension}"
        return clean_final_filename(new_filename)

    # ==========================================================
    # 12) No match => return None
    # ==========================================================
    app_logger.info(f"No pattern matched for: {filename}")
    return None


def rename_files(directory):
    """
    Walk through the given directory (including subdirectories) and rename
    all files that match the patterns above, skipping hidden files.
    """

    app_logger.info("********************// Rename Directory Files //********************")
    app_logger.info(f"Starting rename process for directory: {directory}")
    app_logger.info(f"Current working directory: {os.getcwd()}")
    #app_logger.info(f"Directory exists: {os.path.exists(directory)}")
    #app_logger.info(f"Directory is directory: {os.path.isdir(directory)}")
    
    files_processed = 0
    files_renamed = 0

    for subdir, dirs, files in os.walk(directory):
        # Skip hidden directories.
        dirs[:] = [d for d in dirs if not is_hidden(os.path.join(subdir, d))]
        #app_logger.info(f"Processing subdirectory: {subdir} with {len(files)} files")
        
        # List all files in this subdirectory
        #for filename in files:
            #app_logger.info(f"Found file: {filename} in {subdir}")
        
        for filename in files:
            files_processed += 1
            old_path = os.path.join(subdir, filename)
            
            app_logger.info(f"Processing file: {filename}")
            #app_logger.info(f"Full old path: {old_path}")
            #app_logger.info(f"File exists: {os.path.exists(old_path)}")
            #app_logger.info(f"File size: {os.path.getsize(old_path) if os.path.exists(old_path) else 'N/A'}")
            
            # Skip hidden files.
            if is_hidden(old_path):
                app_logger.info(f"Skipping hidden file: {old_path}")
                continue

            app_logger.info(f"Processing file: {filename}")
            new_name = get_renamed_filename(filename)

            if new_name and new_name != filename:
                new_path = os.path.join(subdir, new_name)

                # Check if target file already exists and generate unique filename if needed
                original_new_path = new_path
                new_path = get_unique_filepath(new_path)

                if new_path != original_new_path:
                    app_logger.warning(f"File already exists at destination. Using unique filename to prevent overwrite.")
                    app_logger.info(f"Original target: {original_new_path}")
                    app_logger.info(f"New target: {new_path}")

                app_logger.info(f"Renaming:\n  {old_path}\n  --> {new_path}\n")
                try:
                    os.rename(old_path, new_path)
                    files_renamed += 1
                    #app_logger.info(f"Successfully renamed: {filename} -> {new_name}")
                    
                    # Verify the rename actually happened
                    if os.path.exists(new_path) and not os.path.exists(old_path):
                        app_logger.info(f"Rename verification successful: new file exists, old file removed")
                    else:
                        app_logger.warning(f"Rename verification failed: new file exists: {os.path.exists(new_path)}, old file exists: {os.path.exists(old_path)}")
                        
                except Exception as e:
                    app_logger.error(f"Failed to rename {filename}: {e}")
            else:
                if new_name is None:
                    app_logger.info(f"No rename pattern matched for: {filename}")
                else:
                    app_logger.info(f"No change needed for: {filename}")
    
    app_logger.info(f"Rename process complete. Processed {files_processed} files, renamed {files_renamed} files.")
    return files_renamed


def rename_file(file_path):
    """
    Renames a single file if it matches either pattern using the logic
    in get_renamed_filename(), skipping hidden files.
    """
    app_logger.info("********************// Rename Single File //********************")

    # Skip hidden files using the is_hidden helper.
    if is_hidden(file_path):
        app_logger.info(f"Skipping hidden file: {file_path}")
        return None

    directory, filename = os.path.split(file_path)
    new_name = get_renamed_filename(filename)

    if new_name and new_name != filename:
        new_path = os.path.join(directory, new_name)

        # Check if target file already exists and generate unique filename if needed
        original_new_path = new_path
        new_path = get_unique_filepath(new_path)

        if new_path != original_new_path:
            app_logger.warning(f"File already exists at destination. Using unique filename to prevent overwrite.")
            app_logger.info(f"Original target: {original_new_path}")
            app_logger.info(f"New target: {new_path}")

        app_logger.info(f"Renaming:\n  {file_path}\n  --> {new_path}\n")
        os.rename(file_path, new_path)
        return new_path
    else:
        app_logger.info("No renaming pattern matched or no change needed.")
        return None
    

def test_parentheses_cleaning():
    """
    Test function to verify the new parentheses cleaning logic works correctly
    """
    test_cases = [
        # Test case 1: Remove parentheses without 4-digit year
        ("2000AD 1700 (01-09-10).cbz", "2000AD 1700.cbz"),
        
        # Test case 2: Keep 4-digit year, remove everything after
        ("Comic Name v3 051 (2018) (DCP-Scan Final).cbz", "Comic Name v3 051 (2018).cbz"),
        
        # Test case 3: Keep 4-digit year, remove everything after
        ("Title (2019) (digital) (scan).cbz", "Title (2019).cbz"),
        
        # Test case 4: No 4-digit year, remove all parentheses
        ("Comic (digital) (scan) (final).cbz", "Comic.cbz"),
        
        # Test case 5: Multiple 4-digit years, keep first one, remove rest
        ("Comic (2018) (2019) (digital).cbz", "Comic (2018).cbz"),
        
        # Test case 6: 4-digit year in middle, remove before and after
        ("Comic (scan) (2018) (digital).cbz", "Comic (2018).cbz"),
        
        # Test case 7: No parentheses
        ("Comic Name 001.cbz", "Comic Name 001.cbz"),
        
        # Test case 8: Only 4-digit year parentheses
        ("Comic Name (2020).cbz", "Comic Name (2020).cbz"),
    ]
    
    print("Testing parentheses cleaning logic:")
    print("=" * 50)
    
    for i, (input_name, expected_output) in enumerate(test_cases, 1):
        result = clean_parentheses_content(input_name)
        status = "✓ PASS" if result == expected_output else "✗ FAIL"
        print(f"Test {i}: {status}")
        print(f"  Input:    {input_name}")
        print(f"  Expected: {expected_output}")
        print(f"  Got:      {result}")
        if result != expected_output:
            print(f"  ERROR: Expected '{expected_output}' but got '{result}'")
        print()


def test_custom_rename():
    """
    Test function to verify the custom rename functionality works correctly
    """
    print("Testing custom rename functionality:")
    print("=" * 50)

    # Test the new patterns first
    test_files = [
        "Justice League (1987-09) 05 (DobisP.R.-Novus-HD).cbz",
        "Justice League (1987-06) 02 (DobisP.R.-Novus-HD).cbz",
        "Justice League International v1 Annual 001 (1987) (FBScan).cbr"
    ]

    print("Testing value extraction for new patterns:")
    for filename in test_files:
        values = extract_comic_values(filename)
        print(f"File: {filename}")
        print(f"  Extracted: {values}")
        print()

    print("Testing custom pattern application:")
    print("=" * 50)
    
    # Test custom pattern application
    test_values = {
        'series_name': 'Spider-Man 2099',
        'volume_number': 'v2',
        'year': '1992',
        'issue_number': '044'
    }
    
    test_patterns = [
        ("{series_name} {issue_number} ({year})", "Spider-Man 2099 044 (1992)"),
        ("{series_name} [{year}] {issue_number}", "Spider-Man 2099 [1992] 044"),
        ("issue{issue_number}", "issue044"),
        ("{volume_number}_{issue_number}", "v2_044"),
        ("{series_name} - {year}", "Spider-Man 2099 - 1992"),
        ("{series_name} {volume_number} {issue_number}", "Spider-Man 2099 v2 044"),
    ]
    
    for i, (pattern, expected) in enumerate(test_patterns, 1):
        result = apply_custom_pattern(test_values, pattern)
        status = "✓ PASS" if result == expected else "✗ FAIL"
        print(f"Test {i}: {status}")
        print(f"  Pattern:  {pattern}")
        print(f"  Expected: {expected}")
        print(f"  Got:      {result}")
        if result != expected:
            print(f"  ERROR: Expected '{expected}' but got '{result}'")
        print()
    
    # Test value extraction
    test_filename = "Spider-Man 2099 v2 044 (1992) (digital).cbz"
    extracted_values = extract_comic_values(test_filename)
    print("Value extraction test:")
    print(f"  Input:    {test_filename}")
    print(f"  Extracted: {extracted_values}")
    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        app_logger.info("No directory provided!")
        # Run tests if no directory provided
        test_parentheses_cleaning()
        print()
        test_custom_rename()
    else:
        directory = sys.argv[1]
        rename_files(directory)
