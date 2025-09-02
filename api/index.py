from flask import Flask, request
import requests
import os
from pymongo import MongoClient
from datetime import datetime

app = Flask(__name__)

# 🔑 Environment Variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")
DOBBY_API_KEY = os.getenv("DOBBY_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
ETHERSCAN_API = "https://api.etherscan.io/api"

# MongoDB setup
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["tracker_bot"]
users_col = db["users"]
tx_col = db["transactions"]


# ==========================
# Telegram Webhook
# ==========================
@app.route("/", methods=["GET"])
def home():
    return "🤖 Wallet Tracker Bot is live!"


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "").strip()

        if text.lower() == "/start":
            send_message(chat_id, "👋 Welcome to Wallet Tracker!\n\n"
                                  "Use `/add <wallet>` to start tracking.\n"
                                  "We’ll alert you when transactions happen.")

        elif text.startswith("/add"):
            parts = text.split()
            if len(parts) == 2:
                wallet = parts[1]
                add_wallet(chat_id, wallet)
                send_message(chat_id, f"✅ Added wallet `{wallet}` for tracking.")
            else:
                send_message(chat_id, "⚠️ Usage: `/add <wallet_address>`")

        elif text.lower() == "/mywallets":
            wallets = get_wallets(chat_id)
            if wallets:
                send_message(chat_id, "📌 Your tracked wallets:\n" + "\n".join(wallets))
            else:
                send_message(chat_id, "❌ You have no wallets saved.")

        else:
            send_message(chat_id, "🤔 Unknown command. Try `/add <wallet>`")

    return {"status": "ok"}


# ==========================
# Helpers
# ==========================
def send_message(chat_id, text):
    url = f"{BASE_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    requests.post(url, json=payload)


def add_wallet(chat_id, wallet):
    """Save wallet under the user's account"""
    users_col.update_one(
        {"chat_id": chat_id},
        {"$addToSet": {"wallets": wallet}},
        upsert=True
    )


def get_wallets(chat_id):
    user = users_col.find_one({"chat_id": chat_id})
    return user.get("wallets", []) if user else []


def check_transactions():
    """Check Etherscan for new txs of all users' wallets"""
    users = users_col.find()
    for user in users:
        chat_id = user["chat_id"]
        for wallet in user.get("wallets", []):
            url = f"{ETHERSCAN_API}"
            params = {
                "module": "account",
                "action": "txlist",
                "address": wallet,
                "startblock": 0,
                "endblock": 99999999,
                "sort": "desc",
                "apikey": ETHERSCAN_API_KEY
            }

            try:
                res = requests.get(url, params=params).json()
                if res.get("status") == "1":
                    latest_tx = res["result"][0]  # newest tx
                    tx_hash = latest_tx["hash"]

                    # Check if we already alerted this tx
                    if not tx_col.find_one({"hash": tx_hash}):
                        # Save tx to DB
                        tx_col.insert_one({"hash": tx_hash, "time": datetime.utcnow()})

                        amount_eth = int(latest_tx["value"]) / 10**18
                        from_addr = latest_tx["from"]
                        to_addr = latest_tx["to"]

                        # Ask Dobby to explain
                        dobby_summary = ask_dobby(f"Explain this Ethereum transaction:\n"
                                                  f"Hash: {tx_hash}\n"
                                                  f"From: {from_addr}\n"
                                                  f"To: {to_addr}\n"
                                                  f"Value: {amount_eth} ETH")

                        message = (f"🚨 New transaction detected!\n\n"
                                   f"💸 Amount: {amount_eth:.5f} ETH\n"
                                   f"🔗 [View on Etherscan](https://etherscan.io/tx/{tx_hash})\n"
                                   f"📍 From: `{from_addr}`\n"
                                   f"📍 To: `{to_addr}`\n\n"
                                   f"🤖 Dobby says: {dobby_summary}")

                        send_message(chat_id, message)

            except Exception as e:
                print("Error fetching tx:", e)


def ask_dobby(query):
    url = "https://api.dobby.ai/ask"
    headers = {"Authorization": f"Bearer {DOBBY_API_KEY}"}
    payload = {"question": query}

    try:
        res = requests.post(url, headers=headers, json=payload).json()
        return res.get("answer", "⚠️ No response from Dobby.")
    except Exception as e:
        return f"❌ Error asking Dobby: {str(e)}"


# ==========================
# Vercel Handler
# ==========================
def handler(request):
    with app.request_context(request.environ):
        return app.full_dispatch_request()
