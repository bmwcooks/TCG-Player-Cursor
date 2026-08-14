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
URL = "https://www.tcgplayer.com/product/672394/pokemon-me03-perfect-order-perfect-order-booster-box?page=1&Language=English"
SHEET_ID = "17yp7twEPAwu4P42p8AGUolW4515IkqvUBu2rvQS8iUM"
TAB_NAME = "TCGPlayer Data"

def get_product_name(soup):
    """Finds the product title and cleans up appended affiliate/tracking text."""
    h1s = soup.find_all('h1')
    for h1 in h1s:
        text = h1.get_text(separator=" ", strip=True)
        if text:
            # Removes "Shop with Affiliates" and any trailing numbers glued to it
            text = re.sub(r'(?i)Shop with Affiliates.*', '', text).strip()
            return text
    return "Unknown Product"

def extract_metric(soup, label):
    """Robustly searches for a text label and extracts the closest number/price."""
    nodes = soup.find_all(string=re.compile(label, re.IGNORECASE))
    for node in nodes:
        parent = node.parent
        
        # Check next HTML siblings for a number
        for sibling in parent.next_siblings:
            if sibling.name:
                sib_text = sibling.get_text(strip=True)
                if re.search(r'\d+', sib_text):
                    return sib_text
                    
        # Check parent container text as a fallback
        if parent.parent:
            full_text = parent.parent.get_text(separator=" ", strip=True)
            pattern = re.compile(rf"{label}.*?(\$?\d+[,\d]*\.?\d*)", re.IGNORECASE)
            match = pattern.search(full_text)
            if match:
                return match.group(1)
    return "N/A"

def main():
    print("Initializing Scrapling StealthySession...")
    with StealthySession(headless=True, solve_cloudflare=True) as session:
        page = session.fetch(URL)
        html_content = page.body

    soup = BeautifulSoup(html_content, 'html.parser')

    product_name = get_product_name(soup)
    market_price = extract_metric(soup, "Market Price")
    recent_sale = extract_metric(soup, "Most Recent Sale")
    listed_median = extract_metric(soup, "Listed Median")
    current_sellers = extract_metric(soup, "Current Sellers")
    current_quantity = extract_metric(soup, "Current Quantity")
    today_date = datetime.datetime.now().strftime("%Y-%m-%d")

    data_row = [
        today_date,
        product_name,
        market_price,
        recent_sale,
        listed_median,
        current_sellers,
        current_quantity
    ]
    print("Extracted Data:", data_row)

    # --- Google Sheets Integration ---
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
        headers = ["Date Pulled", "Product Name", "Market Price", "Most Recent Sale", "Listed Median", "Current Sellers", "Current Quantity"]
        worksheet.append_row(headers)
        
    worksheet.append_row(data_row)
    print("Appended row to Google Sheets.")

    # --- Discord Integration ---
    print("Sending Discord notification...")
    discord_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not discord_url:
        raise ValueError("DISCORD_WEBHOOK_URL environment variable is missing.")
        
    discord_message = {
        "content": (
            f"**TCGPlayer Daily Update** 📊\n\n"
            f"**Product:** {product_name}\n"
            f"**Market Price:** {market_price}\n"
            f"**Most Recent Sale:** {recent_sale}\n"
            f"**Listed Median:** {listed_median}\n"
            f"**Current Sellers:** {current_sellers}\n"
            f"**Current Quantity:** {current_quantity}\n\n"
            f"[View on TCGPlayer](<{URL}>)"
        )
    }
    
    response = requests.post(discord_url, json=discord_message)
    if response.status_code in [200, 204]:
        print("Discord notification sent successfully!")
    else:
        print(f"Failed to send to Discord: {response.status_code}, {response.text}")

if __name__ == "__main__":
    main()
