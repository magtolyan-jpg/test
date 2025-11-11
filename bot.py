import os
import re
import io
import time
import logging
import asyncio
from pathlib import Path
from typing import Tuple, Optional, Dict, List

import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
)

# Load env
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
API_URL = os.getenv("API_URL", "https://giganoob.com/data/html/users_snapshot.json").strip()
CACHE_TTL = int(os.getenv("CACHE_TTL", "30"))
CRYPTO_CACHE_TTL = int(os.getenv("CRYPTO_CACHE_TTL", "30"))

# Webhook params (Render)
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "tg-webhook").strip()
BASE_URL = (os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

# ==== Keyboards ====
KB_START = InlineKeyboardMarkup([
    [InlineKeyboardButton("All stats", callback_data="menu_snapshot")],
    [InlineKeyboardButton("Giga users", callback_data="menu_users")],
    [InlineKeyboardButton("Crypto price", callback_data="menu_crypto")],
    [InlineKeyboardButton("Crypto charts", callback_data="menu_charts")],
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

def KB_SNAPSHOT():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⟳ Обновить", callback_data="refresh_snapshot")],
        [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")],
    ])

def KB_CHARTS():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⟳ Обновить", callback_data="refresh_charts")],
        [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")],
    ])

def KB_BACK():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]])

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

# ==== Utils ====
def fmt_int(n: Optional[int]) -> str:
    return f"{n:,}".replace(",", " ") if isinstance(n, int) else "—"

def fmt_usd(n: Optional[float]) -> str:
    if n is None: return "—"
    return f"{n:,.0f} $".replace(",", " ")

def fmt_usd_delta(d: Optional[float]) -> str:
    if d is None: return "—"
    sign = "+" if d >= 0 else ""
    return f"{sign}{abs(d):,.0f} $".replace(",", " ")

def fmt_pct(p: Optional[float]) -> str:
    if p is None: return "—"
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.2f}%"

def fmt_rub(n: Optional[float]) -> str:
    if n is None: return "—"
    return f"{n:,.2f} ₽".replace(",", " ")

def cache_busted(url: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}t={int(time.time() * 1000)}"

# ==== /users ====
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
            users = data.get("users"); juiced = data.get("juiced")
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
        _cache_data = (users, juiced); _cache_ts = time.monotonic()
        return users, juiced

def format_users_message(users, juiced) -> str:
    pct = None
    if isinstance(users, int) and isinstance(juiced, int) and users > 0:
        pct = juiced / users * 100.0
    return (f"Users: {fmt_int(users)}\nJuiced: {fmt_int(juiced)} ({pct:.1f}%)"
            if pct is not None else
            f"Users: {fmt_int(users)}\nJuiced: {fmt_int(juiced)}")

async def send_users(chat_id: int | str, bot) -> None:
    users, juiced = await get_stats_cached(force=False)
    await bot.send_message(chat_id=chat_id, text=format_users_message(users, juiced),
                           reply_markup=KB_USERS(), disable_web_page_preview=True)

async def handle_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: await send_users(update.effective_chat.id, context.bot)
    except Exception:
        log.exception("handle_users failed")
        await update.effective_message.reply_text("Не удалось получить данные с сайта.")

async def on_refresh_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try: await q.answer("Обновляю…", cache_time=0)
    except Exception: pass
    try:
        users, juiced = await get_stats_cached(force=True)
        msg = format_users_message(users, juiced)
        try: await q.edit_message_text(msg, reply_markup=KB_USERS())
        except Exception: await q.message.reply_text(msg, reply_markup=KB_USERS())
    except Exception:
        log.exception("refresh users failed")
        await q.message.reply_text("Не удалось обновить данные.")

# ==== Crypto prices & helpers ====
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
        except Exception: pass
        try:
            c_btc = await client.get("https://api.coinbase.com/v2/prices/BTC-USD/spot")
            c_eth = await client.get("https://api.coinbase.com/v2/prices/ETH-USD/spot")
            if c_btc.status_code == 200 and c_eth.status_code == 200:
                prices["BTC"] = float(c_btc.json()["data"]["amount"])
                prices["ETH"] = float(c_eth.json()["data"]["amount"])
                return prices
        except Exception: pass
    return prices

