"""
Simulation: GetComics search with brand keywords.

Patches get_brand_keywords to use hardcoded defaults for testing.
"""
import sys
sys.path.insert(0, 'C:/Users/trumb/clu-comics')

# Hardcoded brand keywords matching DEFAULT_BRAND_KEYWORDS in database.py
DEFAULT_BRAND_KEYWORDS = {
    "DC": ["rebirth", "new 52", "all-star"],
    "Marvel": ["marvel now", "legacy", "ultimate", "all-new", "fresh start"],
    "Image": ["black flag", "top shelf"],
    "Dark Horse": ["maverick"],
    "IDW": ["revolution", "artist's edition"],
    "Boom": ["discovering wonderland"],
    "Dynamite": ["the dark tower"],
    "Valiant": ["bloodshot", "harbinger"],
    "Ahoy": [],  # No brand keywords for Ahoy
}

# Monkey-patch get_brand_keywords before importing getcomics
def patched_get_brand_keywords(publisher_name=None):
    if publisher_name:
        keywords = DEFAULT_BRAND_KEYWORDS.get(publisher_name, [])
        return [kw.lower() for kw in keywords]
    return []

# Apply patch
import models.getcomics as gc
gc.get_brand_keywords = patched_get_brand_keywords

# Now import scoring functions (they'll use our patched version)
from models.getcomics import search_getcomics, score_getcomics_result, accept_result
import time


def test_series_search(series_name, issue_number, year=None, volume=None, publisher=None):
    """Test a search for a specific series/issue."""
    print('-' * 70)
    print(f'Search: {series_name} #{issue_number}' +
          (f' ({year})' if year else '') +
          (f' Vol.{volume}' if volume else ''))
    print(f'Publisher: {publisher}')
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
    print('=' * 70)
    print('GETCOMICS SEARCH & SCORING SIMULATION (WITH BRAND KEYWORDS)')
    print('=' * 70)
    print()

    # Test cases with brand matching potential
    tests = [
        # Batman Rebirth - should match via "Rebirth" brand
        ('Batman Rebirth', '1', 2016, 3, 'DC'),

        # Batman (Rebirth era) searching for Vol 3
        ('Batman', '1', 2016, 3, 'DC'),

        # Flash Gordon - no brand keywords for Ahoy
        ('Flash Gordon', '1', 2024, None, 'Ahoy'),

        # Nightwing - should reject 2025 Annual
        ('Nightwing', '1', 2025, None, 'DC'),

        # Captain America with Marvel brands
        ('Captain America', '1', None, 6, 'Marvel'),

        # Absolute Batman - "Absolute" is the brand/line
        ('Absolute Batman', '1', 2025, 1, 'DC'),

        # Justice League Dark Annual
        ('Justice League Dark Annual', '1', None, None, 'DC'),
    ]

    for series, issue, year, volume, pub in tests:
        test_series_search(series, issue, year, volume, pub)
        time.sleep(2)


if __name__ == '__main__':
    main()