#!/usr/bin/env python3
"""Unit checks for Pokémon singles scrape wiring (network optional)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scraper


def test_select_chart_sku_prefers_near_mint_for_singles():
    skus = [
        {"language": "English", "condition": "Heavily Played", "variant": "Holofoil", "buckets": [
            {"bucketStartDate": "2026-09-01", "quantitySold": 1, "marketPrice": 3}
        ]},
        {"language": "English", "condition": "Near Mint", "variant": "Holofoil", "buckets": [
            {"bucketStartDate": "2026-09-01", "quantitySold": 4, "marketPrice": 15.91}
        ]},
        {"language": "English", "condition": "Lightly Played", "variant": "Holofoil", "buckets": [
            {"bucketStartDate": "2026-09-01", "quantitySold": 2, "marketPrice": 9}
        ]},
    ]
    sku = scraper.select_chart_sku(skus)
    assert sku["condition"] == "Near Mint"
    assert scraper.parse_history_buckets({"result": skus})[0]["marketPrice"] == 15.91


def test_select_chart_sku_filters_condition():
    skus = [
        {"language": "English", "condition": "Near Mint", "variant": "Holofoil", "buckets": [
            {"bucketStartDate": "2026-09-01", "quantitySold": 4, "marketPrice": 15.91}
        ]},
        {"language": "English", "condition": "Lightly Played", "variant": "Holofoil", "buckets": [
            {"bucketStartDate": "2026-09-01", "quantitySold": 2, "marketPrice": 9.25}
        ]},
    ]
    lp = scraper.select_chart_sku(skus, condition="Lightly Played")
    assert lp["condition"] == "Lightly Played"
    assert scraper.parse_history_buckets({"result": skus}, condition="Lightly Played")[0]["marketPrice"] == 9.25
    assert scraper.select_chart_sku(skus, condition="Damaged") is None


def test_listing_quantity_from_aggs():
    aggs = {"quantity": [
        {"value": 1, "count": 52},
        {"value": 2, "count": 2},
    ]}
    assert scraper.listing_quantity_from_aggs(aggs) == 56


def test_build_condition_stats_merges_listings_and_charts():
    month = {"result": [
        {"language": "English", "condition": "Near Mint", "variant": "Holofoil", "buckets": [
            {"bucketStartDate": "2026-09-09", "quantitySold": 3, "marketPrice": "16.82"},
            {"bucketStartDate": "2026-09-10", "quantitySold": 1, "marketPrice": "16.90"},
        ]},
        {"language": "English", "condition": "Lightly Played", "variant": "Holofoil", "buckets": [
            {"bucketStartDate": "2026-09-09", "quantitySold": 5, "marketPrice": "9.10"},
            {"bucketStartDate": "2026-09-10", "quantitySold": 0, "marketPrice": "9.10"},
        ]},
    ]}
    listings = {
        "Near Mint": {"currentSellers": 54, "currentQuantity": 56},
        "Lightly Played": {"currentSellers": 63, "currentQuantity": 85},
    }
    stats = scraper.build_condition_stats(month, listings, "2026-09-10")
    assert stats["Near Mint"]["marketPrice"] == 16.9
    assert stats["Near Mint"]["lastDaySales"] == 3
    assert stats["Near Mint"]["currentQuantity"] == 56
    assert stats["Near Mint"]["currentSellers"] == 54
    assert stats["Lightly Played"]["marketPrice"] == 9.1
    assert stats["Lightly Played"]["lastDaySales"] == 5
    assert stats["Lightly Played"]["currentQuantity"] == 85
    charts = scraper.condition_charts_from_month(month)
    assert charts["Lightly Played"]["sold"] == [5, 0]
    assert charts["Lightly Played"]["prices"] == [9.1, 9.1]


def test_merge_keeps_condition_charts():
    previous = {
        "productId": "91144",
        "preferredCondition": "Near Mint",
        "ranges": {"1M": {"points": [{"date": "2026-09-01", "marketPrice": 16}]}},
        "conditionCharts": {
            "Lightly Played": {"variant": "Holofoil", "dates": ["2026-09-01"], "prices": [9], "sold": [2]},
        },
    }
    incoming = {
        "productId": "91144",
        "ranges": {"1M": {"points": []}},
        "conditionCharts": {"Lightly Played": {"variant": "Holofoil", "dates": [], "prices": [], "sold": []}},
    }
    merged = scraper.merge_chart_product(previous, incoming)
    assert merged["ranges"]["1M"]["points"][0]["marketPrice"] == 16
    assert merged["conditionCharts"]["Lightly Played"]["sold"] == [2]
    assert merged["preferredCondition"] == "Near Mint"


def test_select_chart_sku_still_prefers_unopened_sealed():
    skus = [
        {"language": "English", "condition": "Near Mint", "variant": "Normal", "buckets": [
            {"bucketStartDate": "2026-09-01", "quantitySold": 2, "marketPrice": 12}
        ]},
        {"language": "English", "condition": "Unopened", "variant": "Normal", "buckets": [
            {"bucketStartDate": "2026-09-01", "quantitySold": 8, "marketPrice": 199}
        ]},
    ]
    sku = scraper.select_chart_sku(skus)
    assert sku["condition"] == "Unopened"


def test_read_singles_entries():
    os.environ.pop("SCRAPE_SINGLES_LIMIT", None)
    os.environ.pop("SCRAPE_SINGLES", None)
    entries = scraper.read_singles_entries()
    assert len(entries) >= 3000
    assert all(row.get("productKind") == "single" for row in entries[:5])
    assert entries[0]["url"].startswith("https://www.tcgplayer.com/product/")
    os.environ["SCRAPE_SINGLES_LIMIT"] = "3"
    assert len(scraper.read_singles_entries()) == 3
    os.environ["SCRAPE_SINGLES"] = "0"
    assert scraper.read_singles_entries() == []
    os.environ.pop("SCRAPE_SINGLES_LIMIT", None)
    os.environ.pop("SCRAPE_SINGLES", None)


def test_merge_condition_stats_keeps_prices():
    previous = {
        "Near Mint": {
            "variant": "Holofoil",
            "language": "English",
            "marketPrice": 2.54,
            "lastDaySales": 2,
            "currentQuantity": 90,
            "currentSellers": 70,
        }
    }
    incoming = {
        "Near Mint": {
            "variant": "",
            "language": "English",
            "marketPrice": None,
            "lastDaySales": None,
            "currentQuantity": 106,
            "currentSellers": 79,
        },
        "Lightly Played": {
            "variant": "",
            "language": "English",
            "marketPrice": None,
            "lastDaySales": None,
            "currentQuantity": 27,
            "currentSellers": 23,
        },
    }
    merged = scraper.merge_condition_stats(previous, incoming)
    assert merged["Near Mint"]["marketPrice"] == 2.54
    assert merged["Near Mint"]["lastDaySales"] == 2
    assert merged["Near Mint"]["currentQuantity"] == 106
    assert merged["Near Mint"]["currentSellers"] == 79
    assert merged["Near Mint"]["variant"] == "Holofoil"
    assert merged["Lightly Played"]["currentQuantity"] == 27


def test_upsert_does_not_wipe_condition_prices():
    existing = [{
        "date": "2026-09-10",
        "productId": "201170",
        "productKind": "single",
        "preferredCondition": "Near Mint",
        "marketPrice": 2.54,
        "lastDaySales": 0,
        "currentQuantity": 90,
        "currentSellers": 70,
        "conditions": {
            "Near Mint": {
                "variant": "Holofoil",
                "marketPrice": 2.54,
                "lastDaySales": 0,
                "currentQuantity": 90,
                "currentSellers": 70,
            }
        },
    }]
    incoming = [{
        "date": "2026-09-10",
        "productId": "201170",
        "productKind": "single",
        "preferredCondition": "Near Mint",
        "marketPrice": 2.54,
        "lastDaySales": None,
        "currentQuantity": 106,
        "currentSellers": 79,
        "conditions": {
            "Near Mint": {
                "variant": "",
                "marketPrice": None,
                "lastDaySales": None,
                "currentQuantity": 106,
                "currentSellers": 79,
            }
        },
    }]
    merged = scraper.upsert_records(existing, incoming)
    nm = merged[0]["conditions"]["Near Mint"]
    assert merged[0]["marketPrice"] == 2.54
    assert merged[0]["lastDaySales"] == 0
    assert nm["marketPrice"] == 2.54
    assert nm["lastDaySales"] == 0
    assert nm["currentQuantity"] == 106
    assert nm["variant"] == "Holofoil"


def test_backfill_preferred_condition_copies_top_level():
    row = {
        "productKind": "single",
        "preferredCondition": "Near Mint",
        "marketPrice": 2.54,
        "lastDaySales": 0,
        "currentQuantity": 106,
        "currentSellers": 79,
        "conditions": {
            "Near Mint": {
                "variant": "",
                "marketPrice": None,
                "lastDaySales": None,
                "currentQuantity": 106,
                "currentSellers": 79,
            }
        },
    }
    filled = scraper.backfill_preferred_condition(row)
    nm = filled["conditions"]["Near Mint"]
    assert nm["marketPrice"] == 2.54
    assert nm["lastDaySales"] == 0


def test_discord_omits_single_card_names():
    games = [{"id": "pokemon", "name": "Pokemon", "families": [{"id": "xy", "name": "XY"}]}]
    set_index = {"Flashfire": {"game_id": "pokemon", "game_name": "Pokemon", "family_id": "xy", "family_name": "XY"}}
    family_index = {"xy": {"game_id": "pokemon", "game_name": "Pokemon", "family_id": "xy", "family_name": "XY"}}
    sealed = [{"ok": False, "reason": "empty page", "setName": "Flashfire", "productName": "Flashfire Booster Box", "familyId": "xy", "productKind": "booster-box"}]
    blocks = scraper.format_scrape_status(sealed, games, set_index, family_index)
    text = "\n".join(blocks)
    assert "Flashfire Booster Box" in text


if __name__ == "__main__":
    test_select_chart_sku_prefers_near_mint_for_singles()
    test_select_chart_sku_filters_condition()
    test_listing_quantity_from_aggs()
    test_build_condition_stats_merges_listings_and_charts()
    test_merge_keeps_condition_charts()
    test_merge_condition_stats_keeps_prices()
    test_upsert_does_not_wipe_condition_prices()
    test_backfill_preferred_condition_copies_top_level()
    test_select_chart_sku_still_prefers_unopened_sealed()
    test_read_singles_entries()
    test_discord_omits_single_card_names()
    print("ok")
