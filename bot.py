# bot.py
"""
Polling Telegram payment bot (compatible with your app.py webhook service).
- Persists state to DATA_DIR/paymentbot.json
- Autosaves every 60s on a background thread
- Creates single-use invite links with creates_join_request=True
- Auto-approves join requests for users that have PURCHASE_LOG entries
"""

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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ChatJoinRequestHandler,
)

# ---- logging ----
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---- env / defaults ----
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))

VIP_CHANNEL_ID = int(os.getenv("VIP_CHANNEL_ID", "0"))
DARK_CHANNEL_ID = int(os.getenv("DARK_CHANNEL_ID", "0"))

UPI_ID = os.getenv("UPI_ID", "")
UPI_QR_URL = os.getenv("UPI_QR_URL", "")
UPI_HOW_TO_PAY_LINK = os.getenv("UPI_HOW_TO_PAY_LINK", "")

CRYPTO_ADDRESS = os.getenv("CRYPTO_ADDRESS", "")
CRYPTO_NETWORK = os.getenv("CRYPTO_NETWORK", "BEP20")

REMITLY_INFO = os.getenv("REMITLY_INFO", "")
REMITLY_HOW_TO_PAY_LINK = os.getenv("REMITLY_HOW_TO_PAY_LINK", "")

HELP_BOT_USERNAME = os.getenv("HELP_BOT_USERNAME", "@Dark123222_bot")
HELP_BOT_USERNAME_MD = HELP_BOT_USERNAME.replace("_", "\\_")

# persistence path (Render mount or custom)
DATA_DIR = os.getenv("DATA_DIR", "/data")
DATA_FILE = os.path.join(DATA_DIR, "paymentbot.json")

# timezone
IST = timezone(timedelta(hours=5, minutes=30))

# prices & labels (can be changed with /set_price)
PRICE_CONFIG = {
    "vip": {"upi_inr": 499, "crypto_usd": 6, "remit_inr": 499},
    "dark": {"upi_inr": 1999, "crypto_usd": 24, "remit_inr": 1999},
    "both": {"upi_inr": 1749, "crypto_usd": 21, "remit_inr": 1749},
}
PLAN_LABELS = {"vip": "VIP Channel", "dark": "Dark Channel", "both": "VIP + Dark (Combo 30% OFF)"}

# runtime state (in-memory)
PENDING_PAYMENTS: Dict[str, Dict[str, Any]] = {}
PURCHASE_LOG: list = []
KNOWN_USERS: set = set()
SENT_INVITES: dict = {}  # user_id -> {"vip": link, "dark": link}
CONFIG: dict = {}         # persisted config (channels, payment overrides, etc.)

# ---- helpers ----
def now_ist() -> datetime:
    return datetime.now(IST)

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_CHAT_ID

def _ensure_data_dir():
    try:
        Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("Could not ensure data dir")

def _serialize_state() -> dict:
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

def _deserialize_state(data: dict):
    global PENDING_PAYMENTS, PURCHASE_LOG, KNOWN_USERS, SENT_INVITES, CONFIG
    if not data:
        return
    PENDING_PAYMENTS = data.get("pending_payments", {}) or {}
    PURCHASE_LOG = []
    for p in data.get("purchase_log", []) or []:
        p_copy = dict(p)
        t = p_copy.get("time")
        if isinstance(t, str):
            try:
                p_copy["time"] = datetime.fromisoformat(t)
            except Exception:
                pass
        PURCHASE_LOG.append(p_copy)
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
        logger.info("State saved to %s", DATA_FILE)
    except Exception:
        logger.exception("Failed to save state")

def load_state():
    try:
        if not os.path.exists(DATA_FILE):
            logger.info("No data file found at %s — starting fresh", DATA_FILE)
            return
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _deserialize_state(data)
        logger.info("Loaded state from %s", DATA_FILE)
    except Exception:
        logger.exception("Failed to load state")

