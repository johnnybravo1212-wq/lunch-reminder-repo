import os
import json
import requests
import random
import logging
import re
from datetime import datetime, timedelta, date
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template, render_template_string, abort, redirect, url_for

# Imports for Google Cloud Firestore and Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, firestore

# Imports for Slack Request Verification
from slack_sdk.signature import SignatureVerifier

# --- INITIALIZATION ---

# Initialize Flask App
app = Flask(__name__)

# Set up proper logging
app.logger.setLevel(logging.INFO)

# Initialize Firebase Admin SDK
try:
    # No credentials needed when running on Google Cloud
    firebase_admin.initialize_app()
    app.logger.info("Firebase Admin SDK initialized successfully.")
except Exception as e:
    app.logger.warning(f"Firebase Admin SDK already initialized or failed: {e}")

# Initialize Firestore Client
db = firestore.client()

# --- CONFIGURATION ---
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
    "https://i.imgur.com/XoF6m62.png", # FeelsGoodMan
    "https://i.imgur.com/sBq2pPT.png", # Sad Frog
    "https://i.imgur.com/2OFa0s8.png"  # MonkaS / Nervous Pepe
]

# --- DATABASE HELPER FUNCTIONS ---

def get_all_subscribed_users():
    """Fetches all user data from the 'users' collection in Firestore.""" # <--- ZMĚNA (načítáme celá data)
    users_ref = db.collection('users')
    docs = users_ref.stream()
    # Vracíme slovník, kde klíč je ID a hodnota jsou data dokumentu
    return {doc.id: doc.to_dict() for doc in docs}

def add_user(user_id):
    """Adds a new user to the 'users' collection."""
    # Používáme merge=True, abychom nepřepsali existující data jako snooze
    db.collection('users').document(user_id).set({'subscribed_at': firestore.SERVER_TIMESTAMP}, merge=True)

def remove_user(user_id):
    """Removes a user from the 'users' collection."""
    db.collection('users').document(user_id).delete()

# <--- ZMĚNA: Nová funkce pro snooze ---
def snooze_user_until(user_id, snooze_date):
    """Sets a snooze date for a user."""
    snooze_str = snooze_date.strftime("%Y-%m-%d")
    app.logger.info(f"Snoozing user {user_id} until {snooze_str}")
    user_ref = db.collection('users').document(user_id)
    user_ref.set({'snoozed_until': snooze_str}, merge=True)

# <--- ZMĚNA: Nová funkce pro kontrolu snooze ---
def is_user_snoozed(user_data, check_date):
    """
    Checks if a user is snoozed for a specific date.
    user_data is the dictionary from Firestore.
    """
    snoozed_until_str = user_data.get('snoozed_until')
    if not snoozed_until_str:
        return False
    try:
        snoozed_until_date = datetime.strptime(snoozed_until_str, "%Y-%m-%d").date()
        # Pokud je datum snoozu stejné nebo v budoucnosti, je snooznutý
        if snoozed_until_date >= check_date:
            app.logger.info(f"User is snoozed until {snoozed_until_date}, checking for {check_date}. Result: SNOOZED.")
            return True
    except (ValueError, TypeError) as e:
        app.logger.error(f"Error parsing snooze date '{snoozed_until_str}': {e}")
        return False
    
    app.logger.info(f"User snooze date {snoozed_until_str} is in the past compared to {check_date}. Result: NOT snoozed.")
    return False


def save_user_order(user_id, meal_choice, order_for_date):
    """Saves a user's meal choice to the 'orders' collection."""
    order_data = {
        'slack_user_id': user_id,
        'meal_description': meal_choice,
        'order_for_date': order_for_date.strftime("%Y-%m-%d"),
        'placed_on_date': date.today().strftime("%Y-%m-%d")
    }
    doc_id = f"{user_id}_{order_for_date.strftime('%Y-%m-%d')}"
    db.collection('orders').document(doc_id).set(order_data)
    # <--- ZMĚNA: Po objednávce zrušíme snooze ---
    db.collection('users').document(user_id).set({'snoozed_until': None}, merge=True)


def get_orders_for_date(target_date):
    """Fetches all orders for a specific date."""
    orders_ref = db.collection('orders')
    query = orders_ref.where('order_for_date', '==', target_date.strftime("%Y-%m-%d"))
    return [doc.to_dict() for doc in query.stream()]

