#!/usr/bin/env python3
"""Discover TCGPlayer singles for tracked Pokémon sets (EX/V+ rarity bands)."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from pokemon_singles import (  # noqa: E402
    EXTRA_GROUPS,
    SUBSET_LABEL,
    catalog_row,
    include_card,
    number_of,
    number_sort_key,
    rarity_of,
)
from sync_pokemon_sealed import GROUP_OVERRIDES, pokemon_sets  # noqa: E402

PRODUCTS_PATH = ROOT / "data" / "pokemon_singles.json"
SEALED_PATH = ROOT / "data" / "pokemon_products.json"
PRODUCTS_URL = "https://tcgcsv.com/tcgplayer/3/{group_id}/products"
HEADERS = {"User-Agent": "TCG-Player-Cursor/1.0 (singles sync)"}


def fetch_json(url: str, attempts: int = 3) -> dict:
    last_error = None
    for attempt in range(attempts):
        try:
            response = requests.get(url, headers=HEADERS, timeout=60)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def tracked_set_names() -> set[str]:
    payload = json.loads(SEALED_PATH.read_text(encoding="utf-8"))
    return {row.get("setName") for row in payload.get("products") or [] if row.get("setName")}


def collect_group(group_id: int, set_name: str, family_id: str, subset: str | None) -> list[dict]:
    payload = fetch_json(PRODUCTS_URL.format(group_id=group_id))
    found = []
    for row in payload.get("results") or []:
        name = row.get("name") or ""
        rarity = rarity_of(row)
        number = number_of(row)
        if not include_card(family_id, name, rarity, number, subset):
            continue
        found.append(catalog_row(row, set_name, family_id, group_id, subset))
    return found


def main() -> None:
    tracked = tracked_set_names()
    products = []
    seen = set()
    missing_groups = []
    for item in pokemon_sets():
        set_name = item["setName"]
        if set_name not in tracked:
            continue
        group_id = GROUP_OVERRIDES.get(set_name)
        if not group_id:
            missing_groups.append(set_name)
            continue
        family_id = item["familyId"]
        groups = [{"groupId": group_id, "subset": None}, *EXTRA_GROUPS.get(set_name, [])]
        set_rows = []
        for group in groups:
            gid = group["groupId"]
            subset = group.get("subset")
            rows = collect_group(gid, set_name, family_id, subset)
            set_rows.extend(rows)
            label = SUBSET_LABEL.get(subset or "", "main")
            print(f"{set_name} [{label}]: {len(rows)}")
            time.sleep(0.05)
        set_rows.sort(key=lambda row: (number_sort_key(row.get("number") or ""), (row.get("name") or "").lower()))
        for row in set_rows:
            product_id = row["productId"]
            if product_id in seen:
                continue
            seen.add(product_id)
            products.append(row)

    if missing_groups:
        raise SystemExit(f"Missing group map for: {missing_groups}")

    payload = {
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "notes": (
            "English TCGPlayer singles for tracked Pokémon sets. "
            "XY: EX / Full Art EX / BREAK / Radiant Collection. "
            "SM: GX / FA GX / Tag Team / Rainbow-Secret / Prism Star. "
            "SWSH: V / VMAX / VSTAR / FA-AA / Rainbow-Gold-Secret / Amazing Rare / Radiant / TG. "
            "SV-Mega: ex / FA / IR / SIR / Hyper-Gold / ACE SPEC. "
            "Links only — not added to urls.txt or the daily scrape."
        ),
        "products": products,
    }
    PRODUCTS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(products)} singles -> {PRODUCTS_PATH}")


if __name__ == "__main__":
    main()
