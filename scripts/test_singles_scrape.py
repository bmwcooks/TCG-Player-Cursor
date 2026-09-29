#!/usr/bin/env python3
"""Unit checks for Pokémon singles scrape wiring (network optional)."""
import json
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


def test_compact_range_block_fits_cloudflare_asset_limit():
    block = {
        "interval": "day",
        "label": "1M · daily",
        "points": [
            {"date": "2026-09-01", "quantitySold": 4, "transactionCount": 3, "marketPrice": 15.91, "lowSalePrice": 14, "highSalePrice": 16},
            {"date": "2026-09-02", "quantitySold": 1, "transactionCount": 1, "marketPrice": 16.1, "lowSalePrice": 16, "highSalePrice": 16.2},
        ],
    }
    compact = scraper.compact_range_block(block)
    assert "points" not in compact
    points = scraper.range_points(compact)
    assert points[0]["date"] == "2026-09-01"
    assert points[0]["marketPrice"] == 15.91
    assert points[1]["quantitySold"] == 1
    incoming = {"productId": "1", "ranges": {"1M": block}}
    compact_product = scraper.compact_singles_chart_product(incoming)
    merged = scraper.merge_chart_product(
        compact_product,
        {"productId": "1", "ranges": {"1M": {"interval": "day", "points": []}}},
    )
    assert scraper.range_points(merged["ranges"]["1M"])[0]["quantitySold"] == 4


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


def _single_snapshot(date, product_id="201170", market_price=2.54, last_day_sales=2, sellers=70, quantity=90):
    return {
        "date": date,
        "productId": product_id,
        "productName": "Test Card",
        "setName": "Test Set",
        "productKind": "single",
        "imageUrl": "https://example.test/card.jpg",
        "url": "https://www.tcgplayer.com/product/201170",
        "preferredCondition": "Near Mint",
        "marketPrice": market_price,
        "lastDaySales": last_day_sales,
        "currentQuantity": quantity,
        "currentSellers": sellers,
        "conditions": {
            "Near Mint": {
                "variant": "Holofoil",
                "marketPrice": market_price,
                "lastDaySales": last_day_sales,
                "currentQuantity": quantity,
                "currentSellers": sellers,
            }
        },
    }


def test_collapse_keeps_latest_snapshot_and_prior_sales_row():
    older = _single_snapshot("2026-09-10", market_price=2.54, last_day_sales=2, sellers=70, quantity=90)
    middle = _single_snapshot("2026-09-20", market_price=3.0, last_day_sales=None, sellers=71, quantity=91)
    newer = _single_snapshot("2026-09-24", market_price=None, last_day_sales=None, sellers=79, quantity=106)
    newer["conditions"]["Near Mint"]["marketPrice"] = None
    newer["conditions"]["Near Mint"]["lastDaySales"] = None
    newer["conditions"]["Near Mint"]["variant"] = ""
    sealed_old = {
        "date": "2026-09-23",
        "productId": "999",
        "productKind": "booster-box",
        "marketPrice": 100.0,
        "lastDaySales": 4,
        "currentSellers": None,
    }
    sealed_new = {
        "date": "2026-09-24",
        "productId": "999",
        "productKind": "booster-box",
        "marketPrice": 110.0,
        "lastDaySales": None,
        "currentSellers": 12,
    }
    collapsed = scraper.collapse_tracker_records([newer, middle, older, sealed_new, sealed_old])
    assert len(collapsed) == 4
    single_rows = [row for row in collapsed if row["productId"] == "201170"]
    assert [row["date"] for row in single_rows] == ["2026-09-10", "2026-09-24"]
    latest = single_rows[-1]
    assert latest["marketPrice"] is None
    assert latest["lastDaySales"] is None
    assert latest["currentSellers"] == 79
    assert latest["conditions"]["Near Mint"]["marketPrice"] is None
    assert latest["conditions"]["Near Mint"]["variant"] == ""
    assert single_rows[0]["lastDaySales"] == 2
    sealed_rows = [row for row in collapsed if row["productId"] == "999"]
    assert [row["date"] for row in sealed_rows] == ["2026-09-23", "2026-09-24"]
    assert sealed_rows[-1]["marketPrice"] == 110.0
    assert sealed_rows[-1]["lastDaySales"] is None
    assert sealed_rows[-1]["currentSellers"] == 12
    assert sealed_rows[0]["lastDaySales"] == 4


def test_collapse_does_not_grow_with_extra_days():
    rows = []
    for day in range(1, 31):
        rows.append(_single_snapshot(f"2026-08-{day:02d}", market_price=day, last_day_sales=day))
    collapsed = scraper.collapse_tracker_records(rows)
    assert len(collapsed) == 1
    assert collapsed[0]["date"] == "2026-08-30"
    assert collapsed[0]["marketPrice"] == 30
    again = scraper.collapse_tracker_records(collapsed + [_single_snapshot("2026-09-01", market_price=31, last_day_sales=1)])
    assert len(again) == 1
    assert again[0]["date"] == "2026-09-01"
    assert again[0]["marketPrice"] == 31
    held = scraper.collapse_tracker_records(again + [_single_snapshot("2026-09-02", market_price=32, last_day_sales=None)])
    assert len(held) == 2
    assert held[-1]["date"] == "2026-09-02"
    assert held[-1]["lastDaySales"] is None
    assert held[0]["lastDaySales"] == 1
    # A third day with no sales still keeps only the newest row plus that one sales anchor.
    held_again = scraper.collapse_tracker_records(held + [_single_snapshot("2026-09-03", market_price=33, last_day_sales=None)])
    assert len(held_again) == 2
    assert [row["date"] for row in held_again] == ["2026-09-01", "2026-09-03"]


