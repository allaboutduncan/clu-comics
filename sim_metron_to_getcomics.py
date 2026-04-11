"""
Simulation: Metron series -> GetComics search and scoring.

Tests the full flow:
1. Query Metron for wanted series
2. Search GetComics for those series
3. Score results against Metron issue data
4. Show what would be selected
"""
import sys
sys.path.insert(0, 'C:/Users/trumb/clu-comics')

from models.metron import search_series_by_name, get_all_issues_for_series
from core.config import config
import time


def test_series(series_name, year=None):
    """Test one series end-to-end."""
    print('=' * 80)
    print(f'SERIES: {series_name}' + (f' ({year})' if year else ''))
    print('=' * 80)

    # 1. Get Metron API
    api = config.get_metron_api()
    if not api:
        print("No Metron API configured")
        return

    series = search_series_by_name(api, series_name, year)
    if not series:
        print(f"Not found on Metron: {series_name}")
        return

    print(f"\nMetron found: {series['name']} (id={series['id']}, publisher={series.get('publisher_name')})")

    # 2. Get issues for this series
    issues = get_all_issues_for_series(api, series['id'])
    if not issues:
        print("No issues found on Metron")
        return

    # Convert to dicts if needed
    if issues and hasattr(issues[0], 'dict'):
        issues = [i.dict() for i in issues]
    elif issues and hasattr(issues[0], 'model_dump'):
        issues = [i.model_dump() for i in issues]

    print(f"Metron has {len(issues)} issues")

    # Pick a few issues to test (first, middle, last)
    test_issues = []
    if len(issues) >= 3:
        test_issues = [issues[0], issues[len(issues)//2], issues[-1]]
    else:
        test_issues = issues[:3]

    for issue in test_issues:
        issue_num = issue.get('number', issue.get('issue_number', '?'))
        print(f"\n{'─' * 60}")
        print(f"Testing issue #{issue_num} from Metron")
        print(f"  Title: {issue.get('title', 'N/A')}")
        print(f"  Cover date: {issue.get('cover_date', 'N/A')}")

        # 3. Search GetComics
        query_parts = [series['name']]
        if issue.get('volume'):
            query_parts.extend(['vol', str(issue['volume'])])
        query_parts.append(str(issue_num))
        if issue.get('cover_date'):
            cover_year = str(issue['cover_date'])[:4]
            query_parts.append(cover_year)
        query = ' '.join(query_parts)

        print(f"\n  GetComics query: \"{query}\"")

        from models.getcomics import search_getcomics, score_getcomics_result, accept_result
        results = search_getcomics(query, max_pages=2)
        print(f"  GetComics returned {len(results)} results")

        if not results:
            print("  No results found!")
            time.sleep(1)
            continue

        # Score each result
        scored = []
        publisher_name = series.get('publisher_name')
        volume = issue.get('volume')
        year_search = int(str(issue.get('cover_date', ''))[:4]) if issue.get('cover_date') else None

        for r in results:
            title = r['title']
            score, range_contains, series_match = score_getcomics_result(
                title, series['name'], str(issue_num), year_search,
                series_volume=volume, publisher_name=publisher_name
            )
            decision = accept_result(score, range_contains, series_match)
            scored.append({
                'title': title,
                'link': r.get('link', ''),
                'score': score,
                'decision': decision,
                'series_match': series_match
            })

        # Sort: ACCEPT first (by score desc), then FALLBACK, then REJECT
        decision_order = {'ACCEPT': 0, 'FALLBACK': 1, 'REJECT': 2}
        scored.sort(key=lambda x: (decision_order.get(x['decision'], 3), -x['score']))

        print(f"\n  Top matches:")
        for i, s in enumerate(scored[:5], 1):
            decision = s['decision']
            marker = ''
            if decision == 'ACCEPT':
                marker = ' [SELECT]'
            elif decision == 'FALLBACK':
                marker = ' [FALLBACK]'
            print(f"  {i}. {marker:12} s={s['score']:3} | {s['title'][:60]}")

        time.sleep(1)

    print()


def main():
    # Test with a variety of series
    test_cases = [
        ("Batman", 2025),
        ("Batman Rebirth", 2016),
        ("Flash Gordon", 2024),
        ("Nightwing", 2025),
        ("Captain America", 2005),
        ("Absolute Batman", 2025),
        ("Justice League Dark Annual", None),
    ]

    for series_name, year in test_cases:
        test_series(series_name, year)
        print("\n")
        time.sleep(3)


if __name__ == '__main__':
    main()