async def fetch_usd_rub() -> Optional[float]:
    async with httpx.AsyncClient(timeout=15, headers=CRYPTO_HEADERS, follow_redirects=True) as client:
        try:
            r = await client.get("https://api.exchangerate.host/latest", params={"base": "USD", "symbols": "RUB"})
            if r.status_code == 200:
                rate = r.json().get("rates", {}).get("RUB")
                if isinstance(rate, (int, float)): return float(rate)
        except Exception: pass
        try:
            r = await client.get("https://open.er-api.com/v6/latest/USD")
            if r.status_code == 200:
                rate = r.json().get("rates", {}).get("RUB")
                if isinstance(rate, (int, float)): return float(rate)
        except Exception: pass
    return None

async def fetch_24h_change(symbol: str) -> Optional[float]:
    # Binance 24h ticker: priceChangePercent
    url = "https://api.binance.com/api/v3/ticker/24hr"
    async with httpx.AsyncClient(timeout=15, headers=CRYPTO_HEADERS, follow_redirects=True) as client:
        r = await client.get(url, params={"symbol": symbol})
        if r.status_code != 200:
            return None
        data = r.json()
        try:
            return float(data.get("priceChangePercent"))
        except Exception:
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

# ==== /crypto (с 24h change) ====
async def handle_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        mkt, btc_chg, eth_chg = await asyncio.gather(
            get_market_cached(force=False),
            fetch_24h_change("BTCUSDT"),
            fetch_24h_change("ETHUSDT"),
        )
        btc, eth, usd_rub = mkt.get("BTC"), mkt.get("ETH"), mkt.get("USD_RUB")
        text = (
            f"BTC: {fmt_usd(btc)} ({fmt_pct(btc_chg) if btc_chg is not None else '—'} за 24ч)\n"
            f"ETH: {fmt_usd(eth)} ({fmt_pct(eth_chg) if eth_chg is not None else '—'} за 24ч)\n"
            f"USD/RUB: {fmt_rub(usd_rub)}"
        )
        await update.effective_message.reply_text(text, reply_markup=KB_CRYPTO())
    except Exception:
        log.exception("/crypto failed")
        await update.effective_message.reply_text("Не удалось получить цены/изменение/курс.")

async def on_refresh_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try: await q.answer("Обновляю…", cache_time=0)
    except Exception: pass
    try:
        mkt, btc_chg, eth_chg = await asyncio.gather(
            get_market_cached(force=True),
            fetch_24h_change("BTCUSDT"),
            fetch_24h_change("ETHUSDT"),
        )
        btc, eth, usd_rub = mkt.get("BTC"), mkt.get("ETH"), mkt.get("USD_RUB")
        msg = (
            f"BTC: {fmt_usd(btc)} ({fmt_pct(btc_chg) if btc_chg is not None else '—'} за 24ч)\n"
            f"ETH: {fmt_usd(eth)} ({fmt_pct(eth_chg) if eth_chg is not None else '—'} за 24ч)\n"
            f"USD/RUB: {fmt_rub(usd_rub)}"
        )
        try: await q.edit_message_text(msg, reply_markup=KB_CRYPTO())
        except Exception: await q.message.reply_text(msg, reply_markup=KB_CRYPTO())
    except Exception:
        log.exception("refresh crypto failed")
        await q.message.reply_text("Не удалось обновить цены/курс.")

