# app.py
import base64
import hmac
import hashlib
import os
import json
import tempfile
import shutil
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---- Logging ----
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("paymentbot")

# ---- Env ----
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Telegram bot token
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))  # your admin id

# channels (fill or set via /set_vip etc)
VIP_CHANNEL_ID = int(os.getenv("VIP_CHANNEL_ID", "0"))
DARK_CHANNEL_ID = int(os.getenv("DARK_CHANNEL_ID", "0"))

# Razorpay webhook secret (set this to the "Secret" you enter in Razorpay webhook config)
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

# Where to persist
DATA_DIR = os.getenv("DATA_DIR", "/data")
DATA_FILE = os.path.join(DATA_DIR, "paymentbot.json")

# other payment display info
UPI_ID = os.getenv("UPI_ID", "")
UPI_QR_URL = os.getenv("UPI_QR_URL", "")
CRYPTO_ADDRESS = os.getenv("CRYPTO_ADDRESS", "")
CRYPTO_NETWORK = os.getenv("CRYPTO_NETWORK", "BEP20")
REMITLY_INFO = os.getenv("REMITLY_INFO", "")

# timezone
IST = timezone(timedelta(hours=5, minutes=30))

# runtime state
PENDING_PAYMENTS: Dict[str, Dict[str, Any]] = {}
PURCHASE_LOG: list = []
KNOWN_USERS: set = set()
SENT_INVITES: dict = {}
CONFIG: dict = {}

# helper time
def now_ist():
    return datetime.now(IST)

# persistence helpers
def _ensure_data_dir():
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

def _serialize_state():
    return {
        "pending_payments": PENDING_PAYMENTS,
        "purchase_log": [
            {**{k: (v.isoformat() if isinstance(v, datetime) else v) for k,v in p.items()}}
            for p in PURCHASE_LOG
        ],
        "known_users": list(KNOWN_USERS),
        "sent_invites": {str(k): v for k,v in SENT_INVITES.items()},
        "config": CONFIG,
    }

def _deserialize_state(data):
    global PENDING_PAYMENTS, PURCHASE_LOG, KNOWN_USERS, SENT_INVITES, CONFIG
    if not data:
        return
    PENDING_PAYMENTS = data.get("pending_payments", {}) or {}
    PURCHASE_LOG = []
    for p in data.get("purchase_log", []) or []:
        pc = dict(p)
        t = pc.get("time")
        if isinstance(t, str):
            try:
                pc["time"] = datetime.fromisoformat(t)
            except Exception:
                pass
        PURCHASE_LOG.append(pc)
    KNOWN_USERS = set(data.get("known_users", []) or [])
    sent = data.get("sent_invites", {}) or {}
    new_sent = {}
    for k,v in sent.items():
        try:
            new_sent[int(k)] = v
        except Exception:
            new_sent[k] = v
    SENT_INVITES = new_sent
    CONFIG = data.get("config", {}) or {}

def save_state():
    try:
        _ensure_data_dir()
        payload = _serialize_state()
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        shutil.move(tmp, DATA_FILE)
        logger.info("Saved state to %s", DATA_FILE)
    except Exception as e:
        logger.exception("Save failed: %s", e)

def load_state():
    try:
        if not os.path.exists(DATA_FILE):
            logger.info("No data file found - starting fresh")
            return
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _deserialize_state(data)
        logger.info("Loaded state from %s", DATA_FILE)
    except Exception as e:
        logger.exception("Load failed: %s", e)

# Telegram helper (use HTTP API to create invite link & send message safely from webhook)
def tg_api(method: str, data: dict):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    resp = requests.post(url, json=data, timeout=10)
    try:
        resp.raise_for_status()
    except Exception:
        logger.error("Telegram API error %s: %s", resp.status_code, resp.text)
        raise
    return resp.json()

