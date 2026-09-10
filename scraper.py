import asyncio
import inspect
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
SINGLES_CHART_HISTORY_FILE = os.path.join(DATA_DIR, "singles_chart_history.json")
LATEST_SALES_FILE = os.path.join(DATA_DIR, "latest_sales.json")
PRODUCTS_FILES = [
    os.path.join(DATA_DIR, "pokemon_products.json"),
    os.path.join(DATA_DIR, "one_piece_products.json"),
]
SINGLES_FILE = os.path.join(DATA_DIR, "pokemon_singles.json")
IMAGE_OVERRIDES_FILE = os.path.join(DATA_DIR, "product_image_overrides.json")
CATALOG_FILE = os.path.join(DATA_DIR, "catalog.json")
LATEST_SALES_LIMIT = 100
SINGLES_SALES_LIMIT = 25
DEFAULT_SINGLES_FETCH_CONCURRENCY = 4
MAX_SINGLES_FETCH_CONCURRENCY = 8
DISCORD_MESSAGE_LIMIT = 2000
# Discord counts some emoji as two units; keep a small buffer under the hard cap.
DISCORD_SAFETY_MARGIN = 40
DISCORD_PACK_LIMIT = DISCORD_MESSAGE_LIMIT - DISCORD_SAFETY_MARGIN
DISCORD_HEADER = ""
FAMILY_DISCORD_NAMES = {
    "xy": "XY",
}
PAGE_FETCH_TIMEOUT_MS = 20000
EMPTY_PAGE_RETRIES = 1
DEFAULT_SCRAPE_CONCURRENCY = 3
MAX_SCRAPE_CONCURRENCY = 8
DEFAULT_CHART_FETCH_CONCURRENCY = 1
BLOCKED_PAGE_MARKERS = (
    "just a moment",
    "attention required",
    "cf-browser-verification",
    "checking your browser",
    "enable javascript and cookies",
    "access denied",
    "verify you are human",
)

CHART_RANGES = {
    "month": {"key": "1M", "interval": "day", "label": "1M · daily"},
    "quarter": {"key": "3M", "interval": "3-day", "label": "3M · 3-day totals"},
    "annual": {"key": "1Y", "interval": "week", "label": "1Y · weekly totals"},
}

CARD_CONDITIONS = (
    "Near Mint",
    "Lightly Played",
    "Moderately Played",
    "Heavily Played",
    "Damaged",
)
DEFAULT_CARD_CONDITION = "Near Mint"

API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.tcgplayer.com",
}


def clean_product_title(text):
    text = re.sub(r'(?i)Shop with Affiliates.*', '', text or "").strip()
    text = re.sub(r'\s+', ' ', text)
    return text


def iter_json_ld(soup):
    if soup is None:
        return
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                yield item
                stack.extend(item.values())


def json_ld_name_price(soup):
    name = None
    price = None
    for item in iter_json_ld(soup):
        types = item.get("@type") or ""
        if isinstance(types, list):
            types = " ".join(str(part) for part in types)
        offers = item.get("offers")
        if isinstance(offers, list) and offers:
            offers = offers[0]
        if isinstance(offers, dict):
            price = price or offers.get("price") or offers.get("lowPrice")
        if "Product" in str(types) or offers:
            name = name or item.get("name")
    return clean_product_title(name) if name else None, price


def get_product_name(soup):
    """Finds the product title and cleans up appended affiliate/tracking text."""
    if soup is None:
        return "Unknown Product"
    h1s = soup.find_all('h1')
    for h1 in h1s:
        text = clean_product_title(h1.get_text(separator=" ", strip=True))
        if text:
            return text
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        text = clean_product_title(og["content"])
        if text and text.lower() != "tcgplayer":
            return text
    json_name, _ = json_ld_name_price(soup)
    if json_name:
        return json_name
    return "Unknown Product"


def extract_metric(soup, label):
    """Robustly searches for a text label and extracts the closest number/price."""
    if soup is None:
        return "N/A"
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


def extract_html_market_price(soup):
    """First compact Market Price on the product header, not the history widget blob."""
    _, json_price = json_ld_name_price(soup)
    if soup is not None:
        text = soup.get_text(" ", strip=True)
        header = re.split(r"(?i)foil market price|past \d|we're still gathering", text, maxsplit=1)[0]
        match = re.search(r"(?<![A-Za-z])Market Price\s*\$([0-9][0-9,]*(?:\.\d{2})?)", header)
        if match and usable_price(match.group(1)) is not None:
            return match.group(1)
        labeled = extract_metric(soup, "Market Price")
        if labeled != "N/A" and len(str(labeled)) < 40 and usable_price(labeled) is not None:
            return labeled
    if json_price is not None and usable_price(json_price) is not None:
        return json_price
    return "N/A"


def page_looks_empty(soup, body):
    """True when TCGPlayer returned a shell, bot check, or HTML without a product."""
    text = body.decode("utf-8", "ignore") if isinstance(body, (bytes, bytearray)) else (body or "")
    lowered = text.lower()
    if len(text) < 500:
        return True
    name = get_product_name(soup)
    price = extract_html_market_price(soup)
    has_product = name != "Unknown Product" or price != "N/A"
    if any(marker in lowered for marker in BLOCKED_PAGE_MARKERS) and not has_product:
        return True
    return not has_product


