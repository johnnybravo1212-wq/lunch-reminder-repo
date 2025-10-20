import os
import json
import requests
import random
import logging
import re
from datetime import datetime, timedelta, date
from urllib.parse import urlencode
import calendar

from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template, render_template_string, abort, redirect, url_for

import firebase_admin
from firebase_admin import credentials, firestore, auth
from slack_sdk.signature import SignatureVerifier

# --- INITIALIZATION ---
app = Flask(__name__)
app.logger.setLevel(logging.INFO)
try:
    firebase_admin.initialize_app()
except Exception as e:
    app.logger.warning(f"Firebase already initialized or failed: {e}")
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

# --- EMOJIS & IMAGES ---
URGENT_EMOJIS = ["🚨", "🔥", "⏰", "🍔", "🏃‍♂️", "💨", "‼️", "🐸"]
PEPE_IMAGES = ["https://i.imgur.com/XoF6m62.png", "https://i.imgur.com/sBq2pPT.png", "https://i.imgur.com/2OFa0s8.png"]

# --- DATABASE & HELPER FUNCTIONS ---

def get_user_settings(user_email):
    default = {'notification_frequency': 'daily', 'is_test_user': False}
    if not user_email: return default
    doc = db.collection('user_settings').document(user_email).get()
    if not doc.exists: return default
    settings = doc.to_dict()
    settings.setdefault('notification_frequency', 'daily')
    settings.setdefault('is_test_user', False)
    return settings

def save_user_settings(user_email, settings_data):
    if not user_email: return
    db.collection('user_settings').document(user_email).set(settings_data, merge=True)

def get_all_users_with_settings():
    users_with_settings = {}
    users_ref = db.collection('users').stream()
    for user in users_ref:
        user_data = user.to_dict()
        user_email = user_data.get('google_email')
        users_with_settings[user.id] = { 'user_data': user_data, 'settings': get_user_settings(user_email) }
    return users_with_settings

def save_user_order(ordered_by_id, meal_choice, order_for_date, ordered_for_id):
    order_data = {'ordered_by_user_id': ordered_by_id, 'meal_description': meal_choice, 'ordered_for_user_id': ordered_for_id, 'order_for_date': order_for_date.strftime("%Y-%m-%d"), 'placed_on_date': date.today().strftime("%Y-%m-%d"), 'price': 125 }
    doc_id = f"{ordered_by_id}_{ordered_for_id}_{order_for_date.strftime('%Y-%m-%d')}"
    db.collection('orders').document(doc_id).set(order_data)
    db.collection('users').document(ordered_by_id).set({'snoozed_until': None}, merge=True)

def check_if_user_ordered_for_date(user_id, target_date):
    query = db.collection('orders').where('order_for_date', '==', target_date.strftime("%Y-%m-%d")).where('ordered_by_user_id', '==', user_id)
    return len(list(query.stream())) > 0

def is_user_snoozed(user_data, check_date):
    snoozed_until_str = user_data.get('snoozed_until')
    if not snoozed_until_str: return False
    try: return datetime.strptime(snoozed_until_str, "%Y-%m-%d").date() >= check_date
    except (ValueError, TypeError): return False

def save_daily_menu(menu_date, menu_items):
    db.collection('daily_menus').document(menu_date.strftime("%Y-%m-%d")).set({'menu_items': menu_items}, merge=True)

def get_saved_menu_for_date(target_date):
    menu_doc = db.collection('daily_menus').document(target_date.strftime("%Y-%m-%d")).get()
    return menu_doc.to_dict().get('menu_items', []) if menu_doc.exists else None

def get_daily_menu(target_date):
    try:
        response = requests.get(LUNCHDRIVE_URL, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'lxml')
        target_date_string = target_date.strftime("%-d.%-m.%Y")
        menu_header = soup.find('h2', string=lambda text: text and target_date_string in text)
        if not menu_header: return f"Menu na {target_date.strftime('%d.%m.')} ještě není k dispozici. 🙁"
        menu_table = menu_header.find_next_sibling('table', class_='table-menu')
        if not menu_table: return "Chyba: Tabulka s menu nebyla nalezena."
        menu_items = [cols[2].get_text(strip=True) for row in menu_table.find_all('tr') if len(cols := row.find_all('td')) == 4 and (match := re.search(r'\d+', cols[3].get_text(strip=True))) and int(match.group(0)) == TARGET_PRICE]
        if not menu_items: return f"Na {target_date.strftime('%d.%m.')} bohužel není v nabídce žádné jídlo za {TARGET_PRICE} Kč."
        return menu_items
    except Exception as e:
        app.logger.error(f"CRITICAL ERROR in get_daily_menu: {e}", exc_info=True)
        return "Došlo k závažné chybě při stahování menu."

