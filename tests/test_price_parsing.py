from ebayflip.ebay_client import _parse_price, _parse_shipping_text


def test_parse_price_from_range() -> None:
    price, currency = _parse_price("£129.99 to £159.99")
    assert price == 129.99
    assert currency == "GBP"


def test_parse_price_from_prefix() -> None:
    price, currency = _parse_price("from £89.50")
    assert price == 89.50
    assert currency == "GBP"


def test_parse_price_with_commas() -> None:
    price, currency = _parse_price("£1,299.00")
    assert price == 1299.00
    assert currency == "GBP"


def test_parse_shipping_free() -> None:
    shipping, currency, missing = _parse_shipping_text("Free postage")
    assert shipping == 0.0
    assert currency == "GBP"
    assert missing is False


def test_parse_shipping_not_specified() -> None:
    shipping, currency, missing = _parse_shipping_text("Postage not specified")
    assert shipping == 0.0
    assert currency == "GBP"
    assert missing is True


def test_parse_shipping_cost() -> None:
    shipping, currency, missing = _parse_shipping_text("£4.99 postage")
    assert shipping == 4.99
    assert currency == "GBP"
    assert missing is False
