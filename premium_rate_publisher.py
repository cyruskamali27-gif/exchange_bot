#!/usr/bin/env python3
"""
premium_rate_publisher.py — Cyrus Global Exchange Premium Rate Publisher

Renders all posters via poster_generator.py (pure PIL, no AI, no locked templates).

Input:  latest_rates.json
Output: final/cover.png  final/cad.png  final/usd.png
        final/eur.png    final/usdt.png  final/usacan.png  →  @cyrusGlobalExchange

Errors: Bot API sendMessage → ADMIN_ID only (never to public channel)
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests as _req

from config import BOT_TOKEN, ADMIN_ID
import poster_generator as pg

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE       = Path("/var/www/exchange_bot")
RATES_FILE = BASE / "latest_rates.json"
CACHE_FILE = BASE / "current_price.json"
OUTPUT_DIR = BASE / "final"

# ── Channel config ────────────────────────────────────────────────────────────
_pub_env       = os.environ.get("PUBLISH_CHANNEL", "@cyrusGlobalExchange")
TARGET_CHANNEL = (_pub_env if _pub_env.startswith("@") or _pub_env.lstrip("-").isdigit()
                  else f"@{_pub_env}")

# ── Runtime config ────────────────────────────────────────────────────────────
TORONTO_TZ    = ZoneInfo("America/Toronto")
MONITOR_HOURS = 12
BUY_ADJ       = int(os.environ.get("PUBLISHER_BUY_ADJ", "500"))

# ── Publish order ─────────────────────────────────────────────────────────────
PUBLISH_ORDER = [
    ("cover",  None),
    ("cad",    "CAD"),
    ("usd",    "USD"),
    ("usacan", "USACAN"),
    ("usdt",   "USDT"),
    ("eur",    "EUR"),
]

OUTPUT_FILES = {
    "cover":  OUTPUT_DIR / "cover.png",
    "cad":    OUTPUT_DIR / "cad.png",
    "usd":    OUTPUT_DIR / "usd.png",
    "eur":    OUTPUT_DIR / "eur.png",
    "usdt":   OUTPUT_DIR / "usdt.png",
    "usacan": OUTPUT_DIR / "usacan.png",
}

# ── Currency → poster config mapping ─────────────────────────────────────────
_CURRENCY_CONFIGS: dict = {
    "CAD": {
        "currency_title":    "CANADIAN DOLLAR",
        "currency_title_fa": "دلار کانادا",
        "currency_code":     "CAD",
        "sell_keys": [
            ("sell",           "Cash Sell"),
            ("sell_cheque",    "Left Cheque"),
            ("sell_etransfer", "E-Transfer"),
        ],
        "buy_keys": [
            ("buy",        "Cash Buy"),
            ("buy_direct", "Cash Buy Direct"),
        ],
    },
    "USD": {
        "currency_title":    "US DOLLAR",
        "currency_title_fa": "دلار آمریکا",
        "currency_code":     "USD",
        "sell_keys": [
            ("sell",            "Cash Sell"),
            ("sell_inside_usa", "Inside USA"),
            ("sell_etransfer",  "E-Transfer"),
        ],
        "buy_keys": [
            ("buy",        "Cash Buy"),
            ("buy_direct", "Cash Buy Direct"),
        ],
    },
    "EUR": {
        "currency_title":    "EURO",
        "currency_title_fa": "یورو",
        "currency_code":     "EUR",
        "is_ratio": True,
        "sell_keys": [("sell", "Sell Rate")],
        "buy_keys":  [("buy",  "Buy Rate")],
    },
    "USDT": {
        "currency_title":    "TETHER",
        "currency_title_fa": "تتر",
        "currency_code":     "USDT",
        "sell_keys": [("sell", "Cash Sell")],
        "buy_keys":  [("buy",  "Cash Buy")],
    },
    "USACAN": {
        "currency_title":    "USD / CAD RATE",
        "currency_title_fa": "نرخ دلار به کانادا",
        "currency_code":     "USACAN",
        "is_ratio": True,
        "sell_keys": [("sell", "Sell Rate")],
        "buy_keys":  [("buy",  "Buy Rate")],
    },
}

_BASE_CFG = {
    "company":  "CYRUS GLOBAL EXCHANGE",
    "phone":    "226-962-7729",
    "telegram": "@cyrusGlobalExchange",
    "location": "Guelph, Ontario",
}


def build_poster_cfg(rates_key: str, rates: dict) -> dict | None:
    tmpl   = _CURRENCY_CONFIGS.get(rates_key)
    if not tmpl:
        return None
    prices = rates.get(rates_key, {})
    if not prices:
        return None
    is_ratio = tmpl.get("is_ratio", False)

    def fmt(v):
        if v is None:
            return None
        return f"{v:.2f}" if is_ratio else f"{int(v):,}"

    sell_rows = [
        {"label": lbl, "price": fmt(prices[key])}
        for key, lbl in tmpl["sell_keys"]
        if prices.get(key) is not None
    ]
    buy_rows = [
        {"label": lbl, "price": fmt(prices[key])}
        for key, lbl in tmpl["buy_keys"]
        if prices.get(key) is not None
    ]
    if not sell_rows and not buy_rows:
        return None

    return {
        **_BASE_CFG,
        "currency_title":    tmpl["currency_title"],
        "currency_title_fa": tmpl["currency_title_fa"],
        "currency_code":     tmpl["currency_code"],
        "sell_rows":         sell_rows,
        "buy_rows":          buy_rows,
    }

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("premium_publisher")

# ── Admin notification ────────────────────────────────────────────────────────

def notify_admin(msg: str) -> None:
    try:
        _req.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": ADMIN_ID, "text": f"[PremiumPublisher] {msg}"},
            timeout=10,
        )
    except Exception:
        pass


# ── Telegram posting ──────────────────────────────────────────────────────────

def post_image(path: Path) -> int:
    with open(path, "rb") as f:
        resp = _req.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data={"chat_id": TARGET_CHANNEL},
            files={"photo": f},
            timeout=30,
        )
    data = resp.json()
    if data.get("ok"):
        return data["result"]["message_id"]
    raise RuntimeError(data.get("description", "Bot API error"))


# ── Rate loading ──────────────────────────────────────────────────────────────

def load_rates() -> dict:
    rates: dict = {}
    try:
        data = json.loads(RATES_FILE.read_text(encoding="utf-8"))
        for cur, prices in data.get("rates", {}).items():
            rates[cur] = {k: v for k, v in prices.items() if v is not None}
        log.info(f"Rates loaded from {RATES_FILE.name}")
    except Exception as exc:
        log.warning(f"Cannot read {RATES_FILE.name}: {exc}")
        notify_admin(f"Cannot read rates file: {exc}")

    if not rates:
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            for cur in ("USD", "CAD", "USDT"):
                entry = cache.get(cur, {})
                s = entry.get("our_sell") or entry.get("price")
                b = entry.get("our_buy")  or entry.get("buy")
                if s or b:
                    rates[cur] = {}
                    if s:
                        rates[cur]["sell"] = int(s)
                    if b:
                        rates[cur]["buy"]  = int(b)
            if rates:
                log.info(f"Rates from fallback {CACHE_FILE.name}")
        except Exception as exc:
            log.warning(f"Cannot read {CACHE_FILE.name}: {exc}")

    return rates


def save_rates(rates: dict, now: datetime) -> None:
    RATES_FILE.write_text(
        json.dumps({"rates": rates, "updated_at": now.isoformat()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def rates_changed(new: dict, stored: dict) -> bool:
    old = stored.get("rates", {})
    for cur, prices in new.items():
        for k, v in prices.items():
            if old.get(cur, {}).get(k) != v:
                return True
    return False


# ── Derived rates ─────────────────────────────────────────────────────────────

def _derive_eur(rates: dict) -> dict | None:
    usd = rates.get("USD", {})
    cad = rates.get("CAD", {})
    if not usd.get("sell") or not cad.get("sell"):
        return None
    try:
        resp        = _req.get("https://open.er-api.com/v6/latest/USD", timeout=8)
        eur_per_usd = resp.json().get("rates", {}).get("EUR")
        if not eur_per_usd:
            return None
        sell = round(usd["sell"] / eur_per_usd / cad["sell"], 2)
        buy  = (round(usd["buy"] / eur_per_usd / cad["buy"], 2)
                if usd.get("buy") and cad.get("buy") else None)
        log.info(f"[EUR] EUR/USD={eur_per_usd:.4f}  sell={sell}")
        r = {"sell": sell}
        if buy:
            r["buy"] = buy
        return r
    except Exception as exc:
        log.warning(f"[EUR] live rate fetch failed: {exc}")
        return None


# ── Bahmani text parser ───────────────────────────────────────────────────────

_CUR_KWS: dict[str, list[str]] = {
    "CAD":  ["دلار کانادا", "کانادا", "کاناد", "CAD"],
    "EUR":  ["یورو", "اروپا", "EUR"],
    "USDT": ["تتر", "USDT", "tether"],
    "USD":  ["دلار آمریکا", "دلار امریکا", "USD", "دلار"],
}
_BUY_KWS  = ["خرید", "buy", "خریدار"]
_SELL_KWS = ["فروش", "sell", "حواله"]


def _norm(text: str) -> str:
    for fa, en in zip("۰۱۲۳۴۵۶۷۸۹", "0123456789"):
        text = text.replace(fa, en)
    return re.sub(r"(\d{1,3})\.(\d{3})\b", r"\1,\2", text.replace("،", ","))


def _parse_bahmani(text: str) -> dict:
    text   = _norm(text)
    result: dict = {}
    for m in re.finditer(r"(\d{2,3},\d{3}|\d{5,6})", text):
        val = int(m.group().replace(",", ""))
        if not 10_000 <= val <= 500_000:
            continue
        ctx = text[max(0, m.start() - 200): m.end() + 200].lower()
        cur = next(
            (c for c, kws in _CUR_KWS.items() if any(k.lower() in ctx for k in kws)),
            None,
        )
        if not cur:
            continue
        direction = "buy" if any(k in ctx for k in _BUY_KWS) else "sell"
        result.setdefault(cur, {})
        if direction not in result[cur]:
            result[cur][direction] = val
            log.info(f"  [Bahmani] {cur} {direction} = {val:,}")
    return result


def _apply_adj(parsed: dict) -> dict:
    out = {}
    for cur, p in parsed.items():
        out[cur] = {}
        if p.get("sell") is not None:
            out[cur]["sell"] = p["sell"]
        if p.get("buy") is not None:
            out[cur]["buy"] = p["buy"] + BUY_ADJ
    return out


# ── Poster rendering ──────────────────────────────────────────────────────────

def generate_cover(now: datetime) -> Path | None:
    try:
        out = OUTPUT_FILES["cover"]
        pg.generate_date_cover(now=now, output_path=out)
        log.info(f"[cover] → {out.name}")
        return out
    except Exception as exc:
        log.error(f"[cover] render failed: {exc}")
        notify_admin(f"Cover render failed: {exc}")
        return None


def generate_currency(poster_key: str, rates_key: str, rates: dict) -> Path | None:
    cfg = build_poster_cfg(rates_key, rates)
    if not cfg:
        log.warning(f"[{poster_key}] no prices or config — skipping")
        return None
    try:
        out = OUTPUT_FILES[poster_key]
        pg.generate_poster(cfg, out)
        log.info(f"[{poster_key}] → {out.name}")
        return out
    except Exception as exc:
        log.error(f"[{poster_key}] render failed: {exc}")
        notify_admin(f"{poster_key.upper()} render failed: {exc}")
        return None


# ── Main post cycle ───────────────────────────────────────────────────────────

async def post_all(force: bool = False, fresh_text: str | None = None) -> None:
    now = datetime.now(TORONTO_TZ)
    log.info(f"=== post_all force={force}  {now:%Y-%m-%d %H:%M %Z} ===")

    # 1 — resolve rates
    rates: dict = {}
    if fresh_text:
        parsed = _parse_bahmani(fresh_text)
        if parsed:
            rates = _apply_adj(parsed)
            log.info(f"Rates from Bahmani text (adj): {rates}")
    if not rates:
        rates = load_rates()

    # 2 — derive EUR (always fresh — never stored)
    eur = _derive_eur(rates)
    if eur:
        rates["EUR"] = eur

    if not rates:
        log.warning("No rate data — skipping post")
        notify_admin("No rate data available — post skipped")
        return

    # 3 — change detection
    stored: dict = {}
    try:
        stored = json.loads(RATES_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    if not force and not rates_changed(rates, stored):
        log.info("Rates unchanged — skipping")
        return

    log.info(f"Posting: {'forced' if force else 'rates changed'}")

    # 4 — render and post all posters in order
    for poster_key, rates_key in PUBLISH_ORDER:
        if poster_key == "cover":
            path = generate_cover(now)
        else:
            path = generate_currency(poster_key, rates_key, rates)

        if path and path.exists():
            try:
                mid = post_image(path)
                log.info(f"[{poster_key}] message_id={mid}")
                await asyncio.sleep(2)
            except Exception as exc:
                log.error(f"[{poster_key}] post failed: {exc}")
                notify_admin(f"{poster_key} post failed: {exc}")
        elif poster_key != "cover":
            # cover failures already notified inside generate_cover()
            pass

    save_rates(rates, now)
    log.info("=== done ===")


# ── Scheduler ─────────────────────────────────────────────────────────────────

async def run_publisher() -> None:
    log.info("Premium Rate Publisher starting")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    monitor_end       = datetime.now(TORONTO_TZ) + timedelta(hours=MONITOR_HOURS)
    last_daily_date   = None
    last_checked_hour = -1

    while True:
        try:
            now   = datetime.now(TORONTO_TZ)
            today = now.date()
            hour  = now.hour

            if now <= monitor_end:
                if hour != last_checked_hour and now.minute < 10:
                    remaining = int((monitor_end - now).total_seconds() / 3600)
                    log.info(f"[12h] Hourly check {hour:02d}:00 (~{remaining}h left)")
                    await post_all(force=False)
                    last_checked_hour = hour
            else:
                if hour == 9 and last_daily_date != today:
                    log.info("9 AM daily post")
                    await post_all(force=True)
                    last_daily_date   = today
                    last_checked_hour = hour
                elif hour != last_checked_hour and now.minute < 10:
                    log.info(f"Hourly check {hour:02d}:00")
                    await post_all(force=False)
                    last_checked_hour = hour

        except Exception as exc:
            log.error(f"Scheduler error: {exc}")
            notify_admin(f"Scheduler error: {exc}")

        await asyncio.sleep(60)


if __name__ == "__main__":
    import sys
    if "--force" in sys.argv:
        asyncio.run(post_all(force=True))
    else:
        asyncio.run(run_publisher())
