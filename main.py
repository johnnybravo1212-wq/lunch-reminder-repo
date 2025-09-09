import os
import json
import requests
import random
from datetime import datetime, timedelta
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template, abort, redirect, url_for

# Imports for Google Cloud Firestore and Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, firestore

# Imports for Slack Request Verification
from slack_sdk.signature import SignatureVerifier

# --- INITIALIZATION ---

# Initialize Flask App
app = Flask(__name__)

# Initialize Firebase Admin SDK
# On Google Cloud Run, the SDK automatically finds the project credentials.
# For local testing, you'd need a service account JSON file.
try:
    firebase_admin.initialize_app()
except Exception as e:
    print(f"Firebase Admin SDK already initialized or failed: {e}")

# Initialize Firestore Client
db = firestore.client()

# --- CONFIGURATION ---
# Load configuration from environment variables
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
SLACK_CLIENT_ID = os.environ.get("SLACK_CLIENT_ID")
SLACK_CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET")
BASE_URL = os.environ.get("BASE_URL")
LUNCHDRIVE_URL = os.environ.get("LUNCHDRIVE_URL", "https://lunchdrive.cz/cs/d/3792")
TARGET_PRICE = int(os.environ.get("TARGET_PRICE", 125))
ADMIN_SECRET_KEY = os.environ.get("ADMIN_SECRET_KEY")

# Emojis and other fun stuff
URGENT_EMOJIS = ["🚨", "🔥", "⏰", "🍔", "🏃‍♂️", "💨", "‼️", "🐸"]
PEPE_IMAGES = [
    "https://i.imgur.com/rvC5iI6.png", # Pepe Silvia
    "https://i.imgur.com/VzBqS1h.png", # Smug Frog
    "https://i.imgur.com/dJNDaF7.png"  # Pepe Punch
]

# --- DATABASE HELPER FUNCTIONS ---

def get_all_subscribed_users():
    """Fetches all user IDs from the 'users' collection in Firestore."""
    users_ref = db.collection('users')
    docs = users_ref.stream()
    return [doc.id for doc in docs]

def add_user(user_id):
    """Adds a new user to the 'users' collection."""
    db.collection('users').document(user_id).set({'subscribed_at': firestore.SERVER_TIMESTAMP})

def remove_user(user_id):
    """Removes a user from the 'users' collection."""
    db.collection('users').document(user_id).delete()

def save_user_order(user_id, meal_choice, order_for_date):
    """Saves a user's meal choice to the 'orders' collection."""
    order_data = {
        'slack_user_id': user_id,
        'meal_description': meal_choice,
        'order_for_date': order_for_date.strftime("%Y-%m-%d"),
        'placed_on_date': datetime.now().strftime("%Y-%m-%d")
    }
    # Using user_id and date as a unique document ID to prevent duplicate orders
    doc_id = f"{user_id}_{order_for_date.strftime('%Y-%m-%d')}"
    db.collection('orders').document(doc_id).set(order_data)

def get_orders_for_date(target_date):
    """Fetches all orders for a specific date."""
    orders_ref = db.collection('orders')
    query = orders_ref.where('order_for_date', '==', target_date.strftime("%Y-%m-%d"))
    return [doc.to_dict() for doc in query.stream()]

# --- MENU SCRAPING LOGIC ---

