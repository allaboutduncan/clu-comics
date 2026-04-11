"""
Simulation: Direct GetComics search with various series.

Tests the search and scoring flow with realistic queries.
"""
import sys
sys.path.insert(0, 'C:/Users/trumb/clu-comics')

from models.getcomics import search_getcomics, score_getcomics_result, accept_result
import time


def test_series_search(series_name, issue_number, year=None, volume=None, publisher=None):
    """Test a search for a specific series/issue."""
    print('-' * 70)
    print(f'Search: {series_name} #{issue_number}' +
          (f' ({year})' if year else '') +
          (f' Vol.{volume}' if volume else ''))
    print('-' * 70)

    # Build query
    query_parts = [series_name]
    if volume:
        query_parts.extend(['vol', str(volume)])
    query_parts.append(str(issue_number))
    if year:
        query_parts.append(str(year))
    query = ' '.join(query_parts)

    print(f'Query: "{query}"')

    results = search_getcomics(query, max_pages=2)
    print(f'Found: {len(results)} results')

    if not results:
        print()
        return

    # Score each result
    scored = []
    for r in results:
        title = r['title']
        score, range_contains, series_match = score_getcomics_result(
            title, series_name, str(issue_number), year,
            series_volume=volume, publisher_name=publisher
        )
        decision = accept_result(score, range_contains, series_match)
        scored.append({
            'title': title,
            'link': r.get('link', ''),
            'score': score,
            'decision': decision,
        })

    # Sort
    decision_order = {'ACCEPT': 0, 'FALLBACK': 1, 'REJECT': 2}
    scored.sort(key=lambda x: (decision_order.get(x['decision'], 3), -x['score']))

    # Show top 5
    print()
    for i, s in enumerate(scored[:5], 1):
        marker = ''
        if s['decision'] == 'ACCEPT':
            marker = ' [SELECT]'
        elif s['decision'] == 'FALLBACK':
            marker = ' [FALLBACK]'
        print(f'  {i}. {marker:12} s={s["score"]:3} | {s["title"][:55]}')
    print()


def main():
    """Run simulation tests."""
    print('=' * 70)
    print('GETCOMICS SEARCH & SCORING SIMULATION')
    print('=' * 70)
    print()

    # Test cases: (series, issue, year, volume, publisher)
    tests = [
        # Batman variants
        ('Batman', '1', 2025, 3, 'DC'),
        ('Batman', '50', 2025, 3, 'DC'),
        ('Batman Rebirth', '1', 2016, 3, 'DC'),
        ('Absolute Batman', '1', 2025, 1, 'DC'),

        # Other series
        ('Flash Gordon', '1', 2024, None, 'Ahoy'),
        ('Nightwing', '1', 2025, None, 'DC'),
        ('Captain America', '1', 2005, 6, 'Marvel'),
        ('Justice League Dark Annual', '1', None, None, 'DC'),

        # Format variants - should reject
        ('Batman', '1', 2025, 3, 'DC'),  # Will see TPB/Omnibus in results

        # Range packs
        ('Batman', '5', 2025, 3, 'DC'),
    ]

    for series, issue, year, volume, pub in tests:
        test_series_search(series, issue, year, volume, pub)
        time.sleep(2)


if __name__ == '__main__':
    main()