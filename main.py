import os
import locale
from datetime import datetime
import json
import requests
from bs4 import BeautifulSoup
from flask import Flask

# Initialize the Flask web application
app = Flask(__name__)

# --- CONFIGURATION ---
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
YOUR_SLACK_USER_ID = os.environ.get("YOUR_SLACK_USER_ID")
LUNCHDRIVE_URL = "https://lunchdrive.cz/cs/d/3792"

def get_daily_menu():
    print("Step 2: Attempting to get daily menu.")
    try:
        print("  - Setting locale to cs_CZ.UTF-8")
        locale.setlocale(locale.LC_TIME, 'cs_CZ.UTF-8')
        print("  - Locale set successfully.")
    except locale.Error as e:
        # This is a non-critical error, we just log it and continue.
        print(f"  - WARNING: Czech locale not available on this server. This might be the problem. Error: {e}")

    try:
        print(f"  - Fetching URL: {LUNCHDRIVE_URL}")
        response = requests.get(LUNCHDRIVE_URL, timeout=15) # Increased timeout
        response.raise_for_status() # Check for HTTP errors like 404 or 500
        print("  - URL fetched successfully. Parsing HTML.")

        soup = BeautifulSoup(response.content, 'html.parser')
        
        today_date_string = datetime.now().strftime("%A %-d.%-m.%Y").lower()
        print(f"  - Searching for today's menu header with string: '{today_date_string}'")
        
        todays_header = soup.find('h2', string=lambda text: text and today_date_string in text.lower())

        if not todays_header:
            print("  - CRITICAL: Today's menu header was NOT found on the page.")
            return "Dnes se nevaří, nebo se nepodařilo najít dnešní menu. 🙁"

        print("  - Menu header found. Extracting menu items from the table.")
        menu_table = todays_header.find_next_sibling('table', class_='table-menu')
        menu_items = []
        for row in menu_table.find_all('tr'):
            cols = row.find_all('td')
            if len(cols) == 3:
                label, name, price = (c.get_text(strip=True) for c in cols)
                menu_items.append(f"• *{label}:* {name} - _{price}_")
        
        if not menu_items:
            print("  - WARNING: Menu table was found but it's empty.")
            return "Menu pro dnešek je prázdné."
            
        print("  - Menu items extracted successfully.")
        return "\n".join(menu_items)

    except Exception as e:
        print(f"  - CRITICAL ERROR in get_daily_menu: {e}")
        return "Došlo k závažné chybě při zpracování menu."

def send_slack_dm(menu_text):
    print("Step 3: Attempting to send Slack DM.")
    if not SLACK_BOT_TOKEN or not YOUR_SLACK_USER_ID:
        error_msg = "  - CRITICAL: SLACK_BOT_TOKEN or YOUR_SLACK_USER_ID is not configured."
        print(error_msg)
        return error_msg

    message_payload = {
        "channel": YOUR_SLACK_USER_ID,
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "🚨 Nejvyšší čas objednat oběd! 🚨", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Dnešní nabídka z Mona's - Bistro:*"}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": menu_text}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "Objednávejte zde: <https://lunchdrive.cz/cs/d/3792|LunchDrive>"}}
        ]
    }
    try:
        print("  - Sending request to Slack API.")
        response = requests.post("https://slack.com/api/chat.postMessage", json=message_payload, headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'})
        response.raise_for_status()
        result = response.json()
        if result.get("ok"):
            print("  - Slack DM sent successfully!")
            return "OK"
        else:
            error_msg = f"  - CRITICAL: Error sending Slack DM: {result.get('error')}"
            print(error_msg)
            return error_msg
    except Exception as e:
        error_msg = f"  - CRITICAL ERROR in send_slack_dm: {e}"
        print(error_msg)
        return error_msg

# This is the main entry point for Cloud Run
@app.route('/')
def trigger_reminder():
    print("--- Step 1: Request received, starting reminder process. ---")
    # Wrap everything in a try-except block to guarantee we log any crash
    try:
        if datetime.now().weekday() in [0, 1, 2, 3, 6]: # Sun-Thu
            print("  - It's a valid day for a reminder.")
            menu = get_daily_menu()
            result = send_slack_dm(menu)
            print(f"--- Process finished. Final status: {result} ---")
            return (result, 200)
        else:
            msg = "Not a reminder day. No action taken."
            print(f"--- Process finished. {msg} ---")
            return (msg, 200)
    except Exception as e:
        # This is a final safety net.
        final_error = f"--- A FATAL UNEXPECTED ERROR occurred: {e} ---"
        print(final_error)
        return (final_error, 500)

if __name__ == "__main__":
    # This part is for local testing, Cloud Run uses a Gunicorn server instead.
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
