"""
Simulation: GetComics search with simpler queries that match GetComics indexing.
"""
import sys
sys.path.insert(0, 'C:/Users/trumb/clu-comics')

# Hardcoded brand keywords matching DEFAULT_BRAND_KEYWORDS in database.py
DEFAULT_BRAND_KEYWORDS = {
    "DC": ["rebirth", "new 52", "all-star"],
    "Marvel": ["marvel now", "legacy", "ultimate", "all-new", "fresh start"],
    "Ahoy": [],
}

# Monkey-patch get_brand_keywords before importing getcomics
def patched_get_brand_keywords(publisher_name=None):
    if publisher_name:
        keywords = DEFAULT_BRAND_KEYWORDS.get(publisher_name, [])
        return [kw.lower() for kw in keywords]
    return []

import models.getcomics as gc
gc.get_brand_keywords = patched_get_brand_keywords

from models.getcomics import search_getcomics, score_getcomics_result, accept_result
import time


def test(query, series_name, issue_number, year=None, volume=None, publisher=None):
    """Test scoring for a query against expected series/issue."""
    print('-' * 70)
    print(f'Query: "{query}"')
    print(f'Expected: {series_name} #{issue_number}' + (f' ({year})' if year else ''))
    print('-' * 70)

    results = search_getcomics(query, max_pages=2)
    print(f'Found: {len(results)} results\n')

    if not results:
        print()
        return

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

    # Show all ACCEPT/FALLBACK and top REJECTs
    print('Results:')
    for i, s in enumerate(scored[:8], 1):
        marker = ''
        if s['decision'] == 'ACCEPT':
            marker = ' [SELECT]'
        elif s['decision'] == 'FALLBACK':
            marker = ' [FALLBACK]'
        print(f'  {i}. {marker:12} s={s["score"]:3} | {s["title"][:50]}')
    print()


def main():
    print('=' * 70)
    print('DIRECT GETCOMICS SEARCH SIMULATION')
    print('=' * 70)
    print()

    # Tests with queries that GetComics can find
    tests = [
        # (query, series, issue, year, volume, publisher)
        ('Batman Rebirth 1 2016', 'Batman Rebirth', '1', 2016, 3, 'DC'),
        ('Batman Rebirth 50 2017', 'Batman Rebirth', '50', 2017, 3, 'DC'),
        ('Absolute Batman 2025', 'Absolute Batman', '14', 2025, 1, 'DC'),
        ('Absolute Batman 2025 1', 'Absolute Batman', '1', 2025, 1, 'DC'),
        ('Flash Gordon 1 2024', 'Flash Gordon', '1', 2024, None, 'Ahoy'),
        ('Nightwing 2025', 'Nightwing', '1', 2025, None, 'DC'),
        ('Justice League Dark Annual 1', 'Justice League Dark Annual', '1', None, None, 'DC'),
        ('Captain America 2005', 'Captain America', '1', 2005, 5, 'Marvel'),
    ]

    for query, series, issue, year, volume, pub in tests:
        test(query, series, issue, year, volume, pub)
        time.sleep(2)


if __name__ == '__main__':
    main()