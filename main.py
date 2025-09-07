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
# The script will get these values from the secure environment variables you set up later.
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
YOUR_SLACK_USER_ID = os.environ.get("YOUR_SLACK_USER_ID")
LUNCHDRIVE_URL = "https://lunchdrive.cz/cs/d/3792"

def get_daily_menu():
    """Fetches and parses the lunch menu for the current day."""
    try:
        locale.setlocale(locale.LC_TIME, 'cs_CZ.UTF-8')
    except locale.Error:
        print("Warning: Czech locale not available.")

    try:
        response = requests.get(LUNCHDRIVE_URL, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        today_date_string = datetime.now().strftime("%A %-d.%-m.%Y").lower()
        
        todays_header = soup.find('h2', string=lambda text: text and today_date_string in text.lower())

        if not todays_header:
            return "Dnes se nevaří, nebo se nepodařilo najít dnešní menu. 🙁"

        menu_table = todays_header.find_next_sibling('table', class_='table-menu')
        menu_items = []
        for row in menu_table.find_all('tr'):
            cols = row.find_all('td')
            if len(cols) == 3:
                label, name, price = (c.get_text(strip=True) for c in cols)
                menu_items.append(f"• *{label}:* {name} - _{price}_")

        return "\n".join(menu_items) if menu_items else "Menu pro dnešek je prázdné."
    except Exception as e:
        print(f"An error occurred while getting the menu: {e}")
        return "Došlo k chybě při zpracování menu."

def send_slack_dm(menu_text):
    """Sends the formatted menu as a direct message."""
    if not SLACK_BOT_TOKEN or not YOUR_SLACK_USER_ID:
        error_msg = "Error: SLACK_BOT_TOKEN or YOUR_SLACK_USER_ID is not configured."
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
        response = requests.post("https://slack.com/api/chat.postMessage", json=message_payload, headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'})
        response.raise_for_status()
        if response.json().get("ok"):
            print("Slack DM sent successfully!")
            return "OK"
        else:
            error_msg = f"Error sending Slack DM: {response.json().get('error')}"
            print(error_msg)
            return error_msg
    except Exception as e:
        error_msg = f"An error occurred sending the Slack DM: {e}"
        print(error_msg)
        return error_msg

# This defines the web endpoint. When Cloud Scheduler calls our URL, this code runs.
@app.route('/')
def trigger_reminder():
    if datetime.now().weekday() in [0, 1, 2, 3, 6]: # Sun-Thu
        print("Valid day. Fetching menu...")
        menu = get_daily_menu()
        result = send_slack_dm(menu)
        return (result, 200)
    else:
        msg = "Not a day for reminders. No action taken."
        print(msg)
        return (msg, 200)

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))