def check_if_user_ordered_for_date(user_id, target_date):
    """
    Checks if a specific user has already placed an order for a given date.
    Returns True if an order exists, False otherwise.
    """
    doc_id = f"{user_id}_{target_date.strftime('%Y-%m-%d')}"
    order_doc = db.collection('orders').document(doc_id).get()
    return order_doc.exists

def save_daily_menu(menu_date, menu_items):
    """Saves the menu for a specific date into the 'daily_menus' collection."""
    doc_id = menu_date.strftime("%Y-%m-%d")
    menu_data = {
        'date': doc_id,
        'menu_items': menu_items,
        'created_at': firestore.SERVER_TIMESTAMP
    }
    db.collection('daily_menus').document(doc_id).set(menu_data)
    app.logger.info(f"Successfully saved menu for {doc_id} to Firestore.")

def get_saved_menu_for_date(target_date):
    """Retrieves a saved menu for a specific date from Firestore."""
    doc_id = target_date.strftime("%Y-%m-%d")
    app.logger.info(f"Attempting to get saved menu for {doc_id} from Firestore.")
    menu_doc = db.collection('daily_menus').document(doc_id).get()
    if menu_doc.exists:
        app.logger.info(f"Found saved menu for {doc_id}.")
        return menu_doc.to_dict().get('menu_items', [])
    else:
        app.logger.error(f"Could not find saved menu for {doc_id} in Firestore.")
        return None

# --- MENU SCRAPING LOGIC ---

def get_daily_menu(target_date):
    """
    Fetches and parses the menu for a *specific* day from LunchDrive.
    Returns a list of menu items or an error string.
    """
    app.logger.info(f"Attempting to get menu for date: {target_date.strftime('%Y-%m-%d')}.")
    try:
        response = requests.get(LUNCHDRIVE_URL, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'lxml')
        # Změna pro robustnější hledání data (pro případy jako "1.1.2024")
        target_date_string = target_date.strftime("%-d.%-m.%Y")
        app.logger.info(f"Searching for menu header with date: '{target_date_string}'")
        
        menu_header = soup.find('h2', string=lambda text: text and target_date_string in text)

        if not menu_header:
            app.logger.error(f"CRITICAL: Menu header for {target_date_string} was NOT found.")
            return f"Menu na {target_date.strftime('%d.%m.')} ještě není k dispozici. 🙁"
        
        menu_table = menu_header.find_next_sibling('table', class_='table-menu')
        
        menu_items = []
        if not menu_table:
            return "Chyba: Tabulka s menu nebyla nalezena."

        for row in menu_table.find_all('tr'):
            cols = row.find_all('td')
            if len(cols) == 4:
                name = cols[2].get_text(strip=True)
                price_text = cols[3].get_text(strip=True)
                
                match = re.search(r'\d+', price_text)
                if match:
                    try:
                        price_as_int = int(match.group(0))
                        if price_as_int == TARGET_PRICE:
                            menu_items.append(name)
                    except (ValueError, TypeError):
                        continue
        
        if not menu_items:
            app.logger.warning(f"No meals found for target price {TARGET_PRICE} Kč on {target_date_string}")
            return f"Na {target_date.strftime('%d.%m.')} bohužel není v nabídce žádné jídlo za {TARGET_PRICE} Kč."

        return menu_items

    except Exception as e:
        app.logger.error(f"CRITICAL ERROR in get_daily_menu: {e}", exc_info=True)
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
            app.logger.error(f"Slack API Error: {result.get('error')}")
        return result
    except Exception as e:
        app.logger.error(f"An error occurred in send_slack_message: {e}", exc_info=True)
        return None