def get_daily_menu():
    """
    Fetches and parses the menu for the *next* day from LunchDrive.
    Returns a list of menu items or an error string.
    """
    print("Attempting to get tomorrow's menu.")
    try:
        response = requests.get(LUNCHDRIVE_URL, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        tomorrow = datetime.now() + timedelta(days=1)
        target_date_string = tomorrow.strftime("%-d.%-m.%Y")
        
        print(f"Searching for menu header with date: '{target_date_string}'")
        todays_header = soup.find('h2', string=lambda text: text and target_date_string in text)

        if not todays_header:
            print("CRITICAL: Tomorrow's menu header was NOT found.")
            return "Menu na zítra ještě není k dispozici. 🙁"

        menu_table = todays_header.find_next_sibling('table', class_='table-menu')
        menu_items = []
        if not menu_table:
            return "Chyba: Tabulka s menu nebyla nalezena."

        for row in menu_table.find_all('tr'):
            cols = row.find_all('td')
            if len(cols) == 3:
                name = cols[1].get_text(strip=True)
                price_text = cols[2].get_text(strip=True)
                try:
                    price_clean = price_text.replace('Kč', '').strip()
                    price_as_int = int(price_clean)
                    if price_as_int == TARGET_PRICE:
                        menu_items.append(name)
                except (ValueError, TypeError):
                    continue
        
        if not menu_items:
            return f"Na zítra bohužel není v nabídce žádné jídlo za {TARGET_PRICE} Kč."
            
        return menu_items

    except Exception as e:
        print(f"CRITICAL ERROR in get_daily_menu: {e}")
        return "Došlo k závažné chybě při stahování menu."

# --- SLACK API & MESSAGE BUILDING ---

def send_slack_message(payload):
    """Generic function to send a message to the Slack API."""
    try:
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            json=payload,
            headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'}
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            print(f"Slack API Error: {result.get('error')}")
        return result
    except Exception as e:
        print(f"An error occurred in send_slack_message: {e}")
        return None

def build_reminder_message_blocks(menu_items):
    """Builds the Slack Block Kit structure for the daily reminder."""
    random_emoji = random.choice(URGENT_EMOJIS)
    
    # Format menu items for display
    menu_text = "\n".join([f"• {item}" for item in menu_items])
    
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{random_emoji} PepeEats: Objednej oběd NA ZÍTRA! {random_emoji}", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Zítřejší nabídka za {TARGET_PRICE} Kč:*"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": menu_text}},
        {"type": "image", "image_url": random.choice(PEPE_IMAGES), "alt_text": "A wild Pepe appears"},
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ Mám objednáno", "emoji": True}, "style": "primary", "action_id": "open_order_modal"},
                {"type": "button", "text": {"type": "plain_text", "text": "Zrušit odběr", "emoji": True}, "style": "danger", "action_id": "unsubscribe", "value": "unsubscribe_clicked"}
            ]
        }
    ]
    return blocks

def build_order_modal_view(menu_items):
    """Builds the Slack modal for selecting a meal."""
    options = [{"text": {"type": "plain_text", "text": item, "emoji": True}, "value": item} for item in menu_items]
    
    view = {
        "type": "modal",
        "callback_id": "order_submission",
        "title": {"type": "plain_text", "text": "PepeEats", "emoji": True},
        "submit": {"type": "plain_text", "text": "Uložit", "emoji": True},
        "close": {"type": "plain_text", "text": "Zrušit", "emoji": True},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Super! Co sis dnes objednal/a?"}},
            {
                "type": "input",
                "block_id": "meal_selection_block",
                "element": {
                    "type": "static_select",
                    "placeholder": {"type": "plain_text", "text": "Vyber jídlo", "emoji": True},
                    "options": options,
                    "action_id": "meal_selection_action"
                },
                "label": {"type": "plain_text", "text": "Tvoje volba", "emoji": True}
            }
        ]
    }
    return view

# --- FLASK ROUTES (ENDPOINTS) ---

@app.before_request
def verify_slack_request():
    """Verify that incoming requests from Slack are authentic."""
    # This verification is only for the interactive endpoint
    if request.path == '/slack/interactive':
        verifier = SignatureVerifier(SLACK_SIGNING_SECRET)
        if not verifier.is_valid_request(request.get_data(), request.headers):
            print("Invalid Slack signature")
            abort(403)

