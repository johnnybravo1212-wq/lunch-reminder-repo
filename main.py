import os
import json
import requests
import random
import logging
import re
import time
from datetime import datetime, timedelta, date
from urllib.parse import urlencode
import calendar

# --- ZMĚNA: Nový import pro svátky ---
import holidays

from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template, render_template_string, abort, redirect, url_for
from duckduckgo_search import DDGS
from googletrans import Translator
import google.generativeai as genai

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
OWNER_SLACK_ID = os.environ.get("OWNER_SLACK_ID")  # Your Slack user ID for feedback notifications
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID")  # Google Custom Search Engine ID
GOOGLE_CSE_API_KEY = os.environ.get("GOOGLE_CSE_API_KEY")  # Google API Key
UNSPLASH_ACCESS_KEY = os.environ.get("UNSPLASH_ACCESS_KEY")  # Unsplash Access Key
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")  # Google Gemini API Key
IMAGE_SEARCH_PROVIDER = os.environ.get("IMAGE_SEARCH_PROVIDER", "google").lower()  # unsplash, google, or duckduckgo
USE_AI_VALIDATION = os.environ.get("USE_AI_VALIDATION", "false").lower() == "true"  # Use Gemini to validate images
ENABLE_IMAGES = os.environ.get("ENABLE_IMAGES", "false").lower() == "true"  # Enable/disable images completely

# Initialize Google Translator
translator = Translator()

# Initialize Gemini AI (optional)
gemini_model = None
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-1.5-flash')
        app.logger.info("Gemini AI initialized successfully")
    except Exception as e:
        app.logger.warning(f"Failed to initialize Gemini: {e}")

# --- ZMĚNA: Vytvoření instance českých svátků ---
cz_holidays = holidays.CZ()

# --- EMOJIS & IMAGES ---
URGENT_EMOJIS = ["🚨", "🔥", "⏰", "🍔", "🏃‍♂️", "💨", "‼️", "🐸"]
PEPE_IMAGES = ["https://i.imgur.com/XoF6m62.png", "https://i.imgur.com/sBq2pPT.png", "https://i.imgur.com/2OFa0s8.png"]

# --- RATING REACTIONS ---
RATING_REACTIONS = {
    'high': [  # 80-100%
        "Parádní volba! Minule {rating}% 🌟",
        "Tohle ti chutnalo! ({rating}%) 😋",
        "Tvůj favorit! Minule {rating}% ⭐",
        "Safe bet! Minule {rating}% 👍"
    ],
    'medium': [  # 50-79%
        "Bylo to OK ({rating}%). Zkusíš znovu? 🤔",
        "Ujde to... Minule {rating}% 😐",
        "Průměr ({rating}%). Možná tentokrát bude lepší? 🤷",
    ],
    'low': [  # 0-49%
        "Minule jen {rating}%... Jsi si jistý/á? 😬",
        "Tak tohle ti nechutnalo ({rating}%) 😅",
        "Odvážná volba! Minule {rating}% 🫣",
        "Dáš tomu druhou šanci? Minule {rating}% 🙈"
    ],
    'multiple': [
        "Objednáváš to už {count}x! 🔥",
        "{count}x objednáno. Fanda? 😄",
        "Toto máš rád/a! ({count}x) ❤️"
    ]
}

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
    order_data = {
        'ordered_by_user_id': ordered_by_id,
        'meal_description': meal_choice,
        'ordered_for_user_id': ordered_for_id,
        'order_for_date': order_for_date.strftime("%Y-%m-%d"),
        'placed_on_date': date.today().strftime("%Y-%m-%d"),
        'price': 125,
        'rating': None,
        'rated_at': None
    }
    doc_id = f"{ordered_by_id}_{ordered_for_id}_{order_for_date.strftime('%Y-%m-%d')}"
    db.collection('orders').document(doc_id).set(order_data)
    db.collection('users').document(ordered_by_id).set({'snoozed_until': None}, merge=True)

def check_if_user_ordered_for_date(user_id, target_date):
    query = db.collection('orders').where('order_for_date', '==', target_date.strftime("%Y-%m-%d")).where('ordered_by_user_id', '==', user_id)
    return len(list(query.stream())) > 0

def get_user_dish_history(user_id, dish_name):
    """Get user's previous orders and ratings for a specific dish"""
    try:
        # Normalize dish name for comparison
        normalized_dish = dish_name.strip().lower()

        orders_ref = db.collection('orders').where('ordered_for_user_id', '==', user_id).stream()

        matching_orders = []
        for order in orders_ref:
            order_data = order.to_dict()
            meal_desc = order_data.get('meal_description', '').strip().lower()

            if meal_desc == normalized_dish:
                matching_orders.append({
                    'date': order_data.get('order_for_date'),
                    'rating': order_data.get('rating'),
                    'meal_description': order_data.get('meal_description')
                })

        return matching_orders
    except Exception as e:
        app.logger.error(f"Error getting dish history for {user_id}: {e}")
        return []

