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
PAGE_FETCH_TIMEOUT_MS = 20000
EMPTY_PAGE_RETRIES = 1
DEFAULT_SCRAPE_CONCURRENCY = 3
MAX_SCRAPE_CONCURRENCY = 8
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
    labeled = extract_metric(soup, "Market Price")
    if labeled != "N/A":
        return labeled
    _, json_price = json_ld_name_price(soup)
    if json_price is not None:
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


def fetch_price_history(product_id, range_name, referer, attempts=2):
    api_url = f"https://infinite-api.tcgplayer.com/price/history/{product_id}/detailed?range={range_name}"
    print(f"DEBUG: Requesting {range_name} chart data from {api_url}")
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(api_url, headers=request_headers(referer), timeout=15)
            if response.status_code != 200:
                last_error = f"status {response.status_code}"
                print(f"DEBUG: {range_name} chart request failed with status {response.status_code} (attempt {attempt}/{attempts})")
            else:
                rows = parse_history_buckets(response.json())
                if rows:
                    return rows
                last_error = "empty buckets"
                print(f"DEBUG: {range_name} chart returned no buckets (attempt {attempt}/{attempts})")
        except Exception as exc:
            last_error = shorten_error(exc)
            print(f"DEBUG: {range_name} chart request error: {exc} (attempt {attempt}/{attempts})")
        if attempt < attempts:
            time.sleep(0.8)
    if last_error:
        print(f"DEBUG: {range_name} chart gave up ({last_error})")
    return []


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
        },
    }


def fetch_chart_ranges(product_id, url, today_date):
    range_payload = {}
    daily_buckets = []
    last_day_sales = "N/A"
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


async def scrape_with_session(entries, ctx, concurrency):
    from scrapling.fetchers import AsyncStealthySession

    print(
        f"Initializing Scrapling AsyncStealthySession with {concurrency} "
        f"headless Chrome tab(s) for {len(entries)} URLs..."
    )
    async with AsyncStealthySession(
        headless=True,
        solve_cloudflare=True,
        max_pages=concurrency,
        timeout=PAGE_FETCH_TIMEOUT_MS,
    ) as session:
        return await scrape_all_entries(session, entries, ctx, concurrency)


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
    today_date = datetime.datetime.now().strftime("%Y-%m-%d")
    concurrency = scrape_concurrency()
    ctx = {
        "today_date": today_date,
        "image_lookup": load_image_lookup(),
        "kind_lookup": catalog_kind_lookup(),
        "products_by_id": None,
        "products_by_url": None,
    }
    games_order, set_index, family_index = load_set_catalog()
    ctx["products_by_id"], ctx["products_by_url"] = product_lookups()

    results = asyncio.run(scrape_with_session(entries, ctx, concurrency))

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