# ==== Snapshot (всё в одном) ====
async def handle_snapshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        (users, juiced), mkt, btc_chg, eth_chg = await asyncio.gather(
            get_stats_cached(force=True),
            get_market_cached(force=True),
            fetch_24h_change("BTCUSDT"),
            fetch_24h_change("ETHUSDT"),
        )
        btc, eth, usd_rub = mkt.get("BTC"), mkt.get("ETH"), mkt.get("USD_RUB")
        giga = format_users_message(users, juiced)
        crypto = (
            f"BTC: {fmt_usd(btc)} ({fmt_pct(btc_chg) if btc_chg is not None else '—'} за 24ч)\n"
            f"ETH: {fmt_usd(eth)} ({fmt_pct(eth_chg) if eth_chg is not None else '—'} за 24ч)\n"
            f"USD/RUB: {fmt_rub(usd_rub)}"
        )
        txt = f"Snapshot\n\nGiga\n{giga}\n\nCrypto\n{crypto}"
        await update.effective_message.reply_text(txt, reply_markup=KB_SNAPSHOT())
    except Exception:
        log.exception("snapshot failed")
        await update.effective_message.reply_text("Не удалось собрать snapshot.")

async def on_refresh_snapshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try: await q.answer("Обновляю…", cache_time=0)
    except Exception: pass
    await handle_snapshot(update, context)

# ==== Charts (из прошлой версии) ====
# Быстрый вариант: показывать 2 PNG по кнопке Crypto charts (BTC/ETH, 7d)
# Используем Binance klines + QuickChart
async def fetch_binance_series(symbol: str, interval: str, limit: int) -> List[Tuple[int, float]]:
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": str(limit)}
    async with httpx.AsyncClient(timeout=20, headers=CRYPTO_HEADERS) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        arr = r.json()
    out: List[Tuple[int, float]] = []
    for k in arr:
        try:
            ts = int(k[6]); close = float(k[4])
            out.append((ts, close))
        except Exception:
            continue
    return out

def nearest_price(series: List[Tuple[int, float]], target_ms: int) -> Optional[float]:
    if not series: return None
    idx = min(range(len(series)), key=lambda i: abs(series[i][0] - target_ms))
    return series[idx][1]

def calc_changes_from_series(series: List[Tuple[int, float]]) -> Dict[str, Optional[float]]:
    if not series:
        return {"now": None, "d1h": None, "p1h": None, "d6h": None, "p6h": None, "d24h": None, "p24h": None}
    now_ms = series[-1][0]
    now_price = series[-1][1]
    out = {"now": now_price}
    for label, hours in (("1h", 1), ("6h", 6), ("24h", 24)):
        prev = nearest_price(series, now_ms - hours * 3600 * 1000)
        if prev and prev > 0:
            d = now_price - prev; p = d / prev * 100.0
            out[f"d{label}"] = d; out[f"p{label}"] = p
        else:
            out[f"d{label}"] = None; out[f"p{label}"] = None
    return out

def make_chart_config(series: List[Tuple[int, float]], label: str, color: str) -> Dict:
    data = [round(p, 2) for _, p in series]
    return {
        "type": "line",
        "data": {"labels": ["" for _ in data],
                 "datasets": [{"label": label, "data": data, "borderColor": color,
                               "backgroundColor": "rgba(0,0,0,0)", "borderWidth": 2,
                               "tension": 0.25, "pointRadius": 0}]},
        "options": {"plugins": {"legend": {"display": False}},
                    "scales": {"x": {"display": False}, "y": {"display": False}},
                    "layout": {"padding": 4}}
    }

async def render_chart_png(config: Dict, width: int = 800, height: int = 400) -> bytes:
    payload = {"chart": config, "width": width, "height": height, "format": "png", "backgroundColor": "white"}
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post("https://quickchart.io/chart", json=payload)
        r.raise_for_status()
        return r.content