def calculate_workdays(year, month):
    return sum(1 for day in range(1, calendar.monthrange(year, month)[1] + 1) if date(year, month, day).weekday() < 5)

def get_user_monthly_spending(user_id, year, month):
    start = f"{year}-{month:02d}-01"
    end = f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]}"
    orders_ref = db.collection('orders').where('ordered_by_user_id', '==', user_id).where('order_for_date', '>=', start).where('order_for_date', '<=', end)
    orders = list(orders_ref.stream())
    total_spent = sum(order.to_dict().get('price', 125) for order in orders)
    return total_spent, len(orders)

# --- SLACK API & MESSAGE BUILDING ---

def send_slack_message(payload):
    try: requests.post("https://slack.com/api/chat.postMessage", json=payload, headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'}).raise_for_status()
    except Exception as e: app.logger.error(f"Error in send_slack_message: {e}", exc_info=True)

def send_ephemeral_slack_message(channel_id, user_id, text, blocks=None):
    payload = {"channel": channel_id, "user": user_id, "text": text}
    if blocks: payload["blocks"] = blocks
    try: requests.post("https://slack.com/api/chat.postEphemeral", json=payload, headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'}).raise_for_status()
    except Exception as e: app.logger.error(f"Error in send_ephemeral_slack_message: {e}", exc_info=True)

def build_reminder_message_blocks(menu_items):
    menu_text = "\n".join([f"• {item}" for item in menu_items])
    settings_url = f"{BASE_URL}/settings"
    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"{random.choice(URGENT_EMOJIS)} PepeEats: Objednej oběd NA ZÍTRA!", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Zítřejší nabídka za {TARGET_PRICE} Kč:*"}},
        {"type": "divider"}, {"type": "section", "text": {"type": "mrkdwn", "text": menu_text}},
        {"type": "image", "image_url": random.choice(PEPE_IMAGES), "alt_text": "A wild Pepe appears"},
        {"type": "divider"},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ Mám objednáno"}, "style": "primary", "action_id": "open_order_modal"},
            {"type": "button", "text": {"type": "plain_text", "text": "💰 Kolik zbývá?"}, "action_id": "check_balance"},
            {"type": "button", "text": {"type": "plain_text", "text": "Chybí ti funkce?"}, "action_id": "open_feedback_modal"}
        ]},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"🕒 Nevyhovuje čas? <{settings_url}|Změň si nastavení>"}
        ]}
    ]

def build_order_modal_view(menu_items):
    menu_options = [{"text": {"type": "plain_text", "text": (item[:72] + '...') if len(item) > 75 else item}, "value": item} for item in menu_items]
    return {"type": "modal", "callback_id": "order_submission", "title": {"type": "plain_text", "text": "PepeEats"},
            "submit": {"type": "plain_text", "text": "Uložit"}, "close": {"type": "plain_text", "text": "Zrušit"},
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Super! Co a pro koho sis dnes objednal/a?"}},
                       {"type": "input", "block_id": "meal_selection_block", "element": {"type": "static_select", "placeholder": {"type": "plain_text", "text": "Vyber jídlo"}, "options": menu_options, "action_id": "meal_select_action"}, "label": {"type": "plain_text", "text": "Tvoje volba"}},
                       {"type": "input", "block_id": "person_selection_block", "element": {"type": "users_select", "placeholder": {"type": "plain_text", "text": "Vyber kolegu"}, "action_id": "person_select_action"}, "label": {"type": "plain_text", "text": "Objednávka je pro:"}}]}

# --- FLASK ROUTES ---

def verify_firebase_token(request):
    try: return auth.verify_id_token(request.cookies.get('session_token'))
    except Exception: return None

@app.before_request
def verify_slack_request():
    if request.path in ['/slack/interactive']:
        verifier = SignatureVerifier(SLACK_SIGNING_SECRET)
        if not verifier.is_valid_request(request.get_data(), request.headers): abort(403)

@app.route('/')
def health_check(): return "PepeEats is alive!", 200