@app.route('/send-daily-reminder', methods=['POST'])
def trigger_daily_reminder():
    """
    Endpoint triggered by Cloud Scheduler to send the daily lunch menu.
    """
    print("--- Daily Reminder Job Started ---")
    
    # Run only on Sunday (for Mon) to Thursday (for Fri)
    if datetime.now().weekday() not in [0, 1, 2, 3, 6]:
        print("Not a reminder day (Friday/Saturday). Job ending.")
        return ("Not a reminder day.", 200)

    menu_items = get_daily_menu()
    
    if isinstance(menu_items, str): # It's an error message
        print(f"Could not get menu: {menu_items}")
        # Optionally, send an error to an admin here
        return (menu_items, 200)
    
    subscribed_users = get_all_subscribed_users()
    if not subscribed_users:
        print("No subscribed users to notify.")
        return ("No users.", 200)

    message_blocks = build_reminder_message_blocks(menu_items)
    
    for user_id in subscribed_users:
        print(f"Sending reminder to {user_id}")
        payload = {"channel": user_id, "blocks": message_blocks}
        send_slack_message(payload)
        
    print(f"--- Reminders sent to {len(subscribed_users)} users. Job finished. ---")
    return ("Reminders sent.", 200)

@app.route('/send-morning-reminder', methods=['POST'])
def trigger_morning_reminder():
    """
    Endpoint triggered by Cloud Scheduler to remind users what they ordered.
    """
    print("--- Morning Reminder Job Started ---")
    today = datetime.now()
    
    # Run only on workdays
    if today.weekday() in [5, 6]: # Saturday or Sunday
        print("Not a workday. Job ending.")
        return ("Not a workday.", 200)

    todays_orders = get_orders_for_date(today)
    if not todays_orders:
        print("No orders found for today.")
        return ("No orders for today.", 200)

    for order in todays_orders:
        user_id = order.get('slack_user_id')
        meal = order.get('meal_description')
        if user_id and meal:
            print(f"Sending morning reminder to {user_id}")
            message = f"Dobré ráno! 🐸 Jen připomínám, že dnes máš k obědu: *{meal}*"
            payload = {"channel": user_id, "text": message}
            send_slack_message(payload)
            
    print(f"--- Morning reminders sent for {len(todays_orders)} orders. Job finished. ---")
    return ("Morning reminders sent.", 200)

@app.route('/slack/interactive', methods=['POST'])
def slack_interactive_endpoint():
    """
    Handles all interactive components from Slack (button clicks, modal submits).
    """
    payload = json.loads(request.form.get("payload"))
    user_id = payload["user"]["id"]
    
    # Case 1: User submitted the meal selection modal
    if payload["type"] == "view_submission" and payload["view"]["callback_id"] == "order_submission":
        print(f"Received modal submission from {user_id}")
        
        # Extract the selected meal
        submitted_values = payload["view"]["state"]["values"]
        meal_block = submitted_values["meal_selection_block"]
        action = meal_block["meal_selection_action"]
        selected_meal = action["selected_option"]["value"]
        
        # Save to database
        tomorrow = datetime.now() + timedelta(days=1)
        save_user_order(user_id, selected_meal, tomorrow)
        
        # Send a confirmation DM
        confirmation_text = f"Díky! Uložil jsem, že na zítra máš objednáno: *{selected_meal}*"
        send_slack_message({"channel": user_id, "text": confirmation_text})
        
        return ("", 200) # Acknowledge the request

    # Case 2: User clicked a button in the original message
    if payload["type"] == "block_actions":
        action = payload["actions"][0]
        action_id = action.get("action_id")
        
        # User clicked the "I've Ordered" button
        if action_id == "open_order_modal":
            print(f"User {user_id} clicked 'I've Ordered'. Opening modal.")
            trigger_id = payload["trigger_id"]
            
            # We need to fetch the menu again to populate the modal
            menu_items = get_daily_menu()
            if isinstance(menu_items, str):
                send_slack_message({"channel": user_id, "text": "Omlouvám se, nepodařilo se mi znovu načíst menu pro výběr."})
                return ("", 200)
            
            modal_view = build_order_modal_view(menu_items)
            
            requests.post(
                "https://slack.com/api/views.open",
                json={"trigger_id": trigger_id, "view": modal_view},
                headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'}
            )
            return ("", 200)
            
        # User clicked the "Unsubscribe" button
        elif action_id == "unsubscribe":
            print(f"User {user_id} clicked 'Unsubscribe'.")
            remove_user(user_id)
            
            # Send confirmation message
            confirmation_text = "Je mi to líto, ale zrušil jsem ti odběr. Kdyby sis to rozmyslel, stačí se znovu přihlásit. 🐸"
            send_slack_message({"channel": user_id, "text": confirmation_text})
            
            return ("", 200)
            
    return ("Unhandled interaction", 200)

