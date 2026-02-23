import defusedxml.ElementTree as SafeET
import re
import os
from database import search_file_index
from app_logging import app_logger

class CBLLoader:
    def __init__(self, file_content, filename=None, rename_pattern=None):
        self.root = SafeET.fromstring(file_content)
        self.books = []
        self.name = self.root.find('Name').text if self.root.find('Name') is not None else "Unknown Reading List"
        self.publisher = self._extract_publisher(filename)
        self.rename_pattern = rename_pattern or '{series_name} {issue_number}'

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
        # Replace ':' with ' -' before cleaning (e.g., "Batman: The Dark Knight" -> "Batman - The Dark Knight")
        series_cleaned = series.replace(':', ' -') if series else ''
        # Clean series name (remove special chars except dash)
        clean_series = re.sub(r'[^\w\s-]', '', series_cleaned)

        # Pad issue number to 3 digits
        padded_number = number.zfill(3) if number else ''

        # Replace placeholders in pattern
        search_term = self.rename_pattern
        search_term = search_term.replace('{series_name}', clean_series)
        search_term = search_term.replace('{series}', clean_series)
        search_term = search_term.replace('{issue_number}', padded_number)
        search_term = search_term.replace('{issue}', padded_number)
        search_term = search_term.replace('{volume}', volume or '')
        search_term = search_term.replace('{year}', year or '')
        search_term = search_term.replace('{start_year}', volume or year or '')

        # Clean up any remaining empty placeholders and extra spaces
        search_term = re.sub(r'\{[^}]+\}', '', search_term)
        search_term = re.sub(r'\s+', ' ', search_term).strip()

        # Remove empty parentheses
        search_term = re.sub(r'\(\s*\)', '', search_term).strip()

        return search_term

    def match_file(self, series, number, volume, year):
        """
        Attempt to match a book to a file in the library.
        Strategy:
        1. Search by filename using rename pattern format
        2. Filter results by expected path structure:
           - /Publisher/Series/vVolume/  (subfolder format)
           - /Publisher/Series (Volume)/  (year in folder name)
        3. REQUIRE issue number to match in filename
        """
        if not series or not number:
            return None

        # Clean series name for search - replace ':' with ' -'
        series_cleaned = series.replace(':', ' -')
        clean_series = re.sub(r'[^\w\s-]', '', series_cleaned)
        padded_3 = number.zfill(3)  # "18" -> "018"

        # Build search patterns - prioritize pattern-based search
        results = []
        search_patterns = []

        # First try the rename pattern format (most likely to match)
        pattern_search = self._format_search_term(series, number, volume, year)
        if pattern_search:
            search_patterns.append(pattern_search)

        # Fallback patterns
        search_patterns.extend([
            f"{clean_series} {padded_3}",         # "Avengers 018"
            f"{clean_series} {number}",           # "Avengers 18"
            f"{clean_series} #{padded_3}",        # "Avengers #018"
            f"{clean_series} #{number}",          # "Avengers #18"
        ])

        # Remove duplicates while preserving order
        search_patterns = list(dict.fromkeys(search_patterns))

        for pattern in search_patterns:
            results = search_file_index(pattern, limit=20)
            if results:
                break

        # If no results, try without first word (e.g., "The Flash" -> "Flash")
        if not results:
            words = clean_series.split()
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
            results = search_file_index(clean_series, limit=100)

        if not results:
            return None

        # Score and rank results - MUST have issue number match
        best_match = None
        best_score = 0

        for res in results:
            path = res['path'].lower().replace('\\', '/')
            filename = res['name'].lower()

            # REQUIRED: Check issue number in filename (avoid matching #1 in #10, or 19 in 2019)
            # Pattern: must be preceded by non-digit (space, #, or start), then optional zeros, then the number
            # Followed by non-digit (space, dot, paren, or end)
            padded_2 = number.zfill(2)
            padded_3 = number.zfill(3)
            # Match patterns like: #19, #019, " 19", " 019", "_019" etc.
            issue_pattern = rf'(?:^|[^\d])#?\s*(?:0*{re.escape(number)}|{re.escape(padded_2)}|{re.escape(padded_3)})(?:[^\d]|$)'
            if not re.search(issue_pattern, filename):
                continue  # Skip files that don't have the correct issue number

            score = 10  # Base score for having correct issue number

            # Check publisher in path
            if self.publisher and self.publisher.lower() in path:
                score += 10

            # Check series name in path
            if series.lower() in path:
                score += 5

            # Check volume - two possible formats:
            if volume:
                # Format 1: /vVolume/ subfolder (e.g., /v2021/)
                if f"/v{volume}/" in path:
                    score += 15
                # Format 2: Series (Volume) folder (e.g., /Inferno (2021)/)
                elif f"({volume})" in path:
                    score += 15
                # Format 3: Just year in path somewhere
                elif volume in path:
                    score += 5

            if score > best_score:
                best_score = score
                best_match = res['path']

        return best_match
