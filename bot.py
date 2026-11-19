"""
bot.py — Shipping Waybill & Label Studio Backend
Runs FastAPI (API for Mini App) + python-telegram-bot (long polling) concurrently.
"""

# ==================== CONFIG ====================
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
ADMIN_ID = 123456789
WEBAPP_URL = "https://your-domain.com"   # HTTPS URL where index.html is served
DB_PATH = "/data/label_studio.db"
PORT = 8000
# ================================================

import os
import re
import io
import base64
import json
import hmac
import hashlib
import sqlite3
import asyncio
import secrets
import csv
from datetime import datetime
from urllib.parse import parse_qsl
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware

from telegram import (
    Update, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    filters, PreCheckoutQueryHandler
)

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, inch
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.graphics.barcode import code128
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.graphics import renderPDF

# ==================== PLANS ====================
PLANS = {
    "basic":    {"credits": 50,   "stars": 200,  "name": "Basic Plan"},
    "starter":  {"credits": 100,  "stars": 350,  "name": "Starter Plan"},
    "pro":      {"credits": 300,  "stars": 750,  "name": "Pro Plan"},
    "business": {"credits": 1000, "stars": 1600, "name": "Business Plan"},
}

# ==================== DATABASE ====================
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            credits INTEGER DEFAULT 5,
            free_used INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            tracking_id TEXT UNIQUE NOT NULL,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER PRIMARY KEY,
            store_name TEXT,
            store_phone TEXT,
            return_address TEXT,
            currency TEXT DEFAULT 'USD',
            logo TEXT,
            qr_mode TEXT DEFAULT 'whatsapp'
        );
        CREATE TABLE IF NOT EXISTS drafts (
            user_id INTEGER PRIMARY KEY,
            data TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_orders_tracking ON orders(tracking_id);
        CREATE INDEX IF NOT EXISTS idx_drafts_updated ON drafts(updated_at);
    """)
    conn.commit()
    conn.close()

# ==================== HELPERS ====================
def sanitize(text) -> str:
    """Remove emojis and unsupported characters before rendering PDF."""
    if text is None:
        return ""
    text = str(text)
    return "".join(c for c in text if (0x20 <= ord(c) <= 0x7E) or (0x0600 <= ord(c) <= 0x06FF)).strip()

def generate_tracking_id() -> str:
    return "WB" + datetime.utcnow().strftime("%y%m%d") + secrets.token_hex(3).upper()

def validate_init_data(init_data: str):
    """Validate Telegram WebApp initData. Returns user dict or None."""
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc_hash, received_hash):
            return None
        user = parsed.get("user")
        return json.loads(user) if user else None
    except Exception:
        return None

def get_user_from_request(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(401, "Invalid initData")
    return user

def ensure_user(user_id: int, name: str = "", username: str = ""):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO users (user_id, name, username, credits, free_used, created_at) VALUES (?, ?, ?, 5, 0, ?)",
            (user_id, sanitize(name), sanitize(username), datetime.utcnow().isoformat()),
        )
        conn.commit()
    conn.close()

def get_settings(user_id: int) -> dict:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM settings WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return {"store_name": "", "store_phone": "", "return_address": "",
                "currency": "USD", "logo": "", "qr_mode": "whatsapp"}
    return dict(row)

def get_credits(user_id: int) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row["credits"] if row else 0

def deduct_credit(user_id: int, count: int = 1) -> bool:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET credits = credits - ? WHERE user_id = ? AND credits >= ?",
                (count, user_id, count))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def add_credits(user_id: int, count: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (count, user_id))
    conn.commit()
    conn.close()

# ==================== PDF GENERATION ====================
def _page_size(size: str):
    if size == "58mm":
        return (58 * mm, 130 * mm)
    if size == "A4":
        return A4
    return (4 * inch, 6 * inch)  # 4x6 thermal

def _cur_sym(currency: str) -> str:
    return {"USD": "$", "EUR": "€", "GBP": "£", "SAR": "SR ", "AED": "AED ", "EGP": "E£ "}.get(currency, currency + " ")

def _draw_label(c: canvas.Canvas, order: dict, settings: dict, tracking_id: str, page_w: float, page_h: float):
    margin = 8 * mm if page_w > 100 * mm else 5 * mm
    top = page_h - margin
    cur_y = top

    currency = settings.get("currency", "USD")
    sym = _cur_sym(currency)
    items = order.get("items", [])
    subtotal = sum(float(it.get("qty", 0)) * float(it.get("price", 0)) for it in items)
    shipping = float(order.get("shipping", 0) or 0)
    prepaid = bool(order.get("prepaid", False))
    deposit = 0 if prepaid else float(order.get("deposit", 0) or 0)
    total_cod = max(0.0, subtotal + shipping - deposit)

# -------- HEADER --------
store_name = sanitize(settings.get("store_name", "")) or "YOUR STORE"
store_phone = sanitize(settings.get("store_phone", ""))
return_addr = sanitize(settings.get("return_address", ""))
logo_data = settings.get("logo", "")

text_start_x = margin

if logo_data and logo_data.startswith("data:image"):
    try:
        header = logo_data.split(",")[1]
        img_bytes = base64.b64decode(header)
        img = ImageReader(io.BytesIO(img_bytes))
        c.drawImage(img, margin, cur_y - 25, width=20*mm, height=20*mm, preserveAspectRatio=True, mask='auto')
        text_start_x = margin + 22 * mm
    except Exception:
        pass

c.setFillColor(colors.HexColor("#0f172a"))
c.setFont("Helvetica-Bold", 13)
c.drawString(text_start_x, cur_y - 12, store_name[:34])

c.setFont("Helvetica", 7)
c.setFillColor(colors.HexColor("#475569"))
meta_y = cur_y - 22

if store_phone:
    c.drawString(text_start_x, meta_y, "Ph: " + store_phone[:40])
    meta_y -= 9
if return_addr:
    c.drawString(text_start_x, meta_y, "Return: " + return_addr[:54])
    meta_y -= 9

    # Horizontal rule
    c.setStrokeColor(colors.HexColor("#0f172a"))
    c.setLineWidth(1.2)
    c.line(margin, cur_y, page_w - margin, cur_y)
    cur_y -= 8

    # -------- RECIPIENT --------
    c.setFillColor(colors.HexColor("#64748b"))
    c.setFont("Helvetica-Bold", 6)
    c.drawString(margin, cur_y, "SHIP TO")
    cur_y -= 10
    c.setFillColor(colors.HexColor("#0f172a"))
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin, cur_y, sanitize(order.get("name", ""))[:44])
    cur_y -= 11
    c.setFont("Helvetica", 8)
    c.setFillColor(colors.HexColor("#334155"))
    c.drawString(margin, cur_y, sanitize(order.get("phone", ""))[:44])
    cur_y -= 9
    addr_line = sanitize(order.get("address", ""))
    city = sanitize(order.get("city", ""))
    full_addr = addr_line + (", " + city if city else "")
    
    max_chars = 55 if page_w < 120 * mm else 80
    while full_addr and cur_y > margin + 100:
        line = full_addr[:max_chars]
        c.drawString(margin, cur_y, line)
        full_addr = full_addr[max_chars:]
        cur_y -= 9
        if not full_addr:
            break

    cur_y -= 6

    # -------- ITEMS TABLE --------
    c.setFillColor(colors.HexColor("#64748b"))
    c.setFont("Helvetica-Bold", 6)
    c.drawString(margin, cur_y, "ITEMS")
    cur_y -= 8

    tbl_left = margin
    tbl_right = page_w - margin
    col_qty_x = tbl_right - 100
    col_price_x = tbl_right - 66
    col_total_x = tbl_right

    # Header
    c.setFillColor(colors.HexColor("#94a3b8"))
    c.setFont("Helvetica-Bold", 6)
    c.drawString(tbl_left, cur_y, "ITEM")
    c.drawRightString(col_qty_x + 30, cur_y, "QTY")
    c.drawRightString(col_price_x + 30, cur_y, "PRICE")
    c.drawRightString(col_total_x, cur_y, "TOTAL")
    cur_y -= 3
    c.setStrokeColor(colors.HexColor("#cbd5e1"))
    c.setLineWidth(0.5)
    c.line(tbl_left, cur_y, tbl_right, cur_y)
    cur_y -= 9

    for it in items:
        if cur_y < margin + 55:
            break
        name = sanitize(it.get("name", ""))
        qty = it.get("qty", 1)
        price = float(it.get("price", 0))
        line_total = qty * price
        c.setFillColor(colors.HexColor("#0f172a"))
        c.setFont("Helvetica", 7)
        c.drawString(tbl_left, cur_y, name[:40])
        c.drawRightString(col_qty_x + 30, cur_y, str(qty))
        c.drawRightString(col_price_x + 30, cur_y, sym + f"{price:.2f}")
        c.drawRightString(col_total_x, cur_y, sym + f"{line_total:.2f}")
        cur_y -= 3
        c.setStrokeColor(colors.HexColor("#e2e8f0"))
        c.line(tbl_left, cur_y, tbl_right, cur_y)
        cur_y -= 8

    cur_y -= 3

    # -------- OPTIONS (hide empties) --------
    weight = order.get("weight")
    notes = sanitize(order.get("notes", ""))
    inspection = order.get("inspection", False)
    opt_parts = []
    if weight: opt_parts.append(f"Weight: {sanitize(str(weight))} kg")
    if notes: opt_parts.append(f"Notes: {notes}")
    if inspection: opt_parts.append("Allow Inspection")

    if opt_parts:
        c.setFillColor(colors.HexColor("#334155"))
        c.setFont("Helvetica", 7)
        for opt in opt_parts:
            c.drawString(margin, cur_y, opt[:90])
            cur_y -= 9
        cur_y -= 3

    # -------- FINANCIAL BOX --------
    box_h = 44 if deposit > 0 else 34
    box_top = cur_y
    c.setFillColor(colors.HexColor("#f1f5f9"))
    c.rect(margin, box_top - box_h, page_w - 2 * margin, box_h, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#334155"))
    c.setFont("Helvetica", 7)
    ln_y = box_top - 10
    c.drawString(margin + 6, ln_y, "Subtotal")
    c.drawRightString(page_w - margin - 6, ln_y, sym + f"{subtotal:.2f}")
    ln_y -= 9
    c.drawString(margin + 6, ln_y, "Shipping")
    c.drawRightString(page_w - margin - 6, ln_y, sym + f"{shipping:.2f}")
    if deposit > 0:
        ln_y -= 9
        c.drawString(margin + 6, ln_y, "Deposit Paid")
        c.drawRightString(page_w - margin - 6, ln_y, "-" + sym + f"{deposit:.2f}")

    # Total
    c.setStrokeColor(colors.HexColor("#0f172a"))
    c.setLineWidth(1.2)
    c.line(margin + 4, box_top - box_h + 15, page_w - margin - 4, box_top - box_h + 15)
    c.setFillColor(colors.HexColor("#0f172a"))
    c.setFont("Helvetica-Bold", 10)
    if prepaid:
        c.setFillColor(colors.HexColor("#16a34a"))
        c.drawString(margin + 6, box_top - box_h + 3, "PREPAID")
        c.drawRightString(page_w - margin - 6, box_top - box_h + 3, "DO NOT COLLECT")
    else:
        c.drawString(margin + 6, box_top - box_h + 3, "TOTAL COD")
        c.drawRightString(page_w - margin - 6, box_top - box_h + 3, sym + f"{total_cod:.2f}")

    cur_y = box_top - box_h - 8

    # -------- BARCODE + QR (footer) --------
    footer_y = margin + 2
    barcode_w = 42 * mm if page_w > 100 * mm else 35 * mm

    try:
        bc = code128.Code128(tracking_id, barHeight=10 * mm, barWidth=0.55)
        bc.drawOn(c, margin, footer_y + 4)
    except Exception:
        c.setFont("Courier", 8)
        c.drawString(margin, footer_y + 4, tracking_id)

    c.setFont("Helvetica", 5)
    c.setFillColor(colors.HexColor("#64748b"))
    c.drawCentredString(margin + barcode_w / 2, footer_y - 2, tracking_id)

    # QR
    qr_size = 16 * mm
    qr_x = page_w - margin - qr_size
    qr_y = footer_y + 2

    qr_mode = settings.get("qr_mode", "whatsapp")
    if qr_mode == "whatsapp":
        phone = sanitize(order.get("phone", "")).replace(" ", "").replace("+", "")
        qr_data = f"https://wa.me/{phone}" if phone else "https://wa.me/"
    else:
        summary_lines = [
            f"Order: {tracking_id}",
            f"Customer: {sanitize(order.get('name', ''))}",
            f"Phone: {sanitize(order.get('phone', ''))}",
            f"Address: {sanitize(order.get('address', ''))}, {sanitize(order.get('city', ''))}",
            "--- Items ---",
        ]
        for it in items:
            summary_lines.append(
                f"{sanitize(it.get('name', ''))} x{it.get('qty', 1)} @ {it.get('price', 0)} = {float(it.get('qty',0))*float(it.get('price',0)):.2f}"
            )
        summary_lines.append(f"Subtotal: {subtotal:.2f} {currency}")
        summary_lines.append(f"Shipping: {shipping:.2f} {currency}")
        if deposit > 0:
            summary_lines.append(f"Deposit: -{deposit:.2f} {currency}")
        summary_lines.append("PREPAID" if prepaid else f"TOTAL COD: {total_cod:.2f} {currency}")
        qr_data = "\n".join(summary_lines)

    try:
        qr_widget = QrCodeWidget(qr_data)
        bounds = qr_widget.getBounds()
        bw = bounds[2] - bounds[0]
        bh = bounds[3] - bounds[1]
        d = Drawing(qr_size, qr_size, transform=[qr_size / bw, 0, 0, qr_size / bh, 0, 0])
        d.add(qr_widget)
        renderPDF.draw(d, c, qr_x, qr_y)
    except Exception:
        c.setFont("Helvetica", 5)
        c.rect(qr_x, qr_y, qr_size, qr_size)
        c.drawCentredString(qr_x + qr_size / 2, qr_y + qr_size / 2, "QR")


def generate_pdf(orders: list, settings: dict, size: str = "4x6") -> bytes:
    buffer = io.BytesIO()
    page_w, page_h = _page_size(size)
    c = canvas.Canvas(buffer, pagesize=(page_w, page_h))

    for order in orders:
        tracking_id = order.get("_tracking_id") or generate_tracking_id()
        _draw_label(c, order, settings, tracking_id, page_w, page_h)
        c.showPage()

    c.save()
    buffer.seek(0)
    return buffer.read()

# ==================== BOT HANDLERS ====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.full_name, user.username or "")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name FROM users WHERE user_id = ?", (user.id,))
    row = cur.fetchone()
    conn.close()
    stored_name = row["name"] if row and row["name"] else ""

    webapp_btn = InlineKeyboardMarkup([[
        InlineKeyboardButton("🚀 Open Label Studio", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])

    if not stored_name or len(stored_name.split()) < 2:
        await update.message.reply_text(
            "👋 Welcome to *Shipping Waybill Studio*!\n\n"
            "Please send me your *full name* (at least 2 words) to complete registration.\n\n"
            "You'll get *5 free labels* upon registration.",
            parse_mode="Markdown",
        )
        context.user_data["awaiting_name"] = True
        return

    await update.message.reply_text(
        f"✅ Welcome back, *{stored_name}*!\n\n"
        f"💳 Your balance: *{get_credits(user.id)} labels*\n\n"
        "Tap the button below to launch the Label Studio app.",
        parse_mode="Markdown",
        reply_markup=webapp_btn,
    )

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Unauthorized.")
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as n FROM users")
    total_users = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) as n FROM orders")
    total_orders = cur.fetchone()["n"]
    conn.close()

    text = (
        "🛡️ *Admin Dashboard*\n\n"
        f"👥 Total Merchants: *{total_users}*\n"
        f"📦 Total Labels Issued: *{total_orders}*\n\n"
        "Commands:\n"
        "`/addcredit <user_id> <amount>` — add credits\n"
        "`/deductcredit <user_id> <amount>` — deduct credits\n"
        "`/broadcast <message>` — send message to all users\n"
        "`/userinfo <user_id>` — view user"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_addcredit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        args = context.args
        if len(args) < 2: raise ValueError
        uid, amt = int(args[0]), int(args[1])
        add_credits(uid, amt)
        await update.message.reply_text(f"✅ Added {amt} credits to user {uid}. New: {get_credits(uid)}")
    except Exception:
        await update.message.reply_text("Usage: /addcredit <user_id> <amount>")

async def cmd_deductcredit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        args = context.args
        if len(args) < 2: raise ValueError
        uid, amt = int(args[0]), int(args[1])
        ok = deduct_credit(uid, amt)
        await update.message.reply_text(
            f"✅ Deducted {amt} from {uid}. New: {get_credits(uid)}" if ok
            else "⚠️ Could not deduct (insufficient balance or invalid user)."
        )
    except Exception:
        await update.message.reply_text("Usage: /deductcredit <user_id> <amount>")

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    msg = " ".join(context.args)
    if not msg:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    users = [r["user_id"] for r in cur.fetchall()]
    conn.close()

    success = 0
    for uid in users:
        try:
            await context.bot.send_message(uid, msg)
            success += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"✅ Broadcast sent to {success}/{len(users)} users.")

async def cmd_userinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        uid = int(context.args[0])
    except Exception:
        await update.message.reply_text("Usage: /userinfo <user_id>")
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (uid,))
    row = cur.fetchone()
    conn.close()
    if not row:
        await update.message.reply_text("User not found.")
        return
    await update.message.reply_text(
        f"👤 User ID: {row['user_id']}\n"
        f"Name: {row['name'] or '-'}\n"
        f"Username: @{row['username'] or '-'}\n"
        f"Credits: {row['credits']}\n"
        f"Joined: {row['created_at']}"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()
    if not text: return

    ensure_user(user.id, user.full_name, user.username or "")

    if context.user_data.get("awaiting_name"):
        parts = text.split()
        if len(parts) < 2:
            await update.message.reply_text("Please send your *full name* (at least 2 words).", parse_mode="Markdown")
            return
        name = sanitize(text)
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET name = ? WHERE user_id = ?", (name, user.id))
        conn.commit()
        conn.close()
        context.user_data["awaiting_name"] = False
        webapp_btn = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 Open Label Studio", web_app=WebAppInfo(url=WEBAPP_URL))
        ]])
        await update.message.reply_text(
            f"✅ Registered as *{name}*!\n🎁 You have *5 free labels*.\n\n"
            "Tap below to start creating labels, or paste any order text here.",
            parse_mode="Markdown",
            reply_markup=webapp_btn,
        )
        return

    parsed = parse_smart_paste(text)
    if parsed and parsed.get("name") and parsed.get("phone"):
        if get_credits(user.id) < 1:
            await update.message.reply_text("⚠️ You have 0 credits left. Please buy more in the app.")
            return

        settings = get_settings(user.id)
        tracking_id = generate_tracking_id()
        parsed["_tracking_id"] = tracking_id

        pdf_bytes = generate_pdf([parsed], settings, "4x6")
        if not deduct_credit(user.id, 1):
            await update.message.reply_text("⚠️ Insufficient credits.")
            return

        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO orders (user_id, tracking_id, data, created_at) VALUES (?, ?, ?, ?)",
            (user.id, tracking_id, json.dumps(parsed, ensure_ascii=False), datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()

        await update.message.reply_document(
            document=io.BytesIO(pdf_bytes),
            filename=f"label_{tracking_id}.pdf",
            caption=f"✅ Label created: `{tracking_id}`\n💳 Remaining credits: *{get_credits(user.id)}*",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        "ℹ️ Please use the app button to create a label, or paste a full order text like:\n\n"
        "`Name: John Doe\nPhone: +1234567890\nAddress: 123 Main St\nCity: New York\nItems: T-Shirt|2|20, Cap|1|10`",
        parse_mode="Markdown",
    )

def parse_smart_paste(text: str) -> dict:
    result = {"name": "", "phone": "", "address": "", "city": "",
              "items": [], "shipping": 0, "deposit": 0,
              "weight": "", "notes": "", "inspection": False}

    patterns = {
        "name": r"^(?:name|full[\s_-]?name|recipient|to|customer)\s*[:=]\s*(.+)$",
        "phone": r"^(?:phone|mobile|tel|telephone|contact)\s*[:=]\s*(.+)$",
        "address": r"^(?:address|addr|street)\s*[:=]\s*(.+)$",
        "city": r"^(?:city|town)\s*[:=]\s*(.+)$",
        "items": r"^(?:items?|products?|order(?:\s*items)?)\s*[:=]\s*(.+)$",
        "shipping": r"^(?:shipping|ship[\s_-]?fee|delivery[\s_-]?fee)\s*[:=]\s*(.+)$",
        "deposit": r"^(?:deposit|paid|advance)\s*[:=]\s*(.+)$",
        "weight": r"^(?:weight|wt)\s*[:=]\s*(.+)$",
        "notes": r"^(?:notes?|comments?|remarks?)\s*[:=]\s*(.+)$",
        "inspection": r"^(?:inspection|allow[\s_-]?inspection)\s*[:=]\s*(.+)$",
    }

    for line in text.splitlines():
        line = line.strip()
        if not line: continue
        for key, pat in patterns.items():
            m = re.match(pat, line, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                if key == "items":
                    result["items"] = _parse_items_list(val)
                elif key in ("shipping", "deposit"):
                    try:
                        result[key] = float(re.sub(r"[^0-9.\-]", "", val) or 0)
                    except Exception:
                        result[key] = 0
                elif key == "inspection":
                    result["inspection"] = bool(re.search(r"yes|true|1|allow|✓", val, re.I))
                else:
                    result[key] = sanitize(val)
                break
    return result

def _parse_items_list(text: str) -> list:
    if not text: return []
    parts = re.split(r"[,;،]\s*", text)
    items = []
    for p in parts:
        p = p.strip()
        if not p: continue
        it = _parse_item_string(p)
        if it: items.append(it)
    return items

def _parse_item_string(s: str):
    s = s.strip()
    if not s: return None
    
    m = re.match(r"^(.+?)\s*\|\s*(\d+(?:\.\d+)?)\s*\|\s*(\d+(?:\.\d+)?)\s*$", s)
    if m: return {"name": sanitize(m.group(1)), "qty": float(m.group(2)) or 1, "price": float(m.group(3)) or 0}
    
    m = re.match(r"^(.+?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*@\s*(\d+(?:\.\d+)?)\s*$", s)
    if m: return {"name": sanitize(m.group(1)), "qty": float(m.group(2)) or 1, "price": float(m.group(3)) or 0}
    
    m = re.match(r"^(\d+(?:\.\d+)?)\s*[x×]\s*(.+?)\s*@\s*(\d+(?:\.\d+)?)\s*$", s)
    if m: return {"name": sanitize(m.group(2)), "qty": float(m.group(1)) or 1, "price": float(m.group(3)) or 0}
    
    if re.search(r"[a-zA-Z\u0600-\u06FF]", s):
        return {"name": sanitize(s), "qty": 1, "price": 0}
    return None

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    doc = update.message.document
    if not doc: return
    fname = (doc.file_name or "").lower()
    if not (fname.endswith(".xlsx") or fname.endswith(".csv")):
        await update.message.reply_text("Please send an .xlsx or .csv file.")
        return
    ensure_user(user.id, user.full_name, user.username or "")
    await update.message.reply_text("📥 Processing your file...")
    try:
        file_obj = await context.bot.get_file(doc.file_id)
        data = await file_obj.download_as_bytearray()

        import openpyxl
        orders = []
        if fname.endswith(".xlsx"):
            wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
            sheet = wb.active
            headers = [str(c.value or "").strip() for c in sheet[1]]
            for row in sheet.iter_rows(min_row=2, values_only=True):
                rowdict = dict(zip(headers, row))
                parsed = _parse_excel_row(rowdict)
                if parsed: orders.append(parsed)
        else:
            reader = csv.DictReader(io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))
            for rowdict in reader:
                parsed = _parse_excel_row(rowdict)
                if parsed: orders.append(parsed)

        if not orders:
            await update.message.reply_text("No valid rows found in the file.")
            return

        credits = get_credits(user.id)
        if credits < len(orders):
            await update.message.reply_text(f"⚠️ You need *{len(orders)}* credits but have *{credits}*.", parse_mode="Markdown")
            return

        settings = get_settings(user.id)
        for o in orders:
            o["_tracking_id"] = generate_tracking_id()

        pdf_bytes = generate_pdf(orders, settings, "4x6")
        deduct_credit(user.id, len(orders))

        conn = get_db()
        cur = conn.cursor()
        for o in orders:
            cur.execute(
                "INSERT INTO orders (user_id, tracking_id, data, created_at) VALUES (?, ?, ?, ?)",
                (user.id, o["_tracking_id"], json.dumps(o, ensure_ascii=False), datetime.utcnow().isoformat()),
            )
        conn.commit()
        conn.close()

        await update.message.reply_document(
            document=io.BytesIO(pdf_bytes),
            filename=f"bulk_labels_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.pdf",
            caption=f"✅ Generated *{len(orders)}* labels\n💳 Remaining: *{get_credits(user.id)}*",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

def _parse_excel_row(rowdict: dict) -> dict:
    def get(*keys):
        for k in rowdict.keys():
            if str(k).strip().lower() in [x.lower() for x in keys]:
                return str(rowdict[k]).strip() if rowdict[k] is not None else ""
        return ""

    name = get("Name", "Full Name", "Customer")
    if not name: return None
    items_raw = get("Items", "Products", "Order Items")
    items = _parse_items_list(items_raw) or [{"name": "", "qty": 1, "price": 0}]
    return {
        "name": sanitize(name),
        "phone": sanitize(get("Phone", "Mobile", "Tel")),
        "address": sanitize(get("Address", "Street")),
        "city": sanitize(get("City", "Town")),
        "items": items,
        "shipping": float(re.sub(r"[^0-9.\-]", "", get("Shipping", "Ship Fee")) or 0),
        "deposit": float(re.sub(r"[^0-9.\-]", "", get("Deposit", "Paid")) or 0),
        "weight": sanitize(get("Weight")),
        "notes": sanitize(get("Notes", "Comments")),
        "inspection": bool(re.search(r"yes|true|1|allow", get("Inspection", "Allow Inspection"), re.I)),
    }

async def handle_precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q: PreCheckoutQuery = update.pre_checkout_query
    try: await q.answer(ok=True)
    except Exception: await q.answer(ok=False, error_message="Payment declined.")

async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    user_id = update.effective_user.id
    plan_key = payload.replace("plan_", "") if payload.startswith("plan_") else payload
    plan = PLANS.get(plan_key)
    if not plan: return
    add_credits(user_id, plan["credits"])
    try:
        await update.message.reply_text(
            f"✅ Payment received!\n⭐ {plan['stars']} stars\n"
            f"💳 Added *{plan['credits']}* credits.\n\nNew balance: *{get_credits(user_id)}*",
            parse_mode="Markdown",
        )
    except Exception: pass


# ==================== BOT APP ====================
bot_app = Application.builder().token(BOT_TOKEN).build()
bot_app.add_handler(CommandHandler("start", cmd_start))
bot_app.add_handler(CommandHandler("admin", cmd_admin))
bot_app.add_handler(CommandHandler("addcredit", cmd_addcredit))
bot_app.add_handler(CommandHandler("deductcredit", cmd_deductcredit))
bot_app.add_handler(CommandHandler("broadcast", cmd_broadcast))
bot_app.add_handler(CommandHandler("userinfo", cmd_userinfo))
bot_app.add_handler(PreCheckoutQueryHandler(handle_precheckout))
bot_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, handle_successful_payment))
bot_app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))


# ==================== FASTAPI APP ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    print("✅ Bot polling started")
    yield
    await bot_app.updater.stop()
    await bot_app.stop()
    await bot_app.shutdown()

api_app = FastAPI(title="Label Studio API", lifespan=lifespan)
api_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@api_app.get("/")
async def root():
    return {"status": "ok", "service": "Label Studio API"}

@api_app.get("/api/user")
async def api_user(request: Request):
    tg_user = get_user_from_request(request)
    ensure_user(tg_user["id"], tg_user.get("first_name", "") + " " + tg_user.get("last_name", ""), tg_user.get("username", ""))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (tg_user["id"],))
    row = cur.fetchone()
    conn.close()
    return {"user_id": row["user_id"], "name": row["name"], "credits": row["credits"], "username": row["username"]}

@api_app.get("/api/settings")
async def api_get_settings(request: Request):
    tg_user = get_user_from_request(request)
    ensure_user(tg_user["id"])
    return get_settings(tg_user["id"])

@api_app.post("/api/settings")
async def api_save_settings(request: Request):
    tg_user = get_user_from_request(request)
    ensure_user(tg_user["id"])
    body = await request.json()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO settings (user_id, store_name, store_phone, return_address, currency, logo, qr_mode)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             store_name=excluded.store_name,
             store_phone=excluded.store_phone,
             return_address=excluded.return_address,
             currency=excluded.currency,
             logo=excluded.logo,
             qr_mode=excluded.qr_mode""",
        (tg_user["id"], sanitize(body.get("store_name", "")), sanitize(body.get("store_phone", "")),
         sanitize(body.get("return_address", "")), body.get("currency", "USD"), body.get("logo", ""), body.get("qr_mode", "whatsapp")),
    )
    conn.commit()
    conn.close()
    return get_settings(tg_user["id"])