async def send_coin_chart(chat_id: int | str, context: ContextTypes.DEFAULT_TYPE,
                          symbol_pair: str, symbol_short: str, color: str) -> None:
    series = await fetch_binance_series(symbol_pair, "1h", 168)  # 7d
    if not series:
        await context.bot.send_message(chat_id=chat_id, text=f"{symbol_short}: не удалось получить данные.")
        return
    cfg = make_chart_config(series, f"{symbol_short} 7d", color)
    png = await render_chart_png(cfg)
    chg = calc_changes_from_series(series)
    cap = "\n".join([
        f"{symbol_short}: {fmt_usd(chg.get('now'))}",
        f"1ч: {fmt_usd_delta(chg.get('d1h'))} ({fmt_pct(chg.get('p1h'))})",
        f"6ч: {fmt_usd_delta(chg.get('d6h'))} ({fmt_pct(chg.get('p6h'))})",
        f"24ч: {fmt_usd_delta(chg.get('d24h'))} ({fmt_pct(chg.get('p24h'))})",
    ])
    await context.bot.send_photo(chat_id=chat_id, photo=io.BytesIO(png), caption=cap)

async def send_charts_pack(chat_id: int | str, context: ContextTypes.DEFAULT_TYPE):
    await send_coin_chart(chat_id, context, "BTCUSDT", "BTC", "#f2a900")
    await send_coin_chart(chat_id, context, "ETHUSDT", "ETH", "#3c3c3d")
    await context.bot.send_message(chat_id=chat_id, text="Crypto charts (7d)", reply_markup=KB_CHARTS())

async def handle_charts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_charts_pack(update.effective_chat.id, context)

async def on_refresh_charts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try: await q.answer("Обновляю графики…", cache_time=0)
    except Exception: pass
    await send_charts_pack(update.effective_chat.id, context)

# ==== /convert ====
UNITS = {"usd", "rub", "btc", "eth", "$", "₽"}

def norm_unit(u: str) -> Optional[str]:
    u = u.lower()
    if u in ("$", "usd"): return "usd"
    if u in ("rub", "₽", "rubles", "rur"): return "rub"
    if u in ("btc", "Ƀ"): return "btc"
    if u in ("eth",): return "eth"
    return None

async def handle_convert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    m = re.match(r"^[!/](?:convert|conv)\s+([0-9]+(?:[.,][0-9]+)?)\s+([a-zA-Z₽$]+)\s+([a-zA-Z₽$]+)", text)
    if not m:
        await update.effective_message.reply_text("Использование: /convert 0.05 btc rub")
        return
    amount = float(m.group(1).replace(",", "."))
    src = norm_unit(m.group(2)); dst = norm_unit(m.group(3))
    if not src or not dst or src == dst:
        await update.effective_message.reply_text("Поддержка: usd, rub, btc, eth. Пример: /convert 100 usd rub")
        return

    mkt = await get_market_cached(force=True)
    btc, eth, usd_rub = mkt.get("BTC"), mkt.get("ETH"), mkt.get("USD_RUB")
    if any(v is None for v in (btc, eth, usd_rub)):
        await update.effective_message.reply_text("Нет котировок для конвертации, попробуйте ещё раз.")
        return

    # src -> USD
    usd = None
    if src == "usd": usd = amount
    elif src == "rub": usd = amount / usd_rub
    elif src == "btc": usd = amount * btc
    elif src == "eth": usd = amount * eth

    # USD -> dst
    out = None
    if dst == "usd": out = usd
    elif dst == "rub": out = usd * usd_rub
    elif dst == "btc": out = usd / btc
    elif dst == "eth": out = usd / eth

    if out is None:
        await update.effective_message.reply_text("Не удалось конвертировать.")
        return

    # Красиво отформатируем
    def fmt_unit(u: str, v: float) -> str:
        if u == "usd": return fmt_usd(v)
        if u == "rub": return fmt_rub(v)
        if u == "btc": return f"{v:.8f} BTC"
        if u == "eth": return f"{v:.8f} ETH"
        return str(v)

    await update.effective_message.reply_text(
        f"{amount:g} {src.upper()} = {fmt_unit(dst, out)}",
        disable_web_page_preview=True
    )

# ==== start/menu/chatid ====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Выберите действие:", reply_markup=KB_START)