def create_invite_and_send(user_id: int, plan: str):
    """
    Creates single-use invite(s) for the plan and sends the link(s) to user_id.
    Returns dict of links created.
    """
    links = {}
    headers = {"Content-Type":"application/json"}
    if plan in ("vip", "both") and VIP_CHANNEL_ID:
        payload = {
            "chat_id": VIP_CHANNEL_ID,
            "member_limit": 1,
            "name": f"user_{user_id}_vip",
            # you can set creates_join_request True if you want request flow
            "creates_join_request": False
        }
        r = tg_api("createChatInviteLink", payload)
        link = r.get("result", {}).get("invite_link")
        links["vip"] = link
    if plan in ("dark", "both") and DARK_CHANNEL_ID:
        payload = {
            "chat_id": DARK_CHANNEL_ID,
            "member_limit": 1,
            "name": f"user_{user_id}_dark",
            "creates_join_request": False
        }
        r = tg_api("createChatInviteLink", payload)
        link = r.get("result", {}).get("invite_link")
        links["dark"] = link

    # store in memory and persist
    if links:
        SENT_INVITES.setdefault(user_id, {}).update(links)
        save_state()
        # send to user
        lines = []
        if "vip" in links:
            lines.append(f"🔑 VIP Channel:\n{links['vip']}")
        if "dark" in links:
            lines.append(f"🕶 Dark Channel:\n{links['dark']}")
        text = "✅ Payment confirmed — here are your access links:\n\n" + "\n\n".join(lines)
        try:
            tg_api("sendMessage", {"chat_id": user_id, "text": text})
        except Exception:
            logger.exception("Couldn't send invite message to user")
    return links

# FastAPI app for webhook
app = FastAPI()


@app.on_event("startup")
def start_bot_thread():
    # run bot.main() in a separate daemon thread so uvicorn (FastAPI) keeps serving
    t = threading.Thread(target=main, daemon=True)
    t.start()
    logger.info("Started Telegram polling bot in background thread (startup).")

def verify_razorpay_signature(body_bytes: bytes, signature: str, secret: str) -> bool:
    """
    Razorpay sends X-Razorpay-Signature which is base64(hmac_sha256(body, secret)).
    This function computes HMAC-SHA256 over the raw request body, base64-encodes it,
    then compares it (constant-time) with the signature header.
    """
    if not secret:
        logger.warning("No RAZORPAY_WEBHOOK_SECRET set - rejecting webhooks")
        return False
    try:
        computed = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).digest()
        expected_sig = base64.b64encode(computed).decode("utf-8")
        return hmac.compare_digest(expected_sig, signature)
    except Exception as e:
        logger.exception("Error verifying razorpay signature: %s", e)
        return False

@app.post("/razorpay_webhook")
async def razorpay_webhook(request: Request):
    raw = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    if not signature:
        raise HTTPException(status_code=400, detail="Missing signature")
    if not verify_razorpay_signature(raw, signature, RAZORPAY_WEBHOOK_SECRET):
        logger.warning("Invalid signature for webhook")
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()
    logger.info("Webhook received: event=%s", payload.get("event"))
    # Handle different event shapes (payment.captured, payment.link.paid, payment.authorized etc.)
    event = payload.get("event", "")
    # Try to find payment entity and notes
    notes = {}
    try:
        # Payment object may be at payload['payload']['payment']['entity']
        if payload.get("payload", {}).get("payment", {}):
            ent = payload["payload"]["payment"]["entity"]
            notes = ent.get("notes", {}) or {}
        # Payment link events may include payload.payment.entity too
        elif payload.get("payload", {}).get("payment_link", {}):
            ent = payload["payload"]["payment_link"]["entity"]
            # sometimes the link has 'notes' or you may want payload['payload']['payment']['entity']
            notes = ent.get("notes", {}) or {}
        else:
            # fallback to top-level
            notes = payload.get("payload", {}).get("payment", {}).get("entity", {}).get("notes", {}) or {}
    except Exception:
        notes = {}

    # Extract telegram user id and plan (these must be added to the payment link notes when creating link)
    tg_id = None
    plan = None
    try:
        if "telegram_user_id" in notes:
            tg_id = int(notes.get("telegram_user_id"))
        elif "telegram_id" in notes:
            tg_id = int(notes.get("telegram_id"))
        plan = notes.get("plan")
    except Exception:
        logger.exception("Error parsing notes")

    # Accept only certain events where payment is captured/paid
    if event in ("payment.captured", "payment.authorized", "payment.link.paid", "payment.captured.*", "payment.paid"):
        # record purchase in PURCHASE_LOG
        rec = {
            "time": now_ist().isoformat(),
            "razorpay_event": event,
            "notes": notes,
        }
        if tg_id:
            rec["user_id"] = tg_id
            rec["plan"] = plan
        PURCHASE_LOG.append(rec)
        save_state()
        # if we have a telegram id, create invite and send
        if tg_id and plan:
            try:
                create_invite_and_send(tg_id, plan)
                logger.info("Delivered invites to %s", tg_id)
            except Exception:
                logger.exception("Failed to deliver invites")
        return PlainTextResponse("ok")
    else:
        logger.info("Ignoring event %s", event)
        return PlainTextResponse("ignored")