def save_rating(user_id, order_date, rating_value):
    """Save rating for user's order on a specific date"""
    try:
        # Find the order for this user and date
        query = db.collection('orders').where('ordered_for_user_id', '==', user_id).where('order_for_date', '==', order_date.strftime("%Y-%m-%d"))
        orders = list(query.stream())

        if orders:
            order_doc = orders[0]
            order_doc.reference.update({
                'rating': rating_value,
                'rated_at': firestore.SERVER_TIMESTAMP
            })
            app.logger.info(f"Saved rating {rating_value} for user {user_id} on {order_date}")
            return True
        else:
            app.logger.warning(f"No order found for user {user_id} on {order_date}")
            return False
    except Exception as e:
        app.logger.error(f"Error saving rating: {e}")
        return False

def get_orders_needing_ratings(target_date):
    """Get all orders for a specific date that haven't been rated yet"""
    try:
        # Get all orders for target date
        query = db.collection('orders').where('order_for_date', '==', target_date.strftime("%Y-%m-%d"))
        orders = list(query.stream())

        # Filter for unrated orders (rating is None, empty string, or missing)
        unrated = [order for order in orders if not order.to_dict().get('rating')]

        app.logger.info(f"[DEBUG] Found {len(orders)} total orders for {target_date}, {len(unrated)} need ratings")
        return unrated
    except Exception as e:
        app.logger.error(f"Error getting orders needing ratings: {e}")
        return []

def generate_dish_comment(dish_history):
    """Generate funny comment based on user's history with this dish"""
    if not dish_history:
        return None

    order_count = len(dish_history)
    rated_orders = [o for o in dish_history if o.get('rating') is not None]

    # If ordered multiple times, prioritize that
    if order_count >= 3:
        return random.choice(RATING_REACTIONS['multiple']).format(count=order_count)

    # If has rating from last time
    if rated_orders:
        last_rating = rated_orders[-1].get('rating')

        if last_rating >= 80:
            return random.choice(RATING_REACTIONS['high']).format(rating=last_rating)
        elif last_rating >= 50:
            return random.choice(RATING_REACTIONS['medium']).format(rating=last_rating)
        else:
            return random.choice(RATING_REACTIONS['low']).format(rating=last_rating)

    # Ordered before but never rated
    if order_count >= 2:
        return random.choice(RATING_REACTIONS['multiple']).format(count=order_count)

    return None

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
        app.logger.info(f"[DEBUG] Fetching menu for {target_date.strftime('%Y-%m-%d')}")
        response = requests.get(LUNCHDRIVE_URL, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'lxml')
        target_date_string = target_date.strftime("%-d.%-m.%Y")
        menu_header = soup.find('h2', string=lambda text: text and target_date_string in text)
        if not menu_header: return f"Menu na {target_date.strftime('%d.%m.')} ještě není k dispozici. 🙁"
        menu_table = menu_header.find_next_sibling('table', class_='table-menu')
        if not menu_table: return "Chyba: Tabulka s menu nebyla nalezena."

        # Get dish names
        dish_names = [cols[2].get_text(strip=True) for row in menu_table.find_all('tr') if len(cols := row.find_all('td')) == 4 and (match := re.search(r'\d+', cols[3].get_text(strip=True))) and int(match.group(0)) == TARGET_PRICE]
        if not dish_names: return f"Na {target_date.strftime('%d.%m.')} bohužel není v nabídce žádné jídlo za {TARGET_PRICE} Kč."

        app.logger.info(f"[DEBUG] Found {len(dish_names)} dishes: {dish_names}")

        # Fetch images for each dish (if enabled)
        menu_items = []
        for dish_name in dish_names:
            if ENABLE_IMAGES:
                app.logger.info(f"[DEBUG] Searching image for: {dish_name}")
                image_url = get_or_cache_dish_image(dish_name)
                app.logger.info(f"[DEBUG] Image URL result: {image_url}")
            else:
                image_url = None

            menu_items.append({
                'name': dish_name,
                'image_url': image_url
            })

        app.logger.info(f"[DEBUG] Final menu_items: {menu_items}")
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