@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    user = verify_firebase_token(request)
    if not user: return redirect(url_for('login_page'))
    user_email = user.get('email', '')
    if not (user_email.endswith('@rohlik.cz') or user_email == 'johnnybravo1212@gmail.com'):
        return redirect(url_for('unauthorized_page'))
    
    try:
        slack_res = requests.get("https://slack.com/api/users.lookupByEmail", headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'}, params={'email': user_email})
        slack_res.raise_for_status()
        if (info := slack_res.json()).get('ok'):
            db.collection('users').document(info['user']['id']).set({'google_email': user_email}, merge=True)
    except Exception as e:
        app.logger.error(f"Failed to link account for {user_email}: {e}")

    if request.method == 'POST':
        settings_data = {
            'notification_frequency': request.form.get('notification_frequency'),
            'is_test_user': 'is_test_user' in request.form
        }
        save_user_settings(user_email, settings_data)
        return redirect(url_for('settings_page') + '?saved=true')

    return render_template('settings.html', user=user, settings=get_user_settings(user_email))

@app.route('/unauthorized')
def unauthorized_page(): return render_template('unauthorized.html'), 403

@app.route('/login')
def login_page(): return render_template('login.html')

@app.route('/logout')
def logout():
    response = redirect(url_for('login_page'))
    response.set_cookie('session_token', '', expires=0, path='/')
    return response

@app.route('/send-daily-reminder', methods=['POST'])
def trigger_daily_reminder():
    app.logger.info("!!! DYNAMIC REMINDER JOB STARTED !!!")
    current_hour_prague = (datetime.utcnow().hour + 2) % 24
    
    today = date.today()
    if today.weekday() not in [0, 1, 2, 3, 6]: return "Not a reminder day (Fri/Sat).", 200

    next_day = today + timedelta(days=3) if today.weekday() == 4 else today + timedelta(days=1)
    
    menu_items = get_saved_menu_for_date(next_day) or get_daily_menu(next_day)
    if isinstance(menu_items, str):
        app.logger.error(f"Could not get menu: {menu_items}")
        return menu_items, 500
    save_daily_menu(next_day, menu_items)
    
    all_users = get_all_users_with_settings()
    if not all_users: return "No users found.", 200

    message_blocks = build_reminder_message_blocks(menu_items)
    users_reminded = 0

    for user_id, data in all_users.items():
        user_data, settings = data['user_data'], data['settings']
        
        if check_if_user_ordered_for_date(user_id, next_day) or is_user_snoozed(user_data, next_day): continue

        send_now = False
        if settings.get('is_test_user'):
            send_now = True
            app.logger.info(f"Sending to TEST USER {user_id} because test mode is ON.")
        else:
            freq = settings.get('notification_frequency', 'daily')
            if 9 <= current_hour_prague < 17:
                if freq == '2' and (current_hour_prague - 9) % 2 == 0: send_now = True
                elif freq == '4' and (current_hour_prague - 9) % 4 == 0: send_now = True
                elif freq == 'daily' and 11 <= current_hour_prague < 13: send_now = True

        if send_now:
            send_slack_message({"channel": user_id, "blocks": message_blocks})
            users_reminded += 1
            
    app.logger.info(f"Job finished. Dynamic reminders sent to {users_reminded} users.")
    return f"Dynamic reminders sent to {users_reminded} users.", 200

@app.route('/slack/interactive', methods=['POST'])
def slack_interactive_endpoint():
    payload = json.loads(request.form.get("payload"))
    user_id = payload["user"]["id"]
    channel_id = payload["channel"]["id"]
    trigger_id = payload.get("trigger_id")
    today = date.today()
    order_for = today + timedelta(days=3) if today.weekday() == 4 else today + timedelta(days=1)

    if payload["type"] == "view_submission" and payload["view"]["callback_id"] == "feedback_submission":
        feedback_text = payload["view"]["state"]["values"]["feedback_block"]["feedback_input"]["value"]
        db.collection("feedback").add({ "text": feedback_text, "user_id": user_id, "submitted_at": firestore.SERVER_TIMESTAMP })
        send_ephemeral_slack_message(channel_id, user_id, "Díky za zpětnou vazbu! Uložil jsem si to. 🐸")
        return ("", 200)

    if payload["type"] == "view_submission" and payload["view"]["callback_id"] == "order_submission":
        values = payload["view"]["state"]["values"]
        selected_meal = values["meal_selection_block"]["meal_select_action"]["selected_option"]["value"]
        selected_user_id = values["person_selection_block"]["person_select_action"]["selected_user"]
        
        save_user_order(user_id, selected_meal, order_for, selected_user_id)
        
        send_slack_message({"channel": user_id, "text": f"Díky! Uložil jsem, že na {order_for.strftime('%d.%m.')} máš pro <@{selected_user_id}> objednáno: *{selected_meal}*"})
        
        if user_id != selected_user_id:
             send_slack_message({"channel": selected_user_id, "text": f"Ahoj! Jen abys věděl/a, <@{user_id}> ti právě objednal/a na zítra k obědu: *{selected_meal}*"})
        
        return ("", 200)

    if payload["type"] == "block_actions":
        action_id = payload["actions"][0].get("action_id")

        if action_id == "check_balance":
            year, month = today.year, today.month
            workdays = calculate_workdays(year, month)
            total_budget = workdays * 125
            spent, count = get_user_monthly_spending(user_id, year, month)
            text = (f"*Finanční přehled pro tento měsíc:*\n"
                    f"• Měsíční rozpočet: *{total_budget} Kč* ({workdays} prac. dní)\n"
                    f"• Objednáno: *{count} jídel*\n"
                    f"• Utraceno: *{spent} Kč*\n"
                    f"• Zbývá: *{total_budget - spent} Kč*")
            send_ephemeral_slack_message(channel_id, user_id, text)
            return ("", 200)

        if action_id == "open_feedback_modal":
            feedback_modal = { "type": "modal", "callback_id": "feedback_submission", "title": {"type": "plain_text", "text": "Zpětná vazba pro PepeEats"}, "submit": {"type": "plain_text", "text": "Odeslat"},
                "blocks": [{"type": "input", "block_id": "feedback_block", "label": {"type": "plain_text", "text": "Co bys vylepšil/a nebo přidal/a?"},
                            "element": {"type": "plain_text_input", "action_id": "feedback_input", "multiline": True}}]}
            requests.post("https://slack.com/api/views.open", json={"trigger_id": trigger_id, "view": feedback_modal}, headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'})
            return ("", 200)

        if action_id in ["open_order_modal", "ho_order_for_other"]:
            menu = get_saved_menu_for_date(order_for) or get_daily_menu(order_for)
            if isinstance(menu, str): send_ephemeral_slack_message(channel_id, user_id, "Chyba: Nepodařilo se načíst menu.")
            else: requests.post("https://slack.com/api/views.open", json={"trigger_id": trigger_id, "view": build_order_modal_view(menu)}, headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'})
        
        elif action_id in ["snooze_today", "ho_skip_ordering"]:
            db.collection('users').document(user_id).set({'snoozed_until': order_for.strftime("%Y-%m-%d")}, merge=True)
            msg = "OK, pro dnešek máš klid. 🤫" if action_id == "snooze_today" else "Jasně, pro zítřek tě přeskočím. Užij si home office! 💻"
            send_ephemeral_slack_message(channel_id, user_id, msg)
        
        elif action_id == "home_office_tomorrow":
            blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "Chceš i přesto objednat oběd pro někoho jiného?"}},
                      {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Ano, objednám"}, "style": "primary", "action_id": "ho_order_for_other"},
                                                       {"type": "button", "text": {"type": "plain_text", "text": "Ne, přeskočit"}, "action_id": "ho_skip_ordering"}]}]
            send_ephemeral_slack_message(channel_id, user_id, "Objednávka pro někoho jiného?", blocks)
        
        elif action_id == "unsubscribe":
            db.collection('users').document(user_id).delete()
            send_slack_message({"channel": user_id, "text": "Je mi to líto, ale zrušil jsem ti odběr. 🐸"})
        
        return ("", 200)

    return ("Unhandled interaction", 200)

