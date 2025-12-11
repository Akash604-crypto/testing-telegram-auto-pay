# app.py
import os
import json
import base64
import hmac
import hashlib
import tempfile
import shutil
import logging
import threading
import time
import subprocess
import sys
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse

# ---- Logging ----
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("paymentbot")

# ---- Env (set these in Render) ----
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
VIP_CHANNEL_ID = int(os.getenv("VIP_CHANNEL_ID", "0"))
DARK_CHANNEL_ID = int(os.getenv("DARK_CHANNEL_ID", "0"))
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
DATA_DIR = os.getenv("DATA_DIR", "/data")
DATA_FILE = os.path.join(DATA_DIR, "paymentbot.json")

# payment info defaults (can override via /set_x commands in your bot)
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

def now_ist() -> datetime:
    return datetime.now(IST)

# persistence helpers
def _ensure_data_dir():
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

def _serialize_state():
    return {
        "pending_payments": PENDING_PAYMENTS,
        "purchase_log": [
            {**{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in p.items()}}
            for p in PURCHASE_LOG
        ],
        "known_users": list(KNOWN_USERS),
        "sent_invites": {str(k): v for k, v in SENT_INVITES.items()},
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
    for k, v in sent.items():
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

# Telegram HTTP API helper (safe from webhook context)
def tg_api(method: str, data: dict):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    resp = requests.post(url, json=data, timeout=15)
    try:
        resp.raise_for_status()
    except Exception:
        logger.error("Telegram API error %s: %s", resp.status_code, resp.text)
        raise
    return resp.json()

def create_invite_and_send(user_id: int, plan: str):
    links = {}
    try:
        if plan in ("vip", "both") and VIP_CHANNEL_ID:
            payload = {
                "chat_id": VIP_CHANNEL_ID,
                "member_limit": 1,
                "name": f"user_{user_id}_vip",
                "creates_join_request": False
            }
            r = tg_api("createChatInviteLink", payload)
            link = r.get("result", {}).get("invite_link")
            if link:
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
            if link:
                links["dark"] = link

        if links:
            SENT_INVITES.setdefault(user_id, {}).update(links)
            save_state()
            lines = []
            if "vip" in links:
                lines.append(f"🔑 VIP Channel:\n{links['vip']}")
            if "dark" in links:
                lines.append(f"🕶 Dark Channel:\n{links['dark']}")
            text = "✅ Payment confirmed — here are your access links:\n\n" + "\n\n".join(lines)
            try:
                tg_api("sendMessage", {"chat_id": user_id, "text": text})
            except Exception:
                logger.exception("Failed to send invite message to user")
    except Exception:
        logger.exception("Error creating/sending invites")
    return links

# FastAPI app & webhook
app = FastAPI()

def verify_razorpay_signature(body_bytes: bytes, signature: str, secret: str) -> bool:
    """
    Razorpay X-Razorpay-Signature is base64(hmac_sha256(body, secret)).
    """
    if not secret:
        logger.warning("RAZORPAY_WEBHOOK_SECRET not set; rejecting webhooks")
        return False
    try:
        computed = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).digest()
        expected_sig = base64.b64encode(computed).decode("utf-8")
        return hmac.compare_digest(expected_sig, signature)
    except Exception:
        logger.exception("Error verifying signature")
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
    event = payload.get("event", "")
    logger.info("Webhook received: event=%s", event)

    # find notes (Razorpay payload shapes vary)
    notes = {}
    try:
        if payload.get("payload", {}).get("payment", {}):
            ent = payload["payload"]["payment"]["entity"]
            notes = ent.get("notes", {}) or {}
        elif payload.get("payload", {}).get("payment_link", {}):
            ent = payload["payload"]["payment_link"]["entity"]
            notes = ent.get("notes", {}) or {}
        else:
            notes = payload.get("payload", {}).get("payment", {}).get("entity", {}).get("notes", {}) or {}
    except Exception:
        notes = {}

    # parse relevant notes
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

    # Accept captured/paid events
    if event in ("payment.captured", "payment.authorized", "payment.link.paid", "payment.paid"):
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

# Autosave thread (persist state every 60s)
def _autosave_loop():
    try:
        while True:
            time.sleep(60)
            try:
                save_state()
            except Exception:
                logger.exception("Autosave failed")
    except Exception:
        logger.exception("Autosave thread stopped")

autosave_thr = threading.Thread(target=_autosave_loop, daemon=True)
autosave_thr.start()

# Try to import and run bot.py (if present). This is optional: if bot.py is not available, webhook still works.
def start_bot_in_background():
    """
    Start bot.py as a separate process. This avoids asyncio event-loop-in-thread errors.
    Child stdout/stderr are streamed to the main logger so you can view bot logs in Render.
    """
    try:
        bot_path = Path("bot.py")
        if not bot_path.exists():
            logger.warning("bot.py not found; skipping background bot start.")
            return

        py = sys.executable or "python"
        cmd = [py, str(bot_path)]
        logger.info("Spawning bot process: %s", " ".join(shlex.quote(p) for p in cmd))

        # Start the process; keep pipes so we can capture output
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=os.environ)

        # Stream child's stdout to our logger (daemon thread)
        def _stream_child_output(p):
            try:
                if p.stdout is None:
                    return
                for raw_line in iter(p.stdout.readline, b""):
                    try:
                        line = raw_line.decode("utf-8", "replace").rstrip()
                        if line:
                            logger.info("[bot] %s", line)
                    except Exception:
                        pass
            except Exception:
                logger.exception("Error streaming bot child output")

        t = threading.Thread(target=_stream_child_output, args=(proc,), daemon=True)
        t.start()

    except Exception:
        logger.exception("Could not spawn bot.py process")

# FastAPI startup event — load state and kick bot
@app.on_event("startup")
async def on_startup():
    load_state()
    start_bot_in_background()
    logger.info("Webhook service started; bot background start attempted (if bot.py present).")

# lightweight health endpoint
@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok", "time": now_ist().isoformat()})