def get_slack_id_from_email(email):
    try:
        slack_res = requests.get("https://slack.com/api/users.lookupByEmail", headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'}, params={'email': email})
        slack_res.raise_for_status()
        if (info := slack_res.json()).get('ok'):
            return info['user']['id']
    except Exception as e:
        app.logger.error(f"Failed to lookup user by email {email}: {e}")
    return None

def clean_dish_name_for_search(dish_name):
    """Clean dish name: remove allergens, grams, and other noise"""
    # Remove allergen numbers in parentheses: (1 3 4 7 10)
    cleaned = re.sub(r'\([0-9\s]+\)', '', dish_name)
    # Remove grams: 250g, 250 g, etc.
    cleaned = re.sub(r'\d+\s*g\b', '', cleaned)
    # Remove ml: 250ml, 250 ml, etc.
    cleaned = re.sub(r'\d+\s*ml\b', '', cleaned)
    # Remove extra whitespace
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def validate_image_with_ai(image_url, dish_name):
    """Use Gemini Vision to validate if image matches the dish"""
    if not USE_AI_VALIDATION or not gemini_model:
        return True  # Skip validation if disabled

    try:
        # Download image to validate
        response = requests.get(image_url, timeout=5)
        if response.status_code != 200:
            return False

        # Prepare prompt for Gemini
        prompt = f"""Look at this image. Is this a realistic photo of "{dish_name}" food/meal?

Answer ONLY with a JSON object:
{{
  "is_match": true/false,
  "is_realistic": true/false,
  "confidence": 0-100,
  "reason": "brief explanation"
}}

Criteria:
- is_match: Does the image show {dish_name}?
- is_realistic: Is it a realistic photo (not artistic/illustration/menu)?
- confidence: How confident are you (0-100)?

Answer:"""

        # Call Gemini Vision
        image_data = response.content
        result = gemini_model.generate_content([prompt, {"mime_type": "image/jpeg", "data": image_data}])

        # Parse response
        response_text = result.text.strip()
        app.logger.info(f"[DEBUG] Gemini validation for '{dish_name}': {response_text}")

        # Try to parse JSON response
        import json
        try:
            # Extract JSON from response (might have markdown code blocks)
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0].strip()
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0].strip()

            validation = json.loads(response_text)

            # Accept image if it's realistic and somewhat matches
            is_acceptable = validation.get('is_realistic', False) and validation.get('confidence', 0) > 40

            app.logger.info(f"[DEBUG] AI validation result: {is_acceptable} (confidence: {validation.get('confidence')}%)")
            return is_acceptable

        except:
            # If parsing fails, accept the image (fallback)
            app.logger.warning(f"Failed to parse Gemini response, accepting image by default")
            return True

    except Exception as e:
        app.logger.error(f"AI validation failed for '{dish_name}': {e}")
        return True  # Accept on error (fallback)

def translate_to_english(text):
    """Translate Czech text to English using Google Translate"""
    try:
        # Detect if already English (skip translation)
        detection = translator.detect(text)
        if detection.lang == 'en':
            app.logger.info(f"[DEBUG] Text already in English: '{text}'")
            return text

        # Translate to English
        translation = translator.translate(text, src='cs', dest='en')
        translated_text = translation.text

        app.logger.info(f"[DEBUG] Translated '{text}' → '{translated_text}'")
        return translated_text

    except Exception as e:
        app.logger.error(f"Translation failed for '{text}': {e}")
        # Fallback: return original text
        return text

def search_food_image_unsplash(dish_name):
    """Search Unsplash for food image and return first result URL"""
    try:
        if not UNSPLASH_ACCESS_KEY:
            app.logger.error("Unsplash not configured (missing ACCESS_KEY)")
            return None

        # Clean dish name before searching
        cleaned_name = clean_dish_name_for_search(dish_name)

        # Translate to English for better Unsplash results
        english_name = translate_to_english(cleaned_name)

        # Build search query - focus on restaurant/meal style photos
        search_query = f"{english_name} meal restaurant"
        app.logger.info(f"[DEBUG] Unsplash search query: '{dish_name}' → '{cleaned_name}' → '{english_name}' → '{search_query}'")

        url = "https://api.unsplash.com/search/photos"

        params = {
            'query': search_query,
            'per_page': 3,  # Get 3 results for fallback
            'orientation': 'landscape',
            'content_filter': 'high'  # Family-friendly content
        }

        headers = {
            'Authorization': f'Client-ID {UNSPLASH_ACCESS_KEY}'
        }

        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()

        if 'results' in data and len(data['results']) > 0:
            # Try to find a valid image URL from results
            for photo in data['results']:
                # Use "small" size (400px width - perfect for Slack, faster loading)
                image_url = photo.get('urls', {}).get('small')

                if is_valid_image_url(image_url):
                    app.logger.info(f"Found image (Unsplash) for '{dish_name}': {image_url}")
                    return image_url
                else:
                    app.logger.warning(f"Skipping invalid Unsplash result: {image_url}")

            # If no valid URL found, return None
            app.logger.warning(f"No valid image found (Unsplash) for '{dish_name}'")
            return None
        else:
            app.logger.warning(f"No image results (Unsplash) for '{dish_name}'")
            return None

    except Exception as e:
        app.logger.error(f"Error searching Unsplash for image of '{dish_name}': {e}", exc_info=True)
        return None

