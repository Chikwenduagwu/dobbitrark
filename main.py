#!/usr/bin/env python3
"""
Telegram Ethereum/Sepolia Tracker Bot with Dobby (Fireworks) summaries.

How it works:
- /start : help
- /add <address> [chain] : add address to your watchlist (chain optional: mainnet|sepolia)
- /remove <address> [chain] : remove address
- /list : list tracked addresses for you
- The bot polls Etherscan periodically and notifies users of new txs.
- Each notification is passed to Fireworks (Dobby) to produce a short summary.

Environment variables required:
- TELEGRAM_TOKEN
- ETHERSCAN_API_KEY
- FIREWORKS_API_KEY
Optional:
- POLL_INTERVAL (seconds, default 60)
"""

import os
import time
import threading
import logging
import sqlite3
import requests
from typing import Optional, Dict, Any, List, Tuple

from telegram.ext import Updater, CommandHandler, CallbackContext
from telegram import Update, ParseMode

# --------- Configuration ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")
FIREWORKS_API_KEY = os.getenv("FIREWORKS_API_KEY")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))  # seconds
MAX_TX_FETCH = int(os.getenv("MAX_TX_FETCH", "12"))

if not TELEGRAM_TOKEN:
    raise SystemExit("Missing TELEGRAM_TOKEN environment variable")
if not ETHERSCAN_API_KEY:
    logging.warning("Missing ETHERSCAN_API_KEY - Etherscan features will not work")
if not FIREWORKS_API_KEY:
    logging.warning("Missing FIREWORKS_API_KEY - Dobby summarization will be disabled")

# Etherscan endpoints
ETHERSCAN_BASE = "https://api.etherscan.io/api"
SEPOLIA_BASE = "https://api-sepolia.etherscan.io/api"

# --------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger("dobby-tracker")

# --------- Database ----------
DB_PATH = os.getenv("DB_PATH", "tracker.sqlite")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()

# Create tables
cur.executescript("""
CREATE TABLE IF NOT EXISTS users (
  telegram_id TEXT PRIMARY KEY,
  created_at INTEGER DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS addresses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  telegram_id TEXT NOT NULL,
  address TEXT NOT NULL,
  network TEXT NOT NULL CHECK(network IN ('mainnet','sepolia')),
  created_at INTEGER DEFAULT (strftime('%s','now')),
  UNIQUE(telegram_id, address, network)
);

CREATE TABLE IF NOT EXISTS last_seen (
  address TEXT NOT NULL,
  network TEXT NOT NULL,
  last_time INTEGER DEFAULT 0,
  last_hash TEXT DEFAULT '',
  PRIMARY KEY(address, network)
);
""")
conn.commit()

# Prepared statements
def db_add_user(tid: str):
    cur.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES (?)", (tid,))
    conn.commit()

def db_add_address(tid: str, address: str, network: str) -> bool:
    try:
        cur.execute("INSERT OR IGNORE INTO addresses(telegram_id,address,network) VALUES (?,?,?)", (tid, address.lower(), network))
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        logger.exception("db_add_address error: %s", e)
        return False

def db_remove_address(tid: str, address: str, network: str) -> bool:
    cur.execute("DELETE FROM addresses WHERE telegram_id=? AND address=? AND network=?", (tid, address.lower(), network))
    conn.commit()
    return cur.rowcount > 0

def db_list_addresses(tid: str) -> List[Tuple[str,str]]:
    cur.execute("SELECT address, network FROM addresses WHERE telegram_id=? ORDER BY network, address", (tid,))
    return cur.fetchall()

def db_list_all_tracked() -> List[Tuple[str,str]]:
    cur.execute("SELECT DISTINCT address, network FROM addresses")
    return cur.fetchall()

def db_get_last_seen(address: str, network: str) -> Tuple[int, str]:
    cur.execute("SELECT last_time, last_hash FROM last_seen WHERE address=? AND network=?", (address.lower(), network))
    r = cur.fetchone()
    if r:
        return int(r[0]), r[1]
    return 0, ""

def db_upsert_last_seen(address: str, network: str, last_time: int, last_hash: str):
    cur.execute("""
    INSERT INTO last_seen(address, network, last_time, last_hash)
    VALUES (?,?,?,?)
    ON CONFLICT(address, network) DO UPDATE SET last_time=excluded.last_time, last_hash=excluded.last_hash
    """, (address.lower(), network, last_time, last_hash))
    conn.commit()

