import json
import os
import time
import requests
import datetime
import re

# --- Configuration ---
URLS_FILE = "urls.txt"
DATA_DIR = "data"
DATA_FILE = os.path.join(DATA_DIR, "tracker_data.json")
CHART_HISTORY_FILE = os.path.join(DATA_DIR, "chart_history.json")
LATEST_SALES_FILE = os.path.join(DATA_DIR, "latest_sales.json")
PRODUCTS_FILES = [
    os.path.join(DATA_DIR, "pokemon_products.json"),
    os.path.join(DATA_DIR, "one_piece_products.json"),
]
IMAGE_OVERRIDES_FILE = os.path.join(DATA_DIR, "product_image_overrides.json")
CATALOG_FILE = os.path.join(DATA_DIR, "catalog.json")
LATEST_SALES_LIMIT = 100
DISCORD_MESSAGE_LIMIT = 2000
# Discord counts some emoji as two units; keep a small buffer under the hard cap.
DISCORD_SAFETY_MARGIN = 40
DISCORD_PACK_LIMIT = DISCORD_MESSAGE_LIMIT - DISCORD_SAFETY_MARGIN
DISCORD_HEADER = ""
FAMILY_DISCORD_NAMES = {
    "xy": "XY",
}

CHART_RANGES = {
    "month": {"key": "1M", "interval": "day", "label": "1M · daily"},
    "quarter": {"key": "3M", "interval": "3-day", "label": "3M · 3-day totals"},
    "annual": {"key": "1Y", "interval": "week", "label": "1Y · weekly totals"},
}

API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.tcgplayer.com",
}


def get_product_name(soup):
    """Finds the product title and cleans up appended affiliate/tracking text."""
    h1s = soup.find_all('h1')
    for h1 in h1s:
        text = h1.get_text(separator=" ", strip=True)
        if text:
            text = re.sub(r'(?i)Shop with Affiliates.*', '', text).strip()
            return text
    return "Unknown Product"


def extract_metric(soup, label):
    """Robustly searches for a text label and extracts the closest number/price."""
    nodes = soup.find_all(string=re.compile(label, re.IGNORECASE))
    for node in nodes:
        parent = node.parent
        for sibling in parent.next_siblings:
            if sibling.name:
                sib_text = sibling.get_text(strip=True)
                if re.search(r'\d+', sib_text):
                    return sib_text
        if parent.parent:
            full_text = parent.parent.get_text(separator=" ", strip=True)
            pattern = re.compile(rf"{label}.*?(\$?\d+[,\d]*\.?\d*)", re.IGNORECASE)
            match = pattern.search(full_text)
            if match:
                return match.group(1)
    return "N/A"


def select_chart_sku(result_list):
    """Prefer the Unopened / English / Normal SKU that drives the Market Price History chart."""
    if not result_list:
        return None
    skus = result_list if isinstance(result_list, list) else [result_list]

    def score(sku):
        condition = str(sku.get("condition") or "").lower()
        language = str(sku.get("language") or "").lower()
        variant = str(sku.get("variant") or "").lower()
        return (
            1 if language == "english" else 0,
            1 if condition == "unopened" else 0,
            1 if variant == "normal" else 0,
        )

    return max(skus, key=score)


def parse_history_buckets(history_data):
    """Parse Infinite API chart buckets (daily, 3-day, or weekly) into dated rows."""
    if not isinstance(history_data, dict):
        return []
    result = history_data.get("result") or history_data.get("results") or []
    sku = select_chart_sku(result)
    if not sku:
        return []

    rows = []
    for bucket in sku.get("buckets") or []:
        if not isinstance(bucket, dict):
            continue
        raw_date = bucket.get("bucketStartDate") or bucket.get("date")
        if not raw_date:
            continue
        rows.append({
            "date": str(raw_date)[:10],
            "quantitySold": parse_numeric(bucket.get("quantitySold")),
            "transactionCount": parse_numeric(bucket.get("transactionCount")),
            "marketPrice": parse_numeric(bucket.get("marketPrice"), as_float=True),
            "lowSalePrice": parse_numeric(bucket.get("lowSalePrice"), as_float=True),
            "highSalePrice": parse_numeric(bucket.get("highSalePrice"), as_float=True),
        })
    rows.sort(key=lambda row: row["date"])
    return rows