async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    c = update.effective_chat
    await update.effective_message.reply_text(
        f"chat_id: {c.id}\n"
        f"type: {c.type}\n"
        f"title: {c.title or '-'}",
        reply_markup=KB_BACK()
    )

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    try: await q.answer()
    except Exception: pass
    chat_id = update.effective_chat.id

    if data == "menu_snapshot":
        await handle_snapshot(update, context)
    elif data == "menu_users":
        await send_users(chat_id, context.bot)
    elif data == "menu_crypto":
        await handle_crypto(update, context)
    elif data == "menu_charts":
        await handle_charts(update, context)
    elif data == "menu_chatid":
        await chatid(update, context)

async def on_back_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try: await q.answer()
    except Exception: pass
    try:
        await q.edit_message_text("Выберите действие:", reply_markup=KB_START)
    except Exception:
        await context.bot.send_message(chat_id=update.effective_chat.id,
                                       text="Выберите действие:", reply_markup=KB_START)

# ==== run webhook ====
def main():
    if not TOKEN:
        raise SystemExit("Заполните TELEGRAM_TOKEN в Environment")
    if not BASE_URL:
        raise SystemExit("Нужен BASE_URL или RENDER_EXTERNAL_URL")

    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("users", handle_users))
    app.add_handler(CommandHandler("crypto", handle_crypto))
    app.add_handler(CommandHandler("snapshot", handle_snapshot))
    app.add_handler(CommandHandler("convert", handle_convert))
    app.add_handler(CommandHandler("conv", handle_convert))
    app.add_handler(CommandHandler("charts", handle_charts))
    app.add_handler(CommandHandler("chatid", chatid))

    # Callbacks
    app.add_handler(CallbackQueryHandler(on_refresh_users, pattern=r"^refresh_users$"))
    app.add_handler(CallbackQueryHandler(on_refresh_crypto, pattern=r"^refresh_crypto$"))
    app.add_handler(CallbackQueryHandler(on_refresh_charts, pattern=r"^refresh_charts$"))
    app.add_handler(CallbackQueryHandler(on_refresh_snapshot, pattern=r"^refresh_snapshot$"))
    app.add_handler(CallbackQueryHandler(handle_menu, pattern=r"^menu_(snapshot|users|crypto|charts|chatid)$"))
    app.add_handler(CallbackQueryHandler(on_back_menu, pattern=r"^back_menu$"))
    app.add_handler(CallbackQueryHandler(on_refresh_users, pattern=r"^refresh$"))  # legacy

    # Aliases
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^!users\b", re.IGNORECASE)), handle_users))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^!crypto\b", re.IGNORECASE)), handle_crypto))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^!charts\b", re.IGNORECASE)), handle_charts))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^!(?:convert|conv)\b", re.IGNORECASE)), handle_convert))

    webhook_url = f"{BASE_URL}/{WEBHOOK_PATH}"
    log.info("Starting webhook on port %s, path '/%s', webhook_url=%s", PORT, WEBHOOK_PATH, webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )

# Snapshot depends on functions above
async def handle_snapshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        (users, juiced), mkt, btc_chg, eth_chg = await asyncio.gather(
            get_stats_cached(force=True),
            get_market_cached(force=True),
            fetch_24h_change("BTCUSDT"),
            fetch_24h_change("ETHUSDT"),
        )
        btc, eth, usd_rub = mkt.get("BTC"), mkt.get("ETH"), mkt.get("USD_RUB")
        giga = format_users_message(users, juiced)
        crypto = (
            f"BTC: {fmt_usd(btc)} ({fmt_pct(btc_chg) if btc_chg is not None else '—'} за 24ч)\n"
            f"ETH: {fmt_usd(eth)} ({fmt_pct(eth_chg) if eth_chg is not None else '—'} за 24ч)\n"
            f"USD/RUB: {fmt_rub(usd_rub)}"
        )
        txt = f"Snapshot\n\nGiga\n{giga}\n\nCrypto\n{crypto}"
        await update.effective_message.reply_text(txt, reply_markup=KB_SNAPSHOT())
    except Exception:
        log.exception("snapshot failed")
        await update.effective_message.reply_text("Не удалось собрать snapshot.")

if __name__ == "__main__":
    main()
