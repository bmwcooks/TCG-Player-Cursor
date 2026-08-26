#!/usr/bin/env python3
"""Rebuild urls.txt from Pokémon and One Piece product catalogs."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URLS_PATH = ROOT / "urls.txt"
CATALOGS = (
    ROOT / "data" / "pokemon_products.json",
    ROOT / "data" / "one_piece_products.json",
)

HEADER = """# Group products under a set with `# Set: Name`.
# Pokémon sealed listings: booster boxes, Elite Trainer Boxes, and
# booster bundles (Lost Origin and later).
# One Piece listings: English booster boxes for catalogued OP, EB, and PRB sets.
"""


def load_products(path: Path) -> list[dict]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("products") or [])


def write_tracked_urls(root: Path | None = None) -> int:
    base = root or ROOT
    products: list[dict] = []
    for path in (
        base / "data" / "pokemon_products.json",
        base / "data" / "one_piece_products.json",
    ):
        products.extend(load_products(path))

    lines = [HEADER.rstrip(), ""]
    current = None
    for row in products:
        set_name = row.get("setName") or "Other"
        if set_name != current:
            current = set_name
            lines.append(f"# Set: {current}")
            lines.append("")
        lines.append(f"# {row.get('name') or 'Product'}")
        lines.append(row["url"])
        lines.append("")

    (base / "urls.txt").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return len(products)


if __name__ == "__main__":
    count = write_tracked_urls()
    print(f"Wrote {count} URLs -> {URLS_PATH}")
