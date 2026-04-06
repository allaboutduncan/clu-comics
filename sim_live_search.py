"""
Live GetComics search and scoring simulation.
Performs actual GetComics searches and shows raw results + scoring.
"""
import sys
sys.path.insert(0, 'C:/Users/trumb/clu-comics')

from models.getcomics import search_getcomics, score_getcomics_result, accept_result, parse_result_title
from models.getcomics import normalize_series_name


def search_and_score(series_name, issue_number, year=None, volume=None, volume_year=None, max_pages=2):
    """Perform a GetComics search and show all results with scoring."""
    print('=' * 80)
    print(f'SEARCH AND SCORE: {series_name} #{issue_number}' + (f' ({year})' if year else ''))
    print('=' * 80)

    # Build search query
    query_parts = [series_name]
    if volume:
        query_parts.extend(['vol', str(volume)])
    query_parts.append(str(issue_number))
    if year:
        query_parts.append(str(year))
    query = ' '.join(query_parts)

    print(f'\nQuery: "{query}"')
    print('-' * 60)

    # Perform actual search
    results = search_getcomics(query, max_pages=max_pages)

    if not results:
        print('No results found!')
        return

    print(f'Found {len(results)} results\n')

    # Show ALL raw results first
    print('## RAW RESULTS (before scoring) ##')
    print('-' * 60)
    for i, r in enumerate(results, 1):
        print(f'{i:2}. {r["title"]}')
    print()

    # Now show each result with scoring
    print('## SCORED RESULTS ##')
    print('-' * 60)

    scored_results = []
    for r in results:
        title = r['title']

        # Parse the title
        parsed = parse_result_title(title)
        print(f'\nTitle: {title}')
        print(f'  Parsed: name="{parsed.get("name")}", volume={parsed.get("volume")}, '
              f'issue={parsed.get("issue")}, year={parsed.get("year")}, '
              f'range={parsed.get("issue_range")}, arc={parsed.get("is_arc")}, '
              f'format_variants={parsed.get("format_variants")}')
        print(f'  Multi-content: multi={parsed.get("is_multi_series")}, '
              f'range={parsed.get("is_range_pack")}, '
              f'bulk={parsed.get("is_bulk_pack")}, '
              f'tpb_in_pack={parsed.get("has_tpb_in_pack")}')

        # Score it
        score, range_contains, series_match = score_getcomics_result(
            title, series_name, issue_number, year,
            series_volume=volume,
            volume_year=volume_year
        )
        decision = accept_result(score, range_contains, series_match)

        scored_results.append({
            'title': title,
            'link': r['link'],
            'score': score,
            'range_contains': range_contains,
            'series_match': series_match,
            'decision': decision
        })

        print(f'  Score: {score}, range_contains={range_contains}, series_match={series_match}')
        print(f'  Decision: {decision}')

    # Sort by decision priority then score
    print('\n\n## SUMMARY (sorted by decision then score) ##')
    print('-' * 60)
    decision_order = {'ACCEPT': 0, 'FALLBACK': 1, 'REJECT': 2}
    scored_results.sort(key=lambda x: (decision_order.get(x['decision'], 3), -x['score']))

    for i, r in enumerate(scored_results, 1):
        decision = r['decision']
        score = r['score']
        title = r['title']
        # Truncate long titles
        if len(title) > 60:
            title = title[:57] + '...'
        print(f'{i:2}. [{decision:8}] score={score:3} | {title}')


def main():
    """Run multiple search/score tests."""
    tests = [
        # (series, issue, year, volume, volume_year)
        ('Batman', '1', 2025, None, None),
        ('Batman', '50', 2025, 3, None),
        ('Flash Gordon', '1', 2024, None, 2024),
        ('Captain America', '1', 2005, 6, None),
        ('Nightwing', '1', 2025, None, None),
        ('Batman Rebirth', '1', 2016, None, None),
        ('Batman Vol 3', '1', 2017, None, None),
    ]

    for series, issue, year, volume, volume_year in tests:
        search_and_score(series, issue, year, volume, volume_year)
        print('\n\n')

        # Rate limit between searches
        import time
        time.sleep(3)


if __name__ == '__main__':
    main()