@app.route('/subscribe', methods=['GET'])
def subscribe():
    """Renders the subscription page with the 'Add to Slack' button."""
    # Construct the Slack OAuth URL
    params = {
        'client_id': SLACK_CLIENT_ID,
        'scope': 'chat:write,users:read', # Scopes needed by the app
        'redirect_uri': f"{BASE_URL}/slack/oauth/callback"
    }
    slack_auth_url = f"https://slack.com/oauth/v2/authorize?{urlencode(params)}"
    return render_template('subscribe.html', slack_auth_url=slack_auth_url)

@app.route('/slack/oauth/callback', methods=['GET'])
def oauth_callback():
    """Handles the redirect from Slack after user authorization."""
    code = request.args.get('code')
    if not code:
        return ("OAuth failed: No code provided.", 400)
        
    # Exchange the temporary code for an access token
    response = requests.post("https://slack.com/api/oauth.v2.access", data={
        'client_id': SLACK_CLIENT_ID,
        'client_secret': SLACK_CLIENT_SECRET,
        'code': code,
        'redirect_uri': f"{BASE_URL}/slack/oauth/callback"
    })
    
    data = response.json()
    if not data.get('ok'):
        return (f"OAuth Error: {data.get('error')}", 400)

    # The response contains authed_user.id
    user_id = data.get('authed_user', {}).get('id')
    if user_id:
        print(f"New user subscribed: {user_id}")
        add_user(user_id)
        
        # Send a welcome message
        welcome_text = "Vítej v PepeEats! 🎉 Od teď ti budu posílat denní připomínky na oběd."
        send_slack_message({"channel": user_id, "text": welcome_text})
        
        return "<h1>Success!</h1><p>You have been subscribed to PepeEats. You can close this window now.</p>"
    
    return ("OAuth failed: Could not get user ID.", 500)

@app.route('/admin', methods=['GET'])
def admin_panel():
    """Displays the admin dashboard with subscribers and today's orders."""
    # Simple password protection
    secret = request.args.get('secret')
    if secret != ADMIN_SECRET_KEY:
        abort(403) # Forbidden

    # Fetch data for the template
    users_docs = db.collection('users').stream()
    users_list = [{'id': doc.id} for doc in users_docs]
    
    today = datetime.now()
    orders_list = get_orders_for_date(today)
    
    return render_template('admin.html', users=users_list, orders=orders_list, today_str=today.strftime('%Y-%m-%d'))


if __name__ == "__main__":
    # This block is for local development only.
    # It allows you to run the app with `python app.py`.
    # It uses python-dotenv to load the .env file.
    from dotenv import load_dotenv
    load_dotenv()
    
    # Reload environment variables after loading .env
    SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
    SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
    SLACK_CLIENT_ID = os.environ.get("SLACK_CLIENT_ID")
    SLACK_CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET")
    BASE_URL = os.environ.get("BASE_URL")
    ADMIN_SECRET_KEY = os.environ.get("ADMIN_SECRET_KEY")

    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
