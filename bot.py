import os
import re
import time
import logging
import asyncio
from pathlib import Path
from typing import Tuple, Optional, Dict

import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
)

# Загружаем .env из папки скрипта (Render тоже прокинет переменные окружения)
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
API_URL = os.getenv("API_URL", "https://giganoob.com/data/html/users_snapshot.json").strip()
CHANNEL = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()   # @username или -100...
CACHE_TTL = int(os.getenv("CACHE_TTL", "30"))            # кэш /users, сек
CRYPTO_CACHE_TTL = int(os.getenv("CRYPTO_CACHE_TTL", "30"))  # кэш /crypto, сек

# Webhook/сервер для Render
PORT = int(os.getenv("PORT", "10000"))  # Render прокидывает PORT
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "tg-webhook").strip()
BASE_URL = (os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

# Кнопки меню
KB_START = InlineKeyboardMarkup([
    [InlineKeyboardButton("Giga users", callback_data="menu_users")],
    [InlineKeyboardButton("Crypto price", callback_data="menu_crypto")],
    [InlineKeyboardButton("ChatID", callback_data="menu_chatid")],
])

def KB_USERS():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⟳ Обновить", callback_data="refresh_users")],
        [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")],
    ])

def KB_CRYPTO():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⟳ Обновить", callback_data="refresh_crypto")],
        [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")],
    ])

def KB_BACK():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")],
    ])

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; UsersJuicedBot/1.0)",
    "Referer": "https://giganoob.com/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
CRYPTO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CryptoPrices/1.0)",
    "Accept": "application/json",
}

# ========= Утилиты =========
def parse_channel_id(val: str) -> str | int | None:
    if not val:
        return None
    v = val.strip()
    if v.startswith("-100") and v[4:].isdigit():
        try:
            return int(v)
        except Exception:
            return v
    return v

def fmt_int(n: Optional[int]) -> str:
    return f"{n:,}".replace(",", " ") if isinstance(n, int) else "—"

def fmt_usd(n: Optional[float]) -> str:
    if n is None:
        return "—"
    return f"{n:,.0f} $".replace(",", " ")

def fmt_rub(n: Optional[float]) -> str:
    if n is None:
        return "—"
    return f"{n:,.2f} ₽".replace(",", " ")

def cache_busted(url: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}t={int(time.time() * 1000)}"

# ========= /users =========
_fetch_lock = asyncio.Lock()
_cache_ts = 0.0
_cache_data: Tuple[Optional[int], Optional[int]] = (None, None)

async def fetch_stats() -> Tuple[Optional[int], Optional[int]]:
    url = cache_busted(API_URL)
    async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
    users = juiced = None
    if isinstance(data, dict):
        if isinstance(data.get("totals"), dict):
            users = data["totals"].get("users")
            juiced = data["totals"].get("juiced")
        else:
            users = data.get("users")
            juiced = data.get("juiced")
    try: users = int(users) if users is not None else None
    except Exception: users = None
    try: juiced = int(juiced) if juiced is not None else None
    except Exception: juiced = None
    return users, juiced

async def get_stats_cached(force: bool = False) -> Tuple[Optional[int], Optional[int]]:
    global _cache_ts, _cache_data
    now = time.monotonic()
    if not force and (now - _cache_ts) < CACHE_TTL and all(v is not None for v in _cache_data):
        return _cache_data
    async with _fetch_lock:
        now = time.monotonic()
        if not force and (now - _cache_ts) < CACHE_TTL and all(v is not None for v in _cache_data):
            return _cache_data
        users, juiced = await fetch_stats()
        _cache_data = (users, juiced)
        _cache_ts = time.monotonic()
        return users, juiced

def format_users_message(users, juiced) -> str:
    pct = None
    if isinstance(users, int) and isinstance(juiced, int) and users > 0:
        pct = juiced / users * 100.0
    if pct is not None:
        return f"Users: {fmt_int(users)}\nJuiced: {fmt_int(juiced)} ({pct:.1f}%)"
    else:
        return f"Users: {fmt_int(users)}\nJuiced: {fmt_int(juiced)}"

async def send_users(chat_id: int | str, bot) -> None:
    users, juiced = await get_stats_cached(force=False)
    msg = format_users_message(users, juiced)
    await bot.send_message(chat_id=chat_id, text=msg, reply_markup=KB_USERS(), disable_web_page_preview=True)

async def handle_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await send_users(update.effective_chat.id, context.bot)
    except Exception:
        log.exception("handle_users failed")
        await update.effective_message.reply_text("Не удалось получить данные с сайта.")

async def on_refresh_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await q.answer("Обновляю…", cache_time=0)
    except Exception:
        pass
    try:
        users, juiced = await get_stats_cached(force=True)
        msg = format_users_message(users, juiced)
        try:
            await q.edit_message_text(msg, reply_markup=KB_USERS())
        except Exception:
            await q.message.reply_text(msg, reply_markup=KB_USERS())
    except Exception:
        log.exception("refresh users failed")
        await q.message.reply_text("Не удалось обновить данные.")

# ========= /crypto =========
_market_cache_ts = 0.0
_market_cache: Dict[str, Optional[float]] = {"BTC": None, "ETH": None, "USD_RUB": None}