def search_food_image_google(dish_name):
    """Search Google Custom Search for food image and return first result URL"""
    try:
        if not GOOGLE_CSE_ID or not GOOGLE_CSE_API_KEY:
            app.logger.error("Google Custom Search not configured (missing CSE_ID or API_KEY)")
            return None

        # Clean dish name before searching
        cleaned_name = clean_dish_name_for_search(dish_name)

        # Build better search query - focus on dish photos, exclude menus
        search_query = f"{cleaned_name} recipe dish food -menu -jídelní -lístek -restaurace"
        app.logger.info(f"[DEBUG] Cleaned search query: '{dish_name}' → '{search_query}'")

        url = "https://www.googleapis.com/customsearch/v1"

        params = {
            'key': GOOGLE_CSE_API_KEY,
            'cx': GOOGLE_CSE_ID,
            'q': search_query,
            'searchType': 'image',
            'num': 3,  # Get 3 results to have fallback options
            'safe': 'active',
            'imgType': 'photo',  # Only photos, not clipart
            'fileType': 'jpg,png'  # Only common image formats
        }

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        if 'items' in data and len(data['items']) > 0:
            # Try to find a valid image URL from results
            for item in data['items']:
                image_url = item.get('link')
                # Check if URL is valid and is actually an image
                if is_valid_image_url(image_url) and any(image_url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
                    # Validate with AI if enabled
                    if USE_AI_VALIDATION and validate_image_with_ai(image_url, dish_name):
                        app.logger.info(f"Found AI-validated image (Google) for '{dish_name}': {image_url}")
                        return image_url
                    elif not USE_AI_VALIDATION:
                        app.logger.info(f"Found image (Google) for '{dish_name}': {image_url}")
                        return image_url
                    else:
                        app.logger.warning(f"AI rejected Google image: {image_url}")
                else:
                    app.logger.warning(f"Skipping invalid Google result: {image_url}")

            # If no valid URL found, return None
            app.logger.warning(f"No valid image found (Google) for '{dish_name}'")
            return None
        else:
            app.logger.warning(f"No image results (Google) for '{dish_name}'")
            return None

    except Exception as e:
        app.logger.error(f"Error searching Google for image of '{dish_name}': {e}", exc_info=True)
        return None

def search_food_image_duckduckgo(dish_name):
    """Search DuckDuckGo for food image and return first result URL"""
    try:
        # Clean up dish name for better search results
        search_query = f"{dish_name} jídlo food"

        # Add delay to avoid rate limiting (2-4 seconds random)
        time.sleep(random.uniform(2, 4))

        with DDGS() as ddgs:
            results = list(ddgs.images(
                keywords=search_query,
                region='cz-cs',
                safesearch='moderate',
                max_results=1
            ))

            if results and len(results) > 0:
                image_url = results[0].get('image')
                app.logger.info(f"Found image (DuckDuckGo) for '{dish_name}': {image_url}")
                return image_url
            else:
                app.logger.warning(f"No image found (DuckDuckGo) for '{dish_name}'")
                return None

    except Exception as e:
        app.logger.error(f"Error searching DuckDuckGo for image of '{dish_name}': {e}", exc_info=True)
        # If rate limited, return None instead of crashing
        return None

def search_food_image(dish_name):
    """Search for food image using configured provider with fallbacks"""
    app.logger.info(f"[DEBUG] Using image search provider: {IMAGE_SEARCH_PROVIDER}")

    # Try primary provider
    if IMAGE_SEARCH_PROVIDER == "unsplash":
        result = search_food_image_unsplash(dish_name)
        if result:
            return result
        # Fallback to Google if Unsplash fails
        app.logger.info(f"[DEBUG] Unsplash failed, trying Google fallback for '{dish_name}'")
        result = search_food_image_google(dish_name)
        if result:
            return result
        # Final fallback to DuckDuckGo
        app.logger.info(f"[DEBUG] Google failed, trying DuckDuckGo fallback for '{dish_name}'")
        return search_food_image_duckduckgo(dish_name)

    elif IMAGE_SEARCH_PROVIDER == "google":
        result = search_food_image_google(dish_name)
        if result:
            return result
        # Fallback to Unsplash
        app.logger.info(f"[DEBUG] Google failed, trying Unsplash fallback for '{dish_name}'")
        return search_food_image_unsplash(dish_name)

    else:  # duckduckgo
        return search_food_image_duckduckgo(dish_name)

def get_or_cache_dish_image(dish_name):
    """Get cached image URL or search and cache if not found"""
    if not dish_name:
        return None

    # Normalize dish name for consistent caching
    normalized_name = dish_name.strip().lower()

    # Check cache first
    try:
        cache_doc = db.collection('dish_images').document(normalized_name).get()
        if cache_doc.exists:
            cached_data = cache_doc.to_dict()
            image_url = cached_data.get('image_url')
            app.logger.info(f"[DEBUG] Using cached image for '{dish_name}': {image_url}")
            return image_url
        else:
            app.logger.info(f"[DEBUG] No cache found for '{dish_name}', will search now")
    except Exception as e:
        app.logger.error(f"Error reading image cache for '{dish_name}': {e}")

    # Not in cache, search for it
    app.logger.info(f"[DEBUG] Starting image search for '{dish_name}'")
    image_url = search_food_image(dish_name)
    app.logger.info(f"[DEBUG] Image search result for '{dish_name}': {image_url}")

    # Cache the result (even if None, to avoid repeated failed searches)
    if image_url:
        try:
            db.collection('dish_images').document(normalized_name).set({
                'image_url': image_url,
                'dish_name_original': dish_name,
                'last_updated': firestore.SERVER_TIMESTAMP
            })
            app.logger.info(f"Cached image for '{dish_name}'")
        except Exception as e:
            app.logger.error(f"Error caching image for '{dish_name}': {e}")

    return image_url

# --- SLACK API & MESSAGE BUILDING ---

def send_slack_message(payload):
    try:
        app.logger.info(f"[DEBUG] Sending Slack message to channel: {payload.get('channel')}")
        response = requests.post("https://slack.com/api/chat.postMessage", json=payload, headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'})
        response.raise_for_status()
        response_data = response.json()

        if not response_data.get('ok'):
            app.logger.error(f"Slack API error: {response_data.get('error')} - {response_data}")
        else:
            app.logger.info(f"[DEBUG] Slack message sent successfully to {payload.get('channel')}")
    except Exception as e:
        app.logger.error(f"Error in send_slack_message: {e}", exc_info=True)

def send_ephemeral_slack_message(channel_id, user_id, text, blocks=None):
    payload = {"channel": channel_id, "user": user_id, "text": text}
    if blocks: payload["blocks"] = blocks
    try: requests.post("https://slack.com/api/chat.postEphemeral", json=payload, headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'}).raise_for_status()
    except Exception as e: app.logger.error(f"Error in send_ephemeral_slack_message: {e}", exc_info=True)

def is_valid_image_url(url):
    """Check if URL is valid and actually accessible by making a HEAD request"""
    if not url:
        return False
    if not url.startswith(('http://', 'https://')):
        return False
    if len(url) < 10:
        return False

    # Whitelist trusted domains (skip blacklist check for these)
    whitelisted_domains = [
        'images.unsplash.com',
        'unsplash.com'
    ]

    url_lower = url.lower()
    is_whitelisted = any(domain in url_lower for domain in whitelisted_domains)

    # Blacklist domains that typically have menus/lists instead of food photos
    if not is_whitelisted:
        blacklisted_domains = [
            'lookaside.instagram.com',
            'menu',
            'jidelni',
            'listek',
            'damejidlo',
            'wolt.com',
            'bolt.eu',
            'uber',
            'lunchdrive'
        ]

        if any(domain in url_lower for domain in blacklisted_domains):
            app.logger.warning(f"Image URL from blacklisted domain: {url}")
            return False

    # Actually test if the image is downloadable
    try:
        response = requests.head(url, timeout=5, allow_redirects=True)

        # Check if request was successful
        if response.status_code != 200:
            app.logger.warning(f"Image URL returned status {response.status_code}: {url}")
            return False

        # Check Content-Type is an image
        content_type = response.headers.get('Content-Type', '').lower()
        if not any(img_type in content_type for img_type in ['image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/jpg']):
            app.logger.warning(f"Image URL has wrong Content-Type '{content_type}': {url}")
            return False

        app.logger.info(f"[DEBUG] Validated image URL successfully: {url}")
        return True

    except Exception as e:
        app.logger.warning(f"Failed to validate image URL {url}: {e}")
        return False

def build_reminder_message_blocks(menu_items, user_id=None):
    settings_url = f"{BASE_URL}/settings"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{random.choice(URGENT_EMOJIS)} PepeEats: Objednej oběd NA ZÍTRA!", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Zítřejší nabídka za {TARGET_PRICE} Kč:*"}},
        {"type": "divider"}
    ]

    # Add each menu item with its image
    for item in menu_items:
        dish_name = item.get('name', item) if isinstance(item, dict) else item
        image_url = item.get('image_url') if isinstance(item, dict) else None

        # Check user's history with this dish
        comment = None
        if user_id:
            dish_history = get_user_dish_history(user_id, dish_name)
            comment = generate_dish_comment(dish_history)

        # Build dish text with optional comment
        dish_text = f"• *{dish_name}*"
        if comment:
            dish_text += f"\n   _{comment}_"

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": dish_text}
        })

        # Add image ONLY if URL is valid and accessible
        if image_url and is_valid_image_url(image_url):
            try:
                blocks.append({
                    "type": "image",
                    "image_url": image_url,
                    "alt_text": dish_name
                })
                app.logger.info(f"[DEBUG] Added image for '{dish_name}': {image_url}")
            except Exception as e:
                app.logger.warning(f"Failed to add image block for '{dish_name}': {e}")
        else:
            app.logger.warning(f"[DEBUG] Skipping invalid image URL for '{dish_name}': {image_url}")

    # Add Pepe image and rest of the message
    blocks.extend([
        {"type": "divider"},
        {"type": "image", "image_url": random.choice(PEPE_IMAGES), "alt_text": "A wild Pepe appears"},
        {"type": "divider"},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ Mám objednáno"}, "style": "primary", "action_id": "open_order_modal"},
            {"type": "button", "text": {"type": "plain_text", "text": "Dnes neobjednávám"}, "style": "danger", "action_id": "snooze_today"},
            {"type": "button", "text": {"type": "plain_text", "text": "💰 Kolik zbývá?"}, "action_id": "check_balance"},
            {"type": "button", "text": {"type": "plain_text", "text": "Chybí ti funkce?"}, "action_id": "open_feedback_modal"}
        ]},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"🕒 Nevyhovuje čas? <{settings_url}|Změň si nastavení>"}
        ]}
    ])

    return blocks

