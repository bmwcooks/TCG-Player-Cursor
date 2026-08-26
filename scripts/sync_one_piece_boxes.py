#!/usr/bin/env python3
"""Discover TCGPlayer English booster boxes for catalogued One Piece sets."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from tracked_urls import write_tracked_urls  # noqa: E402

CATALOG_PATH = ROOT / "data" / "catalog.json"
PRODUCTS_PATH = ROOT / "data" / "one_piece_products.json"
PRODUCTS_URL = "https://tcgcsv.com/tcgplayer/68/{group_id}/products"
HEADERS = {"User-Agent": "TCG-Player-Cursor/1.0 (catalog sync)"}

GROUP_OVERRIDES = {
    "Romance Dawn": 3188,
    "Paramount War": 17698,
    "Pillars of Strength": 22890,
    "Kingdoms of Intrigue": 23024,
    "Awakening of the New Era": 23213,
    "Wings of the Captain": 23272,
    "500 Years in the Future": 23387,
    "Two Legends": 23462,
    "Emperors in the New World": 23589,
    "Royal Blood": 23766,
    "A Fist of Divine Speed": 24241,
    "Legacy of the Master": 24302,
    "Carrying On His Will": 24303,
    "The Azure Sea's Seven": 24537,
    "Adventure on Kami's Island": 24637,
    "The Time of Battle": 24664,
    "The World's Strongest Warriors": 24736,
    "Memorial Collection": 23333,
    "Anime 25th Collection": 23834,
    "Heroines Edition": 24545,
    "Heroines Edition vol.2": 24820,
    "The Best Vol. 1": 23496,
    "The Best Vol. 2": 24305,
}

SKIP_SETS = {"Egghead Crisis"}

SKIP_NAME = re.compile(
    r"box case|booster box case|\bcase\b|box topper|promotion pack|"
    r"double pack|display|sleeved|booster pack",
    re.I,
)

FAMILY_KIND = {
    "op": "booster-box",
    "eb": "extra-booster-box",
    "prb": "premium-booster-box",
}


def one_piece_sets():
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    game = next(item for item in catalog["games"] if item["id"] == "one-piece")
    sets = []
    for family in game["families"]:
        for item in family["sets"]:
            sets.append({**item, "familyId": family["id"], "familyName": family["name"]})
    return sets


def is_booster_box(name: str) -> bool:
    text = name or ""
    if SKIP_NAME.search(text):
        return False
    lower = text.lower()
    if "booster box" in lower:
        return True
    # Extra booster SKUs sometimes omit "Booster": "... Collection Box"
    if re.search(r"(extra booster|premium booster).*\bbox\b", lower) and "pack" not in lower:
        return True
    return False


def fetch_json(url: str) -> dict:
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.json()


def main() -> None:
    products = []
    missing_groups = []
    for item in one_piece_sets():
        set_name = item["setName"]
        if set_name in SKIP_SETS:
            print(f"{set_name}: skipped (no standalone English booster box)")
            continue
        group_id = GROUP_OVERRIDES.get(set_name)
        if not group_id:
            missing_groups.append(set_name)
            continue
        payload = fetch_json(PRODUCTS_URL.format(group_id=group_id))
        found = []
        for row in payload.get("results") or []:
            name = row.get("name") or ""
            if not is_booster_box(name):
                continue
            product_id = str(row["productId"])
            url = row.get("url") or f"https://www.tcgplayer.com/product/{product_id}"
            if "Language=English" not in url:
                joiner = "&" if "?" in url else "?"
                url = f"{url}{joiner}page=1&Language=English"
            found.append({
                "productId": product_id,
                "name": name,
                "setName": set_name,
                "familyId": item["familyId"],
                "kind": FAMILY_KIND.get(item["familyId"], "booster-box"),
                "url": url,
                "imageUrl": row.get("imageUrl") or f"https://tcgplayer-cdn.tcgplayer.com/product/{product_id}_200w.jpg",
                "groupId": group_id,
            })
        found.sort(key=lambda row: row["name"].lower())
        products.extend(found)
        print(f"{set_name}: {len(found)} booster box(es)")
        time.sleep(0.05)

    if missing_groups:
        raise SystemExit(f"Missing group map for: {missing_groups}")

    PRODUCTS_PATH.write_text(
        json.dumps({"updatedAt": "2026-08-26", "products": products}, indent=2) + "\n",
        encoding="utf-8",
    )
    write_tracked_urls(ROOT)
    print(f"Wrote {len(products)} products -> {PRODUCTS_PATH}")


if __name__ == "__main__":
    main()