def test_projected_tracker_size_stays_under_github_limit():
    """Two snapshots per card stay under GitHub's 100 MiB cap as the catalog grows."""
    sample = _single_snapshot("2026-09-24")
    for condition in ("Lightly Played", "Moderately Played", "Heavily Played", "Damaged"):
        sample["conditions"][condition] = {
            "variant": "Holofoil",
            "language": "English",
            "marketPrice": 1.25,
            "lastDaySales": 0,
            "currentQuantity": 10,
            "currentSellers": 8,
        }
    anchor = dict(sample)
    anchor["date"] = "2026-09-10"
    anchor["lastDaySales"] = 2
    per_pair = scraper.tracker_json_size([sample, anchor]) - 2
    for count in (3875, 8000, 20000):
        estimate = 2 + count * per_pair
        assert estimate < scraper.GITHUB_FILE_HARD_LIMIT_BYTES, (count, estimate)
        assert estimate < scraper.DATA_FILE_PUSH_LIMIT_BYTES, (count, estimate)


def test_fit_tracker_records_omits_images_only_when_over_budget():
    rows = [_single_snapshot("2026-09-24", product_id=str(product_id)) for product_id in range(1, 6)]
    fitted = scraper.fit_tracker_records(rows, limit=10**9)
    assert all(row.get("imageUrl") for row in fitted)
    # Force the slim path: budget fits records without imageUrl, not with it.
    full = scraper.tracker_json_size(scraper.collapse_tracker_records(rows))
    slim_rows = []
    for row in scraper.collapse_tracker_records(rows):
        trimmed = dict(row)
        trimmed.pop("imageUrl", None)
        slim_rows.append(trimmed)
    slim = scraper.tracker_json_size(slim_rows)
    assert slim < full
    fitted = scraper.fit_tracker_records(rows, limit=slim)
    assert all("imageUrl" not in row for row in fitted)
    try:
        scraper.fit_tracker_records(rows, limit=slim - 1)
    except RuntimeError as exc:
        assert "push guard" in str(exc)
    else:
        raise AssertionError("expected RuntimeError when even slim snapshots exceed the guard")


def test_save_records_writes_compact_latest_snapshot():
    import tempfile
    original_file = scraper.DATA_FILE
    original_dir = scraper.DATA_DIR
    try:
        with tempfile.TemporaryDirectory() as tmp:
            scraper.DATA_DIR = tmp
            scraper.DATA_FILE = os.path.join(tmp, "tracker_data.json")
            rows = [
                _single_snapshot("2026-09-01", market_price=1.0, last_day_sales=1),
                _single_snapshot("2026-09-02", market_price=2.0, last_day_sales=3),
            ]
            saved = scraper.save_records(rows)
            assert len(saved) == 1
            assert saved[0]["marketPrice"] == 2.0
            text = open(scraper.DATA_FILE, encoding="utf-8").read()
            assert "\n  " not in text
            loaded = scraper.load_records()
            assert len(loaded) == 1
            assert loaded[0]["date"] == "2026-09-02"
            assert os.path.getsize(scraper.DATA_FILE) < scraper.DATA_FILE_PUSH_LIMIT_BYTES
    finally:
        scraper.DATA_FILE = original_file
        scraper.DATA_DIR = original_dir


def test_committed_tracker_archive_is_bounded():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tracker_data.json")
    if not os.path.exists(path):
        return
    size = os.path.getsize(path)
    assert size <= scraper.DATA_FILE_PUSH_LIMIT_BYTES, size
    with open(path, encoding="utf-8") as handle:
        rows = json.load(handle)
    from collections import Counter
    ids = [str(row.get("productId") or "") for row in rows if row.get("productId")]
    assert ids
    counts = Counter(ids)
    assert max(counts.values()) <= 2


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
    test_compact_range_block_fits_cloudflare_asset_limit()
    test_merge_keeps_condition_charts()
    test_merge_condition_stats_keeps_prices()
    test_upsert_does_not_wipe_condition_prices()
    test_backfill_preferred_condition_copies_top_level()
    test_collapse_keeps_latest_snapshot_and_prior_sales_row()
    test_collapse_does_not_grow_with_extra_days()
    test_projected_tracker_size_stays_under_github_limit()
    test_fit_tracker_records_omits_images_only_when_over_budget()
    test_save_records_writes_compact_latest_snapshot()
    test_committed_tracker_archive_is_bounded()
    test_select_chart_sku_still_prefers_unopened_sealed()
    test_read_singles_entries()
    test_discord_omits_single_card_names()
    print("ok")