# <--- ZMĚNA: Nová funkce pro dočasné zprávy ---
def send_ephemeral_slack_message(channel_id, user_id, text, blocks=None):
    """Sends an ephemeral message visible only to a specific user."""
    payload = {
        "channel": channel_id,
        "user": user_id,
        "text": text
    }
    if blocks:
        payload["blocks"] = blocks
    
    try:
        response = requests.post(
            "https://slack.com/api/chat.postEphemeral",
            json=payload,
            headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'}
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            app.logger.error(f"Slack Ephemeral API Error: {result.get('error')}")
    except Exception as e:
        app.logger.error(f"An error occurred in send_ephemeral_slack_message: {e}", exc_info=True)


def build_reminder_message_blocks(menu_items):
    """Builds the Slack Block Kit structure for the daily reminder."""
    random_emoji = random.choice(URGENT_EMOJIS)
    menu_text = "\n".join([f"• {item}" for item in menu_items])
    open_app_url = f"{BASE_URL}/open-lunchdrive"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{random_emoji} PepeEats: Objednej oběd NA ZÍTRA! {random_emoji}", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Zítřejší nabídka za {TARGET_PRICE} Kč:*"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": menu_text}},
        {"type": "image", "image_url": random.choice(PEPE_IMAGES), "alt_text": "A wild Pepe appears"},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"Klikněte zde pro objednání: <{open_app_url}|*Otevřít LunchDrive*>"}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ Mám objednáno", "emoji": True}, "style": "primary", "action_id": "open_order_modal"},
            # <--- ZMĚNA: Přidání nových tlačítek ---
            {"type": "button", "text": {"type": "plain_text", "text": "Snooze pro dnešek", "emoji": True}, "action_id": "snooze_today"},
            {"type": "button", "text": {"type": "plain_text", "text": "Zítra jsem na HO", "emoji": True}, "action_id": "home_office_tomorrow"},
            {"type": "button", "text": {"type": "plain_text", "text": "Zrušit odběr", "emoji": True}, "style": "danger", "action_id": "unsubscribe", "value": "unsubscribe_clicked"}
        ]}
    ]
    return blocks

def build_order_modal_view(menu_items):
    """Builds the Slack modal for selecting a meal."""
    options = []
    for item in menu_items:
        # Limit pro délku textu v options je 75 znaků
        display_text = (item[:72] + '...') if len(item) > 75 else item
        options.append({
            "text": {"type": "plain_text", "text": display_text, "emoji": True},
            # 'value' může být delší, zde uchováváme plný název
            "value": item
        })

    view = {
        "type": "modal", "callback_id": "order_submission", "title": {"type": "plain_text", "text": "PepeEats", "emoji": True},
        "submit": {"type": "plain_text", "text": "Uložit", "emoji": True}, "close": {"type": "plain_text", "text": "Zrušit", "emoji": True},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Super! Co sis dnes objednal/a?"}},
            {"type": "input", "block_id": "meal_selection_block", "element": {"type": "static_select", "placeholder": {"type": "plain_text", "text": "Vyber jídlo", "emoji": True}, "options": options, "action_id": "meal_selection_action"}, "label": {"type": "plain_text", "text": "Tvoje volba", "emoji": True}}
        ]
    }
    return view

# --- FLASK ROUTES (ENDPOINTS) ---

# ... (cesty '/', '/open-lunchdrive', a 'before_request' zůstávají stejné) ...
@app.route('/')
def health_check():
    """A simple endpoint to confirm the server is running."""
    return "PepeEats is alive!", 200

