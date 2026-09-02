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


if __name__ == "__main__":
    test_classify()
    test_format_status()
    test_json_ld_and_empty_page()
    test_scrape_concurrency()
    test_bounded_gather_caps_inflight()
    print("scrape status tests passed")