@api_app.post("/api/draft")
async def api_save_draft(request: Request):
    tg_user = get_user_from_request(request)
    body = await request.json()
    order = body.get("order", {})
    conn = get_db()
    conn.execute(
        """INSERT INTO drafts (user_id, data, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at""",
        (tg_user["id"], json.dumps(order, ensure_ascii=False), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return {"ok": True}

@api_app.get("/api/draft")
async def api_get_draft(request: Request):
    tg_user = get_user_from_request(request)
    conn = get_db()
    row = conn.execute("SELECT data, updated_at FROM drafts WHERE user_id = ?", (tg_user["id"],)).fetchone()
    conn.close()
    if not row: return {"order": None, "updated_at": None}
    return {"order": json.loads(row["data"]), "updated_at": row["updated_at"]}

@api_app.delete("/api/draft")
async def api_delete_draft(request: Request):
    tg_user = get_user_from_request(request)
    conn = get_db()
    conn.execute("DELETE FROM drafts WHERE user_id = ?", (tg_user["id"],))
    conn.commit()
    conn.close()
    return {"ok": True}

@api_app.post("/api/create-label")
async def api_create_label(request: Request):
    tg_user = get_user_from_request(request)
    body = await request.json()
    order = body.get("order", {})
    size = body.get("size", "4x6")

    order["name"] = sanitize(order.get("name", ""))
    order["phone"] = sanitize(order.get("phone", ""))
    order["address"] = sanitize(order.get("address", ""))
    order["city"] = sanitize(order.get("city", ""))
    order["notes"] = sanitize(order.get("notes", ""))
    order["items"] = [{"name": sanitize(it.get("name", "")), "qty": float(it.get("qty", 1) or 1), "price": float(it.get("price", 0) or 0)} for it in order.get("items", [])]

    if not order["name"] or not order["phone"]: raise HTTPException(400, "Missing required fields")
    if not order["items"]: raise HTTPException(400, "No items")
    if get_credits(tg_user["id"]) < 1: raise HTTPException(402, "Insufficient credits")

    settings = get_settings(tg_user["id"])
    tracking_id = generate_tracking_id()
    order["_tracking_id"] = tracking_id

    pdf_bytes = generate_pdf([order], settings, size)
    if not deduct_credit(tg_user["id"], 1): raise HTTPException(402, "Insufficient credits")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO orders (user_id, tracking_id, data, created_at) VALUES (?, ?, ?, ?)",
                (tg_user["id"], tracking_id, json.dumps(order, ensure_ascii=False), datetime.utcnow().isoformat()))
    conn.execute("DELETE FROM drafts WHERE user_id = ?", (tg_user["id"],))
    conn.commit()
    conn.close()

    async def _send():
        try:
            await bot_app.bot.send_document(
                chat_id=tg_user["id"], document=io.BytesIO(pdf_bytes), filename=f"label_{tracking_id}.pdf",
                caption=f"📄 Label `{tracking_id}`\n💳 Remaining: *{get_credits(tg_user['id'])}*", parse_mode="Markdown")
        except Exception as e: print("send_document error:", e)
    asyncio.create_task(_send())

    return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=label_{tracking_id}.pdf"})

@api_app.post("/api/bulk-labels")
async def api_bulk_labels(request: Request):
    tg_user = get_user_from_request(request)
    body = await request.json()
    orders = body.get("orders", [])
    size = body.get("size", "4x6")

    if not orders: raise HTTPException(400, "No orders")
    if get_credits(tg_user["id"]) < len(orders): raise HTTPException(402, f"Insufficient credits: have {get_credits(tg_user['id'])}, need {len(orders)}")

    settings = get_settings(tg_user["id"])
    prepared = []
    for o in orders:
        o["name"] = sanitize(o.get("name", ""))
        o["phone"] = sanitize(o.get("phone", ""))
        o["address"] = sanitize(o.get("address", ""))
        o["city"] = sanitize(o.get("city", ""))
        o["notes"] = sanitize(o.get("notes", ""))
        o["items"] = [{"name": sanitize(it.get("name", "")), "qty": float(it.get("qty", 1) or 1), "price": float(it.get("price", 0) or 0)} for it in o.get("items", [])]
        o["_tracking_id"] = generate_tracking_id()
        prepared.append(o)

    pdf_bytes = generate_pdf(prepared, settings, size)
    if not deduct_credit(tg_user["id"], len(prepared)): raise HTTPException(402, "Insufficient credits")

    conn = get_db()
    cur = conn.cursor()
    for o in prepared:
        cur.execute("INSERT INTO orders (user_id, tracking_id, data, created_at) VALUES (?, ?, ?, ?)",
                    (tg_user["id"], o["_tracking_id"], json.dumps(o, ensure_ascii=False), datetime.utcnow().isoformat()))
    conn.execute("DELETE FROM drafts WHERE user_id = ?", (tg_user["id"],))
    conn.commit()
    conn.close()

    async def _send():
        try:
            await bot_app.bot.send_document(
                chat_id=tg_user["id"], document=io.BytesIO(pdf_bytes), filename=f"bulk_labels_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.pdf",
                caption=f"📦 *{len(prepared)}* labels generated\n💳 Remaining: *{get_credits(tg_user['id'])}*", parse_mode="Markdown")
        except Exception as e: print("bulk send error:", e)
    asyncio.create_task(_send())

    return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": "attachment; filename=bulk_labels.pdf"})

@api_app.get("/api/history")
async def api_history(request: Request):
    tg_user = get_user_from_request(request)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT tracking_id, data, created_at FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 20", (tg_user["id"],))
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        try: d = json.loads(r["data"])
        except Exception: d = {}
        d.pop("_tracking_id", None)
        result.append({"tracking_id": r["tracking_id"], "data": d, "created_at": r["created_at"]})
    return result

@api_app.post("/api/invoice")
async def api_invoice(request: Request):
    tg_user = get_user_from_request(request)
    body = await request.json()
    plan_key = body.get("plan")
    plan = PLANS.get(plan_key)
    if not plan: raise HTTPException(400, "Invalid plan")
    try:
        link = await bot_app.bot.create_invoice_link(
            title=plan["name"], description=f"{plan['credits']} label credits for Shipping Waybill Studio",
            payload=f"plan_{plan_key}", currency="XTR", prices=[LabeledPrice(label=plan["name"], amount=plan["stars"])])
        return {"url": link, "plan": plan_key}
    except Exception as e: raise HTTPException(500, f"Invoice creation failed: {e}")

@api_app.post("/api/parse-paste")
async def api_parse_paste(request: Request):
    get_user_from_request(request)
    body = await request.json()
    return parse_smart_paste(body.get("text", ""))

# ==================== MAIN ====================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(api_app, host="0.0.0.0", port=port, log_level="info")