# ---- invite creation & delivery ----
async def create_and_store_invites(context: ContextTypes.DEFAULT_TYPE, user_id: int, plan: str, require_join_request: bool = True):
    """
    Create single-use invite links (member_limit=1).
    If require_join_request=True, links will create join requests and bot will need to approve them.
    Stores links in SENT_INVITES and persists.
    Returns dict of created links.
    """
    created = {}
    try:
        user_links = SENT_INVITES.setdefault(user_id, {})
        # VIP
        if plan in ("vip", "both") and VIP_CHANNEL_ID:
            if "vip" not in user_links:
                vip_obj = await context.bot.create_chat_invite_link(
                    chat_id=VIP_CHANNEL_ID,
                    member_limit=1,
                    creates_join_request=require_join_request,
                    name=f"user_{user_id}_vip"
                )
                user_links["vip"] = vip_obj.invite_link
                save_state()
            created["vip"] = user_links.get("vip")
        # DARK
        if plan in ("dark", "both") and DARK_CHANNEL_ID:
            if "dark" not in user_links:
                dark_obj = await context.bot.create_chat_invite_link(
                    chat_id=DARK_CHANNEL_ID,
                    member_limit=1,
                    creates_join_request=require_join_request,
                    name=f"user_{user_id}_dark"
                )
                user_links["dark"] = dark_obj.invite_link
                save_state()
            created["dark"] = user_links.get("dark")
    except Exception:
        logger.exception("Error creating invite links")
    return created

async def send_invites_to_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, plan: str):
    user_links = SENT_INVITES.get(user_id, {})
    links = []
    if plan in ("vip", "both") and user_links.get("vip"):
        links.append(f"🔑 VIP Channel:\n{user_links['vip']}")
    if plan in ("dark", "both") and user_links.get("dark"):
        links.append(f"🕶 Dark Channel:\n{user_links['dark']}")
    if links:
        await context.bot.send_message(chat_id=user_id, text="✅ Access granted!\n\n" + "\n\n".join(links))
        return True
    return False

def get_price(plan: str, method: str):
    cfg = CONFIG.get("price_config", PRICE_CONFIG)
    plan_cfg = cfg.get(plan, PRICE_CONFIG.get(plan, {}))
    if method == "upi":
        return plan_cfg.get("upi_inr"), "INR"
    if method == "crypto":
        return plan_cfg.get("crypto_usd"), "USD"
    if method == "remitly":
        return plan_cfg.get("remit_inr"), "INR"
    return None, ""

