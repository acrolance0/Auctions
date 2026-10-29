import os
import json
import time
import requests
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from datetime import datetime
import re

STATE_FILE = 'auction_state.json'

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def predict_price(base_price_str):
    # Extract numbers from string
    num_str = ''.join(filter(str.isdigit, str(base_price_str)))
    if not num_str:
        return "Unknown"
    base_price = int(num_str)
    # Simple heuristic for competitive residential areas (Tokyo/Yokohama)
    predicted_min = int(base_price * 1.4)
    predicted_max = int(base_price * 1.9)
    return f"¥{predicted_min:,} - ¥{predicted_max:,}"

def check_location(address):
    # focus only on tokyo areas and yokohama areas. avoid chiba and saitama.
    if '千葉' in address or '埼玉' in address:
        return False
    if '東京' in address or '横浜' in address:
        return True
    return False

def check_residential(prop_type, details_text):
    # Avoid land sales without residential living space.
    if '土地' in prop_type and '建物' not in prop_type:
        return False
    # If the description explicitly mentions it's just land or empty lot without a house
    if '更地' in details_text and '建物' not in details_text:
        return False
    return True

def fetch_html(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br'
    }
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.content
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None

def scrape_koubai():
    base_url = "https://www.koubai.nta.go.jp/auctionx/public/"

    # 13 is Tokyo, 14 is Kanagawa
    target_addr_ids = [13, 14]
    all_links = set()

    print("Scraping Koubai...")
    for addr_id in target_addr_ids:
        search_url = urljoin(base_url, f"./hp0241.php?addr_id={addr_id}&addr_area=0&addr_wide=0&addr_or_line=0&addr_name=0&zaisan_bunrui_chk%5B100%5D=1")

        # Scrape up to 5 pages per prefecture
        for page in range(5):
            url = f"{search_url}&pageid={page}"
            html = fetch_html(url)
            if not html:
                break
            soup = BeautifulSoup(html, 'html.parser')
            page_has_links = False
            for a in soup.find_all('a', href=True):
                if 'hp0201.php' in a['href']:
                    all_links.add(urljoin(base_url, a['href']))
                    page_has_links = True

            # If no detail links found, assume we've reached the last page
            if not page_has_links:
                break
            time.sleep(1)

    listings = []
    for link in all_links:
        html = fetch_html(link)
        if not html:
            continue
        soup = BeautifulSoup(html, 'html.parser')
        title = soup.title.text.strip() if soup.title else "Unknown Title"

        details = {}
        for th in soup.find_all('th'):
            if th.next_sibling:
                td = th.find_next_sibling('td')
                if td:
                    details[th.text.strip()] = td.text.strip()

        address = details.get("住居表示等", "") + details.get("所在地（登記簿表示内容）", "")
        prop_type = details.get("種別", "")
        price_str = details.get("見積価額", "")
        desc = details.get("地盤・地勢", "") + " " + details.get("使用状況", "")

        if check_location(address) and check_residential(prop_type, desc):
            listing_id = f"koubai_{re.search(r'baikyaku_no=([0-9]+)', link).group(1) if re.search(r'baikyaku_no=([0-9]+)', link) else hash(link)}"

            clean_desc = desc[:200] + "..." if len(desc) > 200 else desc

            listings.append({
                'id': listing_id,
                'url': link,
                'title': title,
                'address': address,
                'price': price_str,
                'description': clean_desc,
                'predicted_price': predict_price(price_str),
                'source': 'Koubai'
            })
        time.sleep(0.5)

    return listings

