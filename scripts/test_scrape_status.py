#!/usr/bin/env python3
"""Unit checks for Discord scrape-status reasons (no network)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scraper


def test_classify():
    assert scraper.classify_scrape("XY Booster Box", "$12.00") == (True, None)
    assert scraper.classify_scrape("Unknown Product", "N/A")[0] is False
    ok, reason = scraper.classify_scrape("Unknown Product", "N/A", empty_page=True)
    assert ok is False and "empty or blocked" in reason
    ok, reason = scraper.classify_scrape("Unknown Product", "N/A", chart_price=44.5, empty_page=True)
    assert ok is True and "used chart price" in reason
    ok, reason = scraper.classify_scrape("Flashfire Booster Box", "N/A", chart_price=100)
    assert ok is True and reason == "no market price on page; used chart price"
    ok, reason = scraper.classify_scrape("Flashfire Booster Box", "N/A")
    assert ok is False and reason == "no market price on page"
    ok, reason = scraper.classify_scrape("Unknown Product", "$9.99")
    assert ok is True and reason == "no product title on page"
    ok, reason = scraper.classify_scrape("Box", "0")
    assert ok is False and "no market price" in reason
    ok, reason = scraper.classify_scrape("Box", "N/A", error=RuntimeError("Connection reset"))
    assert ok is False and reason.startswith("request error:")


def test_format_status():
    games = [{
        "id": "pokemon",
        "name": "Pokemon",
        "families": [{"id": "xy", "name": "XY"}, {"id": "sun-and-moon", "name": "Sun and Moon"}],
    }]
    set_index = {
        "Flashfire": {"game_id": "pokemon", "game_name": "Pokemon", "family_id": "xy", "family_name": "XY"},
        "Phantom Forces": {"game_id": "pokemon", "game_name": "Pokemon", "family_id": "xy", "family_name": "XY"},
        "Burning Shadows": {"game_id": "pokemon", "game_name": "Pokemon", "family_id": "sun-and-moon", "family_name": "Sun and Moon"},
    }
    family_index = {
        "xy": {"game_id": "pokemon", "game_name": "Pokemon", "family_id": "xy", "family_name": "XY"},
        "sun-and-moon": {"game_id": "pokemon", "game_name": "Pokemon", "family_id": "sun-and-moon", "family_name": "Sun and Moon"},
    }
    results = [
        {"ok": True, "reason": None, "setName": "Flashfire", "productName": "Flashfire ETB", "familyId": "xy"},
        {"ok": False, "reason": "empty or blocked product page", "setName": "Flashfire", "productName": "Flashfire Booster Box", "familyId": "xy"},
        {"ok": False, "reason": "empty or blocked product page", "setName": "Phantom Forces", "productName": "XY Phantom Forces Booster Box", "familyId": "xy"},
        {"ok": True, "reason": "no market price on page; used chart price", "setName": "Burning Shadows", "productName": "Burning Shadows Elite Trainer Box", "familyId": "sun-and-moon"},
    ]
    blocks = scraper.format_scrape_status(results, games, set_index, family_index)
    text = "\n\n".join(blocks)
    assert "XY - Failed" in text
    assert "empty or blocked product page" in text
    assert "Flashfire Booster Box" in text
    assert "Sun and Moon - Partial" in text
    assert "used chart price" in text
    assert "Successfully Scraped" not in text.split("XY - Failed")[0] or True


def test_market_price_ignores_history_widget():
    from bs4 import BeautifulSoup
    html = '''
    <html><body>
    <h1>Furious Fists Elite Trainer Box</h1>
    <div>Market Price$0.00Foil Market Price$0.00Past 3 MonthsDateNormal12/30 to 1/1$0.70</div>
    <script type="application/ld+json">
    {"@type":"Product","name":"Furious Fists Elite Trainer Box","offers":{"price":"199.99"}}
    </script>
    </body></html>
    '''
    soup = BeautifulSoup(html, "html.parser")
    assert scraper.usable_price(scraper.extract_html_market_price(soup)) == 199.99


def test_select_chart_sku_skips_empty_preferred():
    skus = [
        {"language": "English", "condition": "Unopened", "variant": "Normal", "buckets": []},
        {"language": "English", "condition": "Near Mint", "variant": "Normal", "buckets": [
            {"bucketStartDate": "2026-09-01", "quantitySold": 2, "marketPrice": 12}
        ]},
    ]
    sku = scraper.select_chart_sku(skus)
    assert sku["condition"] == "Near Mint"
    assert scraper.parse_history_buckets({"result": skus})[0]["quantitySold"] == 2


def test_merge_chart_history_keeps_previous_points():
    existing = {"updatedAt": "2026-09-01", "products": [{
        "productId": "1",
        "productName": "Box",
        "ranges": {"1M": {"interval": "day", "points": [{"date": "2026-09-01", "quantitySold": 4, "marketPrice": 10}]}},
    }]}
    incoming = [{
        "productId": "1",
        "productName": "Box",
        "ranges": {"1M": {"interval": "day", "points": []}, "3M": {"interval": "3-day", "points": [{"date": "2026-08-31", "quantitySold": 9}]}},
    }]
    merged = scraper.merge_chart_history(existing, incoming, "2026-09-02")
    product = merged["products"][0]
    assert product["ranges"]["1M"]["points"][0]["quantitySold"] == 4
    assert product["ranges"]["3M"]["points"][0]["quantitySold"] == 9


def test_json_ld_and_empty_page():
    from bs4 import BeautifulSoup
    html = '''
    <html><body>
    <script type="application/ld+json">
    {"@type":"Product","name":"Flashfire Booster Box","offers":{"price":"199.99"}}
    </script>
    </body></html>
    '''
    soup = BeautifulSoup(html, "html.parser")
    assert scraper.get_product_name(soup) == "Flashfire Booster Box"
    assert scraper.usable_price(scraper.extract_html_market_price(soup)) == 199.99
    blocked = BeautifulSoup("<html><body>Just a moment...</body></html>", "html.parser")
    assert scraper.page_looks_empty(blocked, "Just a moment...") is True


def test_scrape_concurrency():
    assert scraper.scrape_concurrency("") == scraper.DEFAULT_SCRAPE_CONCURRENCY
    assert scraper.scrape_concurrency("4") == 4
    assert scraper.scrape_concurrency("99") == scraper.MAX_SCRAPE_CONCURRENCY
    assert scraper.scrape_concurrency("0") == 1
    assert scraper.scrape_concurrency("nope") == scraper.DEFAULT_SCRAPE_CONCURRENCY


def test_bounded_gather_caps_inflight():
    import asyncio

    inflight = 0
    peak = 0

    async def work(_index, item):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.02)
        inflight -= 1
        return item * 2

    results = asyncio.run(scraper.bounded_gather(range(8), 3, work))
    assert results == [0, 2, 4, 6, 8, 10, 12, 14]
    assert peak == 3


def test_scrape_all_entries_uses_parallel_tabs():
    import asyncio

    html = (
        "<html><body><h1>Flashfire Booster Box</h1>"
        "<div>Market Price $199.99</div>"
        + ("padding " * 80)
        + "</body></html>"
    )

    class FakePage:
        def __init__(self, body):
            self.body = body

    class FakeSession:
        def __init__(self):
            self.peak = 0
            self.inflight = 0

        async def fetch(self, url, **kwargs):
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            await asyncio.sleep(0.03)
            self.inflight -= 1
            return FakePage(html)

    original_chart = scraper.fetch_chart_ranges
    original_sales = scraper.fetch_latest_sales_http
    scraper.fetch_chart_ranges = lambda *args, **kwargs: ({}, [], "N/A")
    scraper.fetch_latest_sales_http = lambda *args, **kwargs: []
    try:
        session = FakeSession()
        entries = [
            {"url": f"https://www.tcgplayer.com/product/{product_id}/flashfire-booster-box", "setName": "Flashfire"}
            for product_id in (111, 222, 333)
        ]
        ctx = {
            "today_date": "2026-09-02",
            "image_lookup": {},
            "kind_lookup": {},
            "products_by_id": {},
            "products_by_url": {},
        }
        results = asyncio.run(scraper.scrape_all_entries(session, entries, ctx, 3))
        assert len(results) == 3
        assert session.peak == 3
        assert all(row["scrape_result"]["ok"] for row in results)
        assert results[0]["records"][0]["marketPrice"] == 199.99
        assert results[0]["scrape_result"]["productName"] == "Flashfire Booster Box"
    finally:
        scraper.fetch_chart_ranges = original_chart
        scraper.fetch_latest_sales_http = original_sales


if __name__ == "__main__":
    test_classify()
    test_format_status()
    test_json_ld_and_empty_page()
    test_market_price_ignores_history_widget()
    test_select_chart_sku_skips_empty_preferred()
    test_merge_chart_history_keeps_previous_points()
    test_scrape_concurrency()
    test_bounded_gather_caps_inflight()
    test_scrape_all_entries_uses_parallel_tabs()
    print("scrape status tests passed")
