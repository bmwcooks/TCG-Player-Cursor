import json
import os
import requests
import datetime
import re
from bs4 import BeautifulSoup
from scrapling.fetchers import StealthySession

# --- Configuration ---
URLS_FILE = "urls.txt"
DATA_DIR = "data"
DATA_FILE = os.path.join(DATA_DIR, "tracker_data.json")


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


def find_latest_sales(obj):
    """Recursively searches JSON arrays backwards to find the most recent sales integer."""
    if isinstance(obj, dict):
        # Look for daily sales volume keys, specifically ignoring averages or totals
        for k in ["itemsSold", "sales", "volume", "sold", "ItemsSold", "quantitySold"]:
            if k in obj and obj[k] is not None:
                # We want the daily number, not the 'totalQuantitySold' for the quarter
                if "total" not in k.lower() and "average" not in k.lower():
                    return str(obj[k])
                
        # Target common array wrapper keys (added 'timeline' and 'transactions')
        for key in ["data", "results", "result", "priceHistory", "points", "timeline", "transactions"]:
            if key in obj:
                res = find_latest_sales(obj[key])
                if res != "N/A": return res
                
        # Deep search fallback
        for k, v in obj.items():
            # Skip drilling into summary stats
            if "total" in k.lower() or "average" in k.lower():
                continue
            res = find_latest_sales(v)
            if res != "N/A": return res
            
    elif isinstance(obj, list) and len(obj) > 0:
        # Search the list backwards (newest dates are typically at the end of the array)
        for item in reversed(obj):
            res = find_latest_sales(item)
            if res != "N/A": return res
            
    return "N/A"


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


def load_records():
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_records(records):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
        f.write("\n")


def upsert_records(existing, new_records):
    """Replace a snapshot when the same product is scraped again on the same date."""
    index = {(row.get("date"), str(row.get("productId"))): i for i, row in enumerate(existing)}
    for record in new_records:
        key = (record.get("date"), str(record.get("productId")))
        if key in index:
            existing[index[key]] = record
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

    with open(URLS_FILE, 'r') as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]

    if not urls:
        print(f"No valid URLs found in {URLS_FILE}.")
        return

    # --- 2. Scrape Data ---
    all_data_rows = []
    discord_blocks = []
    today_date = datetime.datetime.now().strftime("%Y-%m-%d")

    print(f"Initializing Scrapling StealthySession to scrape {len(urls)} URLs...")
    with StealthySession(headless=True, solve_cloudflare=True) as session:
        for url in urls:
            print(f"\n======================================")
            print(f"Scraping: {url}")
            try:
                # Normal fetch without XHR interception
                page = session.fetch(url)
                soup = BeautifulSoup(page.body, 'html.parser')

                product_name = get_product_name(soup)
                market_price = extract_metric(soup, "Market Price")
                recent_sale = extract_metric(soup, "Most Recent Sale")
                listed_median = extract_metric(soup, "Listed Median")
                current_sellers = extract_metric(soup, "Current Sellers")
                current_quantity = extract_metric(soup, "Current Quantity")

                # --- DIRECT API QUERY FOR CHART DATA ---
                last_day_sales = "N/A"
                product_id_match = re.search(r'product/(\d+)', url)
                
                if product_id_match:
                    product_id = product_id_match.group(1)
                    
                    # Target the infinite-api that drives the Market Price History chart
                    api_url = f"https://infinite-api.tcgplayer.com/price/history/{product_id}/detailed?range=quarter"
                    
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "application/json",
                        "Referer": url,
                        "Origin": "https://www.tcgplayer.com"
                    }
                    
                    try:
                        print(f"DEBUG: Requesting chart data directly from {api_url}")
                        api_response = requests.get(api_url, headers=headers, timeout=10)
                        
                        if api_response.status_code == 200:
                            history_data = api_response.json()
                            last_day_sales = find_latest_sales(history_data)
                            
                            if last_day_sales != "N/A":
                                print(f">>> SUCCESS: Found Sales Data: {last_day_sales}")
                            else:
                                print(f"DEBUG: Parsed JSON, but no sales data keys were found. Raw: {str(history_data)[:250]}...")
                        else:
                            print(f"DEBUG: Direct API request failed with status {api_response.status_code}")
                            
                    except Exception as e:
                        print(f"DEBUG: Direct API request error: {e}")

                product_id = extract_product_id(url)
                record = {
                    "date": today_date,
                    "productId": product_id,
                    "productName": product_name,
                    "marketPrice": parse_numeric(market_price, as_float=True),
                    "recentSale": parse_numeric(recent_sale, as_float=True),
                    "listedMedian": parse_numeric(listed_median, as_float=True),
                    "currentSellers": parse_numeric(current_sellers),
                    "currentQuantity": parse_numeric(current_quantity),
                    "lastDaySales": parse_numeric(last_day_sales),
                    "url": url,
                }
                all_data_rows.append(record)

                # Build text block for Discord
                block = (
                    f"**{product_name}**\n"
                    f"Market: {market_price} | Recent Sale: {recent_sale} | Median: {listed_median}\n"
                    f"Sellers: {current_sellers} | Qty: {current_quantity} | Last Day Sales: {last_day_sales}\n"
                    f"[View Listing](<{url}>)"
                )
                discord_blocks.append(block)

            except Exception as e:
                print(f"Failed to scrape {url}: {e}")

    if not all_data_rows:
        print("\nNo data was successfully scraped. Exiting.")
        return

    # --- 3. Local JSON storage ---
    print("\nWriting tracker data to local JSON...")
    existing_records = load_records()
    merged_records = upsert_records(existing_records, all_data_rows)
    save_records(merged_records)
    print(f"Saved {len(all_data_rows)} snapshot(s). Archive now has {len(merged_records)} record(s) in {DATA_FILE}.")

    # --- 4. Discord Integration ---
    print("Sending Discord notification...")
    discord_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not discord_url:
        print("DISCORD_WEBHOOK_URL environment variable is missing; skipping Discord notification.")
        return
        
    header = "**TCGPlayer Daily Update** 📊\n\n"
    current_message = header
    messages_to_send = []

    for block in discord_blocks:
        if len(current_message) + len(block) + 4 > 1900:
            messages_to_send.append(current_message)
            current_message = block + "\n\n"
        else:
            current_message += block + "\n\n"
            
    if current_message.strip():
        messages_to_send.append(current_message)

    for i, msg_text in enumerate(messages_to_send):
        response = requests.post(discord_url, json={"content": msg_text})
        if response.status_code in [200, 204]:
            print(f"Discord notification part {i+1}/{len(messages_to_send)} sent successfully!")
        else:
            print(f"Failed to send Discord part {i+1}: {response.status_code}, {response.text}")

if __name__ == "__main__":
    main()