@app.route("/open-lunchdrive")
def open_lunchdrive():
    fallback_play = "https://play.google.com/store/apps/details?id=cz.trueapps.lunchdrive&hl=en"
    fallback_ios = "https://apps.apple.com/cz/app/lunchdrive/id1496245341"
    package = "cz.trueapps.lunchdrive"
    ua = request.headers.get("User-Agent", "")
    app.logger.info("open-lunchdrive hit - UA: %s, IP: %s", ua, request.remote_addr)
    html = """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta charset="utf-8">
  <title>Otevřít LunchDrive…</title>
  <style>
    body{font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Oxygen,Ubuntu,Cantarell,'Open Sans','Helvetica Neue',sans-serif;margin:0;padding:1.5rem;text-align:center;background-color:#f5f5f5;}
    .container{max-width:400px;margin:2rem auto;background:#fff;padding:2rem;border-radius:12px;box-shadow:0 4px 12px rgba(0,0,0,0.1);}
    h3{font-size:1.5rem;margin-top:0;}
    p{color:#555;}
    .button{display:inline-block;padding:0.8rem 1.5rem;margin-top:1rem;background-color:#007aff;color:white;text-decoration:none;border-radius:8px;font-weight:600;}
    .note{font-size:0.9em;color:#888;margin-top:2rem;}
  </style>
  <script>
    (function(){
      var ua = navigator.userAgent || "";
      var isAndroid = /android/i.test(ua);
      var isIOS = /iphone|ipad|ipod/i.test(ua);
      var now = Date.now();
      var fallbackPlay = "{{ fallback_play }}";
      var fallbackIOS = "{{ fallback_ios }}";
      var packageName = "{{ package }}";
      var schemeUrl = "lunchdrive://open";
      var intentUrl = "intent://#Intent;package=" + packageName + ";S.browser_fallback_url=" + encodeURIComponent(fallbackPlay) + ";end";
      function openWithLocation(url){ try { window.location.href = url; } catch(e) {} }
      if (isAndroid) {
        var iframe = document.createElement('iframe');
        iframe.style.display = 'none';
        document.body.appendChild(iframe);
        try { iframe.src = schemeUrl; } catch(e){}
        setTimeout(function(){ openWithLocation(intentUrl); }, 700);
        setTimeout(function(){ if (Date.now() - now < 3500) { openWithLocation(fallbackPlay); } }, 2400);
      } else if (isIOS) {
        openWithLocation(schemeUrl);
        setTimeout(function(){ if (Date.now() - now < 2500) { openWithLocation(fallbackIOS); } }, 2000);
      } else {
        openWithLocation(fallbackPlay);
      }
    })();
  </script>
</head>
<body>
  <div class="container">
    <h3>Pokouším se otevřít aplikaci LunchDrive…</h3>
    <p>Pokud se aplikace neotevřela automaticky, pravděpodobně to blokuje interní prohlížeč Slacku.</p>
    <a href="{{ fallback_play }}" class="button">Otevřít manuálně v obchodě</a>
    <p class="note"><b>Tip:</b> Pro nejlepší funkčnost klikněte na tři tečky (⋮) vpravo nahoře a zvolte "Otevřít v systémovém prohlížeči".</p>
  </div>
</body>
</html>
"""
    return render_template_string(html, fallback_play=fallback_play, fallback_ios=fallback_ios, package=package, ua=ua)

@app.before_request
def verify_slack_request():
    """Verify that incoming requests from Slack are authentic."""
    # Kontrolujeme jen cesty, které skutečně přicházejí ze Slacku
    if request.path in ['/slack/interactive', '/slack/events']:
        verifier = SignatureVerifier(SLACK_SIGNING_SECRET)
        if not verifier.is_valid_request(request.get_data(), request.headers):
            app.logger.error("Invalid Slack signature")
            abort(403)

@app.route('/send-daily-reminder', methods=['POST'])
def trigger_daily_reminder():
    """Endpoint triggered by Cloud Scheduler to send the daily lunch menu."""
    app.logger.info("!!! DAILY REMINDER JOB STARTED !!!")
    today = date.today()
    
    # Připomínky se posílají v Ne, Po, Út, St, Čt
    if today.weekday() not in [0, 1, 2, 3, 6]:
        app.logger.info(f"Not a reminder day (Today is weekday {today.weekday()}). Job ending.")
        return ("Not a reminder day.", 200)

    # Vypočítáme den, pro který se objednává
    if today.weekday() == 4: # Pátek -> objednává se na Pondělí
        next_day = today + timedelta(days=3)
    else: # Jinak na další den
        next_day = today + timedelta(days=1)

    app.logger.info(f"Today is {today.strftime('%Y-%m-%d')}. Checking for menu and orders for {next_day.strftime('%Y-%m-%d')}.")
    
    menu_items = get_daily_menu(next_day)
    if isinstance(menu_items, str):
        app.logger.warning(f"Could not get menu: {menu_items}")
        # Můžeme poslat zprávu adminovi? Zatím logujeme.
        return (menu_items, 200)

    save_daily_menu(next_day, menu_items)

    all_users = get_all_subscribed_users() # Získáme {id: data}
    if not all_users:
        app.logger.info("No subscribed users to notify.")
        return ("No users.", 200)

    message_blocks = build_reminder_message_blocks(menu_items)
    users_reminded = 0
    
    for user_id, user_data in all_users.items():
        # <--- ZMĚNA: Přidána kontrola na snooze ---
        if not check_if_user_ordered_for_date(user_id, next_day) and not is_user_snoozed(user_data, next_day):
            app.logger.info(f"Sending reminder to {user_id} for {next_day.strftime('%Y-%m-%d')}")
            payload = {"channel": user_id, "blocks": message_blocks}
            send_slack_message(payload)
            users_reminded += 1
        else:
            if check_if_user_ordered_for_date(user_id, next_day):
                app.logger.info(f"Skipping user {user_id}, they have already ordered for {next_day.strftime('%Y-%m-%d')}.")
            else:
                app.logger.info(f"Skipping user {user_id}, they are snoozed for {next_day.strftime('%Y-%m-%d')}.")
                
    app.logger.info(f"--- Reminders sent to {users_reminded} users. Job finished. ---")
    return ("Reminders sent.", 200)

