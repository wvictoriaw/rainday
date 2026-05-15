import requests
import sqlite3
import time
from constants import TELE_TOKEN, DB_NAME

# --- CONFIGURATION ---
TOKEN = TELE_TOKEN
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

def init_db():
    """Creates the database and table if they don't exist."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (chat_id TEXT PRIMARY KEY, confirmed INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()

def add_pending_user(chat_id):
    """Adds a user to the DB but doesn't confirm them yet."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # Use INSERT OR IGNORE so we don't crash if they send /start twice
    c.execute("INSERT OR IGNORE INTO users (chat_id, confirmed) VALUES (?, 0)", (chat_id,))
    conn.commit()
    conn.close()

def confirm_user(chat_id):
    """Sets a user as confirmed."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET confirmed = 1 WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()

def handle_updates(offset):
    """Checks for new messages and button clicks."""
    url = f"{BASE_URL}/getUpdates?timeout=30&offset={offset}"
    try:
        response = requests.get(url).json()
    except Exception as e:
        print(f"Connection error: {e}")
        return offset

    if not response.get("ok"):
        return offset

    for update in response.get("result", []):
        offset = update["update_id"] + 1
        
        # 1. Handle the /start command
        if "message" in update and update["message"].get("text") == "/start":
            chat_id = str(update["message"]["chat"]["id"])
            add_pending_user(chat_id)
            
            # Send Welcome + Confirmation Button
            payload = {
                "chat_id": chat_id,
                "text": "Welcome! Rainday will send you updates when there are big rains on the expressways. Please note that we are in beta and apologise in advance for any spams / incoherent notifications you may receive. If you're okay with that, please confirm you'd like to receive notifications:",
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": "✅ Confirm", "callback_data": "confirm_me"}
                    ]]
                }
            }
            requests.post(f"{BASE_URL}/sendMessage", json=payload)
            print(f"Sent confirmation request to {chat_id}")

        # 2. Handle the Button Click
        elif "callback_query" in update:
            cb = update["callback_query"]
            chat_id = str(cb["message"]["chat"]["id"])
            
            if cb["data"] == "confirm_me":
                confirm_user(chat_id)
                # Remove the "loading" state from the button
                requests.post(f"{BASE_URL}/answerCallbackQuery", data={"callback_query_id": cb["id"]})
                # Send final confirmation
                requests.post(f"{BASE_URL}/sendMessage", data={
                    "chat_id": chat_id, 
                    "text": "Verified! You will now receive alerts."
                })
                print(f"User {chat_id} is now confirmed!")

    return offset

def main():
    init_db()
    print("Bot Listener is running... Press Ctrl+C to stop.")
    update_offset = 0
    
    while True:
        # We update the offset so we don't process the same message twice
        update_offset = handle_updates(update_offset)
        time.sleep(1) 

if __name__ == "__main__":
    main()