def parse_daily_buckets(history_data):
    """Daily 1M buckets used for the dated tracker archive."""
    return [
        {
            "date": row["date"],
            "quantitySold": row["quantitySold"],
            "marketPrice": row["marketPrice"],
        }
        for row in parse_history_buckets(history_data)
    ]


def latest_completed_sales(buckets, today_date):
    """Most recent completed day's item volume (skip today's incomplete bucket)."""
    completed = [
        row for row in buckets
        if row.get("date") and row["date"] < today_date and row.get("quantitySold") is not None
    ]
    if not completed:
        return "N/A"
    return str(completed[-1]["quantitySold"])


def history_records_for_product(product_id, product_name, url, buckets, today_date, set_name=None, image_url=None, product_kind=None):
    """Dated snapshots for every completed chart day so the dashboard can plot true daily volume."""
    records = []
    for bucket in buckets:
        if not bucket.get("date") or bucket["date"] >= today_date:
            continue
        records.append({
            "date": bucket["date"],
            "productId": product_id,
            "productName": product_name,
            "setName": set_name,
            "productKind": product_kind,
            "imageUrl": image_url,
            "marketPrice": bucket.get("marketPrice"),
            "recentSale": None,
            "listedMedian": None,
            "currentSellers": None,
            "currentQuantity": None,
            "lastDaySales": bucket.get("quantitySold"),
            "url": url,
        })
    return records


def parse_numeric(value, as_float=False):
    """Normalize scraped strings like '$1,234.56' into chartable numbers. Returns None if unparseable."""
    if value is None or value == "N/A":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if as_float else int(value)
    text = str(value).replace(",", "").replace("$", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group(0))
    if as_float:
        return round(number, 2)
    return int(round(number))


def extract_product_id(url):
    match = re.search(r"product/(\d+)", url)
    return match.group(1) if match else None


