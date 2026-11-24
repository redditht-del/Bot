"""
Ricevitore webhook: TradingView (alert) -> Trade 212 (ordini reali sul conto demo/live).

Variabili d'ambiente da impostare sul server:
  T212_API_KEY, T212_API_SECRET   chiavi API Trade 212
  WEBHOOK_SECRET                  password lunga inventata da te
  T212_ENV                        "demo" (default) oppure "live"
  DRY_RUN                         "true" (default) = simula, "false" = invia ordini
  BUDGET_PER_TRADE                importo per acquisto (default 200)
"""
import base64
import hmac
import math
import os
import threading
import time

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
lock = threading.Lock()

ENV = os.environ.get("T212_ENV", "demo")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
BUDGET = float(os.environ.get("BUDGET_PER_TRADE", "200"))
SECRET = os.environ.get("WEBHOOK_SECRET", "")
BASE = {"demo": "https://demo.trading212.com/api/v0",
        "live": "https://live.trading212.com/api/v0"}[ENV]

_token = base64.b64encode(
    f"{os.environ.get('T212_API_KEY', '')}:{os.environ.get('T212_API_SECRET', '')}".encode()
).decode()
HEADERS = {"Authorization": f"Basic {_token}", "Content-Type": "application/json"}

# Ticker TradingView -> ticker Trade 212. Se manca, usa SIMBOLO_US_EQ.
TICKER_MAP = {
    "AAPL": "AAPL_US_EQ",
    "MSFT": "MSFT_US_EQ",
    "NVDA": "NVDA_US_EQ",
}


def t212(method, path, **kw):
    for _ in range(4):
        r = requests.request(method, BASE + path, headers=HEADERS, timeout=20, **kw)
        if r.status_code == 429:
            time.sleep(6)
            continue
        if not r.ok:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}
    raise RuntimeError("Rate limit persistente")


def held_quantity(ticker):
    for path in ("/equity/positions", "/equity/portfolio"):
        try:
            data = t212("GET", path)
            break
        except RuntimeError as e:
            if "404" not in str(e):
                raise
    else:
        raise RuntimeError("Endpoint posizioni non trovato")
    for p in data:
        tk = p.get("ticker") or p.get("instrument", {}).get("ticker")
        if tk == ticker:
            return p.get("quantity", 0) or 0
    return 0


def handle(action, tv_ticker, price):
    """Eseguito in background per rispondere subito a TradingView (limite 3 secondi)."""
    with lock:
        try:
            ticker = TICKER_MAP.get(tv_ticker, f"{tv_ticker}_US_EQ")
            held = held_quantity(ticker)
            if action == "buy":
                if held > 0:
                    print(f"SKIP buy {ticker}: gia' posseduto ({held})")
                    return
                qty = math.floor(BUDGET / price) if price else 0
                if qty < 1:
                    print(f"SKIP buy {ticker}: budget troppo basso per prezzo {price}")
                    return
            elif action == "sell":
                if held <= 0:
                    print(f"SKIP sell {ticker}: nessuna posizione")
                    return
                qty = -held
            else:
                return
            if DRY_RUN:
                print(f"[DRY RUN] {action} {ticker} qty={qty}")
                return
            res = t212("POST", "/equity/orders/market",
                       json={"ticker": ticker, "quantity": qty, "extendedHours": False})
            print(f"ORDINE {action} {ticker} qty={qty} -> {res}")
            time.sleep(2)
        except Exception as e:
            print("ERRORE:", e)


@app.route("/")
def health():
    return f"ok | env={ENV} | dry_run={DRY_RUN}"


@app.route("/webhook", methods=["POST"])
def webhook():
    # TradingView non puo' inviare header personalizzati: il segreto sta nell'URL (?secret=...)
    if not SECRET or not hmac.compare_digest(request.args.get("secret", ""), SECRET):
        return jsonify(error="unauthorized"), 401
    data = request.get_json(silent=True, force=True) or {}
    action = str(data.get("action", "")).lower()
    ticker = str(data.get("ticker", "")).upper()
    try:
        price = float(data.get("price", 0))
    except (TypeError, ValueError):
        price = 0
    if action not in ("buy", "sell") or not ticker:
        return jsonify(error="payload non valido"), 400
    threading.Thread(target=handle, args=(action, ticker, price), daemon=True).start()
    return jsonify(status="ricevuto"), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
