#!/usr/bin/env python3
"""Discover TCGPlayer sealed products for catalogued Pokémon sets."""

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
PRODUCTS_PATH = ROOT / "data" / "pokemon_products.json"
GROUPS_URL = "https://tcgcsv.com/tcgplayer/3/groups"
PRODUCTS_URL = "https://tcgcsv.com/tcgplayer/3/{group_id}/products"
HEADERS = {"User-Agent": "TCG-Player-Cursor/1.0 (catalog sync)"}
BUNDLE_FROM = "2022-09-09"

GROUP_OVERRIDES = {
    "XY Kalos Starter Set": 1522,
    "XY": 1387,
    "Flashfire": 1464,
    "Furious Fists": 1481,
    "Phantom Forces": 1494,
    "Primal Clash": 1509,
    "Double Crisis": 1525,
    "Roaring Skies": 1534,
    "Ancient Origins": 1576,
    "BREAKthrough": 1661,
    "BREAKpoint": 1701,
    "Generations": 1728,
    "Fates Collide": 1780,
    "Steam Siege": 1815,
    "Evolutions": 1842,
    "Sun & Moon": 1863,
    "Guardians Rising": 1919,
    "Burning Shadows": 1957,
    "Shining Legends": 2054,
    "Crimson Invasion": 2071,
    "Ultra Prism": 2178,
    "Forbidden Light": 2209,
    "Celestial Storm": 2278,
    "Dragon Majesty": 2295,
    "Lost Thunder": 2328,
    "Team Up": 2377,
    "Detective Pikachu": 2409,
    "Unbroken Bonds": 2420,
    "Unified Minds": 2464,
    "Hidden Fates": 2480,
    "Cosmic Eclipse": 2534,
    "Sword & Shield": 2585,
    "Rebel Clash": 2626,
    "Darkness Ablaze": 2675,
    "Champion's Path": 2685,
    "Vivid Voltage": 2701,
    "Shining Fates": 2754,
    "Battle Styles": 2765,
    "Chilling Reign": 2807,
    "Evolving Skies": 2848,
    "Celebrations": 2867,
    "Fusion Strike": 2906,
    "Brilliant Stars": 2948,
    "Astral Radiance": 3040,
    "Pokémon GO": 3064,
    "Lost Origin": 3118,
    "Silver Tempest": 3170,
    "Crown Zenith": 17688,
    "Scarlet & Violet": 22873,
    "Paldea Evolved": 23120,
    "Obsidian Flames": 23228,
    "151": 23237,
    "Paradox Rift": 23286,
    "Paldean Fates": 23353,
    "Temporal Forces": 23381,
    "Twilight Masquerade": 23473,
    "Shrouded Fable": 23529,
    "Stellar Crown": 23537,
    "Surging Sparks": 23651,
    "Prismatic Evolutions": 23821,
    "Journey Together": 24073,
    "Destined Rivals": 24269,
    "Black Bolt": 24325,
    "White Flare": 24326,
    "Mega Evolution": 24380,
    "Phantasmal Flames": 24448,
    "Ascended Heroes": 24541,
    "Perfect Order": 24587,
    "Chaos Rising": 24655,
    "Pitch Black": 24688,
    "30th Celebration": 24722,
    "Delta Reign": 24831,
}

SKIP_NAME = re.compile(
    r"code card|\bcase\b|half booster|set of 2|display|bulk|dollar general|"
    r"sam'?s club|costco|surprise box|digital bundle",
    re.I,
)


def pokemon_sets():
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    game = next(item for item in catalog["games"] if item["id"] == "pokemon")
    sets = []
    for family in game["families"]:
        for item in family["sets"]:
            sets.append({**item, "familyId": family["id"], "familyName": family["name"]})
    return sets


def normalize_name(value: str) -> str:
    text = (value or "").lower().replace("é", "e").replace("&", "and")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def belongs_to_set(product_name: str, set_name: str) -> bool:
    product = normalize_name(product_name)
    expected = normalize_name(set_name)
    if expected and expected in product:
        return True
    # Older XY ETBs are stored in the set group as "Elite Trainer Box [Pokemon]".
    if product.startswith("elite trainer box"):
        return True
    return False


def classify(name: str, set_name: str, released: str | None) -> str | None:
    text = name or ""
    if SKIP_NAME.search(text):
        return None
    if not belongs_to_set(text, set_name):
        return None
    lower = text.lower()
    if "booster bundle" in lower:
        if (released or "") < BUNDLE_FROM:
            return None
        return "booster-bundle"
    if "booster box" in lower:
        return "booster-box"
    if "elite trainer box" in lower:
        return "etb"
    return None


def kind_rank(kind: str, name: str) -> tuple:
    lower = name.lower()
    pc = 1 if "pokemon center" in lower or "pokémon center" in lower else 0
    order = {"booster-box": 0, "etb": 1, "booster-bundle": 2}[kind]
    return (order, pc, name.lower())


def fetch_json(url: str) -> dict:
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.json()


def main() -> None:
    sets = pokemon_sets()
    products = []
    missing_groups = []
    for item in sets:
        group_id = GROUP_OVERRIDES.get(item["setName"])
        if not group_id:
            missing_groups.append(item["setName"])
            continue
        payload = fetch_json(PRODUCTS_URL.format(group_id=group_id))
        found = []
        for row in payload.get("results") or []:
            kind = classify(row.get("name") or "", item["setName"], item.get("released"))
            if not kind:
                continue
            product_id = str(row["productId"])
            url = row.get("url") or f"https://www.tcgplayer.com/product/{product_id}"
            if "Language=English" not in url:
                joiner = "&" if "?" in url else "?"
                url = f"{url}{joiner}page=1&Language=English"
            found.append({
                "productId": product_id,
                "name": row.get("name"),
                "setName": item["setName"],
                "familyId": item["familyId"],
                "kind": kind,
                "url": url,
                "imageUrl": row.get("imageUrl") or f"https://tcgplayer-cdn.tcgplayer.com/product/{product_id}_200w.jpg",
                "groupId": group_id,
            })
            time.sleep(0.01)
        found.sort(key=lambda row: kind_rank(row["kind"], row["name"]))
        products.extend(found)
        print(f"{item['setName']}: {len(found)} sealed products")
        time.sleep(0.05)

    if missing_groups:
        raise SystemExit(f"Missing group map for: {missing_groups}")

    PRODUCTS_PATH.write_text(json.dumps({"updatedAt": "2026-08-26", "products": products}, indent=2) + "\n", encoding="utf-8")
    write_tracked_urls(ROOT)

    counts = {}
    for row in products:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    print(f"Wrote {len(products)} products -> {PRODUCTS_PATH}")
    print(counts)


if __name__ == "__main__":
    main()
