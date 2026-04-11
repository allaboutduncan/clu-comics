"""
Show score_comic output with all intermediate ComicScore fields.
Useful for understanding WHY a result scored the way it did.
"""
import sys
import logging
logging.disable(logging.CRITICAL)
sys.path.insert(0, 'C:/Users/trumb/clu-comics')

from models.getcomics import score_comic, search_criteria, accept_result

cases = [
    # Flash Gordon #5 searches
    ("Flash Gordon", "5", 2024, None, None, None, [],
     "Flash Gordon #5 (2024)", "bare issue"),
    ("Flash Gordon", "5", 2024, None, None, None, [],
     "Flash Gordon Quarterly #5 (2024)", "Quarterly as SERIES name"),
    ("Flash Gordon", "5", 2024, None, None, None, [],
     "Flash Gordon - Quarterly #5 (2024)", "Quarterly as DASH sub-series"),
    ("Flash Gordon", "5", 2024, None, None, None, [],
     "Flash Gordon #1-12 (2024)", "range containing #5"),
    ("Flash Gordon", "5", 2024, None, None, None, [],
     "Flash Gordon #5-10 (2024)", "range ending on #5 (bulk pack)"),
    ("Flash Gordon", "5", 2024, None, None, None, [],
     "Flash Gordon Vol. 1 #5 (2024)", "bare with Vol."),
    ("Flash Gordon", "5", 2024, None, None, None, [],
     "Flash Gordon Vol. 1 - 2024 #5", "dash before year"),

    # Flash Gordon Quarterly search (should accept Quarterly variant)
    ("Flash Gordon Quarterly", "5", 2024, None, None, None, ["quarterly"],
     "Flash Gordon Quarterly #5 (2024)", "searching for Quarterly"),

    # Batman searches
    ("Batman", "1", 2020, 3, None, "DC",
     ["annual"], "Batman #1 (2020)", "basic match"),
    ("Batman", "1", 2020, 3, None, "DC",
     ["annual"], "Batman Annual #1 (2020)", "Annual variant"),
    ("Batman", "1", 2020, 3, None, "DC",
     ["annual"], "Batman Vol. 3 #1 (2020)", "Vol match"),
    ("Batman", "1", 2020, 3, None, "DC",
     ["annual"], "Batman Vol. 6 #1 (2020)", "Vol mismatch"),
    ("Batman", "1", 2020, 3, None, "DC",
     [], "Batman - Court of Owls #1 (2020)", "arc sub-series"),
    ("Batman", "1", 2020, None, None, "DC",
     [], "Batman #1-50 (2020)", "range containing #1"),
    ("Batman", "50", 2020, None, None, "DC",
     [], "Batman #1-50 (2020)", "range ending on #50 (bulk pack)"),
    ("Batman", "1", 2020, None, None, "DC",
     [], "Batman Annual #1 (2020)", "Annual (different series)"),
    ("Batman", "1", 2020, None, None, "DC",
     [], "Batman Inc #1 (2020)", "Batman Inc (different series)"),
    ("Batman", "1", 2020, None, None, "DC",
     [], "Batman Adventures #1 (2020)", "Batman Adventures (different series)"),
    ("Batman", "1", 2020, None, None, "DC",
     [], "The Batman #1 (2020)", "The prefix swap"),
]


def fmt_field(name, value):
    """Format a field value for display, highlighting notable values."""
    if isinstance(value, bool):
        return f"  {name}: {'Y' if value else 'N'}"
    if value is None or value == 0 or value == "":
        return f"  {name}: {value!r}"
    return f"  {name}: {value}"


for series, issue, year, series_vol, vol_yr, publisher, variants, result, desc in cases:
    search = search_criteria(
        series_name=series,
        issue_number=issue,
        year=year,
        series_volume=series_vol,
        volume_year=vol_yr,
        publisher_name=publisher,
        accept_variants=variants,
    )
    cs = score_comic(result, search)
    decision = accept_result(cs.score, cs.range_contains_target, cs.series_match)

    print(f"\n{'='*70}")
    print(f"SEARCH: {series} #{issue}" + (f" ({year})" if year else ""))
    print(f"RESULT: {result}")
    print(f"DESC:   {desc}")
    print(f"{'-'*70}")
    print(f"  score: {cs.score}")
    print(f"  decision: {decision}")
    print(fmt_field("series_match", cs.series_match))
    print(fmt_field("sub_series_type", cs.sub_series_type))
    print(fmt_field("variant_accepted", cs.variant_accepted))
    print(fmt_field("detected_variant", cs.detected_variant))
    print(fmt_field("used_the_swap", cs.used_the_swap))
    print(fmt_field("remaining_is_different_series", cs.remaining_is_different_series))
    print(fmt_field("year_in_series_name", cs.year_in_series_name))
    print(fmt_field("range_contains_target", cs.range_contains_target))