# ---- handlers ----
async def handle_chat_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if not req:
        return
    requester = req.from_user
    chat = req.chat
    user_id = requester.id
    chat_id = chat.id
    logger.info("Join request from %s (%s) for chat %s", requester.username, user_id, chat_id)

    def user_has_access_for_chat(uid: int, chat_id: int) -> bool:
        for p in PURCHASE_LOG:
            try:
                if p.get("user_id") == uid:
                    plan = p.get("plan")
                    if plan == "vip" and chat_id == VIP_CHANNEL_ID:
                        return True
                    if plan == "dark" and chat_id == DARK_CHANNEL_ID:
                        return True
                    if plan == "both" and chat_id in (VIP_CHANNEL_ID, DARK_CHANNEL_ID):
                        return True
            except Exception:
                continue
        return False

    allowed = user_has_access_for_chat(user_id, chat_id)
    try:
        if allowed:
            await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
            logger.info("Approved join for %s into %s", user_id, chat_id)
            try:
                await context.bot.send_message(chat_id=user_id, text="✅ Your join request has been approved — welcome!")
            except Exception:
                pass
        else:
            await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
            logger.info("Declined join for %s into %s", user_id, chat_id)
            try:
                await context.bot.send_message(chat_id=user_id, text="❌ We couldn't verify a purchase for this channel. Contact support.")
            except Exception:
                pass
    except Exception:
        logger.exception("Error handling join request")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    KNOWN_USERS.add(user.id)
    save_state()
    keyboard = [
        [InlineKeyboardButton("💎 VIP Channel (₹499)", callback_data="plan_vip")],
        [InlineKeyboardButton("🕶 Dark Channel (₹1999)", callback_data="plan_dark")],
        [InlineKeyboardButton("🔥 Both (30% OFF)", callback_data="plan_both")],
        [InlineKeyboardButton("🆘 Help", callback_data="plan_help")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        "Welcome to Payment Bot 👋\n\n"
        "Choose what you want to unlock:\n"
        "• 💎 VIP Channel – premium content\n"
        "• 🕶 Dark Channel – ultra premium\n"
        "• 🔥 Both – combo offer with 30% OFF\n\n"
        "After you choose a plan, I'll show payment options."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    else:
        await update.callback_query.message.reply_text(text, reply_markup=reply_markup)

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    # plan selection
    if data in ("plan_vip", "plan_dark", "plan_both"):
        plan = data.split("_", 1)[1]
        context.user_data["selected_plan"] = plan
        context.user_data["waiting_for_proof"] = None
        context.user_data["payment_deadline"] = None

        label = PLAN_LABELS.get(plan, plan.upper())
        upi_price, _ = get_price(plan, "upi")
        crypto_price, _ = get_price(plan, "crypto")
        remit_price, _ = get_price(plan, "remitly")

        keyboard = [
            [InlineKeyboardButton(f"💳 UPI (₹{upi_price})", callback_data="pay_upi")],
            [InlineKeyboardButton(f"🪙 Crypto (${crypto_price})", callback_data="pay_crypto")],
            [InlineKeyboardButton(f"🌍 Remitly (₹{remit_price})", callback_data="pay_remitly")],
            [InlineKeyboardButton("⬅ Back", callback_data="back_start")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = f"You selected: *{label}*\n\nChoose your payment method below:"
        try:
            await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        except Exception:
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        return

    # help
    if data == "plan_help":
        help_text = ("🆘 *Help & Support*\n\n" f"For assistance, contact: {HELP_BOT_USERNAME_MD}\n\nType /start anytime to restart.")
        try:
            await query.message.edit_text(help_text, parse_mode="Markdown")
        except Exception:
            await query.message.reply_text(help_text, parse_mode="Markdown")
        return

    if data == "back_start":
        fake_update = Update(update.update_id, message=update.effective_message)
        await start(fake_update, context)
        return

    user_plan = context.user_data.get("selected_plan")
    if data in ("pay_upi", "pay_crypto", "pay_remitly") and not user_plan:
        await query.message.reply_text("First choose a plan with /start before selecting payment method.")
        return

    if data in ("pay_upi", "pay_crypto", "pay_remitly"):
        method_map = {"pay_upi": "upi", "pay_crypto": "crypto", "pay_remitly": "remitly"}
        method = method_map[data]
        context.user_data["waiting_for_proof"] = method

        amount, currency = get_price(user_plan, method)
        label = PLAN_LABELS.get(user_plan, user_plan.upper())

        deadline = now_ist() + timedelta(minutes=30)
        context.user_data["payment_deadline"] = deadline.timestamp()
        deadline_str = deadline.strftime("%d %b %Y, %I:%M %p IST")

        if method == "upi":
            msg = (
                "🧾 *UPI Payment Instructions*\n\n"
                f"Plan: *{label}*\n"
                f"Amount: *₹{amount}*\n\n"
                f"UPI ID: `{UPI_ID}`\n\n"
                "1️⃣ Open any UPI app (GPay, PhonePe, Paytm, etc.)\n"
                "2️⃣ Choose *Scan & Pay* or *Pay UPI ID*\n"
                "3️⃣ Either scan the QR image below or pay directly to the UPI ID above.\n"
                "4️⃣ Enter the amount shown above and confirm.\n\n"
                f"If you're confused, see this guide: {UPI_HOW_TO_PAY_LINK}\n\n"
                f"⏳ Time limit: until *{deadline_str}*\n\n"
                "After payment send screenshot/photo here plus optional UTR."
            )
            await query.message.reply_text(msg, parse_mode="Markdown")
            if UPI_QR_URL:
                await query.message.reply_photo(photo=UPI_QR_URL, caption=f"📷 Scan this QR to pay.\nUPI ID: `{UPI_ID}`", parse_mode="Markdown")
        elif method == "crypto":
            msg = (
                "🪙 *Crypto Payment Instructions*\n\n"
                f"Plan: *{label}*\n"
                f"Amount: *${amount}*\n\n"
                f"Network: `{CRYPTO_NETWORK}`\n"
                f"Address: `{CRYPTO_ADDRESS}`\n\n"
                f"⏳ Time limit: until *{deadline_str}*\n\n"
                "After payment send screenshot/photo + TXID here."
            )
            await query.message.reply_text(msg, parse_mode="Markdown")
        else:
            msg = (
                "🌍 *Remitly Payment Instructions*\n\n"
                f"Plan: *{label}*\n"
                f"Amount: *₹{amount}*\n\n"
                f"Extra info: {REMITLY_INFO}\n\n"
                f"⏳ Time limit: until *{deadline_str}*\n\n"
                "After payment send screenshot/photo here."
            )
            await query.message.reply_text(msg, parse_mode="Markdown")
        return

    # admin approve/decline/sendlink
    if data.startswith("approve:") or data.startswith("decline:") or data.startswith("sendlink:"):
        action, payment_id = data.split(":", 1)
        payment = PENDING_PAYMENTS.get(payment_id)
        if query.from_user.id != ADMIN_CHAT_ID:
            await query.answer("Only admin can use this.", show_alert=True)
            return
        if not payment:
            await query.message.reply_text("⚠️ This payment request was not found or already processed.")
            return

        user_id = payment["user_id"]
        plan = payment["plan"]
        method = payment["method"]
        amount = payment["amount"]
        currency = payment["currency"]
        username = payment.get("username", "")

        if action == "approve":
            PURCHASE_LOG.append({
                "time": now_ist(),
                "user_id": user_id,
                "username": username,
                "plan": plan,
                "method": method,
                "amount": amount,
                "currency": currency,
            })
            save_state()
            # create invite links that require join request (so the user clicks link and sends join request)
            links = await create_and_store_invites(context, user_id, plan, require_join_request=True)
            # send admin a button to finally send the link to the user
            kb = [
                [
                    InlineKeyboardButton("📤 Send access link to user", callback_data=f"sendlink:{payment_id}"),
                    InlineKeyboardButton("❌ Decline", callback_data=f"decline:{payment_id}"),
                ]
            ]
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=(f"✅ Payment approved for user {user_id}.\n\nClick to send single-use access link to the user (one-time)."), reply_markup=InlineKeyboardMarkup(kb))
            await query.message.reply_text(f"✅ Approved payment (ID: {payment_id}). Admin must click send to deliver link.")
            payment["invite_created"] = True
            payment["invite_links"] = links
            PENDING_PAYMENTS[payment_id] = payment
            save_state()
            return

        if action == "sendlink":
            sent = await send_invites_to_user(context, user_id, plan)
            if sent:
                await query.message.reply_text(f"✅ Invite sent to user {user_id}.")
                payment["invite_sent"] = True
                PENDING_PAYMENTS.pop(payment_id, None)
                save_state()
            else:
                await query.message.reply_text("⚠️ No invite links available for this user; try re-creating them.")
            return

        if action == "decline":
            try:
                await context.bot.send_message(chat_id=user_id, text=("❌ Your payment could not be verified.\nIf this is a mistake, please send a clearer screenshot or contact support: " + HELP_BOT_USERNAME))
            except Exception:
                logger.exception("Can't send decline message")
            await query.message.reply_text(f"❌ Declined payment (ID: {payment_id})")
            PENDING_PAYMENTS.pop(payment_id, None)
            save_state()
            return

async def handle_payment_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message

    method = context.user_data.get("waiting_for_proof")
    plan = context.user_data.get("selected_plan")
    if not method or not plan:
        return

    amount, currency = get_price(plan, method)
    payment_id = str(message.message_id) + "_" + str(int(datetime.now().timestamp()))
    PENDING_PAYMENTS[payment_id] = {
        "user_id": user.id,
        "username": user.username or "",
        "plan": plan,
        "method": method,
        "amount": amount,
        "currency": currency,
    }
    save_state()

    try:
        await context.bot.forward_message(chat_id=ADMIN_CHAT_ID, from_chat_id=chat.id, message_id=message.message_id)
    except Exception:
        logger.exception("Forwarding failed")

    kb = [[InlineKeyboardButton("✅ Approve", callback_data=f"approve:{payment_id}"), InlineKeyboardButton("❌ Decline", callback_data=f"decline:{payment_id}")]]
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=(f"💰 New payment request\nFrom: @{user.username or 'NoUsername'} (ID: {user.id})\nPlan: {PLAN_LABELS.get(plan, plan)}\nMethod: {method.upper()}\nAmount: {amount} {currency}\nPayment ID: {payment_id}\n\nCheck forwarded message and choose:"), reply_markup=InlineKeyboardMarkup(kb))
    await message.reply_text("✅ Payment proof received. We'll verify and send access after approval.")

async def warn_text_not_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    method = context.user_data.get("waiting_for_proof")
    plan = context.user_data.get("selected_plan")
    if not method or not plan:
        return
    await update.message.reply_text("⚠️ Please send a screenshot/photo or document of your payment only. Plain text messages cannot be verified.", parse_mode="Markdown")

# -- admin commands (broadcast, income, set_price, set_upi, set_crypto, set_remitly, set_vip, set_dark) --
async def set_vip_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global VIP_CHANNEL_ID
    user = update.effective_user
    if not is_admin(user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /set_vip <channel_id>")
        return
    try:
        VIP_CHANNEL_ID = int(context.args[0])
        CONFIG.setdefault("channels", {})["vip"] = VIP_CHANNEL_ID
        save_state()
        await update.message.reply_text(f"VIP_CHANNEL_ID updated to {VIP_CHANNEL_ID}")
    except ValueError:
        await update.message.reply_text("channel_id must be an integer (e.g. -1001234567890)")

async def set_dark_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DARK_CHANNEL_ID
    user = update.effective_user
    if not is_admin(user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /set_dark <channel_id>")
        return
    try:
        DARK_CHANNEL_ID = int(context.args[0])
        CONFIG.setdefault("channels", {})["dark"] = DARK_CHANNEL_ID
        save_state()
        await update.message.reply_text(f"DARK_CHANNEL_ID updated to {DARK_CHANNEL_ID}")
    except ValueError:
        await update.message.reply_text("channel_id must be an integer (e.g. -1009876543210)")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast your message text")
        return
    text = " ".join(context.args)
    sent = 0
    failed = 0
    for uid in KNOWN_USERS:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"Broadcast done.\n✅ Sent: {sent}\n❌ Failed: {failed}")

async def income(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return
    mode = "today"
    if context.args:
        mode = context.args[0].lower()
    now = now_ist()
    if mode == "yesterday":
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        label = "Yesterday"
    elif mode in ("7d", "7days", "last7"):
        end = now
        start = now - timedelta(days=7)
        label = "Last 7 days"
    else:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        label = "Today"
    total_inr = 0
    total_usd = 0
    count = 0
    for p in PURCHASE_LOG:
        t = p.get("time")
        if isinstance(t, str):
            try:
                t = datetime.fromisoformat(t)
            except Exception:
                continue
        if start <= t < end:
            count += 1
            if p.get("currency") == "INR":
                total_inr += p.get("amount") or 0
            elif p.get("currency") == "USD":
                total_usd += p.get("amount") or 0
    msg = (f"📊 *Income Insights – {label}*\n\n"
           f"Total orders: *{count}*\n"
           f"INR collected: *₹{total_inr}*\n"
           f"USD collected (crypto): *${total_usd}*\n\n"
           "_Note: stats persist between restarts._")
    await update.message.reply_text(msg, parse_mode="Markdown")

async def set_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return
    if len(context.args) != 3:
        await update.message.reply_text("Usage: /set_price <vip|dark|both> <upi|crypto|remitly> <amount>")
        return
    plan, method, amount_str = context.args
    plan = plan.lower()
    method = method.lower()
    if plan not in PRICE_CONFIG or method not in ("upi", "crypto", "remitly"):
        await update.message.reply_text("Invalid plan or method.")
        return
    try:
        amount = float(amount_str)
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return
    cfg = CONFIG.setdefault("price_config", {})
    plan_cfg = cfg.setdefault(plan, PRICE_CONFIG.get(plan, {})).copy()
    if method == "upi":
        plan_cfg["upi_inr"] = amount
    elif method == "crypto":
        plan_cfg["crypto_usd"] = amount
    else:
        plan_cfg["remit_inr"] = amount
    cfg[plan] = plan_cfg
    CONFIG["price_config"] = cfg
    save_state()
    await update.message.reply_text(f"Updated price for {PLAN_LABELS.get(plan, plan)} [{method}] to {amount}.")

async def set_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global UPI_ID
    user = update.effective_user
    if not is_admin(user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /set_upi <upi_id>")
        return
    UPI_ID = context.args[0]
    CONFIG.setdefault("payment", {})["upi_id"] = UPI_ID
    save_state()
    await update.message.reply_text(f"UPI ID updated to: {UPI_ID}")

async def set_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CRYPTO_ADDRESS
    user = update.effective_user
    if not is_admin(user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /set_crypto <address>")
        return
    CRYPTO_ADDRESS = context.args[0]
    CONFIG.setdefault("payment", {})["crypto_address"] = CRYPTO_ADDRESS
    save_state()
    await update.message.reply_text(f"Crypto address updated to: {CRYPTO_ADDRESS}")

async def set_remitly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global REMITLY_INFO
    user = update.effective_user
    if not is_admin(user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /set_remitly <text>")
        return
    REMITLY_INFO = " ".join(context.args)
    CONFIG.setdefault("payment", {})["remitly_info"] = REMITLY_INFO
    save_state()
    await update.message.reply_text(f"Remitly info updated.")

# ---- main / startup ----
def main():
    _ensure_data_dir()
    load_state()

    # apply CONFIG overrides if present
    global VIP_CHANNEL_ID, DARK_CHANNEL_ID, UPI_ID, CRYPTO_ADDRESS, REMITLY_INFO
    if CONFIG.get("channels", {}).get("vip"):
        try:
            VIP_CHANNEL_ID = int(CONFIG["channels"]["vip"])
        except Exception:
            pass
    if CONFIG.get("channels", {}).get("dark"):
        try:
            DARK_CHANNEL_ID = int(CONFIG["channels"]["dark"])
        except Exception:
            pass
    if CONFIG.get("payment", {}).get("upi_id"):
        UPI_ID = CONFIG["payment"]["upi_id"]
    if CONFIG.get("payment", {}).get("crypto_address"):
        CRYPTO_ADDRESS = CONFIG["payment"]["crypto_address"]
    if CONFIG.get("payment", {}).get("remitly_info"):
        REMITLY_INFO = CONFIG["payment"]["remitly_info"]

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing")
    if not ADMIN_CHAT_ID:
        raise RuntimeError("ADMIN_CHAT_ID missing")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # user handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_buttons))
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, handle_payment_proof))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, warn_text_not_allowed))

    # admin handlers
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("income", income))
    app.add_handler(CommandHandler("set_price", set_price))
    app.add_handler(CommandHandler("set_upi", set_upi))
    app.add_handler(CommandHandler("set_crypto", set_crypto))
    app.add_handler(CommandHandler("set_remitly", set_remitly))
    app.add_handler(CommandHandler("set_vip", set_vip_channel))
    app.add_handler(CommandHandler("set_dark", set_dark_channel))

    # join requests: bot will auto-approve only if PURCHASE_LOG shows user's purchase
    app.add_handler(ChatJoinRequestHandler(handle_chat_join_request))

    # autosave background thread (daemon)
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

    thr = threading.Thread(target=_autosave_loop, daemon=True)
    thr.start()

    try:
        logger.info("Starting bot (polling)...")
        app.run_polling()
    finally:
        try:
            save_state()
        except Exception:
            logger.exception("Final save failed")

if __name__ == "__main__":
    main()