# --------- Etherscan helpers ----------
def etherscan_api_get(base_url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(base_url, params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.exception("Etherscan request error: %s", e)
        return None

def fetch_normal_txs(network: str, address: str, limit: int = MAX_TX_FETCH) -> List[Dict[str, Any]]:
    base = SEPOLIA_BASE if network == "sepolia" else ETHERSCAN_BASE
    params = {
        "module": "account",
        "action": "txlist",
        "address": address,
        "startblock": 0,
        "endblock": 99999999,
        "page": 1,
        "offset": limit,
        "sort": "desc",
        "apikey": ETHERSCAN_API_KEY
    }
    data = etherscan_api_get(base, params)
    if not data:
        return []
    # Etherscan returns status '1' on success, but when no txs, message "No transactions found"
    if data.get("status") == "1" and isinstance(data.get("result"), list):
        return data["result"]
    return []

def fetch_token_txs(network: str, address: str, limit: int = MAX_TX_FETCH) -> List[Dict[str, Any]]:
    base = SEPOLIA_BASE if network == "sepolia" else ETHERSCAN_BASE
    params = {
        "module": "account",
        "action": "tokentx",
        "address": address,
        "startblock": 0,
        "endblock": 99999999,
        "page": 1,
        "offset": limit,
        "sort": "desc",
        "apikey": ETHERSCAN_API_KEY
    }
    data = etherscan_api_get(base, params)
    if not data:
        return []
    if data.get("status") == "1" and isinstance(data.get("result"), list):
        return data["result"]
    return []

# --------- Dobby (Fireworks) summarizer ----------
def dobby_summarize(tx: Dict[str, Any], network: str) -> Optional[str]:
    """
    Given a raw tx from Etherscan (normal or token), call Fireworks API to produce a short summary.
    Return plain text or None on failure.
    """
    if not FIREWORKS_API_KEY:
        return None

    is_token = "tokenSymbol" in tx and tx.get("tokenSymbol")
    when = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(int(tx.get("timeStamp", 0))))
    if is_token:
        amount = float(tx.get("value", 0)) / (10 ** int(tx.get("tokenDecimal", 18)))
        value_line = f"Token: {tx.get('tokenName','?')} ({tx.get('tokenSymbol','?')}), Amount: {amount}"
    else:
        value_line = f"ETH Amount: {float(tx.get('value', 0)) / 1e18:.6f}"

    prompt = (
        f"Summarize this {network} transaction for a non-technical investor in 1-2 short sentences.\n\n"
        f"Hash: {tx.get('hash')}\nFrom: {tx.get('from')}\nTo: {tx.get('to')}\nWhen: {when} UTC\n{value_line}\nGasUsed: {tx.get('gasUsed')}\nGasPrice(wei): {tx.get('gasPrice')}\nDirection: {tx.get('direction','UNKNOWN')}\n\n"
        "Keep it professional. No hashtags or markdown formatting. Output plain text."
    )

    url = "https://api.fireworks.ai/inference/v1/chat/completions"
    payload = {
        "model": "accounts/sentientfoundation-serverless/models/dobby-mini-unhinged-plus-llama-3-1-8b",
        "messages": [
            {"role": "system", "content": "You are a professional transaction summarizer. Keep language neutral and concise."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 120
    }
    headers = {
        "Authorization": f"Bearer {FIREWORKS_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        # Many Fireworks responses are in data.choices[0].message.content
        text = None
        if isinstance(data.get("choices"), list):
            ch = data["choices"][0]
            # try multiple patterns to be robust
            text = (ch.get("message") or {}).get("content") if isinstance(ch.get("message"), dict) else ch.get("text") or ch.get("content")
        # fallback
        if not text:
            text = data.get("output") or data.get("text") or None
        return (text or "").strip() if text else None
    except Exception as e:
        logger.exception("Dobby summarization failed: %s", e)
        return None

# --------- Poller thread ----------
def check_address_for_updates(address: str, network: str):
    """
    Check recent transactions for address on specified network.
    Compare with last_seen and notify subscribers for new txs (oldest-first).
    """
    try:
        normal = fetch_normal_txs(network, address, limit=MAX_TX_FETCH)
        token = fetch_token_txs(network, address, limit=MAX_TX_FETCH)
        combined = []

        for t in normal:
            t["kind"] = "normal"
            combined.append(t)
        for t in token:
            t["kind"] = "erc20"
            combined.append(t)

        # sort descending by timeStamp (strings might be numeric strings)
        combined.sort(key=lambda x: int(x.get("timeStamp", 0)), reverse=True)

        last_time, last_hash = db_get_last_seen(address, network)
        # find txs that are newer than last_time or if same time but different hash include latest
        fresh = [t for t in combined if int(t.get("timeStamp", 0)) > last_time]
        if not fresh and combined:
            # If none newer but top hash differs, include newest single
            if combined[0].get("hash") != last_hash:
                fresh = [combined[0]]

        if not fresh:
            return

        # We want to notify oldest-first among the fresh set
        fresh = list(reversed(fresh))

        # find subscribers
        cur.execute("SELECT telegram_id FROM addresses WHERE address=? AND network=?", (address.lower(), network))
        rows = cur.fetchall()
        subscribers = [r[0] for r in rows]

        for tx in fresh:
            # determine direction relative to address
            addr_l = address.lower()
            direction = ("OUTGOING" if tx.get("from","").lower() == addr_l else
                         "INCOMING" if tx.get("to","").lower() == addr_l else "OTHER")
            tx["direction"] = direction

            # Prepare plain lines
            if tx.get("kind") == "erc20":
                amt = float(tx.get("value", 0)) / (10 ** int(tx.get("tokenDecimal", 18)))
                value_line = f"{tx.get('tokenSymbol','?')} {amt}"
            else:
                value_line = f"{float(tx.get('value',0))/1e18:.6f} ETH"

            short_msg = (
                f"🔔 New {network} tx for {address} — {direction}\n"
                f"Hash: {tx.get('hash')}\nFrom: {tx.get('from')}\nTo: {tx.get('to')}\nAmount: {value_line}\n"
                f"Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(int(tx.get('timeStamp',0))))}"
            )

            # Ask Dobby for a concise professional summary (best-effort)
            summary = dobby_summarize(tx, network) if FIREWORKS_API_KEY else None
            final_text = short_msg
            if summary:
                final_text += "\n\nDobby: " + summary

            # Send to subscribers
            for chat_id in subscribers:
                try:
                    updater.bot.send_message(chat_id=int(chat_id), text=final_text)
                except Exception as e:
                    logger.exception("Failed to send message to %s: %s", chat_id, e)
                time.sleep(0.15)

            # update last_seen for this tx
            db_upsert_last_seen(address, network, int(tx.get("timeStamp", 0)), tx.get("hash"))

    except Exception as e:
        logger.exception("Error checking address %s:%s -> %s", address, network, e)

def poller_loop():
    logger.info("Poller thread started, polling interval %ss", POLL_INTERVAL)
    while True:
        try:
            rows = db_list_all_tracked()
            if rows:
                for address, network in rows:
                    check_address_for_updates(address, network)
                    # small backoff to avoid hitting rate limits
                    time.sleep(0.3)
            else:
                logger.debug("No addresses tracked. Poller sleeping.")
        except Exception as e:
            logger.exception("Poller loop error: %s", e)
        time.sleep(POLL_INTERVAL)

# --------- Telegram handlers ----------
def start(update: Update, context: CallbackContext):
    uid = str(update.effective_chat.id)
    db_add_user(uid)
    update.message.reply_text(
        "Hi — Dobby Tracker Bot here.\n\n"
        "Commands:\n"
        "/add <address> [mainnet|sepolia]  — track address (default mainnet)\n"
        "/remove <address> [mainnet|sepolia] — stop tracking\n"
        "/list — show your tracked addresses\n"
    )

def add_cmd(update: Update, context: CallbackContext):
    uid = str(update.effective_chat.id)
    db_add_user(uid)
    args = context.args
    if not args:
        update.message.reply_text("Usage: /add <0xaddress> [mainnet|sepolia]")
        return
    address = args[0].strip()
    network = "mainnet"
    if len(args) > 1 and args[1].lower() == "sepolia":
        network = "sepolia"

    if not address.startswith("0x") or len(address) != 42:
        update.message.reply_text("Not a valid Ethereum address. It should be 0x... and 42 chars.")
        return

    added = db_add_address(uid, address, network)
    if added:
        update.message.reply_text(f"✅ Tracking {address} on {network}.")
    else:
        update.message.reply_text(f"ℹ️ {address} is already tracked on {network} for you.")

def remove_cmd(update: Update, context: CallbackContext):
    uid = str(update.effective_chat.id)
    args = context.args
    if not args:
        update.message.reply_text("Usage: /remove <0xaddress> [mainnet|sepolia]")
        return
    address = args[0].strip()
    network = "mainnet"
    if len(args) > 1 and args[1].lower() == "sepolia":
        network = "sepolia"

    removed = db_remove_address(uid, address, network)
    if removed:
        update.message.reply_text(f"✅ Removed {address} from {network}.")
    else:
        update.message.reply_text("That address wasn't in your list.")

def list_cmd(update: Update, context: CallbackContext):
    uid = str(update.effective_chat.id)
    rows = db_list_addresses(uid)
    if not rows:
        update.message.reply_text("You have no tracked addresses. Use /add to start.")
        return
    lines = [f"{r[0]} — {r[1]}" for r in rows]
    update.message.reply_text("Your tracked addresses:\n" + "\n".join(lines))

# --------- Entrypoint ----------
if __name__ == "__main__":
    # Updater (long polling)
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("add", add_cmd))
    dp.add_handler(CommandHandler("remove", remove_cmd))
    dp.add_handler(CommandHandler("list", list_cmd))

    # start poller thread
    t = threading.Thread(target=poller_loop, daemon=True)
    t.start()

    logger.info("Starting Telegram polling...")
    updater.start_polling()
    updater.idle()
