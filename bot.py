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

# Load env (Render прокинет переменные окружения)
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
API_URL = os.getenv("API_URL", "https://giganoob.com/data/html/users_snapshot.json").strip()
CACHE_TTL = int(os.getenv("CACHE_TTL", "30"))
CRYPTO_CACHE_TTL = int(os.getenv("CRYPTO_CACHE_TTL", "30"))

# Webhook params (Render)
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "tg-webhook").strip()
BASE_URL = (os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")

# Abstract RPC (по умолчанию ваш mainnet endpoint)
ABSTRACT_RPC = os.getenv("ABSTRACT_RPC", "https://api.mainnet.abs.xyz").strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

# ==== Keyboards ====
KB_START = InlineKeyboardMarkup([
    [InlineKeyboardButton("Snapshot", callback_data="menu_snapshot")],
    [InlineKeyboardButton("Giga users", callback_data="menu_users")],
    [InlineKeyboardButton("Crypto price", callback_data="menu_crypto")],
    [InlineKeyboardButton("Crypto charts", callback_data="menu_charts")],
    [InlineKeyboardButton("Gas ETH", callback_data="menu_gas")],
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

def KB_GAS():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⟳ Обновить", callback_data="refresh_gas")],
        [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")],
    ])

def KB_CHARTS_SELECT(coin: str, tf: str):
    def mark(x, cur): return f"{x} ✓" if x == cur else x
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(mark("BTC", coin), callback_data="charts_coin_BTC"),
         InlineKeyboardButton(mark("ETH", coin), callback_data="charts_coin_ETH")],
        [InlineKeyboardButton(mark("24h", tf), callback_data="charts_tf_24h"),
         InlineKeyboardButton(mark("7d", tf),  callback_data="charts_tf_7d"),
         InlineKeyboardButton(mark("30d", tf), callback_data="charts_tf_30d")],
        [InlineKeyboardButton("⟳ Обновить", callback_data="charts_refresh"),
         InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")],
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

# ==== /crypto (+24h change) ====
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

# ==== Snapshot ====
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

# ==== Charts — выбор монеты/таймфрейма, 1 PNG ====
CHART_PREFS: Dict[int, Dict[str, str]] = {}  # chat_id -> {"coin":"BTC","tf":"7d"}

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

def tf_to_params(tf: str) -> Tuple[str, int]:
    if tf == "24h": return ("15m", 96)
    if tf == "30d": return ("4h", 180)   # ~30 дней
    return ("1h", 168)                    # 7d дефолт

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

async def send_chart_for_pref(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    pref = CHART_PREFS.get(chat_id, {"coin": "BTC", "tf": "7d"})
    coin = pref["coin"]; tf = pref["tf"]
    symbol_pair = "BTCUSDT" if coin == "BTC" else "ETHUSDT"
    interval, limit = tf_to_params(tf)
    series = await fetch_binance_series(symbol_pair, interval, limit)
    if not series:
        await context.bot.send_message(chat_id=chat_id, text=f"{coin}: не удалось получить данные.")
        return
    cfg = make_chart_config(series, f"{coin} {tf}", "#f2a900" if coin == "BTC" else "#3c3c3d")
    png = await render_chart_png(cfg)
    chg = calc_changes_from_series(series)
    cap = "\n".join([
        f"{coin}: {fmt_usd(chg.get('now'))}",
        f"1ч: {fmt_usd_delta(chg.get('d1h'))} ({fmt_pct(chg.get('p1h'))})",
        f"6ч: {fmt_usd_delta(chg.get('d6h'))} ({fmt_pct(chg.get('p6h'))})",
        f"24ч: {fmt_usd_delta(chg.get('d24h'))} ({fmt_pct(chg.get('p24h'))})",
    ])
    await context.bot.send_photo(chat_id=chat_id, photo=io.BytesIO(png), caption=cap)

# ==== Gas ETH ====
async def fetch_eth_mainnet_gas() -> Optional[Dict[str, float]]:
    url = "https://www.etherchain.org/api/gasPriceOracle"
    async with httpx.AsyncClient(timeout=12, headers=CRYPTO_HEADERS) as client:
        r = await client.get(url)
        if r.status_code != 200:
            return None
        d = r.json() or {}
    out = {}
    for k in ("currentBaseFee", "safeLow", "standard", "fast", "fastest", "priorityFee", "recommendedBaseFee"):
        v = d.get(k)
        if isinstance(v, (int, float)): out[k] = float(v)
    return out or None

async def fetch_rpc_gas_gwei(rpc_url: str) -> Optional[float]:
    if not rpc_url:
        return None
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_gasPrice", "params": []}
    async with httpx.AsyncClient(timeout=12, headers={"Content-Type": "application/json"}) as client:
        r = await client.post(rpc_url, json=payload)
        if r.status_code != 200:
            return None
        data = r.json()
    try:
        wei_hex = data.get("result")
        wei = int(wei_hex, 16)
        gwei = wei / 1e9
        return gwei
    except Exception:
        return None

def estimate_fee_usd(gwei: Optional[float], gas_units: int, eth_usd: Optional[float]) -> Optional[float]:
    if gwei is None or eth_usd is None:
        return None
    eth_cost = (gwei * 1e-9) * gas_units
    return eth_cost * eth_usd

async def handle_gas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        gas_mainnet, gas_abs_gwei, mkt = await asyncio.gather(
            fetch_eth_mainnet_gas(),
            fetch_rpc_gas_gwei(ABSTRACT_RPC) if ABSTRACT_RPC else asyncio.sleep(0, result=None),
            get_market_cached(force=False),
        )
        eth_usd = mkt.get("ETH") if isinstance(mkt, dict) else None

        if gas_mainnet:
            base = gas_mainnet.get("currentBaseFee") or gas_mainnet.get("recommendedBaseFee")
            low = gas_mainnet.get("safeLow"); std = gas_mainnet.get("standard"); fast = gas_mainnet.get("fast")
            tx_usd = estimate_fee_usd(std, 21_000, eth_usd)
            swap_usd = estimate_fee_usd(std, 100_000, eth_usd)
            main_text = (
                "ETH Mainnet\n"
                f"Base: {base:.1f} gwei\n"
                f"Low | Std | Fast: {low:.1f} | {std:.1f} | {fast:.1f} gwei\n"
                f"Оценка (Std): transfer ≈ {fmt_usd(tx_usd)}, swap ≈ {fmt_usd(swap_usd)}"
            )
        else:
            main_text = "ETH Mainnet\n— не удалось получить газ с Etherchain"

        if ABSTRACT_RPC:
            if gas_abs_gwei is not None:
                a_tx_usd = estimate_fee_usd(gas_abs_gwei, 21_000, eth_usd)
                a_swap_usd = estimate_fee_usd(gas_abs_gwei, 100_000, eth_usd)
                abs_text = (
                    "Abstract\n"
                    f"Gas price: {gas_abs_gwei:.1f} gwei\n"
                    f"Оценка: transfer ≈ {fmt_usd(a_tx_usd)}, swap ≈ {fmt_usd(a_swap_usd)}"
                )
            else:
                abs_text = "Abstract\n— не удалось получить газ с RPC (проверьте ABSTRACT_RPC)"
        else:
            abs_text = "Abstract\n— не настроено (добавьте ABSTRACT_RPC)"

        text = f"{main_text}\n\n{abs_text}"
        await update.effective_message.reply_text(text, reply_markup=KB_GAS())
    except Exception:
        log.exception("/gas failed")
        await update.effective_message.reply_text("Не удалось получить газ ETH/Abstract.")

async def on_refresh_gas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try: await q.answer("Обновляю газ…", cache_time=0)
    except Exception: pass
    await handle_gas(update, context)

# ==== Charts handlers (меню выбора) ====
async def handle_charts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Показать меню выбора и сразу отправить график по текущему выбору
    q_or_m = update.callback_query
    chat_id = update.effective_chat.id
    pref = CHART_PREFS.get(chat_id) or {"coin": "BTC", "tf": "7d"}
    CHART_PREFS[chat_id] = pref
    try:
        if q_or_m:
            await q_or_m.answer()
            await q_or_m.edit_message_text(
                f"Crypto charts — {pref['coin']} — {pref['tf']}",
                reply_markup=KB_CHARTS_SELECT(pref['coin'], pref['tf'])
            )
        else:
            await update.effective_message.reply_text(
                f"Crypto charts — {pref['coin']} — {pref['tf']}",
                reply_markup=KB_CHARTS_SELECT(pref['coin'], pref['tf'])
            )
    except Exception:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Crypto charts — {pref['coin']} — {pref['tf']}",
            reply_markup=KB_CHARTS_SELECT(pref['coin'], pref['tf'])
        )
    await send_chart_for_pref(chat_id, context)

async def charts_set_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try: await q.answer()
    except Exception: pass
    chat_id = update.effective_chat.id
    coin = "BTC" if q.data.endswith("BTC") else "ETH"
    pref = CHART_PREFS.get(chat_id) or {"coin": "BTC", "tf": "7d"}
    pref["coin"] = coin; CHART_PREFS[chat_id] = pref
    try:
        await q.edit_message_text(f"Crypto charts — {pref['coin']} — {pref['tf']}",
                                  reply_markup=KB_CHARTS_SELECT(pref['coin'], pref['tf']))
    except Exception:
        await context.bot.send_message(chat_id=chat_id,
                                       text=f"Crypto charts — {pref['coin']} — {pref['tf']}",
                                       reply_markup=KB_CHARTS_SELECT(pref['coin'], pref['tf']))
    await send_chart_for_pref(chat_id, context)

async def charts_set_tf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try: await q.answer()
    except Exception: pass
    chat_id = update.effective_chat.id
    tf = "24h" if "24h" in q.data else ("30d" if "30d" in q.data else "7d")
    pref = CHART_PREFS.get(chat_id) or {"coin": "BTC", "tf": "7d"}
    pref["tf"] = tf; CHART_PREFS[chat_id] = pref
    try:
        await q.edit_message_text(f"Crypto charts — {pref['coin']} — {pref['tf']}",
                                  reply_markup=KB_CHARTS_SELECT(pref['coin'], pref['tf']))
    except Exception:
        await context.bot.send_message(chat_id=chat_id,
                                       text=f"Crypto charts — {pref['coin']} — {pref['tf']}",
                                       reply_markup=KB_CHARTS_SELECT(pref['coin'], pref['tf']))
    await send_chart_for_pref(chat_id, context)

async def charts_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try: await q.answer("Обновляю график…", cache_time=0)
    except Exception: pass
    await send_chart_for_pref(update.effective_chat.id, context)

# ==== start/menu/chatid / snapshot ====
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
    if data == "menu_snapshot":
        await handle_snapshot(update, context)
    elif data == "menu_users":
        await send_users(update.effective_chat.id, context.bot)
    elif data == "menu_crypto":
        await handle_crypto(update, context)
    elif data == "menu_charts":
        await handle_charts_menu(update, context)
    elif data == "menu_gas":
        await handle_gas(update, context)
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
    app.add_handler(CommandHandler("charts", handle_charts_menu))
    app.add_handler(CommandHandler("gas", handle_gas))
    app.add_handler(CommandHandler("chatid", chatid))

    # Callbacks
    app.add_handler(CallbackQueryHandler(on_refresh_users, pattern=r"^refresh_users$"))
    app.add_handler(CallbackQueryHandler(on_refresh_crypto, pattern=r"^refresh_crypto$"))
    app.add_handler(CallbackQueryHandler(on_refresh_snapshot, pattern=r"^refresh_snapshot$"))
    app.add_handler(CallbackQueryHandler(on_refresh_gas, pattern=r"^refresh_gas$"))
    app.add_handler(CallbackQueryHandler(handle_menu, pattern=r"^menu_(snapshot|users|crypto|charts|gas|chatid)$"))
    app.add_handler(CallbackQueryHandler(charts_set_coin, pattern=r"^charts_coin_(BTC|ETH)$"))
    app.add_handler(CallbackQueryHandler(charts_set_tf, pattern=r"^charts_tf_(24h|7d|30d)$"))
    app.add_handler(CallbackQueryHandler(charts_refresh, pattern=r"^charts_refresh$"))
    app.add_handler(CallbackQueryHandler(on_back_menu, pattern=r"^back_menu$"))
    app.add_handler(CallbackQueryHandler(on_refresh_users, pattern=r"^refresh$"))  # legacy

    # Aliases
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^!users\b", re.IGNORECASE)), handle_users))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^!crypto\b", re.IGNORECASE)), handle_crypto))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^!charts\b", re.IGNORECASE)), handle_charts_menu))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^!gas\b", re.IGNORECASE)), handle_gas))

    webhook_url = f"{BASE_URL}/{WEBHOOK_PATH}"
    log.info("Starting webhook on port %s, path '/%s', webhook_url=%s", PORT, WEBHOOK_PATH, webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )

# Snapshot reused above
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
