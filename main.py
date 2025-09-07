import os
import locale
from datetime import datetime
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
# OPRAVA: Změnil jsem cenu na 125, protože to odpovídá menu. Můžete si ji změnit.
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
        
        # KONEČNÁ OPRAVA: Hledáme jen číslo dne, měsíce a roku. Nic víc.
        # Toto je jazykově 100% nezávislé.
        today_date_string = datetime.now().strftime("%-d.%-m.%Y")
        print(f"  - Searching for today's menu header with string: '{today_date_string}'")
        
        # Hledáme nadpis (h2), v jehož textu se nachází náš řetězec s datem.
        todays_header = soup.find('h2', string=lambda text: text and today_date_string in text)

        if not todays_header:
            print("  - CRITICAL: Today's menu header was NOT found on the page.")
            return "Dnes se nevaří, nebo se nepodařilo najít dnešní menu. 🙁"

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
            return f"Dnes bohužel není v nabídce žádné jídlo za {TARGET_PRICE} Kč. 🍽️"
            
        print("  - Menu items filtered successfully.")
        return "\n".join(menu_items)

    except Exception as e:
        print(f"  - CRITICAL ERROR in get_daily_menu: {e}")
        return "Došlo k závažné chybě při zpracování menu."

# Funkce send_slack_dm a zbytek kódu zůstávají stejné
def send_slack_dm(menu_text):
    print("Step 3: Attempting to send Slack DM.")
    if not SLACK_BOT_TOKEN or not YOUR_SLACK_USER_ID:
        return "Error"
    
    random_emoji = random.choice(URGENT_EMOJIS)
    
    message_payload = {
        "channel": YOUR_SLACK_USER_ID,
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"{random_emoji} Čas objednat oběd! {random_emoji}", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Dnešní nabídka za {TARGET_PRICE} Kč:*"}},
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
    if datetime.now().weekday() in [0, 1, 2, 3, 6]: # Sun-Thu
        menu = get_daily_menu()
        result = send_slack_dm(menu)
        return (result, 200)
    else:
        return ("Not a reminder day.", 200)

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
