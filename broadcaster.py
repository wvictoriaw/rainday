import sqlite3
import requests
import time
import logging
from datetime import datetime, timedelta
from constants import TELE_TOKEN, DB_NAME, LOG_FILE
from rainday_v2 import run_rainday

# --- CONFIGURATION ---
TOKEN = TELE_TOKEN
CHECK_INTERVAL = 300  # 5 minutes

# --- LOGGING SETUP ---
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def is_within_broadcast_hours():
    """Checks if the current local time is between 08:30 and 23:30."""
    now = datetime.now().time()
    start_time = now.replace(hour=8, minute=30, second=0, microsecond=0)
    end_time = now.replace(hour=23, minute=30, second=0, microsecond=0)
    
    # If current time is >= 08:30 AND <= 23:30
    return start_time <= now <= end_time

def get_confirmed_subscribers():
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT chat_id FROM users WHERE confirmed = 1")
        users = [row[0] for row in c.fetchall()]
        conn.close()
        return users
    except sqlite3.OperationalError:
        return []

def send_telegram_broadcast(user_list, alert_list):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    for alert_dict in alert_list:
        alert_message = alert_dict['message']
        logging.info(f"Starting broadcast for alert: {alert_message[:50]}...")
        for chat_id in user_list:
            payload = {"chat_id": chat_id, "text": alert_message, "parse_mode": "HTML"}
            try:
                time.sleep(0.05) 
                response = requests.post(url, data=payload)
                if response.status_code == 200:
                    logging.info(f"SUCCESS: Sent to {chat_id}")
                else:
                    logging.error(f"FAILED: {chat_id} - {response.text}")
            except Exception as e:
                logging.error(f"CONNECTION ERROR: {chat_id} - {e}")

def seconds_until_broadcast_start():
    """Calculate seconds until the next 08:30 broadcast window."""
    now = datetime.now()
    today_start = now.replace(hour=8, minute=30, second=0, microsecond=0)
    
    if now < today_start:
        # It's before 08:30 today — sleep until today's start
        return (today_start - now).total_seconds() + 60
    else:
        # It's after 23:30 — sleep until tomorrow's 08:30
        tomorrow_start = today_start + timedelta(days=1)
        return (tomorrow_start - now).total_seconds() + 60

def main():
    print(f"Broadcaster started. Active Hours: 08:30 - 23:30. Interval: {CHECK_INTERVAL/60}min")
    logging.info("Broadcaster script started.")
    
    while True:
        sleeptime = CHECK_INTERVAL
        # 1. Check if we are allowed to send messages right now
        if is_within_broadcast_hours():
            try:
                alerts = run_rainday()

                if alerts:
                    subs = get_confirmed_subscribers()
                    if subs:
                        send_telegram_broadcast(subs, alerts)
                        print(f"[{datetime.now().strftime('%H:%M')}] Alerts sent.")
                    else:
                        logging.warning("Alerts found, but no confirmed subscribers.")
                else:
                    print(f"[{datetime.now().strftime('%H:%M')}] No alerts found.")
        
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout): # internet cuts
                error_time = datetime.now().strftime('%H:%M')
                msg = f"[{error_time}] Internet down. Please come back when you have a connection."
                print(msg)
                logging.error(msg)
                input("\n>>> Press [ENTER] to resume broadcasting...")
                continue    
        else:
            # 2. If outside hours, calculate how long to sleep until the next broadcast window
            current_time = datetime.now().strftime('%H:%M')
            print(f"[{current_time}] Sleep Mode: Outside broadcast hours (08:30-23:30).")

            sleeptime = seconds_until_broadcast_start()

        # 3. Wait for the next cycle
        time.sleep(sleeptime)

if __name__ == "__main__":
    main()