def usable_price(value):
    number = parse_numeric(value, as_float=True)
    if number is None or number <= 0:
        return None
    return number


def latest_chart_price(range_payload):
    for key in ("1M", "3M", "1Y"):
        points = ((range_payload or {}).get(key) or {}).get("points") or []
        for point in reversed(points):
            price = usable_price(point.get("marketPrice"))
            if price is not None:
                return price
    return None


def shorten_error(exc, limit=80):
    text = re.sub(r"\s+", " ", str(exc).strip()) or exc.__class__.__name__
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def classify_scrape(html_name, html_price, chart_price=None, empty_page=False, error=None):
    """Return (ok, reason). Reason is set for partial and failed listings."""
    if error:
        return False, f"request error: {shorten_error(error)}"
    name_ok = bool(html_name) and html_name != "Unknown Product"
    page_price = usable_price(html_price)
    chart_ok = usable_price(chart_price) is not None
    if name_ok and page_price is not None:
        return True, None
    if empty_page and not name_ok:
        if chart_ok:
            return True, "empty product page; used chart price"
        return False, "empty or blocked product page"
    if not name_ok and page_price is None:
        if chart_ok:
            return True, "no title or market price on page; used chart price"
        return False, "no title or market price on page"
    if not name_ok:
        return True, "no product title on page"
    if page_price is None:
        if chart_ok:
            return True, "no market price on page; used chart price"
        return False, "no market price on page"
    return True, None


def history_skus(history_data):
    """Infinite detailed-history `result` list (one dict per SKU/condition)."""
    if isinstance(history_data, list):
        rows = history_data
    elif isinstance(history_data, dict):
        rows = history_data.get("result") or history_data.get("results") or []
    else:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    return [row for row in rows if isinstance(row, dict)]


def select_chart_sku(result_list, condition=None):
    """Prefer the Unopened / English / Normal SKU that drives the Market Price History chart."""
    skus = history_skus(result_list)
    if condition:
        wanted = str(condition).strip().lower()
        skus = [sku for sku in skus if str(sku.get("condition") or "").strip().lower() == wanted]
    if not skus:
        return None

    def score(sku):
        sku_condition = str(sku.get("condition") or "").lower()
        language = str(sku.get("language") or "").lower()
        variant = str(sku.get("variant") or "").lower()
        buckets = sku.get("buckets") or []
        if "unopened" in sku_condition:
            condition_rank = 2
        elif "near mint" in sku_condition:
            condition_rank = 1
        else:
            condition_rank = 0
        if variant in ("holofoil", "holo"):
            variant_rank = 2
        elif variant == "normal":
            variant_rank = 1
        else:
            variant_rank = 0
        return (
            1 if buckets else 0,
            1 if language == "english" else 0,
            condition_rank,
            variant_rank,
        )

    return max(skus, key=score)


def points_from_sku(sku):
    """Dated chart rows from one Infinite SKU."""
    rows = []
    if not isinstance(sku, dict):
        return rows
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


def parse_history_buckets(history_data, condition=None):
    """Parse Infinite API chart buckets (daily, 3-day, or weekly) into dated rows."""
    sku = select_chart_sku(history_data, condition=condition)
    if not sku:
        return []
    return points_from_sku(sku)


def latest_sku_price(points):
    for point in reversed(points or []):
        price = usable_price(point.get("marketPrice"))
        if price is not None:
            return price
    return None


def compact_condition_chart(sku):
    """1M-only series compact enough to store every singles condition."""
    points = points_from_sku(sku)
    return {
        "variant": sku.get("variant") or "",
        "dates": [point["date"] for point in points],
        "prices": [point.get("marketPrice") for point in points],
        "sold": [point.get("quantitySold") for point in points],
    }


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