def build_order_modal_view(menu_items):
    # Handle both old format (list of strings) and new format (list of dicts)
    menu_options = []
    for item in menu_items:
        dish_name = item.get('name', item) if isinstance(item, dict) else item
        display_text = (dish_name[:72] + '...') if len(dish_name) > 75 else dish_name
        menu_options.append({
            "text": {"type": "plain_text", "text": display_text},
            "value": dish_name
        })

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
def health_check(): return redirect(url_for('settings_page'))

@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    user = verify_firebase_token(request)
    if not user: return redirect(url_for('login_page'))
    user_email = user.get('email', '')
    if not (user_email.endswith('@rohlik.cz') or user_email == 'johnnybravo1212@gmail.com'):
        return redirect(url_for('unauthorized_page'))
    
    slack_id = get_slack_id_from_email(user_email)
    is_subscribed = db.collection('users').document(slack_id).get().exists if slack_id else False

    if is_subscribed:
        db.collection('users').document(slack_id).set({'google_email': user_email}, merge=True)
    
    if request.method == 'POST':
        settings_data = {
            'notification_frequency': request.form.get('notification_frequency'),
            'is_test_user': 'is_test_user' in request.form
        }
        save_user_settings(user_email, settings_data)
        return redirect(url_for('settings_page') + '?saved=true')
    
    params = {'client_id': SLACK_CLIENT_ID, 'scope': 'chat:write,users:read,users:read.email', 'redirect_uri': f"{BASE_URL}/slack/oauth/callback"}
    slack_auth_url = f"https://slack.com/oauth/v2/authorize?{urlencode(params)}"
    return render_template('settings.html', user=user, settings=get_user_settings(user_email), is_subscribed=is_subscribed, slack_auth_url=slack_auth_url)

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

    # --- ZMĚNA: Kontrola svátků ---
    if next_day in cz_holidays:
        app.logger.info(f"Next day {next_day} is a public holiday. No reminders will be sent.")
        return f"Next day ({next_day}) is a public holiday.", 200

    # TEMPORARY: Force fresh menu fetch for testing (ignore cache)
    app.logger.info(f"[DEBUG] Forcing fresh menu fetch, ignoring cache")
    menu_items = get_daily_menu(next_day)
    # menu_items = get_saved_menu_for_date(next_day) or get_daily_menu(next_day)
    if isinstance(menu_items, str):
        app.logger.error(f"Could not get menu: {menu_items}")
        return menu_items, 500
    save_daily_menu(next_day, menu_items)
    
    all_users = get_all_users_with_settings()
    if not all_users:
        app.logger.warning("No users found in database!")
        return "No users found.", 200

    app.logger.info(f"[DEBUG] Found {len(all_users)} total users in database")
    app.logger.info(f"[DEBUG] Current hour Prague: {current_hour_prague}")

    users_reminded = 0
    users_skipped = 0

    for user_id, data in all_users.items():
        user_data, settings = data['user_data'], data['settings']

        # Check skip conditions
        already_ordered = check_if_user_ordered_for_date(user_id, next_day)
        is_snoozed = is_user_snoozed(user_data, next_day)

        if already_ordered:
            app.logger.info(f"[DEBUG] Skipping {user_id}: already ordered")
            users_skipped += 1
            continue
        if is_snoozed:
            app.logger.info(f"[DEBUG] Skipping {user_id}: snoozed")
            users_skipped += 1
            continue

        send_now = False
        if settings.get('is_test_user'):
            send_now = True
            app.logger.info(f"[DEBUG] Sending to TEST USER {user_id} because test mode is ON.")
        else:
            freq = settings.get('notification_frequency', 'daily')
            if 9 <= current_hour_prague < 17:
                if freq == '2' and (current_hour_prague - 9) % 2 == 0: send_now = True
                elif freq == '4' and (current_hour_prague - 9) % 4 == 0: send_now = True
                elif freq == 'daily' and 11 <= current_hour_prague < 13: send_now = True

            if not send_now:
                app.logger.info(f"[DEBUG] Skipping {user_id}: not their time (freq={freq}, hour={current_hour_prague})")
                users_skipped += 1

        if send_now:
            app.logger.info(f"[DEBUG] Preparing message for user {user_id}")
            # Build personalized message blocks for each user
            message_blocks = build_reminder_message_blocks(menu_items, user_id=user_id)
            send_slack_message({"channel": user_id, "blocks": message_blocks, "text": "Zítřejší menu"})
            users_reminded += 1

    app.logger.info(f"Job finished. Dynamic reminders sent to {users_reminded} users, skipped {users_skipped} users.")
    return f"Dynamic reminders sent to {users_reminded} users.", 200