# --- ZMĚNA: Přidána ochrana před neoprávněným přístupem ---
@app.route('/subscribe', methods=['GET'])
def subscribe():
    user = verify_firebase_token(request)
    if not user:
        return redirect(url_for('login_page', next=url_for('subscribe')))

    user_email = user.get('email', '')
    if not (user_email.endswith('@rohlik.cz') or user_email == 'johnnybravo1212@gmail.com'):
        return redirect(url_for('unauthorized_page'))

    params = {'client_id': SLACK_CLIENT_ID, 'scope': 'chat:write,users:read,users:read.email', 'redirect_uri': f"{BASE_URL}/slack/oauth/callback"}
    slack_auth_url = f"https://slack.com/oauth/v2/authorize?{urlencode(params)}"
    return render_template('subscribe.html', slack_auth_url=slack_auth_url)

@app.route('/slack/oauth/callback', methods=['GET'])
def oauth_callback():
    code = request.args.get('code')
    if not code: return "OAuth failed: No code provided.", 400
    response = requests.post("https://slack.com/api/oauth.v2.access", data={'client_id': SLACK_CLIENT_ID, 'client_secret': SLACK_CLIENT_SECRET, 'code': code, 'redirect_uri': f"{BASE_URL}/slack/oauth/callback"})
    data = response.json()
    if not data.get('ok'): return f"OAuth Error: {data.get('error')}", 400
    user_id = data.get('authed_user', {}).get('id')
    if user_id:
        db.collection('users').document(user_id).set({'subscribed_at': firestore.SERVER_TIMESTAMP}, merge=True)
        send_slack_message({"channel": user_id, "text": "Vítej v PepeEats! 🎉 Od teď ti budu posílat denní připomínky na oběd."})
        return "<h1>Success!</h1><p>You have been subscribed. You can close this window.</p>"
    return "OAuth failed: Could not get user ID.", 500

