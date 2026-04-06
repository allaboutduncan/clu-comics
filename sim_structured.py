"""
Simulation script for structured matching.
Tests match_structured() and multi-content detection.
"""
from models.getcomics import parse_result_title, match_structured, normalize_series_name, get_brand_keywords

def extract_search_criteria(series_name: str, issue_number: str, year: int = None, publisher: str = None) -> dict:
    """Extract structured search criteria from series name and issue."""
    # Normalize series name and extract metadata
    normalized_name, metadata = normalize_series_name(series_name)

    # Extract brand keyword if present - detect from series name itself
    brands = get_brand_keywords(publisher) if publisher else []
    brand = None
    for b in brands:
        if b.lower() in series_name.lower():
            brand = b.lower()
            break

    return {
        'name': normalized_name,
        'volume': metadata.get('volume'),
        'issue_number': issue_number,
        'year': year,
        'brand': brand,
        'is_annual': metadata.get('is_annual', False),
        'is_crossover': metadata.get('is_crossover', False),
    }


def test_match_structured():
    """Test structured matching against various cases."""
    print('=' * 80)
    print('STRUCTURED MATCHING SIMULATION')
    print('=' * 80)

    test_cases = []
    failures = []

    def add_case(title, series, issue, year, expected_decision=None, note=''):
        # Parse the result title
        result_parsed = parse_result_title(title)
        print(f"\n--- {note} ---")
        print(f"Title: {title}")
        print(f"Parsed: name='{result_parsed['name']}', volume={result_parsed['volume']}, "
              f"issue={result_parsed['issue']}, range={result_parsed['issue_range']}, "
              f"year={result_parsed['year']}")
        print(f"Multi-content: multi={result_parsed.get('is_multi_series')}, "
              f"range={result_parsed.get('is_range_pack')}, "
              f"bulk={result_parsed.get('is_bulk_pack')}, "
              f"tpb_pack={result_parsed.get('has_tpb_in_pack')}")

        # Extract search criteria
        search = extract_search_criteria(series, issue, year)
        print(f"Search: name='{search['name']}', volume={search['volume']}, "
              f"issue={search['issue_number']}, year={search['year']}, "
              f"brand={search['brand']}, annual={search['is_annual']}")

        # Run structured match
        score, match_type = match_structured(search, result_parsed)
        print(f"Result: score={score}, match_type={match_type}")

        decision = match_type  # For now, match_type is the decision
        if expected_decision is None:
            status = 'PASS'
        elif decision == expected_decision:
            status = 'OK'
        else:
            status = 'FAIL'
            failures.append({
                'note': note,
                'title': title,
                'series': series,
                'expected': expected_decision,
                'actual': decision,
                'score': score
            })

        test_cases.append({
            'title': title,
            'series': series,
            'score': score,
            'match_type': match_type,
            'expected': expected_decision,
            'status': status,
            'note': note,
            'parsed': result_parsed
        })
        return status

    print('\n## CROSSOVER SEPARATOR TESTS ##')
    print('-' * 60)
    add_case('Batman & Robin #1 (2025)', 'Batman & Robin', '1', 2025, 'accept', 'Exact & match')
    add_case('Batman and Robin #1 (2025)', 'Batman and Robin', '1', 2025, 'accept', 'Exact and match')
    add_case('Batman / Robin #1 (2025)', 'Batman / Robin', '1', 2025, 'accept', 'Exact / match')

    # Cross matching
    add_case('Batman & Robin #1 (2025)', 'Batman and Robin', '1', 2025, 'accept', '& vs and')
    add_case('Batman and Robin #1 (2025)', 'Batman & Robin', '1', 2025, 'accept', 'and vs &')

    # Non-matching crossovers
    add_case('Batman & Robin #1 (2025)', 'Batman', '1', 2025, 'reject', 'Batman & Robin != Batman')

    print('\n## BRAND KEYWORD TESTS ##')
    print('-' * 60)
    # Brand matching via arc detection works even without brand config
    # (this is expected behavior - arc name matching coincident with brand name)
    add_case('Batman Vol. 3 - Rebirth #1 (2025)', 'Batman Rebirth', '1', 2025, 'accept', 'Batman Vol.3-Rebirth arc matches Batman Rebirth')
    add_case('Batman Vol. 3 #1 (2025)', 'Batman Rebirth', '1', 2025, 'reject', 'Batman Vol.3 vs Batman Rebirth (no brand/arc match)')

    # Different volume without brand should fail
    add_case('Batman Vol. 6 #1 (2025)', 'Batman Vol. 3', '1', 2025, 'reject', 'Vol.6 vs Vol.3 - different volumes')

    print('\n## ANNUAL SERIES TESTS ##')
    print('-' * 60)
    add_case('Nightwing Annual Vol. 1 #1 (2025)', 'Nightwing Annual', '1', 2025, 'accept', 'Nightwing Annual exact')
    add_case('Nightwing Annual Vol. 1 #1 (2025)', 'Nightwing', '1', 2025, 'reject', 'Nightwing Annual != Nightwing')

    # Year designation creates different series
    add_case('Justice League Dark 2021 Annual Vol. 1 #1 (2021)', 'Justice League Dark Annual', '1', 2021, 'reject', '2021 Annual != plain Annual')

    print('\n## ISSUE RANGE TESTS ##')
    print('-' * 60)
    add_case('Batman #1-50 (2025)', 'Batman', '1', 2025, 'fallback', 'Range pack #1-50 contains #1')
    add_case('Batman #1-50 (2025)', 'Batman', '25', 2025, 'fallback', 'Range pack #1-50 contains #25')
    add_case('Batman Vol. 3 #1-18 (2025)', 'Batman Vol. 3', '5', 2025, 'fallback', 'Range pack Vol.3 #1-18 contains #5')
    add_case('Batman Vol. 5 #1-50 (2025)', 'Batman Vol. 3', '1', 2025, 'reject', 'Range Vol.5 != search Vol.3')

    # TPB range pack
    add_case('Captain America Vol. 5 #1 - 50 + TPBs (2020)', 'Captain America Vol. 5', '1', 2020, 'fallback', 'TPB range pack contains #1')

    print('\n## VARIANT TYPE TESTS ##')
    print('-' * 60)
    add_case('Batman Vol. 5 TPB #1 (2025)', 'Batman', '1', 2025, 'fallback', 'TPB variant - fallback')
    add_case('Batman Vol. 5 TPB #1 (2025)', 'Batman Vol. 5', '1', 2025, 'fallback', 'TPB with exact volume')
    add_case('Absolute Batman Vol. 1 #1 (2025)', 'Batman', '1', 2025, 'reject', 'Absolute Batman != Batman')
    add_case('Absolute Batman Vol. 1 #1 (2025)', 'Absolute Batman', '1', 2025, 'accept', 'Absolute Batman exact match')

    print('\n## ARC DETECTION TESTS ##')
    print('-' * 60)
    add_case('Batman - Court of Owls #1 (2025)', 'Batman', '1', 2025, 'fallback', 'Arc sub-series - fallback')
    add_case('Batman - Court of Owls #1-11 (2025)', 'Batman', '5', 2025, 'fallback', 'Arc range pack - fallback')
    add_case('Batman - Court of Owls #1 (2025)', 'Batman - Dark Knight', '1', 2025, 'reject', 'Different arcs')

    print('\n## TITLE FORMAT TESTS ##')
    print('-' * 60)
    add_case('Batman: Year One #1 (2025)', 'Batman: Year One', '1', 2025, 'accept', 'Colon separator')
    add_case('Batman - Year One #1 (2025)', 'Batman: Year One', '1', 2025, 'accept', 'Dash vs Colon')
    add_case('All-Star Batman & Steve Robin #1 (2025)', 'All-Star Batman', '1', 2025, 'reject', 'Longer series with &')
    add_case('Batman: One-Shot #1 (2025)', 'Batman', '1', 2025, 'fallback', 'Oneshot variant')

    print('\n## YEAR MISMATCH TESTS ##')
    print('-' * 60)
    add_case('Batman #1 (2020)', 'Batman', '1', 2025, 'reject', 'Wrong year 2020 vs 2025')
    add_case('Batman #1 (2025)', 'Batman', '1', 2025, 'accept', 'Correct year')

    print('\n## RESULTS SUMMARY ##')
    print('=' * 80)

    passed = sum(1 for t in test_cases if t['status'] in ('PASS', 'OK'))
    failed = sum(1 for t in test_cases if t['status'] == 'FAIL')
    total = len(test_cases)

    print(f'Total: {passed} passed, {failed} failed, {total} total')

    if failures:
        print('\n## FAILING CASES ##')
        for f in failures:
            print(f"FAIL: {f['note']}")
            print(f"  Title: {f['title']}")
            print(f"  Series: {f['series']}")
            print(f"  Expected: {f['expected']}, Got: {f['actual']} (score={f['score']})")

    return passed, failed, total, failures