@app.route('/send-rating-requests', methods=['POST'])
def trigger_rating_requests():
    """Send rating requests to users who ordered lunch today"""
    app.logger.info("!!! RATING REQUEST JOB STARTED !!!")

    today = date.today()

    # Don't send on weekends or holidays
    if today.weekday() >= 5 or today in cz_holidays:
        app.logger.info(f"Today is weekend or holiday. No rating requests.")
        return "Weekend or holiday - no rating requests.", 200

    # Get all orders for today that need ratings
    orders_to_rate = get_orders_needing_ratings(today)

    if not orders_to_rate:
        app.logger.info("No orders needing ratings today.")
        return "No orders needing ratings.", 200

    ratings_sent = 0

    for order_doc in orders_to_rate:
        order_data = order_doc.to_dict()
        user_id = order_data.get('ordered_for_user_id')
        meal_desc = order_data.get('meal_description', 'tvoje jídlo')

        if not user_id:
            continue

        # Build rating request message
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Jak ti dnes chutnalo *{meal_desc}*? 🍽️"
                }
            },
            {
                "type": "actions",
                "block_id": f"rating_block_{today.strftime('%Y-%m-%d')}",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "💯 Vynikající"},
                        "value": "100",
                        "action_id": "rate_meal_100"
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "👍 Dobré"},
                        "value": "75",
                        "action_id": "rate_meal_75"
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "😐 Ujde"},
                        "value": "50",
                        "action_id": "rate_meal_50"
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "👎 Slabé"},
                        "value": "25",
                        "action_id": "rate_meal_25"
                    }
                ]
            }
        ]

        send_slack_message({
            "channel": user_id,
            "text": f"Jak ti dnes chutnalo {meal_desc}?",
            "blocks": blocks
        })
        ratings_sent += 1

    app.logger.info(f"Rating requests sent to {ratings_sent} users.")
    return f"Rating requests sent to {ratings_sent} users.", 200