@app.route("/open-lunchdrive")
def open_lunchdrive():
    html = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><meta charset="utf-8"><title>Otevřít LunchDrive…</title><style>body{font-family:system-ui,sans-serif;margin:0;padding:1.5rem;text-align:center;background-color:#f5f5f5;}.container{max-width:400px;margin:2rem auto;background:#fff;padding:2rem;border-radius:12px;box-shadow:0 4px 12px rgba(0,0,0,0.1);}h3{font-size:1.5rem;margin-top:0;}p{color:#555;}.button{display:inline-block;padding:0.8rem 1.5rem;margin-top:1rem;background-color:#007aff;color:white;text-decoration:none;border-radius:8px;font-weight:600;}.note{font-size:0.9em;color:#888;margin-top:2rem;}</style><script>(function(){var u=navigator.userAgent||"",i=/android/i.test(u),o=/iphone|ipad|ipod/i.test(u),n=Date.now(),d="https://play.google.com/store/apps/details?id=cz.trueapps.lunchdrive&hl=en",a="https://apps.apple.com/cz/app/lunchdrive/id1496245341",p="cz.trueapps.lunchdrive",c="lunchdrive://open",l="intent://#Intent;package="+p+";S.browser_fallback_url="+encodeURIComponent(d)+";end";function r(e){try{window.location.href=e}catch(t){}}if(i){var t=document.createElement("iframe");t.style.display="none",document.body.appendChild(t);try{t.src=c}catch(e){}setTimeout(function(){r(l)},700),setTimeout(function(){Date.now()-n<3500&&r(d)},2400)}else o?(r(c),setTimeout(function(){Date.now()-n<2500&&r(a)},2000)):r(d)})();</script></head><body><div class="container"><h3>Pokouším se otevřít aplikaci LunchDrive…</h3><p>Pokud se aplikace neotevřela automaticky, pravděpodobně to blokuje interní prohlížeč Slacku.</p><a href="https://play.google.com/store/apps/details?id=cz.trueapps.lunchdrive&hl=en" class="button">Otevřít manuálně v obchodě</a><p class="note"><b>Tip:</b> Pro nejlepší funkčnost klikněte na tři tečky (⋮) vpravo nahoře a zvolte "Otevřít v systémovém prohlížeči".</p></div></body></html>"""
    return render_template_string(html)

@app.route('/admin', methods=['GET'])
def admin_panel():
    if request.args.get('secret') != ADMIN_SECRET_KEY: abort(403)
    users_docs = db.collection('users').stream()
    users_list = [{'id': doc.id, **doc.to_dict()} for doc in users_docs]
    today = date.today()
    orders_ref = db.collection('orders').where('order_for_date', '==', today.strftime("%Y-%m-%d"))
    orders_list = [doc.to_dict() for doc in orders_ref.stream()]
    feedback_ref = db.collection('feedback').order_by('submitted_at', direction=firestore.Query.DESCENDING).limit(20).stream()
    feedback_list = [doc.to_dict() for doc in feedback_ref]
    return render_template('admin.html', users=users_list, orders=orders_list, feedback=feedback_list, today_str=today.strftime('%Y-%m-%d'))


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
