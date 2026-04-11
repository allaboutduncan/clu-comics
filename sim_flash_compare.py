"""
Compare Flash Gordon Quarterly vs Flash Gordon - Quarterly scoring.
"""
import sys
sys.path.insert(0, 'C:/Users/trumb/clu-comics')

from models.getcomics import score_getcomics_result, accept_result

cases = [
    # (series, issue, year, result_title, expected_behavior)
    ("Flash Gordon", "5", 2024, "Flash Gordon Quarterly #5 (2024)", "Quarterly as SERIES name"),
    ("Flash Gordon", "5", 2024, "Flash Gordon - Quarterly #5 (2024)", "Quarterly as DASH sub-series"),
    ("Flash Gordon Quarterly", "5", 2024, "Flash Gordon Quarterly #5 (2024)", "Searching for Quarterly - should ACCEPT"),
]

for series, issue, year, result, desc in cases:
    score, range_contains, series_match = score_getcomics_result(
        result, series, issue, year
    )
    decision = accept_result(score, range_contains, series_match)
    print(f"\nSearching: {series} #{issue}")
    print(f"Result: {result}")
    print(f"  ({desc})")
    print(f"  Score: {score}, Decision: {decision}")