def test_multi_content_detection():
    """Test multi-content detection in parse_result_title."""
    print('\n\n' + '=' * 80)
    print('MULTI-CONTENT DETECTION TESTS')
    print('=' * 80)

    test_cases = [
        # (title, expected_is_multi, expected_is_range, expected_is_bulk, description)
        ('Batman #1 (2025)', False, False, False, 'Single issue'),
        ('Batman #1-50 (2025)', False, True, True, 'Range pack (bulk)'),
        ('Batman #1-5 (2025)', False, True, False, 'Range pack (small)'),
        ('Batman Vol. 5 #1 - 50 + TPBs (2020)', False, True, True, 'TPB range pack'),
        ('Batman & Robin #1 (2025)', True, False, False, 'Crossover (&)'),
        ('Batman / Superman #1 (2025)', True, False, False, 'Crossover (/)'),
        ('Batman + Robin #1 (2025)', True, False, False, 'Crossover (+)'),
        ('Batman: One-Shot #1 (2025)', False, False, False, 'Oneshot (not multi)'),
        ('Batman: The Killing Joke (2025)', False, False, False, 'Oneshot with colon'),
        ('Batman #1 (2025) + TPBs', False, False, False, 'Single issue with TPB (no range)'),
    ]

    failures = []
    for title, exp_multi, exp_range, exp_bulk, desc in test_cases:
        parsed = parse_result_title(title)
        actual_multi = parsed.get('is_multi_series', False)
        actual_range = parsed.get('is_range_pack', False)
        actual_bulk = parsed.get('is_bulk_pack', False)

        multi_ok = actual_multi == exp_multi
        range_ok = actual_range == exp_range
        bulk_ok = actual_bulk == exp_bulk

        status = 'OK' if (multi_ok and range_ok and bulk_ok) else 'FAIL'
        if status == 'FAIL':
            failures.append({
                'title': title,
                'desc': desc,
                'expected': (exp_multi, exp_range, exp_bulk),
                'actual': (actual_multi, actual_range, actual_bulk)
            })

        print(f"{status}: {title[:50]:50s} | multi={actual_multi} range={actual_range} bulk={actual_bulk} | {desc}")

    print(f"\nMulti-content detection: {len(test_cases) - len(failures)}/{len(test_cases)} passed")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  {f['desc']}: {f['title']}")
            print(f"    Expected: {f['expected']}, Got: {f['actual']}")

    return len(test_cases) - len(failures), len(failures)


if __name__ == '__main__':
    p1, f1, t1, fail1 = test_match_structured()
    p2, f2 = test_multi_content_detection()

    print('\n\n' + '=' * 80)
    print('OVERALL SUMMARY')
    print('=' * 80)
    print(f'Structured matching: {p1}/{t1} passed, {f1} failed')
    print(f'Multi-content detection: {p2}/{p2+f2} passed, {f2} failed')