def load_json_products(path):
    if not os.path.exists(path):
        return []
    try:
        payload = json.load(open(path, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return list(payload.get("products") or [])


def load_catalog_products():
    """Sealed product metadata from Pokémon and One Piece catalog JSON files."""
    products = []
    for path in PRODUCTS_FILES:
        products.extend(load_json_products(path))
    return products


def load_singles_products():
    """Pokémon singles catalog (EX/V+ rarity bands). Not written to urls.txt."""
    return load_json_products(SINGLES_FILE)


def load_listed_products():
    """Sealed catalogs plus Pokémon singles, for images, kinds, and name lookup."""
    return load_catalog_products() + load_singles_products()


def load_image_lookup():
    """TCGPlayer catalog images plus optional local overrides in data/product_image_overrides.json."""
    lookup = {}
    for row in load_listed_products():
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
    for row in load_listed_products():
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


def env_flag(name, default=True):
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def singles_limit():
    raw = os.environ.get("SCRAPE_SINGLES_LIMIT")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def read_singles_entries():
    """Pokémon singles URLs from data/pokemon_singles.json. Optional SCRAPE_SINGLES_LIMIT for tests."""
    if not env_flag("SCRAPE_SINGLES", True):
        return []
    entries = []
    for row in load_singles_products():
        url = row.get("url")
        if not url:
            continue
        entries.append({
            "url": url,
            "setName": row.get("setName"),
            "productKind": "single",
            "listed": row,
        })
    limit = singles_limit()
    if limit is not None:
        entries = entries[:limit]
    return entries


def singles_fetch_concurrency(raw=None):
    """How many singles Infinite/HTTP product jobs may run at once (1–8)."""
    value = os.environ.get("SINGLES_FETCH_CONCURRENCY") if raw is None else raw
    if value is None or str(value).strip() == "":
        return DEFAULT_SINGLES_FETCH_CONCURRENCY
    try:
        count = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SINGLES_FETCH_CONCURRENCY
    return max(1, min(count, MAX_SINGLES_FETCH_CONCURRENCY))


def load_records():
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_json(path, payload, compact=False):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")


def save_records(records):
    save_json(DATA_FILE, records)


def request_headers(referer=None):
    headers = dict(API_HEADERS)
    if referer:
        headers["Referer"] = referer
    return headers


def chart_fetch_concurrency(raw=None):
    """How many Infinite chart HTTP calls may run at once. Keep this low; empty replies wipe Movers."""
    value = os.environ.get("CHART_FETCH_CONCURRENCY") if raw is None else raw
    if value is None or str(value).strip() == "":
        return DEFAULT_CHART_FETCH_CONCURRENCY
    try:
        count = int(value)
    except (TypeError, ValueError):
        return DEFAULT_CHART_FETCH_CONCURRENCY
    return max(1, min(count, 4))


def fetch_price_history_json(product_id, range_name, referer, attempts=4, verbose=True):
    """Raw Infinite detailed history (all SKUs/conditions) for one chart range."""
    api_url = f"https://infinite-api.tcgplayer.com/price/history/{product_id}/detailed?range={range_name}"
    if verbose:
        print(f"DEBUG: Requesting {range_name} chart data from {api_url}")
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(api_url, headers=request_headers(referer), timeout=15)
            if response.status_code != 200:
                last_error = f"status {response.status_code}"
                if verbose:
                    print(f"DEBUG: {range_name} chart request failed with status {response.status_code} (attempt {attempt}/{attempts})")
            else:
                payload = response.json()
                if history_skus(payload):
                    return payload
                last_error = "empty SKUs"
                if verbose:
                    print(f"DEBUG: {range_name} chart returned no SKUs (attempt {attempt}/{attempts})")
        except Exception as exc:
            last_error = shorten_error(exc)
            if verbose:
                print(f"DEBUG: {range_name} chart request error: {exc} (attempt {attempt}/{attempts})")
        if attempt < attempts:
            time.sleep(0.8 * attempt)
    if last_error and verbose:
        print(f"DEBUG: {range_name} chart gave up ({last_error})")
    return {}


def fetch_price_history(product_id, range_name, referer, attempts=4, verbose=True):
    payload = fetch_price_history_json(product_id, range_name, referer, attempts=attempts, verbose=verbose)
    rows = parse_history_buckets(payload)
    if rows:
        return rows
    if verbose and payload:
        print(f"DEBUG: {range_name} chart returned no buckets")
    return []


def listing_quantity_from_aggs(aggregations):
    """Sum listing quantity buckets: value is copies on a listing, count is how many listings."""
    total = 0
    for bucket in (aggregations or {}).get("quantity") or []:
        if not isinstance(bucket, dict):
            continue
        copies = parse_numeric(bucket.get("value"))
        listings = parse_numeric(bucket.get("count"))
        if copies is None or listings is None:
            continue
        total += copies * listings
    return total


def fetch_listings_snapshot(product_id, referer, condition=None, printing=None):
    """size=0 marketplace search: seller count plus listed quantity for one filter."""
    api_url = f"https://mp-search-api.tcgplayer.com/v1/product/{product_id}/listings"
    term = {
        "sellerStatus": "Live",
        "channelId": 0,
        "language": ["English"],
    }
    if condition:
        term["condition"] = [condition]
    if printing:
        term["printing"] = [printing]
    body = {
        "filters": {
            "term": term,
            "range": {"quantity": {"gte": 1}},
            "exclude": {"channelExclusion": 0},
        },
        "from": 0,
        "size": 0,
        "sort": {"field": "price+shipping", "order": "asc"},
        "context": {"shippingCountry": "US", "cart": {}},
        "aggregations": ["listingType"],
    }
    response = requests.post(api_url, headers=request_headers(referer), json=body, timeout=15)
    if response.status_code != 200:
        raise RuntimeError(f"listings status {response.status_code}")
    block = ((response.json() or {}).get("results") or [{}])[0] or {}
    aggregations = block.get("aggregations") or {}
    sellers = parse_numeric(block.get("totalResults"))
    if sellers is None:
        sellers = 0
    return {
        "currentSellers": sellers,
        "currentQuantity": listing_quantity_from_aggs(aggregations),
    }


def fetch_listing_stats(product_id, referer, printing=None, conditions=CARD_CONDITIONS):
    """Per-condition listed quantity and seller counts. One cheap size=0 POST each."""
    stats = {}
    used_printing = printing
    for attempt in range(2):
        stats = {}
        failures = 0
        for condition in conditions:
            try:
                stats[condition] = fetch_listings_snapshot(
                    product_id, referer, condition=condition, printing=used_printing
                )
            except Exception as exc:
                failures += 1
                print(f"DEBUG: listings {product_id} {condition}: {shorten_error(exc)}")
        if used_printing and attempt == 0:
            sellers = sum((row or {}).get("currentSellers") or 0 for row in stats.values())
            if sellers == 0 and failures < len(list(conditions)):
                used_printing = None
                continue
        break
    return stats


def build_condition_stats(month_json, listing_stats, today_date):
    """Market price, last-day sales, and live listing counts for each TCGPlayer card condition."""
    skus = history_skus(month_json)
    listing_stats = listing_stats or {}
    out = {}
    for condition in CARD_CONDITIONS:
        sku = select_chart_sku(skus, condition=condition)
        points = points_from_sku(sku)
        listing = listing_stats.get(condition) or {}
        last_day = latest_completed_sales(
            [{"date": row["date"], "quantitySold": row.get("quantitySold")} for row in points],
            today_date,
        )
        out[condition] = {
            "variant": (sku or {}).get("variant") or "",
            "language": (sku or {}).get("language") or "English",
            "marketPrice": latest_sku_price(points),
            "lastDaySales": parse_numeric(last_day),
            "currentQuantity": listing.get("currentQuantity"),
            "currentSellers": listing.get("currentSellers"),
        }
    return out


def condition_charts_from_month(month_json):
    skus = history_skus(month_json)
    charts = {}
    for condition in CARD_CONDITIONS:
        sku = select_chart_sku(skus, condition=condition)
        if not sku:
            continue
        charts[condition] = compact_condition_chart(sku)
    return charts


def merge_chart_product(previous, incoming):
    """Keep prior range points when this scrape got an empty Infinite reply."""
    merged = dict(incoming or {})
    prev_ranges = (previous or {}).get("ranges") or {}
    new_ranges = dict((incoming or {}).get("ranges") or {})
    for key in ("1M", "3M", "1Y"):
        new_pts = (new_ranges.get(key) or {}).get("points") or []
        old_block = prev_ranges.get(key) or {}
        old_pts = old_block.get("points") or []
        if new_pts:
            continue
        if old_pts:
            new_ranges[key] = old_block
            print(f"DEBUG: kept previous {key} chart for {merged.get('productId')} ({len(old_pts)} points)")
    merged["ranges"] = new_ranges
    if not merged.get("productName") and previous:
        merged["productName"] = previous.get("productName")
    if not merged.get("imageUrl") and previous:
        merged["imageUrl"] = previous.get("imageUrl")
    prev_charts = (previous or {}).get("conditionCharts") or {}
    new_charts = dict((incoming or {}).get("conditionCharts") or {})
    merged_charts = dict(prev_charts)
    for condition, block in new_charts.items():
        if block and (block.get("dates") or block.get("prices") or block.get("sold")):
            merged_charts[condition] = block
    if merged_charts:
        merged["conditionCharts"] = merged_charts
    preferred = (incoming or {}).get("preferredCondition") or (previous or {}).get("preferredCondition")
    if preferred:
        merged["preferredCondition"] = preferred
    return merged


def merge_chart_history(existing_payload, incoming_products, updated_at):
    by_id = {}
    for row in (existing_payload or {}).get("products") or []:
        product_id = str(row.get("productId") or "")
        if product_id:
            by_id[product_id] = row
    for row in incoming_products or []:
        product_id = str(row.get("productId") or "")
        if not product_id:
            continue
        previous = by_id.get(product_id)
        by_id[product_id] = merge_chart_product(previous, row) if previous else row
    return {"updatedAt": updated_at, "products": list(by_id.values())}


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


def fetch_latest_sales_http(product_id, referer, limit=LATEST_SALES_LIMIT, conditions=None):
    """Fallback POST used when the stealth browser capture is empty."""
    api_url = f"https://mpapi.tcgplayer.com/v2/product/{product_id}/latestsales?mpfev=5429"
    page_size = 25
    collected = []
    offset = 0
    while len(collected) < limit:
        body = {
            "conditions": list(conditions or []),
            "languages": [],
            "variants": [],
            "listingType": "All",
            "limit": min(page_size, limit - len(collected)),
            "offset": offset,
        }
        try:
            response = requests.post(api_url, headers=request_headers(referer), json=body, timeout=15)
            if response.status_code != 200:
                print(f"DEBUG: Latest sales HTTP request failed with status {response.status_code}")
                break
            batch = (response.json() or {}).get("data") or []
            if not batch:
                break
            collected.extend(batch)
            offset += len(batch)
            if len(batch) < page_size:
                break
        except Exception as exc:
            print(f"DEBUG: Latest sales HTTP request error: {exc}")
            break
    return collected[:limit]


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
        signal: AbortSignal.timeout(15000),
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


async def maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def scrape_concurrency(raw=None):
    """How many headless Chrome tabs to keep busy at once (1–8)."""
    value = os.environ.get("SCRAPE_CONCURRENCY") if raw is None else raw
    if value is None or str(value).strip() == "":
        return DEFAULT_SCRAPE_CONCURRENCY
    try:
        count = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SCRAPE_CONCURRENCY
    return max(1, min(count, MAX_SCRAPE_CONCURRENCY))


async def bounded_gather(items, concurrency, func):
    """Run async work with a hard cap so Scrapling's tab pool is never oversubscribed."""
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run(index, item):
        async with semaphore:
            return index, await func(index, item)

    pairs = await asyncio.gather(*(run(index, item) for index, item in enumerate(items)))
    pairs.sort(key=lambda row: row[0])
    return [item for _index, item in pairs]


def capture_latest_sales_action(product_id, sink):
    """Async page_action for AsyncStealthySession (it awaits this callback)."""
    async def page_action(page):
        try:
            evaluate = getattr(page, "evaluate")
            try:
                result = evaluate(LATEST_SALES_JS, [str(product_id), LATEST_SALES_LIMIT], isolated_context=False)
            except TypeError:
                result = evaluate(LATEST_SALES_JS, [str(product_id), LATEST_SALES_LIMIT])
            result = await maybe_await(result)
            sales = result.get("sales") if isinstance(result, dict) else result
            if isinstance(sales, list):
                sink.extend(sales)
        except Exception as exc:
            print(f"DEBUG: Browser latest-sales capture failed: {exc}")
        return page
    return page_action


def scrape_succeeded(product_name, market_price):
    """A listing counts as scraped when we have a real name and a usable market price."""
    ok, _reason = classify_scrape(product_name, market_price)
    return ok


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
    for row in load_listed_products():
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


def format_issue_lines(issues):
    """Group Discord failure/partial lines by reason, then set."""
    by_reason = {}
    for set_name, item, reason in issues:
        by_reason.setdefault(reason, {})
        by_reason[reason].setdefault(set_name, [])
        if item not in by_reason[reason][set_name]:
            by_reason[reason][set_name].append(item)
    lines = []
    for reason, sets in by_reason.items():
        parts = [f"{set_name}: {', '.join(items)}" for set_name, items in sets.items()]
        lines.append(f"- {reason} — {'; '.join(parts)}")
    return lines


def format_scrape_status(results, games, set_index, family_index):
    """Discord body: per-era success, or set name plus why listings failed/were partial."""
    grouped = {}
    for row in results:
        place = resolve_scrape_placement(row.get("setName"), row.get("familyId"), set_index, family_index)
        key = (place["game_id"], place["family_id"])
        bucket = grouped.setdefault(key, {
            "game_id": place["game_id"],
            "game_name": place["game_name"],
            "family_name": place["family_name"],
            "failures": [],
            "partials": [],
        })
        set_name = row.get("setName") or "Unknown set"
        item = row.get("productName") or "Unknown product"
        reason = row.get("reason") or "unknown error"
        if not row.get("ok"):
            bucket["failures"].append((set_name, item, reason))
        elif row.get("reason"):
            bucket["partials"].append((set_name, item, reason))

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
            if bucket["failures"]:
                lines.append(f"{bucket['family_name']} - Failed")
                lines.extend(format_issue_lines(bucket["failures"]))
                if bucket["partials"]:
                    lines.extend(format_issue_lines(bucket["partials"]))
            elif bucket["partials"]:
                lines.append(f"{bucket['family_name']} - Partial")
                lines.extend(format_issue_lines(bucket["partials"]))
            else:
                lines.append(f"{bucket['family_name']} - Successfully Scraped")
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


def empty_scrape_result(entry, listed, error=None):
    url = entry["url"]
    product_id = extract_product_id(url)
    fallback_name = listed.get("name") or url
    fallback_set = entry.get("setName") or listed.get("setName")
    return {
        "records": [],
        "chart_product": None,
        "latest_sales": [],
        "scrape_result": {
            "ok": False,
            "reason": f"request error: {shorten_error(error)}" if error else "unknown error",
            "setName": fallback_set or "Other",
            "productName": fallback_name,
            "familyId": listed.get("familyId"),
            "productKind": entry.get("productKind") or listed.get("kind"),
        },
    }


def fetch_history_payloads(product_id, url, verbose=True):
    payloads = {}
    for range_name in CHART_RANGES:
        payloads[range_name] = fetch_price_history_json(product_id, range_name, url, verbose=verbose)
    return payloads


def range_payload_from_histories(payloads, condition=None):
    range_payload = {}
    for range_name, meta in CHART_RANGES.items():
        points = parse_history_buckets(payloads.get(range_name), condition=condition)
        range_payload[meta["key"]] = {
            "interval": meta["interval"],
            "label": meta["label"],
            "points": points,
        }
    return range_payload


def fetch_chart_ranges(product_id, url, today_date, verbose=True):
    range_payload = {}
    daily_buckets = []
    last_day_sales = "N/A"
    try:
        payloads = fetch_history_payloads(product_id, url, verbose=verbose)
        range_payload = range_payload_from_histories(payloads)
        for meta in CHART_RANGES.values():
            points = (range_payload.get(meta["key"]) or {}).get("points") or []
            if verbose:
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
        if last_day_sales != "N/A" and verbose:
            print(f">>> SUCCESS: Found Sales Data: {last_day_sales} across {len(daily_buckets)} daily buckets")
    except Exception as exc:
        print(f"DEBUG: Chart history request error: {exc}")
    return range_payload, daily_buckets, last_day_sales


async def scrape_one_entry(session, entry, ctx):
    """Fetch one product page in a pooled Chrome tab, then pull Infinite chart/sales APIs."""
    from bs4 import BeautifulSoup

    url = entry["url"]
    product_id = extract_product_id(url)
    listed = ctx["products_by_id"].get(str(product_id or "")) or ctx["products_by_url"].get(url.split("?")[0]) or {}
    fallback_name = listed.get("name") or url
    fallback_set = entry.get("setName") or listed.get("setName")
    fallback_family = listed.get("familyId")
    today_date = ctx["today_date"]

    try:
        captured_sales = []
        soup = None
        page_body = ""
        empty_page = False
        for attempt in range(1, EMPTY_PAGE_RETRIES + 2):
            captured_sales.clear()
            fetch_kwargs = {
                "timeout": PAGE_FETCH_TIMEOUT_MS,
                "wait_selector": "h1",
                "wait_selector_state": "visible",
            }
            if product_id:
                fetch_kwargs["page_action"] = capture_latest_sales_action(product_id, captured_sales)
            last_page = await maybe_await(session.fetch(url, **fetch_kwargs))
            page_body = getattr(last_page, "body", "") or ""
            soup = BeautifulSoup(page_body, "html.parser")
            empty_page = page_looks_empty(soup, page_body)
            if not empty_page:
                break
            print(f"DEBUG: empty/blocked product page (attempt {attempt}/{EMPTY_PAGE_RETRIES + 1})", flush=True)
            if attempt <= EMPTY_PAGE_RETRIES:
                await asyncio.sleep(1.5 * attempt)

        product_name = get_product_name(soup)
        html_name = product_name
        set_name = infer_set_name(url, product_name if html_name != "Unknown Product" else "", entry.get("setName")) or fallback_set
        product_kind = ctx["kind_lookup"].get(str(product_id or ""))
        image_url = extract_image_url(soup, product_id, ctx["image_lookup"].get(str(product_id or "")))
        html_price = extract_html_market_price(soup)
        market_price = html_price
        recent_sale = extract_metric(soup, "Most Recent Sale")
        listed_median = extract_metric(soup, "Listed Median")
        current_sellers = extract_metric(soup, "Current Sellers")
        current_quantity = extract_metric(soup, "Current Quantity")

        daily_buckets = []
        range_payload = {}
        chart_price = None
        display_name = fallback_name
        latest_sales = []
        chart_product = None

        if product_id:
            chart_sema = ctx.get("chart_sema")
            if chart_sema is not None:
                async with chart_sema:
                    range_payload, daily_buckets, last_day_sales = await asyncio.to_thread(
                        fetch_chart_ranges, product_id, url, today_date
                    )
            else:
                range_payload, daily_buckets, last_day_sales = await asyncio.to_thread(
                    fetch_chart_ranges, product_id, url, today_date
                )
            chart_price = latest_chart_price(range_payload)
            if usable_price(html_price) is None and chart_price is not None:
                market_price = chart_price
                print(f"DEBUG: Using chart market price {chart_price} (page had {html_price!r})")
            if html_name == "Unknown Product" and fallback_name:
                product_name = fallback_name
            display_name = product_name if product_name and product_name != "Unknown Product" else fallback_name

            if len(captured_sales) < LATEST_SALES_LIMIT:
                fallback_sales = await asyncio.to_thread(fetch_latest_sales_http, product_id, url)
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
            latest_sales = normalized[:LATEST_SALES_LIMIT]
            print(f">>> Latest transactions captured: {len(latest_sales)}")

            chart_product = {
                "productId": product_id,
                "productName": product_name,
                "setName": set_name,
                "productKind": product_kind,
                "imageUrl": image_url,
                "url": url,
                "ranges": range_payload,
            }

        parsed_market = usable_price(market_price)
        record = {
            "date": today_date,
            "productId": product_id,
            "productName": display_name,
            "setName": set_name or fallback_set,
            "productKind": product_kind,
            "imageUrl": image_url,
            "marketPrice": parsed_market,
            "recentSale": parse_numeric(recent_sale, as_float=True),
            "listedMedian": parse_numeric(listed_median, as_float=True),
            "currentSellers": parse_numeric(current_sellers),
            "currentQuantity": parse_numeric(current_quantity),
            "lastDaySales": None,
            "url": url,
        }
        records = [record]
        records.extend(
            history_records_for_product(
                product_id, display_name, url, daily_buckets, today_date, set_name or fallback_set, image_url, product_kind
            )
        )
        ok, reason = classify_scrape(
            html_name,
            html_price,
            chart_price=chart_price,
            empty_page=empty_page,
        )
        if reason:
            print(f">>> Scrape status: {'partial' if ok else 'failed'} — {reason}")
        return {
            "records": records,
            "chart_product": chart_product,
            "latest_sales": latest_sales,
            "scrape_result": {
                "ok": ok,
                "reason": reason,
                "setName": set_name or fallback_set or "Other",
                "productName": display_name,
                "familyId": fallback_family,
                "productKind": product_kind,
            },
        }
    except Exception as exc:
        print(f"Failed to scrape {url}: {exc}")
        return empty_scrape_result(entry, listed, error=exc)


async def scrape_all_entries(session, entries, ctx, concurrency):
    total = len(entries)

    async def run_one(index, entry):
        print(f"\n======================================", flush=True)
        print(f"Scraping [{index + 1}/{total}]: {entry['url']}", flush=True)
        return await scrape_one_entry(session, entry, ctx)

    return await bounded_gather(entries, concurrency, run_one)


def scrape_one_single(entry, ctx):
    """Infinite chart + listings + latest-sales HTTP for one singles card. No Chrome tab."""
    url = entry["url"]
    product_id = extract_product_id(url)
    listed = entry.get("listed") or ctx["products_by_id"].get(str(product_id or "")) or {}
    fallback_name = listed.get("name") or url
    fallback_set = entry.get("setName") or listed.get("setName")
    fallback_family = listed.get("familyId")
    today_date = ctx["today_date"]
    if not product_id:
        return empty_scrape_result(entry, listed, error=RuntimeError("missing product id"))

    try:
        payloads = fetch_history_payloads(product_id, url, verbose=False)
        range_payload = range_payload_from_histories(payloads)
        preferred_sku = select_chart_sku(payloads.get("month"))
        preferred_condition = (preferred_sku or {}).get("condition") or DEFAULT_CARD_CONDITION
        printing = (preferred_sku or {}).get("variant") or None
        listing_stats = fetch_listing_stats(product_id, url, printing=printing)
        conditions = build_condition_stats(payloads.get("month"), listing_stats, today_date)
        preferred_stats = conditions.get(preferred_condition) or conditions.get(DEFAULT_CARD_CONDITION) or {}
        chart_price = preferred_stats.get("marketPrice") or latest_chart_price(range_payload)
        captured_sales = fetch_latest_sales_http(product_id, url, limit=SINGLES_SALES_LIMIT)
        display_name = fallback_name
        set_name = fallback_set
        image_url = listed.get("imageUrl") or ctx["image_lookup"].get(str(product_id))
        product_kind = "single"
        market_price = chart_price

        normalized = [
            sale for sale in (
                normalize_sale(row, product_id, display_name, url, set_name)
                for row in captured_sales
            )
            if sale and sale.get("orderDate")
        ]
        normalized.sort(key=lambda row: row.get("orderDate") or "", reverse=True)
        latest_sales = normalized[:SINGLES_SALES_LIMIT]

        record = {
            "date": today_date,
            "productId": product_id,
            "productName": display_name,
            "setName": set_name,
            "productKind": product_kind,
            "imageUrl": image_url,
            "marketPrice": usable_price(market_price),
            "recentSale": None,
            "listedMedian": None,
            "currentSellers": preferred_stats.get("currentSellers"),
            "currentQuantity": preferred_stats.get("currentQuantity"),
            "lastDaySales": preferred_stats.get("lastDaySales"),
            "url": url,
            "preferredCondition": preferred_condition,
            "conditions": conditions,
        }
        chart_product = {
            "productId": product_id,
            "productName": display_name,
            "setName": set_name,
            "productKind": product_kind,
            "imageUrl": image_url,
            "url": url,
            "ranges": range_payload,
            "preferredCondition": preferred_condition,
            "conditionCharts": condition_charts_from_month(payloads.get("month")),
        }
        ok, reason = classify_scrape(
            display_name,
            market_price if market_price is not None else "N/A",
            chart_price=chart_price,
        )
        return {
            "records": [record],
            "chart_product": chart_product,
            "latest_sales": latest_sales,
            "scrape_result": {
                "ok": ok,
                "reason": reason,
                "setName": set_name or "Other",
                "productName": display_name,
                "familyId": fallback_family,
                "productKind": product_kind,
            },
        }
    except Exception as exc:
        print(f"Failed to scrape single {url}: {exc}")
        return empty_scrape_result(entry, listed, error=exc)


async def scrape_all_singles(entries, ctx, concurrency):
    total = len(entries)

    async def run_one(index, entry):
        if index == 0 or (index + 1) % 50 == 0 or index + 1 == total:
            print(f"Singles [{index + 1}/{total}]: {entry.get('listed', {}).get('name') or entry['url']}", flush=True)
        return await asyncio.to_thread(scrape_one_single, entry, ctx)

    return await bounded_gather(entries, concurrency, run_one)


async def scrape_with_session(entries, ctx, concurrency):
    from scrapling.fetchers import AsyncStealthySession

    ctx = dict(ctx)
    ctx["chart_concurrency"] = ctx.get("chart_concurrency") or chart_fetch_concurrency()
    ctx["chart_sema"] = asyncio.Semaphore(ctx["chart_concurrency"])
    print(
        f"Initializing Scrapling AsyncStealthySession with {concurrency} "
        f"headless Chrome tab(s) for {len(entries)} URLs "
        f"(chart HTTP concurrency {ctx['chart_concurrency']})..."
    )
    async with AsyncStealthySession(
        headless=True,
        solve_cloudflare=True,
        max_pages=concurrency,
        timeout=PAGE_FETCH_TIMEOUT_MS,
    ) as session:
        return await scrape_all_entries(session, entries, ctx, concurrency)


def main():
    sealed_entries = read_tracked_urls() if env_flag("SCRAPE_SEALED", True) else []
    singles_entries = read_singles_entries()
    if not sealed_entries and not singles_entries:
        print("No sealed URLs or Pokémon singles to scrape.")
        return

    today_date = datetime.datetime.now().strftime("%Y-%m-%d")
    concurrency = scrape_concurrency()
    singles_concurrency = singles_fetch_concurrency()
    ctx = {
        "today_date": today_date,
        "image_lookup": load_image_lookup(),
        "kind_lookup": catalog_kind_lookup(),
        "products_by_id": None,
        "products_by_url": None,
        "chart_concurrency": chart_fetch_concurrency(),
    }
    games_order, set_index, family_index = load_set_catalog()
    ctx["products_by_id"], ctx["products_by_url"] = product_lookups()

    results = []
    if sealed_entries:
        print(f"Scraping {len(sealed_entries)} sealed listing(s) with {concurrency} Chrome tab(s).")
        results.extend(asyncio.run(scrape_with_session(sealed_entries, ctx, concurrency)))
    else:
        print("Skipping sealed listings.")

    if singles_entries:
        print(
            f"Scraping {len(singles_entries)} Pokémon single(s) via Infinite/HTTP "
            f"(concurrency {singles_concurrency}, no Chrome tabs)."
        )
        results.extend(asyncio.run(scrape_all_singles(singles_entries, ctx, singles_concurrency)))

    all_data_rows = []
    scrape_results = []
    chart_products = []
    latest_sales_rows = []
    for item in results:
        all_data_rows.extend(item.get("records") or [])
        scrape_results.append(item["scrape_result"])
        if item.get("chart_product"):
            chart_products.append(item["chart_product"])
        latest_sales_rows.extend(item.get("latest_sales") or [])

    if all_data_rows:
        print("\nWriting tracker data to local JSON...")
        existing_records = load_records()
        merged_records = upsert_records(existing_records, all_data_rows)
        save_records(merged_records)
        print(f"Saved {len(all_data_rows)} snapshot(s). Archive now has {len(merged_records)} record(s) in {DATA_FILE}.")
    else:
        print("\nNo data was successfully scraped.")

    if chart_products:
        sealed_charts = [row for row in chart_products if row.get("productKind") != "single"]
        singles_charts = [row for row in chart_products if row.get("productKind") == "single"]
        if sealed_charts:
            existing_charts = {}
            if os.path.exists(CHART_HISTORY_FILE):
                try:
                    existing_charts = json.load(open(CHART_HISTORY_FILE, encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    existing_charts = {}
            sealed_existing = {
                "updatedAt": existing_charts.get("updatedAt"),
                "products": [row for row in (existing_charts.get("products") or []) if row.get("productKind") != "single"],
            }
            merged_charts = merge_chart_history(sealed_existing, sealed_charts, today_date)
            save_json(CHART_HISTORY_FILE, merged_charts)
            print(f"Wrote chart history for {len(merged_charts.get('products') or [])} sealed product(s) to {CHART_HISTORY_FILE}.")
        if singles_charts:
            existing_singles = {}
            if os.path.exists(SINGLES_CHART_HISTORY_FILE):
                try:
                    existing_singles = json.load(open(SINGLES_CHART_HISTORY_FILE, encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    existing_singles = {}
            merged_singles = merge_chart_history(existing_singles, singles_charts, today_date)
            save_json(SINGLES_CHART_HISTORY_FILE, merged_singles, compact=True)
            print(f"Wrote chart history for {len(merged_singles.get('products') or [])} single(s) to {SINGLES_CHART_HISTORY_FILE}.")
    if latest_sales_rows:
        existing_sales = {}
        if os.path.exists(LATEST_SALES_FILE):
            try:
                existing_sales = json.load(open(LATEST_SALES_FILE, encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing_sales = {}
        scraped_ids = {str(row.get("productId") or "") for row in latest_sales_rows}
        kept = [
            row for row in (existing_sales.get("sales") or [])
            if str(row.get("productId") or "") not in scraped_ids
        ]
        save_json(LATEST_SALES_FILE, {"updatedAt": today_date, "sales": latest_sales_rows + kept})
        print(f"Wrote {len(latest_sales_rows)} latest transaction(s) to {LATEST_SALES_FILE}.")

    print("Sending Discord notification...")
    discord_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not discord_url:
        print("DISCORD_WEBHOOK_URL environment variable is missing; skipping Discord notification.")
        return

    sealed_status = [row for row in scrape_results if row.get("productKind") != "single"]
    singles_status = [row for row in scrape_results if row.get("productKind") == "single"]
    status_blocks = format_scrape_status(sealed_status, games_order, set_index, family_index)
    if singles_status:
        ok = sum(1 for row in singles_status if row.get("ok"))
        status_blocks.append(
            f"**Pokémon singles:** {ok}/{len(singles_status)} cards updated (Infinite API, not Chrome)."
        )
    messages_to_send = pack_discord_messages(status_blocks)
    print(
        f"Packed scrape status into {len(messages_to_send)} "
        f"Discord message(s) (max {DISCORD_PACK_LIMIT} chars each, hard cap {DISCORD_MESSAGE_LIMIT})."
    )
    send_discord_messages(discord_url, messages_to_send)


if __name__ == "__main__":
    main()