def load_catalog_products():
    """Sealed product metadata from Pokémon and One Piece catalog JSON files."""
    products = []
    for path in PRODUCTS_FILES:
        if not os.path.exists(path):
            continue
        try:
            payload = json.load(open(path, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        products.extend(payload.get("products") or [])
    return products


def load_image_lookup():
    """TCGPlayer catalog images plus optional local overrides in data/product_image_overrides.json."""
    lookup = {}
    for row in load_catalog_products():
        product_id = str(row.get("productId") or "")
        if product_id and row.get("imageUrl"):
            lookup[product_id] = row["imageUrl"]
    if os.path.exists(IMAGE_OVERRIDES_FILE):
        try:
            overrides = json.load(open(IMAGE_OVERRIDES_FILE, encoding="utf-8"))
            if isinstance(overrides, dict):
                for product_id, image_url in overrides.items():
                    if image_url:
                        lookup[str(product_id)] = image_url
        except (json.JSONDecodeError, OSError):
            pass
    return lookup


def catalog_kind_lookup():
    kinds = {}
    for row in load_catalog_products():
        product_id = str(row.get("productId") or "")
        if product_id and row.get("kind"):
            kinds[product_id] = row["kind"]
    return kinds


def extract_image_url(soup, product_id, known=None):
    if known:
        return known
    og = soup.find("meta", property="og:image") if soup else None
    if og and og.get("content"):
        return og["content"].strip()
    img = soup.find("img") if soup else None
    if img and img.get("src") and "tcgplayer" in img.get("src", "").lower():
        return img["src"]
    if product_id:
        return f"https://tcgplayer-cdn.tcgplayer.com/product/{product_id}_in_1000x1000.jpg"
    return None


def infer_set_name(url, product_name="", explicit=None):
    """Resolve a dashboard set tab name from urls.txt, the product title, or the URL slug."""
    if explicit:
        return explicit.strip()
    name = product_name or ""
    match = re.search(r"(?:ME|SV|PR|SWSH|SM|XY|B[WP])\d+:\s*([^(]+)", name, re.I)
    if match:
        return match.group(1).strip()
    slug = re.search(r"/pokemon-[a-z0-9]+-([a-z0-9-]+)", url or "", re.I)
    if slug:
        parts = slug.group(1).split("-")
        if len(parts) >= 2:
            return " ".join(part.title() for part in parts[:2])
    return "Other"


def read_tracked_urls():
    """Read product URLs and optional `# Set: Name` grouping from urls.txt."""
    if not os.path.exists(URLS_FILE):
        return []
    entries = []
    current_set = None
    with open(URLS_FILE, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                set_match = re.match(r"#\s*set\s*:\s*(.+)$", line, re.I)
                if set_match:
                    current_set = set_match.group(1).strip()
                continue
            entries.append({
                "url": line,
                "setName": current_set,
            })
    return entries


def load_records():
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_json(path, payload):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")


def save_records(records):
    save_json(DATA_FILE, records)


def request_headers(referer=None):
    headers = dict(API_HEADERS)
    if referer:
        headers["Referer"] = referer
    return headers


def fetch_price_history(product_id, range_name, referer):
    api_url = f"https://infinite-api.tcgplayer.com/price/history/{product_id}/detailed?range={range_name}"
    print(f"DEBUG: Requesting {range_name} chart data from {api_url}")
    response = requests.get(api_url, headers=request_headers(referer), timeout=15)
    if response.status_code != 200:
        print(f"DEBUG: {range_name} chart request failed with status {response.status_code}")
        return []
    return parse_history_buckets(response.json())


def normalize_sale(row, product_id, product_name, url, set_name=None):
    if not isinstance(row, dict):
        return None
    return {
        "productId": product_id,
        "productName": product_name,
        "setName": set_name,
        "url": url,
        "orderDate": row.get("orderDate") or row.get("soldDate") or row.get("date"),
        "purchasePrice": parse_numeric(row.get("purchasePrice") or row.get("price"), as_float=True),
        "shippingPrice": parse_numeric(row.get("shippingPrice"), as_float=True) or 0.0,
        "quantity": parse_numeric(row.get("quantity") or row.get("qty")) or 1,
        "condition": row.get("condition") or "",
        "variant": row.get("variant") or "",
        "language": row.get("language") or "",
        "listingType": row.get("listingType") or "",
    }


def fetch_latest_sales_http(product_id, referer, limit=LATEST_SALES_LIMIT):
    """Fallback POST used when the stealth browser capture is empty."""
    api_url = f"https://mpapi.tcgplayer.com/v2/product/{product_id}/latestsales?mpfev=5429"
    body = {
        "conditions": [],
        "languages": [],
        "variants": [],
        "listingType": "All",
        "limit": min(limit, 25),
        "offset": 0,
    }
    try:
        response = requests.post(api_url, headers=request_headers(referer), json=body, timeout=15)
        if response.status_code != 200:
            print(f"DEBUG: Latest sales HTTP request failed with status {response.status_code}")
            return []
        payload = response.json()
        return payload.get("data") or []
    except Exception as exc:
        print(f"DEBUG: Latest sales HTTP request error: {exc}")
        return []


LATEST_SALES_JS = """
async ([productId, limit]) => {
  const all = [];
  const seen = new Set();
  let offset = 0;
  const pageSize = 25;
  for (let i = 0; i < 20 && all.length < limit; i++) {
    const res = await fetch(
      `https://mpapi.tcgplayer.com/v2/product/${productId}/latestsales?mpfev=5429`,
      {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({
          conditions: [],
          languages: [],
          variants: [],
          listingType: "All",
          limit: pageSize,
          offset,
        }),
      }
    );
    if (!res.ok) {
      return { error: res.status, sales: all };
    }
    const json = await res.json();
    const batch = Array.isArray(json.data) ? json.data : [];
    if (!batch.length) break;
    let added = 0;
    for (const row of batch) {
      const key = [row.orderDate, row.purchasePrice, row.quantity, row.customListingId, row.condition].join("|");
      if (seen.has(key)) continue;
      seen.add(key);
      all.push(row);
      added += 1;
    }
    if (added === 0) break;
    offset += batch.length;
    if (batch.length < pageSize) break;
  }
  return { sales: all.slice(0, limit) };
}
"""


def capture_latest_sales_action(product_id, sink):
    def page_action(page):
        try:
            evaluate = getattr(page, "evaluate")
            try:
                result = evaluate(LATEST_SALES_JS, [str(product_id), LATEST_SALES_LIMIT], isolated_context=False)
            except TypeError:
                result = evaluate(LATEST_SALES_JS, [str(product_id), LATEST_SALES_LIMIT])
            sales = result.get("sales") if isinstance(result, dict) else result
            if isinstance(sales, list):
                sink.extend(sales)
        except Exception as exc:
            print(f"DEBUG: Browser latest-sales capture failed: {exc}")
        return page
    return page_action


def scrape_succeeded(product_name, market_price):
    """A listing counts as scraped when the product page yielded a name and market price."""
    if not product_name or product_name == "Unknown Product":
        return False
    if market_price in (None, "", "N/A"):
        return False
    return True


def load_set_catalog():
    """Catalog order plus setName -> game/family labels for Discord scrape status."""
    games = []
    set_index = {}
    family_index = {}
    if not os.path.exists(CATALOG_FILE):
        return games, set_index, family_index
    try:
        catalog = json.load(open(CATALOG_FILE, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return games, set_index, family_index
    for game in catalog.get("games") or []:
        game_id = game.get("id") or "other"
        game_name = "Pokemon" if game_id == "pokemon" else (game.get("name") or "Other")
        families = []
        for family in game.get("families") or []:
            family_id = family.get("id") or "other"
            family_name = FAMILY_DISCORD_NAMES.get(family_id) or family.get("name") or family_id
            families.append({"id": family_id, "name": family_name})
            family_index[family_id] = {
                "game_id": game_id,
                "game_name": game_name,
                "family_id": family_id,
                "family_name": family_name,
            }
            for item in family.get("sets") or []:
                set_name = item.get("setName")
                if not set_name:
                    continue
                set_index[set_name] = {
                    "game_id": game_id,
                    "game_name": game_name,
                    "family_id": family_id,
                    "family_name": family_name,
                }
        games.append({"id": game_id, "name": game_name, "families": families})
    return games, set_index, family_index


def product_lookups():
    """Name/set/family from sealed-product JSON, keyed by productId and URL."""
    by_id = {}
    by_url = {}
    for row in load_catalog_products():
        meta = {
            "name": row.get("name"),
            "setName": row.get("setName"),
            "familyId": row.get("familyId"),
        }
        product_id = str(row.get("productId") or "")
        if product_id:
            by_id[product_id] = meta
        url = row.get("url") or ""
        if url:
            by_url[url.split("?")[0]] = meta
    return by_id, by_url


def resolve_scrape_placement(set_name, family_id, set_index, family_index):
    if set_name and set_name in set_index:
        return set_index[set_name]
    if family_id and family_id in family_index:
        return family_index[family_id]
    return {
        "game_id": "other",
        "game_name": "Other",
        "family_id": "other",
        "family_name": "Other",
    }


def format_scrape_status(results, games, set_index, family_index):
    """Discord body: per-era success, or set name plus the listings that failed."""
    grouped = {}
    for row in results:
        place = resolve_scrape_placement(row.get("setName"), row.get("familyId"), set_index, family_index)
        key = (place["game_id"], place["family_id"])
        bucket = grouped.setdefault(key, {
            "game_id": place["game_id"],
            "game_name": place["game_name"],
            "family_name": place["family_name"],
            "ok": True,
            "failures": {},
        })
        if row.get("ok"):
            continue
        bucket["ok"] = False
        set_name = row.get("setName") or "Unknown set"
        item = row.get("productName") or "Unknown product"
        bucket["failures"].setdefault(set_name, [])
        if item not in bucket["failures"][set_name]:
            bucket["failures"][set_name].append(item)

    preferred = ["pokemon", "one-piece"]
    seen = {game["id"] for game in games}
    game_order = [game_id for game_id in preferred if game_id in seen]
    game_order.extend(game["id"] for game in games if game["id"] not in game_order)
    if any(row.get("game_id") == "other" for row in grouped.values()):
        game_order.append("other")
    game_meta = {game["id"]: game for game in games}
    game_meta.setdefault("other", {"id": "other", "name": "Other", "families": [{"id": "other", "name": "Other"}]})

    blocks = []
    for game_id in game_order:
        game = game_meta[game_id]
        family_ids = [family["id"] for family in game.get("families") or []]
        if game_id == "other" and "other" not in family_ids:
            family_ids.append("other")
        lines = []
        for family_id in family_ids:
            bucket = grouped.get((game_id, family_id))
            if not bucket:
                continue
            if bucket["ok"]:
                lines.append(f"{bucket['family_name']} - Successfully Scraped")
                continue
            lines.append(f"{bucket['family_name']} - Failed")
            for set_name, items in bucket["failures"].items():
                lines.append(f"- {set_name}: {', '.join(items)}")
        if lines:
            blocks.append(f"**{game['name']}:**\n" + "\n".join(lines))
    return blocks


def discord_header(part, total):
    if total <= 1:
        return ""
    prefix = DISCORD_HEADER.strip()
    if prefix:
        return f"{prefix} ({part}/{total})\n\n"
    return f"({part}/{total})\n\n"


def pack_discord_messages(blocks, limit=DISCORD_PACK_LIMIT):
    """Pack scrape-status blocks into Discord messages without exceeding the character cap."""
    items = [str(block).strip() for block in blocks if str(block).strip()]
    if not items:
        return ["No products scraped."]

    # Reserve room for the longest " (999/999)" part tag we might add after packing.
    header_budget = len(discord_header(999, 999))
    body_limit = max(1, limit - header_budget)

    bodies = []
    current = ""
    for block in items:
        if len(block) > body_limit:
            if current:
                bodies.append(current)
                current = ""
            bodies.append(block[: body_limit - 1] + "…")
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > body_limit:
            bodies.append(current)
            current = block
        else:
            current = candidate
    if current:
        bodies.append(current)

    total = len(bodies)
    messages = []
    for index, body in enumerate(bodies, start=1):
        header = discord_header(index, total)
        message = header + body
        if len(message) > limit:
            message = header + body[: max(0, limit - len(header) - 1)] + "…"
        messages.append(message)
    return messages


def send_discord_messages(webhook_url, messages):
    """Post packed recaps one webhook at a time, pausing so Discord rate limits stay happy."""
    total = len(messages)
    for index, content in enumerate(messages, start=1):
        response = requests.post(webhook_url, json={"content": content}, timeout=20)
        if response.status_code in (200, 204):
            print(f"Discord notification {index}/{total} sent ({len(content)}/{DISCORD_MESSAGE_LIMIT} chars).")
        else:
            print(f"Failed to send Discord {index}/{total}: {response.status_code}, {response.text}")
        if index < total:
            time.sleep(0.7)


def upsert_records(existing, new_records):
    """Merge snapshots by (date, productId). Nulls do not wipe richer live-scrape fields."""
    index = {(row.get("date"), str(row.get("productId"))): i for i, row in enumerate(existing)}
    for record in new_records:
        key = (record.get("date"), str(record.get("productId")))
        if key in index:
            merged = dict(existing[index[key]])
            for field, value in record.items():
                if value is not None:
                    merged[field] = value
            existing[index[key]] = merged
        else:
            existing.append(record)
            index[key] = len(existing) - 1
    existing.sort(key=lambda row: (row.get("date") or "", str(row.get("productId") or "")))
    return existing


def main():
    # --- 1. Read URLs ---
    if not os.path.exists(URLS_FILE):
        print(f"Error: {URLS_FILE} not found. Please create it and add some URLs.")
        return

    entries = read_tracked_urls()
    if not entries:
        print(f"No valid URLs found in {URLS_FILE}.")
        return

    # --- 2. Scrape Data ---
    all_data_rows = []
    scrape_results = []
    chart_products = []
    latest_sales_rows = []
    today_date = datetime.datetime.now().strftime("%Y-%m-%d")

    from bs4 import BeautifulSoup
    from scrapling.fetchers import StealthySession

    print(f"Initializing Scrapling StealthySession to scrape {len(entries)} URLs...")
    image_lookup = load_image_lookup()
    kind_lookup = catalog_kind_lookup()
    games_order, set_index, family_index = load_set_catalog()
    products_by_id, products_by_url = product_lookups()
    with StealthySession(headless=True, solve_cloudflare=True) as session:
        for entry in entries:
            url = entry["url"]
            print(f"\n======================================")
            print(f"Scraping: {url}")
            product_id = extract_product_id(url)
            listed = products_by_id.get(str(product_id or "")) or products_by_url.get(url.split("?")[0]) or {}
            fallback_name = listed.get("name") or url
            fallback_set = entry.get("setName") or listed.get("setName")
            fallback_family = listed.get("familyId")
            try:
                captured_sales = []
                fetch_kwargs = {}
                if product_id:
                    fetch_kwargs["page_action"] = capture_latest_sales_action(product_id, captured_sales)

                page = session.fetch(url, **fetch_kwargs)
                soup = BeautifulSoup(page.body, 'html.parser')

                product_name = get_product_name(soup)
                set_name = infer_set_name(url, product_name, entry.get("setName"))
                product_kind = kind_lookup.get(str(product_id or ""))
                image_url = extract_image_url(soup, product_id, image_lookup.get(str(product_id or "")))
                market_price = extract_metric(soup, "Market Price")
                recent_sale = extract_metric(soup, "Most Recent Sale")
                listed_median = extract_metric(soup, "Listed Median")
                current_sellers = extract_metric(soup, "Current Sellers")
                current_quantity = extract_metric(soup, "Current Quantity")

                last_day_sales = "N/A"
                daily_buckets = []
                range_payload = {}

                if product_id:
                    try:
                        for range_name, meta in CHART_RANGES.items():
                            points = fetch_price_history(product_id, range_name, url)
                            range_payload[meta["key"]] = {
                                "interval": meta["interval"],
                                "label": meta["label"],
                                "points": points,
                            }
                            print(f">>> {meta['key']}: {len(points)} {meta['interval']} buckets")
                        daily_buckets = [
                            {
                                "date": point["date"],
                                "quantitySold": point["quantitySold"],
                                "marketPrice": point["marketPrice"],
                            }
                            for point in (range_payload.get("1M") or {}).get("points") or []
                        ]
                        last_day_sales = latest_completed_sales(daily_buckets, today_date)
                        if last_day_sales != "N/A":
                            print(f">>> SUCCESS: Found Sales Data: {last_day_sales} across {len(daily_buckets)} daily buckets")
                    except Exception as e:
                        print(f"DEBUG: Chart history request error: {e}")

                    if len(captured_sales) < LATEST_SALES_LIMIT:
                        fallback_sales = fetch_latest_sales_http(product_id, url)
                        if len(fallback_sales) > len(captured_sales):
                            captured_sales = fallback_sales
                    normalized = [
                        sale for sale in (
                            normalize_sale(row, product_id, product_name, url, set_name)
                            for row in captured_sales
                        )
                        if sale and sale.get("orderDate")
                    ]
                    normalized.sort(key=lambda row: row.get("orderDate") or "", reverse=True)
                    latest_sales_rows.extend(normalized[:LATEST_SALES_LIMIT])
                    print(f">>> Latest transactions captured: {min(len(normalized), LATEST_SALES_LIMIT)}")

                    chart_products.append({
                        "productId": product_id,
                        "productName": product_name,
                        "setName": set_name,
                        "productKind": product_kind,
                        "imageUrl": image_url,
                        "url": url,
                        "ranges": range_payload,
                    })

                record = {
                    "date": today_date,
                    "productId": product_id,
                    "productName": product_name,
                    "setName": set_name,
                    "productKind": product_kind,
                    "imageUrl": image_url,
                    "marketPrice": parse_numeric(market_price, as_float=True),
                    "recentSale": parse_numeric(recent_sale, as_float=True),
                    "listedMedian": parse_numeric(listed_median, as_float=True),
                    "currentSellers": parse_numeric(current_sellers),
                    "currentQuantity": parse_numeric(current_quantity),
                    "lastDaySales": None,
                    "url": url,
                }
                all_data_rows.append(record)
                all_data_rows.extend(
                    history_records_for_product(
                        product_id, product_name, url, daily_buckets, today_date, set_name, image_url, product_kind
                    )
                )
                ok = scrape_succeeded(product_name, market_price)
                item_name = product_name if product_name and product_name != "Unknown Product" else fallback_name
                scrape_results.append({
                    "ok": ok,
                    "setName": set_name or fallback_set or "Other",
                    "productName": item_name,
                    "familyId": fallback_family,
                })

            except Exception as e:
                print(f"Failed to scrape {url}: {e}")
                scrape_results.append({
                    "ok": False,
                    "setName": fallback_set or "Other",
                    "productName": fallback_name,
                    "familyId": fallback_family,
                })

    if all_data_rows:
        print("\nWriting tracker data to local JSON...")
        existing_records = load_records()
        merged_records = upsert_records(existing_records, all_data_rows)
        save_records(merged_records)
        print(f"Saved {len(all_data_rows)} snapshot(s). Archive now has {len(merged_records)} record(s) in {DATA_FILE}.")
    else:
        print("\nNo data was successfully scraped.")

    if chart_products:
        save_json(CHART_HISTORY_FILE, {"updatedAt": today_date, "products": chart_products})
        print(f"Wrote chart history for {len(chart_products)} product(s) to {CHART_HISTORY_FILE}.")
    if latest_sales_rows:
        save_json(LATEST_SALES_FILE, {"updatedAt": today_date, "sales": latest_sales_rows})
        print(f"Wrote {len(latest_sales_rows)} latest transaction(s) to {LATEST_SALES_FILE}.")

    # --- 4. Discord Integration ---
    print("Sending Discord notification...")
    discord_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not discord_url:
        print("DISCORD_WEBHOOK_URL environment variable is missing; skipping Discord notification.")
        return

    status_blocks = format_scrape_status(scrape_results, games_order, set_index, family_index)
    messages_to_send = pack_discord_messages(status_blocks)
    print(
        f"Packed scrape status into {len(messages_to_send)} "
        f"Discord message(s) (max {DISCORD_PACK_LIMIT} chars each, hard cap {DISCORD_MESSAGE_LIMIT})."
    )
    send_discord_messages(discord_url, messages_to_send)

if __name__ == "__main__":
    main()