@app.route('/send-morning-reminder', methods=['POST'])
def trigger_morning_reminder():
    """Endpoint triggered by Cloud Scheduler to remind users what they ordered."""
    app.logger.info("--- Morning Reminder Job Started ---")
    today = date.today()
    if today.weekday() in [5, 6]: # Víkendy přeskočíme
        app.logger.info("Not a workday. Job ending.")
        return ("Not a workday.", 200)

    todays_orders = get_orders_for_date(today)
    if not todays_orders:
        app.logger.info("No orders found for today.")
        return ("No orders for today.", 200)

    for order in todays_orders:
        user_id = order.get('slack_user_id')
        meal = order.get('meal_description')
        if user_id and meal:
            app.logger.info(f"Sending morning reminder to {user_id}")
            message = f"Dobré ráno! 🐸 Jen připomínám, že dnes máš k obědu: *{meal}*"
            payload = {"channel": user_id, "text": message}
            send_slack_message(payload)

    app.logger.info(f"--- Morning reminders sent for {len(todays_orders)} orders. Job finished. ---")
    return ("Morning reminders sent.", 200)

@app.route('/slack/interactive', methods=['POST'])
def slack_interactive_endpoint():
    """Handles all interactive components from Slack (button clicks, modal submits)."""
    payload = json.loads(request.form.get("payload"))
    user_id = payload["user"]["id"]
    
    # Společná logika pro výpočet data objednávky
    today = date.today()
    order_for = today + timedelta(days=1)
    if today.weekday() == 4: # Pátek
        order_for = today + timedelta(days=3)

    # Zpracování odeslání modalu
    if payload["type"] == "view_submission" and payload["view"]["callback_id"] == "order_submission":
        app.logger.info(f"Received modal submission from {user_id}")
        submitted_values = payload["view"]["state"]["values"]
        meal_block = submitted_values["meal_selection_block"]
        action = meal_block["meal_selection_action"]
        selected_meal = action["selected_option"]["value"]
        
        save_user_order(user_id, selected_meal, order_for)
        confirmation_text = f"Díky! Uložil jsem, že na {order_for.strftime('%d.%m.')} máš objednáno: *{selected_meal}*"
        send_slack_message({"channel": user_id, "text": confirmation_text})
        return ("", 200)

    # Zpracování kliknutí na tlačítka
    if payload["type"] == "block_actions":
        action = payload["actions"][0]
        action_id = action.get("action_id")
        channel_id = payload["channel"]["id"] # <--- ZMĚNA

        if action_id == "open_order_modal":
            app.logger.info(f"User {user_id} clicked 'I've Ordered'. Opening modal.")
            trigger_id = payload["trigger_id"]
            
            menu_items = get_saved_menu_for_date(order_for)
            if menu_items is None:
                app.logger.error(f"Failed to retrieve menu from DB for {order_for}. Falling back to live scrape.")
                menu_items = get_daily_menu(order_for)
                if isinstance(menu_items, str):
                    send_slack_message({"channel": user_id, "text": "Omlouvám se, nepodařilo se mi načíst menu ani z databáze, ani z webu."})
                    return ("", 200)

            modal_view = build_order_modal_view(menu_items)
            try:
                requests.post("https://slack.com/api/views.open",
                    json={"trigger_id": trigger_id, "view": modal_view},
                    headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'}
                )
            except Exception as e:
                app.logger.error(f"Failed to post to Slack API: {e}", exc_info=True)

            return ("", 200)

        # <--- ZMĚNA: Logika pro nová tlačítka ---
        elif action_id == "snooze_today":
            app.logger.info(f"User {user_id} clicked 'Snooze'.")
            snooze_user_until(user_id, order_for)
            send_ephemeral_slack_message(channel_id, user_id, "OK, rozumím. Pro dnešek máš klid a nebudu posílat další připomínky. 🤫")
            return ("", 200)

        elif action_id == "home_office_tomorrow":
            app.logger.info(f"User {user_id} clicked 'Home Office'.")
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": "Chápu, že jsi na home office. Chceš i přesto objednat oběd pro někoho jiného?"}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Ano, objednám", "emoji": True}, "style": "primary", "action_id": "ho_order_for_other"},
                    {"type": "button", "text": {"type": "plain_text", "text": "Ne, přeskočit", "emoji": True}, "action_id": "ho_skip_ordering"}
                ]}
            ]
            send_ephemeral_slack_message(channel_id, user_id, "Objednávka pro někoho jiného?", blocks)
            return ("", 200)
        
        elif action_id == "ho_order_for_other":
            # Chová se stejně jako "Mám objednáno" - otevře modal
            app.logger.info(f"User {user_id} chose 'HO - Order for other'.")
            trigger_id = payload["trigger_id"]
            menu_items = get_saved_menu_for_date(order_for)
            if menu_items is None: menu_items = get_daily_menu(order_for)

            if isinstance(menu_items, str):
                send_ephemeral_slack_message(channel_id, user_id, "Chyba: Nepodařilo se načíst menu pro zobrazení formuláře.")
                return ("", 200)

            modal_view = build_order_modal_view(menu_items)
            requests.post("https://slack.com/api/views.open", json={"trigger_id": trigger_id, "view": modal_view}, headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'})
            return ("", 200)
            
        elif action_id == "ho_skip_ordering":
            app.logger.info(f"User {user_id} chose 'HO - Skip'.")
            snooze_user_until(user_id, order_for)
            send_ephemeral_slack_message(channel_id, user_id, "Jasně, pro zítřek tě přeskočím. Užij si home office! 💻")
            return ("", 200)

        elif action_id == "unsubscribe":
            app.logger.info(f"User {user_id} clicked 'Unsubscribe'.")
            remove_user(user_id)
            confirmation_text = "Je mi to líto, ale zrušil jsem ti odběr. Kdyby sis to rozmyslel, stačí se znovu přihlásit. 🐸"
            send_slack_message({"channel": user_id, "text": confirmation_text})
            return ("", 200)

    return ("Unhandled interaction", 200)

# ... (cesty '/subscribe', '/slack/oauth/callback', '/admin' a 'if __name__ == "__main__"' zůstávají stejné) ...
@app.route('/subscribe', methods=['GET'])
def subscribe():
    """Renders the subscription page with the 'Add to Slack' button."""
    params = {
        'client_id': SLACK_CLIENT_ID,
        'scope': 'chat:write,users:read', # Scope pro psaní zpráv a čtení info o uživateli
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

    response = requests.post("https://slack.com/api/oauth.v2.access", data={
        'client_id': SLACK_CLIENT_ID,
        'client_secret': SLACK_CLIENT_SECRET,
        'code': code,
        'redirect_uri': f"{BASE_URL}/slack/oauth/callback"
    })
    data = response.json()

    if not data.get('ok'):
        return (f"OAuth Error: {data.get('error')}", 400)

    user_id = data.get('authed_user', {}).get('id')
    if user_id:
        app.logger.info(f"New user subscribed: {user_id}")
        add_user(user_id)
        welcome_text = "Vítej v PepeEats! 🎉 Od teď ti budu posílat denní připomínky na oběd."
        send_slack_message({"channel": user_id, "text": welcome_text})
        return "<h1>Success!</h1><p>You have been subscribed to PepeEats. You can close this window now.</p>"
    
    return ("OAuth failed: Could not get user ID.", 500)


@app.route('/admin', methods=['GET'])
def admin_panel():
    """Displays the admin dashboard with subscribers and today's orders."""
    secret = request.args.get('secret')
    if secret != ADMIN_SECRET_KEY:
        abort(403) # Forbidden
    
    users_docs = db.collection('users').stream()
    users_list = [{'id': doc.id} for doc in users_docs]
    
    today = date.today()
    orders_list = get_orders_for_date(today)
    
    return render_template('admin.html', users=users_list, orders=orders_list, today_str=today.strftime('%Y-%m-%d'))


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    
    SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
    SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
    SLACK_CLIENT_ID = os.environ.get("SLACK_CLIENT_ID")
    SLACK_CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET")
    BASE_URL = os.environ.get("BASE_URL")
    ADMIN_SECRET_KEY = os.environ.get("ADMIN_SECRET_KEY")

    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