async def fetch_crypto_prices() -> Dict[str, Optional[float]]:
    prices: Dict[str, Optional[float]] = {"BTC": None, "ETH": None}
    async with httpx.AsyncClient(timeout=15, headers=CRYPTO_HEADERS, follow_redirects=True) as client:
        try:
            b_btc = await client.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "BTCUSDT"})
            b_eth = await client.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "ETHUSDT"})
            if b_btc.status_code == 200 and b_eth.status_code == 200:
                prices["BTC"] = float(b_btc.json()["price"])
                prices["ETH"] = float(b_eth.json()["price"])
                return prices
        except Exception:
            pass
        try:
            c_btc = await client.get("https://api.coinbase.com/v2/prices/BTC-USD/spot")
            c_eth = await client.get("https://api.coinbase.com/v2/prices/ETH-USD/spot")
            if c_btc.status_code == 200 and c_eth.status_code == 200:
                prices["BTC"] = float(c_btc.json()["data"]["amount"])
                prices["ETH"] = float(c_eth.json()["data"]["amount"])
                return prices
        except Exception:
            pass
    return prices

async def fetch_usd_rub() -> Optional[float]:
    async with httpx.AsyncClient(timeout=15, headers=CRYPTO_HEADERS, follow_redirects=True) as client:
        try:
            r = await client.get("https://api.exchangerate.host/latest", params={"base": "USD", "symbols": "RUB"})
            if r.status_code == 200:
                rate = r.json().get("rates", {}).get("RUB")
                if isinstance(rate, (int, float)):
                    return float(rate)
        except Exception:
            pass
        try:
            r = await client.get("https://open.er-api.com/v6/latest/USD")
            if r.status_code == 200:
                rate = r.json().get("rates", {}).get("RUB")
                if isinstance(rate, (int, float)):
                    return float(rate)
        except Exception:
            pass
    return None

async def get_market_cached(force: bool = False) -> Dict[str, Optional[float]]:
    global _market_cache_ts, _market_cache
    now = time.monotonic()
    if not force and (now - _market_cache_ts) < CRYPTO_CACHE_TTL and any(_market_cache.values()):
        return _market_cache
    prices, usd_rub = await asyncio.gather(fetch_crypto_prices(), fetch_usd_rub())
    _market_cache = {"BTC": prices.get("BTC"), "ETH": prices.get("ETH"), "USD_RUB": usd_rub}
    _market_cache_ts = time.monotonic()
    return _market_cache

async def handle_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = await get_market_cached(force=False)
        btc, eth, usd_rub = data.get("BTC"), data.get("ETH"), data.get("USD_RUB")
        text = f"BTC: {fmt_usd(btc)}\nETH: {fmt_usd(eth)}\nUSD/RUB: {fmt_rub(usd_rub)}"
        await update.effective_message.reply_text(text, reply_markup=KB_CRYPTO())
    except Exception:
        log.exception("/crypto failed")
        await update.effective_message.reply_text("Не удалось получить цены BTC/ETH или курс USD/RUB.")

async def on_refresh_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await q.answer("Обновляю…", cache_time=0)
    except Exception:
        pass
    try:
        data = await get_market_cached(force=True)
        btc, eth, usd_rub = data.get("BTC"), data.get("ETH"), data.get("USD_RUB")
        msg = f"BTC: {fmt_usd(btc)}\nETH: {fmt_usd(eth)}\nUSD/RUB: {fmt_rub(usd_rub)}"
        try:
            await q.edit_message_text(msg, reply_markup=KB_CRYPTO())
        except Exception:
            await q.message.reply_text(msg, reply_markup=KB_CRYPTO())
    except Exception:
        log.exception("refresh crypto failed")
        await q.message.reply_text("Не удалось обновить цены/курс.")

# ========= /start и ChatID =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Выберите действие:", reply_markup=KB_START)

async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"chat_id: {chat.id}\n"
        f"type: {chat.type}\n"
        f"title: {chat.title or '-'}",
        reply_markup=KB_BACK()
    )

async def on_start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    try:
        await q.answer()
    except Exception:
        pass
    chat_id = update.effective_chat.id
    if data == "menu_users":
        await send_users(chat_id, context.bot)
    elif data == "menu_crypto":
        await handle_crypto(update, context)
    elif data == "menu_chatid":
        chat = update.effective_chat
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"chat_id: {chat.id}\n"
                 f"type: {chat.type}\n"
                 f"title: {chat.title or '-'}",
            reply_markup=KB_BACK()
        )

async def on_back_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    # Пробуем отредактировать текущее сообщение, если нельзя — шлём новое
    try:
        await q.edit_message_text("Выберите действие:", reply_markup=KB_START)
    except Exception:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Выберите действие:", reply_markup=KB_START)

# ========= запуск (webhook) =========
def main():
    if not TOKEN:
        raise SystemExit("Заполните TELEGRAM_TOKEN в .env")
    if not BASE_URL:
        raise SystemExit("Нужен BASE_URL или RENDER_EXTERNAL_URL (Render задаёт автоматически)")

    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("users", handle_users))
    app.add_handler(CommandHandler("crypto", handle_crypto))
    app.add_handler(CommandHandler("chatid", chatid))

    # Кнопки
    app.add_handler(CallbackQueryHandler(on_refresh_users, pattern=r"^refresh_users$"))
    app.add_handler(CallbackQueryHandler(on_refresh_crypto, pattern=r"^refresh_crypto$"))
    app.add_handler(CallbackQueryHandler(on_start_menu, pattern=r"^menu_(users|crypto|chatid)$"))
    app.add_handler(CallbackQueryHandler(on_back_menu, pattern=r"^back_menu$"))
    # Поддержка старых сообщений с callback="refresh"
    app.add_handler(CallbackQueryHandler(on_refresh_users, pattern=r"^refresh$"))

    # Алиасы через текст
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^!users\b", re.IGNORECASE)), handle_users))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^!crypto\b", re.IGNORECASE)), handle_crypto))

    webhook_url = f"{BASE_URL}/{WEBHOOK_PATH}"
    log.info("Starting webhook on port %s, path '/%s', webhook_url=%s", PORT, WEBHOOK_PATH, webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
