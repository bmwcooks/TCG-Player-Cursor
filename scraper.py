import json
import os
import requests
import datetime
import re
from bs4 import BeautifulSoup
from scrapling.fetchers import StealthySession
import gspread
from google.oauth2.service_account import Credentials

# --- Configuration ---
SHEET_ID = "17yp7twEPAwu4P42p8AGUolW4515IkqvUBu2rvQS8iUM"
TAB_NAME = "TCGPlayer Data"
URLS_FILE = "urls.txt"

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
            print(f"Scraping: {url}")
            try:
                # Use capture_xhr to intercept all background API requests to the marketplace API
                page = session.fetch(url, capture_xhr="mpapi.tcgplayer.com")
                soup = BeautifulSoup(page.body, 'html.parser')

                product_name = get_product_name(soup)
                market_price = extract_metric(soup, "Market Price")
                recent_sale = extract_metric(soup, "Most Recent Sale")
                listed_median = extract_metric(soup, "Listed Median")
                current_sellers = extract_metric(soup, "Current Sellers")
                current_quantity = extract_metric(soup, "Current Quantity")

                # --- EXTRACT CAPTURED XHR CHART DATA ---
                last_day_sales = "N/A"
                if hasattr(page, 'captured_xhr') and page.captured_xhr:
                    # Iterate through all captured requests to the TCGPlayer internal API
                    for xhr in page.captured_xhr:
                        try:
                            # Handle different response object structures (callable vs property)
                            body_content = xhr.body() if callable(getattr(xhr, 'body', None)) else xhr.body
                            history_data = json.loads(body_content)
                            
                            # Unwrap 'data', 'results', etc.
                            if isinstance(history_data, dict):
                                data_payload = history_data.get("data", history_data.get("results", history_data.get("priceHistory", [])))
                            else:
                                data_payload = history_data
                            
                            # Ensure it's a list and look for our sales data
                            if isinstance(data_payload, list) and len(data_payload) > 0:
                                last_entry = data_payload[-1]
                                
                                # Check if this specific XHR payload contains sales data
                                if "itemsSold" in last_entry or "sales" in last_entry or "volume" in last_entry:
                                    last_day_sales = str(last_entry.get("itemsSold", last_entry.get("sales", last_entry.get("volume", "N/A"))))
                                    print(f"Successfully extracted sales data from XHR: {last_day_sales}")
                                    break  # We found the sales data, stop searching other XHRs
                        except Exception:
                            # Skip XHRs that aren't valid JSON or don't match our expected structure
                            continue

                # --- EXTRACT CAPTURED XHR CHART DATA ---
                last_day_sales = "N/A"
                if hasattr(page, 'captured_xhr') and page.captured_xhr:
                    # Iterate through captured requests matching our pattern
                    for xhr in page.captured_xhr:
                        try:
                            # Parse the body as JSON
                            history_data = json.loads(xhr.body)
                            
                            # Unwrap 'data', 'results', or 'priceHistory' if the list is inside a dictionary
                            if isinstance(history_data, dict):
                                history_data = history_data.get("data", history_data.get("results", history_data.get("priceHistory", [])))
                            
                            if isinstance(history_data, list) and len(history_data) > 0:
                                last_entry = history_data[-1]
                                last_day_sales = str(last_entry.get("itemsSold", last_entry.get("sales", last_entry.get("volume", "N/A"))))
                                break  # Break out of loop once we find the data
                        except Exception as e:
                            print(f"Could not parse historical sales XHR for {url}: {e}")

                # Build row for Google Sheets (added Last Day Sales)
                data_row = [
                    today_date, product_name, market_price, recent_sale,
                    listed_median, current_sellers, current_quantity, last_day_sales, url
                ]
                all_data_rows.append(data_row)

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
        print("No data was successfully scraped. Exiting.")
        return

    # --- 3. Google Sheets Integration ---
    print("Connecting to Google Sheets...")
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        raise ValueError("GOOGLE_CREDENTIALS environment variable is missing.")
    
    creds_dict = json.loads(creds_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(credentials)
    
    sh = gc.open_by_key(SHEET_ID)
    
    try:
        worksheet = sh.worksheet(TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=TAB_NAME, rows="1000", cols="20")
    
    if not worksheet.get_all_values():
        headers = ["Date Pulled", "Product Name", "Market Price", "Most Recent Sale", "Listed Median", "Current Sellers", "Current Quantity", "Last Day Sales", "URL"]
        worksheet.append_row(headers)
        
    worksheet.append_rows(all_data_rows)
    print(f"Appended {len(all_data_rows)} rows to Google Sheets.")

    # --- 4. Discord Integration ---
    print("Sending Discord notification...")
    discord_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not discord_url:
        raise ValueError("DISCORD_WEBHOOK_URL environment variable is missing.")
        
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
