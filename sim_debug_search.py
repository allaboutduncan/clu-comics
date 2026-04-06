"""
Debug live search issues.
"""
import sys
sys.path.insert(0, 'C:/Users/trumb/clu-comics')

from models.getcomics import search_getcomics, score_getcomics_result, accept_result, parse_result_title
from models.getcomics import normalize_series_name, series_has_same_brand, get_brand_keywords


def debug_search(series_name, issue_number, year=None, volume=None, max_pages=2):
    """Debug search and scoring."""
    print('=' * 80)
    print(f'DEBUG: {series_name} #{issue_number}' + (f' ({year})' if year else ''))
    print('=' * 80)

    # Build query
    query_parts = [series_name]
    if volume:
        query_parts.extend(['vol', str(volume)])
    query_parts.append(str(issue_number))
    if year:
        query_parts.append(str(year))
    query = ' '.join(query_parts)

    print(f'\nQuery: "{query}"')
    print()

    results = search_getcomics(query, max_pages=max_pages)
    if not results:
        print('No results found!')
        return

    print(f'Found {len(results)} results\n')

    for r in results:
        title = r['title']
        print(f'Title: {title}')

        # Debug scoring step by step
        score, range_contains, series_match = score_getcomics_result(
            title, series_name, issue_number, year,
            series_volume=volume
        )
        decision = accept_result(score, range_contains, series_match)

        print(f'  Score: {score}, range={range_contains}, series={series_match}, decision={decision}')
        print()


if __name__ == '__main__':
    tests = [
        ('Batman', '1', 2025, None),
        ('Batman Rebirth', '1', 2016, None),
        ('Flash Gordon', '1', 2024, None),
    ]

    for series, issue, year, volume in tests:
        debug_search(series, issue, year, volume)
        import time
        time.sleep(3)
