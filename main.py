import os
import locale
# ZMĚNA: Musíme importovat 'timedelta' pro výpočet zítřejšího data
from datetime import datetime, timedelta 
import json
import requests
from bs4 import BeautifulSoup
from flask import Flask
import random

app = Flask(__name__)

# --- CONFIGURATION ---
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
YOUR_SLACK_USER_ID = os.environ.get("YOUR_SLACK_USER_ID")
LUNCHDRIVE_URL = "https://lunchdrive.cz/cs/d/3792"
TARGET_PRICE = 125
URGENT_EMOJIS = ["🚨", "🔥", "⏰", "🍔", "🏃‍♂️", "💨", "‼️"]

def get_daily_menu():
    print("Step 2: Attempting to get daily menu.")
    try:
        print(f"  - Fetching URL: {LUNCHDRIVE_URL}")
        response = requests.get(LUNCHDRIVE_URL, timeout=15)
        response.raise_for_status()
        print("  - URL fetched successfully. Parsing HTML.")
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # ZMĚNA: Vypočítáme zítřejší datum
        tomorrow = datetime.now() + timedelta(days=1)
        target_date_string = tomorrow.strftime("%-d.%-m.%Y")
        
        print(f"  - Searching for TOMORROW's menu header with string: '{target_date_string}'")
        
        todays_header = soup.find('h2', string=lambda text: text and target_date_string in text)

        if not todays_header:
            print("  - CRITICAL: Tomorrow's menu header was NOT found on the page.")
            return "Menu na zítra ještě není k dispozici, nebo se dnes nevaří. 🙁"

        print("  - Menu header found. Extracting and filtering menu items.")
        menu_table = todays_header.find_next_sibling('table', class_='table-menu')
        menu_items = []
        for row in menu_table.find_all('tr'):
            cols = row.find_all('td')
            if len(cols) == 3:
                label = cols[0].get_text(strip=True)
                name = cols[1].get_text(strip=True)
                price_text = cols[2].get_text(strip=True)

                try:
                    price_clean = price_text.replace('Kč', '').strip()
                    price_as_int = int(price_clean)
                    
                    if price_as_int == TARGET_PRICE:
                        print(f"  - MATCH FOUND: '{name}' for {price_text}")
                        menu_items.append(f"• *{label}:* {name}")
                except (ValueError, TypeError):
                    continue
        
        if not menu_items:
            print(f"  - WARNING: No meals found for the target price of {TARGET_PRICE} Kč.")
            return f"Na zítra bohužel není v nabídce žádné jídlo za {TARGET_PRICE} Kč. 🍽️"
            
        print("  - Menu items filtered successfully.")
        return "\n".join(menu_items)

    except Exception as e:
        print(f"  - CRITICAL ERROR in get_daily_menu: {e}")
        return "Došlo k závažné chybě při zpracování menu."

def send_slack_dm(menu_text):
    print("Step 3: Attempting to send Slack DM.")
    if not SLACK_BOT_TOKEN or not YOUR_SLACK_USER_ID:
        return "Error"
    
    random_emoji = random.choice(URGENT_EMOJIS)
    
    # ZMĚNA: Změníme text, aby bylo jasné, že jde o zítřejší menu
    message_payload = {
        "channel": YOUR_SLACK_USER_ID,
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"{random_emoji} Nezapomeň objednat oběd NA ZÍTRA! {random_emoji}", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Zítřejší nabídka za {TARGET_PRICE} Kč:*"}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": menu_text}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "Objednávejte zde: <https://lunchdrive.cz/cs/d/3792|LunchDrive>"}}
        ]
    }
    try:
        response = requests.post("https://slack.com/api/chat.postMessage", json=message_payload, headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'})
        response.raise_for_status()
        result = response.json()
        if result.get("ok"):
            print("  - Slack DM sent successfully!")
            return "OK"
        else:
            return f"Error: {result.get('error')}"
    except Exception as e:
        return f"An error occurred in send_slack_dm: {e}"

@app.route('/')
def trigger_reminder():
    print("--- Step 1: Request received, starting reminder process. ---")
    # ZMĚNA: Upravíme dny, kdy se skript spouští. V pátek se neobjednává na sobotu.
    # Běžíme v neděli (na pondělí) až ve čtvrtek (na pátek).
    if datetime.now().weekday() in [0, 1, 2, 3, 6]: # 6=Ne, 0=Po, 1=Út, 2=St, 3=Čt
        menu = get_daily_menu()
        result = send_slack_dm(menu)
        return (result, 200)
    else:
        # V pátek a v sobotu se skript nespustí
        return ("Not a reminder day (Friday/Saturday).", 200)

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