def scrape_981():
    print("Scraping 981.jp...")
    # 981.jp often blocks datacenter IPs (like this sandbox), so we handle errors gracefully.
    listings = []

    # URL for Tokyo search: https://981.jp/ftl/search_re.do?prefCode=13
    # URL for Kanagawa search: https://981.jp/ftl/search_re.do?prefCode=14

    for pref_code in [13, 14]:
        url = f"https://981.jp/ftl/search_re.do?prefCode={pref_code}"
        html = fetch_html(url)
        if not html:
            continue
        soup = BeautifulSoup(html, 'html.parser')

        # This is a generic logic to parse 981.jp items
        # Adjusting the classes based on common structures, but typically they block us anyway.
        for item in soup.find_all('div', class_=re.compile(r'property|item')):
            try:
                link_tag = item.find('a', href=True)
                if not link_tag:
                    continue
                link = urljoin("https://981.jp", link_tag['href'])
                if 'property_detail' not in link and 'detail.do' not in link:
                    continue

                text_content = item.get_text()

                # Try to extract details if possible, otherwise use full text snippet
                address_match = re.search(r'(東京都.*?区.*?|神奈川県横浜市.*?区.*?)[\s\n]', text_content)
                address = address_match.group(1) if address_match else "東京都/横浜市"

                prop_type_match = re.search(r'(戸建|マンション|土地)', text_content)
                prop_type = prop_type_match.group(1) if prop_type_match else "不明"

                price_match = re.search(r'([0-9,]+)\s*円', text_content)
                price_str = price_match.group(0) if price_match else "不明"

                if check_location(address) and check_residential(prop_type, text_content):
                    listing_id = f"981_{hash(link)}"
                    listings.append({
                        'id': listing_id,
                        'url': link,
                        'title': "981.jp Property",
                        'address': address,
                        'price': price_str,
                        'description': text_content[:200].replace('\n', ' ').strip() + "...",
                        'predicted_price': predict_price(price_str),
                        'source': '981.jp'
                    })
            except Exception:
                pass
        time.sleep(1)

    return listings

def generate_report(new_listings, updated_listings):
    date_str = datetime.now().strftime('%Y-%m-%d')
    report_filename = f"daily_auction_report_{date_str}.md"

    with open(report_filename, 'w', encoding='utf-8') as f:
        f.write(f"# Daily Real Estate Auction Report - {date_str}\n\n")
        f.write("Focus: Tokyo & Yokohama Areas | Excludes: Chiba, Saitama, & Bare Land\n\n")

        if not new_listings and not updated_listings:
            f.write("*No new or meaningfully updated listings found today.*\n")
            return report_filename

        if new_listings:
            f.write("## 🆕 New Listings\n\n")
            for item in new_listings:
                f.write(f"### [{item['title']}]({item['url']})\n")
                f.write(f"- **Source**: {item['source']}\n")
                f.write(f"- **Address**: {item['address']}\n")
                f.write(f"- **Current Bid/Estimate**: {item['price']}\n")
                f.write(f"- **Sale Price Prediction**: **{item['predicted_price']}** *(Based on 1.4x-1.9x competitive area multiplier)*\n")
                f.write(f"- **Description**: {item['description']}\n\n")

        if updated_listings:
            f.write("## 🔄 Updated Listings\n\n")
            for item in updated_listings:
                f.write(f"### [{item['title']}]({item['url']})\n")
                f.write(f"- **Source**: {item['source']}\n")
                f.write(f"- **Address**: {item['address']}\n")
                f.write(f"- **New Bid/Estimate**: {item['price']}\n")
                f.write(f"- **Sale Price Prediction**: **{item['predicted_price']}**\n")
                f.write(f"- **Description**: {item['description']}\n\n")

    return report_filename

def main():
    state = load_state()

    all_current_listings = []

    # Scrape sites
    all_current_listings.extend(scrape_koubai())
    all_current_listings.extend(scrape_981())

    new_listings = []
    updated_listings = []

    for listing in all_current_listings:
        lid = listing['id']
        if lid not in state:
            new_listings.append(listing)
            state[lid] = listing
        else:
            # Check for meaningful updates (e.g., price changed)
            if state[lid]['price'] != listing['price']:
                updated_listings.append(listing)
                state[lid] = listing

    report_file = generate_report(new_listings, updated_listings)
    save_state(state)

    print(f"Report generated: {report_file}")

if __name__ == "__main__":
    main()