@app.route('/slack/interactive', methods=['POST'])
def slack_interactive_endpoint():
    payload = json.loads(request.form.get("payload"))
    user_id = payload["user"]["id"]
    today = date.today()
    order_for = today + timedelta(days=3) if today.weekday() == 4 else today + timedelta(days=1)
    
    if payload["type"] == "view_submission":
        if payload["view"]["callback_id"] == "feedback_submission":
            feedback_text = payload["view"]["state"]["values"]["feedback_block"]["feedback_input"]["value"]
            db.collection("feedback").add({ "text": feedback_text, "user_id": user_id, "submitted_at": firestore.SERVER_TIMESTAMP })

            # Notify owner about new feedback
            if OWNER_SLACK_ID:
                notification_blocks = [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": "💬 Nový feedback od uživatele!"
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Od:* <@{user_id}>\n*Feedback:*\n```{feedback_text}```"
                        }
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": f"Zobrazit všechny: <{BASE_URL}/admin?secret={ADMIN_SECRET_KEY}|Admin panel>"
                            }
                        ]
                    }
                ]

                send_slack_message({
                    "channel": OWNER_SLACK_ID,
                    "text": f"Nový feedback od {user_id}: {feedback_text}",
                    "blocks": notification_blocks
                })

            return ("", 200)

        if payload["view"]["callback_id"] == "order_submission":
            values = payload["view"]["state"]["values"]
            selected_meal = values["meal_selection_block"]["meal_select_action"]["selected_option"]["value"]
            selected_user_id = values["person_selection_block"]["person_select_action"]["selected_user"]
            save_user_order(user_id, selected_meal, order_for, selected_user_id)
            send_slack_message({"channel": user_id, "text": f"Díky! Uložil jsem, že na {order_for.strftime('%d.%m.')} máš pro <@{selected_user_id}> objednáno: *{selected_meal}*"})
            if user_id != selected_user_id:
                 send_slack_message({"channel": selected_user_id, "text": f"Ahoj! Jen abys věděl/a, <@{user_id}> ti právě objednal/a na zítra k obědu: *{selected_meal}*"})
            return ("", 200)

    if payload["type"] == "block_actions":
        channel_id = payload["channel"]["id"]
        trigger_id = payload.get("trigger_id")
        action_id = payload["actions"][0].get("action_id")

        # Handle rating submissions
        if action_id.startswith("rate_meal_"):
            rating_value = int(action_id.split("_")[-1])

            # Save the rating
            if save_rating(user_id, today, rating_value):
                # Get reaction message based on rating
                reactions = {
                    100: ["Paráda! Těším se, že si to dáš znovu! 🌟", "Skvělá volba! 💯", "To je radost! 😋"],
                    75: ["Super! 👍", "Dobrá volba! 😊", "Jsem rád/a! ✨"],
                    50: ["Ujde to. Příště to bude lepší! 🤞", "No jo... 😐", "Snad příště víc! 🤷"],
                    25: ["Škoda... Příště zkus něco jiného! 😔", "To nebylo ono, co? 😕", "Díky za zpětnou vazbu! 🙏"]
                }
                reaction = random.choice(reactions.get(rating_value, reactions[75]))
                send_ephemeral_slack_message(channel_id, user_id, f"Díky za hodnocení! {reaction}")
            else:
                send_ephemeral_slack_message(channel_id, user_id, "Hmm, nemohl jsem uložit hodnocení. 😕")

        elif action_id == "check_balance":
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
        elif action_id == "open_feedback_modal":
            feedback_modal = {
                "type": "modal",
                "callback_id": "feedback_submission",
                # --- ZMĚNA: Titulek zkrácen pod 24 znaků ---
                "title": {"type": "plain_text", "text": "Feedback pro PepeEats"},
                "submit": {"type": "plain_text", "text": "Odeslat"},
                "close": {"type": "plain_text", "text": "Zrušit"},
                "blocks": [{
                    "type": "input",
                    "block_id": "feedback_block",
                    "label": {"type": "plain_text", "text": "Co bys vylepšil/a nebo přidal/a?"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "feedback_input",
                        "multiline": True
                    }
                }]
            }
            try:
                # Tento kód je v pořádku, není třeba ho měnit
                response = requests.post(
                    "https://slack.com/api/views.open",
                    json={"trigger_id": trigger_id, "view": feedback_modal},
                    headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'}
                )
                response.raise_for_status()
                response_data = response.json()
                if not response_data.get("ok"):
                    app.logger.error(f"Error opening feedback modal: {response_data.get('error')}")
                    send_ephemeral_slack_message(channel_id, user_id, f"Jejda, nepodařilo se mi otevřít okno pro feedback. Chyba: `{response_data.get('error')}`")
            except requests.exceptions.RequestException as e:
                app.logger.error(f"HTTP Error opening feedback modal: {e}")
                send_ephemeral_slack_message(channel_id, user_id, "Jejda, nepodařilo se mi otevřít okno pro feedback kvůli chybě v síťové komunikaci.")        
        elif action_id in ["open_order_modal", "ho_order_for_other"]:
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

@app.route('/subscribe')
def subscribe():
    return redirect(url_for('settings_page'))

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
        return redirect(url_for('settings_page'))
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
