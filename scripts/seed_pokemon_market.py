#!/usr/bin/env python3
"""Seed chart history from TCGPlayer Infinite API for catalogued Pokémon sealed products."""

from __future__ import annotations

import datetime
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scraper  # noqa: E402

PRODUCTS_PATH = ROOT / "data" / "pokemon_products.json"


def main() -> None:
    payload = json.loads(PRODUCTS_PATH.read_text(encoding="utf-8"))
    products = payload.get("products") or []
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    existing = scraper.load_records()
    new_records = []
    chart_products = []

    for index, product in enumerate(products, start=1):
        product_id = str(product["productId"])
        name = product["name"]
        url = product["url"]
        set_name = product["setName"]
        image_url = product.get("imageUrl")
        kind = product.get("kind")
        print(f"[{index}/{len(products)}] {set_name} · {name}")
        range_payload = {}
        daily = []
        try:
            for range_name, meta in scraper.CHART_RANGES.items():
                points = scraper.fetch_price_history(product_id, range_name, url)
                range_payload[meta["key"]] = {
                    "interval": meta["interval"],
                    "label": meta["label"],
                    "points": points,
                }
                print(f"  {meta['key']}: {len(points)} buckets")
            daily = [
                {
                    "date": point["date"],
                    "quantitySold": point["quantitySold"],
                    "marketPrice": point["marketPrice"],
                }
                for point in (range_payload.get("1M") or {}).get("points") or []
            ]
        except Exception as exc:
            print(f"  history failed: {exc}")

        latest_price = None
        for row in reversed(daily):
            if row.get("marketPrice") is not None:
                latest_price = row["marketPrice"]
                break
        new_records.append({
            "date": today,
            "productId": product_id,
            "productName": name,
            "setName": set_name,
            "productKind": kind,
            "imageUrl": image_url,
            "marketPrice": latest_price,
            "recentSale": None,
            "listedMedian": None,
            "currentSellers": None,
            "currentQuantity": None,
            "lastDaySales": None,
            "url": url,
        })
        new_records.extend(
            scraper.history_records_for_product(
                product_id, name, url, daily, today, set_name, image_url, kind
            )
        )
        chart_products.append({
            "productId": product_id,
            "productName": name,
            "setName": set_name,
            "productKind": kind,
            "imageUrl": image_url,
            "url": url,
            "ranges": range_payload,
        })
        time.sleep(0.12)

    merged = scraper.upsert_records(existing, new_records)
    scraper.save_records(merged)

    chart_path = Path(scraper.CHART_HISTORY_FILE)
    existing_chart = {"products": []}
    if chart_path.exists():
        try:
            existing_chart = json.loads(chart_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing_chart = {"products": []}
    by_id = {
        str(row.get("productId")): row
        for row in (existing_chart.get("products") or [])
        if row.get("productId")
    }
    for row in chart_products:
        by_id[str(row["productId"])] = row
    scraper.save_json(scraper.CHART_HISTORY_FILE, {"updatedAt": today, "products": list(by_id.values())})
    print(f"Seeded {len(chart_products)} products. Archive {len(merged)} rows.")


if __name__ == "__main__":
    main()
