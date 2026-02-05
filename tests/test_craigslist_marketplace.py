from ebayflip.config import RunSettings
from ebayflip.ebay_client import EbayClient, SearchCriteria, _build_html_params, parse_html
from ebayflip.models import Target


def test_build_html_params_for_craigslist() -> None:
    criteria = SearchCriteria(
        query="iphone",
        category_id=None,
        condition=None,
        max_buy_gbp=250.0,
        shipping_max_gbp=None,
        listing_type="any",
    )
    settings = RunSettings(marketplace="craigslist", craigslist_site="london")
    params = _build_html_params(criteria, page=2, settings=settings)
    assert params["query"] == "iphone"
    assert params["max_price"] == 250
    assert params["s"] == 120


def test_parse_html_for_craigslist_cards() -> None:
    html = """
    <ul>
      <li class='cl-static-search-result' data-pid='1001'>
        <a href='https://london.craigslist.org/abc/1001.html'>
          <div class='title'>Nintendo Switch OLED</div>
        </a>
        <div class='price'>£180</div>
        <div class='location'>London</div>
      </li>
    </ul>
    """
    client = EbayClient(RunSettings(marketplace="craigslist"))
    target = Target(id=1, name="switch", query="switch")
    listings, metrics = parse_html(html, target, client)
    assert metrics["card_count"] == 1
    assert len(listings) == 1
    assert listings[0].title == "Nintendo Switch OLED"
    assert listings[0].price_gbp == 180.0
    assert listings[0].url.endswith("1001.html")
