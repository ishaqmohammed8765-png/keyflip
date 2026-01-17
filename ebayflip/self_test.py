from __future__ import annotations

from ebayflip.ebay_client import SearchCriteria, _build_retry_steps, _broaden_query


def run_self_tests() -> None:
    broadened = _broaden_query('iPhone 13 128GB "Unlocked" Graphite')
    assert broadened == "iPhone 13", f"Unexpected broadened query: {broadened}"
    broadened = _broaden_query("iphone14")
    assert broadened == "iphone 14", f"Unexpected broadened query: {broadened}"

    base = SearchCriteria(
        query="Nintendo Switch OLED 64GB White",
        category_id="123",
        condition="3000",
        max_buy_gbp=250.0,
        shipping_max_gbp=10.0,
        listing_type="any",
    )
    steps = _build_retry_steps(base)
    step_labels = [label for label, _ in steps]
    assert step_labels[:4] == [
        "initial",
        "removed category filter",
        "removed condition filter",
        "removed price filters",
    ], f"Unexpected retry order: {step_labels}"


if __name__ == "__main__":
    run_self_tests()
    print("Self-tests passed.")
