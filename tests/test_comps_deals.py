from datetime import date

from ebayflip.comps_deals import (
    CandidateItem,
    CompPoint,
    CompSummary,
    compute_comp_summary,
    confidence_from_summary,
    filter_outlier_comps,
    normalize_title,
)


def test_normalize_title_strips_junk_and_extracts_attributes() -> None:
    title = "Apple iPhone 14 128GB Unlocked - Read Description!"
    title_norm, attributes, query = normalize_title(title)

    assert "read description" not in title_norm
    assert attributes["storage_gb"] == 128
    assert attributes["unlocked"] is True
    assert "128gb" in query
    assert "unlocked" in query


def test_filter_outlier_comps_removes_cases() -> None:
    candidate = CandidateItem(
        source="manual",
        title_raw="iPhone 14",
        title_norm="iphone 14",
        buy_price_gbp=200.0,
        condition_hint=None,
        url=None,
        attributes={},
    )
    comps = [
        CompPoint(10.0, 0.0, 10.0, date.today(), "iPhone 14 case", None),
        CompPoint(200.0, 0.0, 200.0, date.today(), "iPhone 14 phone", None),
    ]
    filtered = filter_outlier_comps(comps, candidate)
    assert len(filtered) == 1
    assert filtered[0].title == "iPhone 14 phone"


def test_filter_outlier_comps_allows_cases_when_requested() -> None:
    candidate = CandidateItem(
        source="manual",
        title_raw="iPhone 14 case",
        title_norm="iphone 14 case",
        buy_price_gbp=10.0,
        condition_hint=None,
        url=None,
        attributes={},
    )
    comps = [
        CompPoint(10.0, 0.0, 10.0, date.today(), "iPhone 14 case", None),
    ]
    filtered = filter_outlier_comps(comps, candidate)
    assert len(filtered) == 1


def test_compute_comp_summary_stats() -> None:
    comps = [
        CompPoint(100.0, 0.0, 100.0, None, None, None),
        CompPoint(200.0, 0.0, 200.0, None, None, None),
        CompPoint(300.0, 0.0, 300.0, None, None, None),
        CompPoint(400.0, 0.0, 400.0, None, None, None),
    ]
    summary = compute_comp_summary("iphone 14", comps, days=30)
    assert summary.sold_median_gbp == 250.0
    assert summary.sold_p25_gbp == 100.0
    assert summary.sold_p75_gbp == 300.0


def test_confidence_scoring() -> None:
    high = CompSummary(
        sold_points=[],
        sold_median_gbp=100.0,
        sold_p25_gbp=90.0,
        sold_p75_gbp=110.0,
        sample_size=30,
        days=30,
        query_used="iphone 14",
    )
    medium = CompSummary(
        sold_points=[],
        sold_median_gbp=100.0,
        sold_p25_gbp=80.0,
        sold_p75_gbp=130.0,
        sample_size=12,
        days=30,
        query_used="iphone 14",
    )
    low = CompSummary(
        sold_points=[],
        sold_median_gbp=100.0,
        sold_p25_gbp=50.0,
        sold_p75_gbp=150.0,
        sample_size=5,
        days=30,
        query_used="iphone 14",
    )
    assert confidence_from_summary(high) == "high"
    assert confidence_from_summary(medium) == "medium"
    assert confidence_from_summary(low) == "low"
