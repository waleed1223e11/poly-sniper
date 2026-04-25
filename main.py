import os, time, logging, threading, json
from datetime import datetime, timezone
import requests
from flask import Flask

# ============================================================
# CONFIGURATION
# ============================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

PAPER_TRADING = os.getenv("PAPER_TRADING", "True").lower() == "true"
BALANCE = float(os.getenv("INITIAL_CAPITAL", "5"))
TRADE_SIZE = 1.0  # $1 minimum order

CHAINLINK_REST = "https://data.chain.link/streams/btc-usd"
POLYMARKET_BOOK = "https://clob.polymarket.com/book"
GAMMA_API = "https://gamma-api.polymarket.com"

# Global state
p0 = None
last_cycle = None
total_trades = 0

# ============================================================
# PHASE DETECTION (Auto-Unlock)
# ============================================================
def get_phase():
    if BALANCE < 10:
        return 1  # Maker Rebates Only
    elif BALANCE < 50:
        return 2  # + Negative-Spread Arb
    elif BALANCE < 200:
        return 3  # + Window Delta Sniper
    else:
        return 4  # + Copy-Trader

# ============================================================
# DATA FETCHING
# ============================================================
def get_chainlink():
    """Fetch real-time BTC/USD price from Chainlink (the resolution source)."""
    try:
        r = requests.get(CHAINLINK_REST, timeout=5)
        return float(r.json()["price"])
    except:
        return None

def get_best_ask(token):
    """Fetch the best ask price for a given token (UP or DOWN)."""
    try:
        r = requests.get(f"{POLYMARKET_BOOK}?token_id={token}", timeout=5)
        data = r.json()
        return float(data["asks"][0]["price"]) if data.get("asks") else None
    except:
        return None

def get_best_bid(token):
    """Fetch the best bid price for a given token."""
    try:
        r = requests.get(f"{POLYMARKET_BOOK}?token_id={token}", timeout=5)
        data = r.json()
        return float(data["bids"][0]["price"]) if data.get("bids") else None
    except:
        return None

# ============================================================
# RISK VALIDATION
# ============================================================
def validate_order(notional):
    """Enforce $1 minimum and 20% max risk per trade."""
    if notional < 1.0:
        return False, "Below $1 minimum"
    if notional > BALANCE * 0.2:
        return False, "Exceeds 20% risk cap"
    return True, "OK"

# ============================================================
# ORDER PLACEMENT
# ============================================================
def place_order(token, shares, price, strategy_name):
    """Log paper trade or (future) submit real order via CLOB API."""
    global total_trades
    notional = shares * price
    valid, msg = validate_order(notional)
    if not valid:
        logging.warning(f"[{strategy_name}] SKIPPED: {msg} (notional ${notional:.2f})")
        return False

    if PAPER_TRADING:
        logging.info(f"📄 [{strategy_name}] PAPER: {token} {shares:.1f} sh @ ${price:.2f} | notional ${notional:.2f}")
        total_trades += 1
    else:
        # Real order: POST to CLOB API with EIP-712 signature
        # endpoint = "https://clob.polymarket.com/order"
        # payload = {"token_id": token, "side": "BUY", "size": shares, "price": price}
        # headers = {"Authorization": f"Bearer {API_KEY}"}
        # requests.post(endpoint, json=payload, headers=headers)
        pass
    return True

# ============================================================
# STREAM 1: MAKER REBATES (Phase 1+, Always On)
# ============================================================
def maker_loop():
    """Place passive limit orders far from spread to farm daily USDC rebates."""
    while True:
        phase = get_phase()
        if phase >= 1:
            for token in ["UP", "DOWN"]:
                best_bid = get_best_bid(token)
                if best_bid and validate_order(2.0):
                    # Place limit buy 5% below best bid — rarely fills, but earns rebates
                    limit_price = best_bid * 0.95
                    place_order(token, 2.0, limit_price, "MakerRebate")
        time.sleep(60)  # Refresh every 60 seconds

# ============================================================
# STREAM 2: NEGATIVE-SPREAD ARBITRAGE (Phase 2+, Unlocked at $10)
# ============================================================
def arb_loop():
    """Scan for Up + Down < $1.00. If found, buy both for guaranteed profit."""
    while True:
        phase = get_phase()
        if phase >= 2:
            up_ask = get_best_ask("UP")
            down_ask = get_best_ask("DOWN")
            if up_ask and down_ask:
                combined = up_ask + down_ask
                if combined < 1.00:
                    if place_order("UP", 1.0, up_ask, "NegSpreadArb"):
                        place_order("DOWN", 1.0, down_ask, "NegSpreadArb")
                        profit = 1.00 - combined
                        logging.info(f"🎯 RISK-FREE ARB: Cost ${combined:.4f} | Guaranteed Profit ${profit:.4f}")
        time.sleep(5)  # Scan every 5 seconds

# ============================================================
# STREAM 3: WINDOW DELTA SNIPER (Phase 3+, Unlocked at $50)
# ============================================================
def sniper_loop():
    """Enter in final 15 seconds with >85% directional confidence."""
    global p0, last_cycle
    while True:
        phase = get_phase()
        if phase < 3:
            time.sleep(1)
            continue

        now = datetime.now(timezone.utc)
        block = now.replace(second=0, microsecond=0)
        cycle = block.replace(minute=(block.minute // 5) * 5)

        # Reset P0 at start of new 5-minute cycle
        if last_cycle != cycle:
            p0 = get_chainlink()
            last_cycle = cycle
            if p0:
                logging.info(f"⚡ [Sniper] New cycle {cycle.time()}, P0=${p0:.2f}")

        curr = get_chainlink()
        if not p0 or not curr:
            time.sleep(0.5)
            continue

        # Only act in final 15 seconds
        sec_elapsed = (now.minute % 5) * 60 + now.second
        sec_remaining = 300 - sec_elapsed
        if sec_remaining > 15 or sec_remaining < 0:
            time.sleep(0.5)
            continue

        # Directional threshold: 0.2% move from cycle start
        delta = (curr - p0) / p0
        if abs(delta) < 0.002:
            time.sleep(0.5)
            continue

        token = "UP" if delta > 0 else "DOWN"
        price = get_best_ask(token)
        if not price or price < 0.85:  # Must be >85% probability
            time.sleep(0.5)
            continue

        place_order(token, 1.0, price, "WindowSniper")
        time.sleep(0.5)

# ============================================================
# STREAM 4: WHALE COPY-TRADER (Phase 4+, Unlocked at $200)
# ============================================================
WHALE_WALLETS = [
    "0xPLACEHOLDER_WALLET_1",
    "0xPLACEHOLDER_WALLET_2"
]

def copy_loop():
    """Mirror trades of known profitable wallets."""
    while True:
        phase = get_phase()
        if phase >= 4:
            for wallet in WHALE_WALLETS:
                try:
                    url = f"https://data-api.polymarket.com/activity?user={wallet}&limit=5"
                    r = requests.get(url, timeout=5)
                    for trade in r.json():
                        if trade.get("market", "").startswith("BTC"):
                            token = trade["outcome"]
                            price = get_best_ask(token)
                            if price:
                                place_order(token, 1.0, price, "CopyTrader")
                except:
                    pass
        time.sleep(20)

# ============================================================
# ADMIN DASHBOARD (Flask)
# ============================================================
app = Flask(__name__)

@app.route("/")
def dashboard():
    phase = get_phase()
    phase_names = {1: "SURVIVAL (Maker Only)", 2: "FOUNDATION (+Arb)", 3: "GROWTH (+Sniper)", 4: "DOMINANCE (All)"}
    return f"""
    <html><body style="font-family:monospace;background:#111;color:#0f0;padding:20px">
    <h1>🧠 PolySniper Bot — Live Status</h1>
    <p><b>Paper Trading:</b> {PAPER_TRADING}</p>
    <p><b>Balance:</b> ${BALANCE:.2f}</p>
    <p><b>Current Phase:</b> {phase_names.get(phase, 'Unknown')}</p>
    <p><b>Total Paper Trades:</b> {total_trades}</p>
    <hr>
    <p>🔵 Maker Rebates: <b>{'ACTIVE' if phase >= 1 else 'LOCKED'}</b></p>
    <p>🟢 Negative-Spread Arb: <b>{'ACTIVE' if phase >= 2 else 'LOCKED (Need $10+)'}</b></p>
    <p>🟠 Window Sniper: <b>{'ACTIVE' if phase >= 3 else 'LOCKED (Need $50+)'}</b></p>
    <p>🔴 Copy-Trader: <b>{'ACTIVE' if phase >= 4 else 'LOCKED (Need $200+)'}</b></p>
    </body></html>
    """

# ============================================================
# MAIN ENTRY POINT
# ============================================================
if __name__ == "__main__":
    # Start all four strategy threads
    for target in [maker_loop, arb_loop, sniper_loop, copy_loop]:
        t = threading.Thread(target=target, daemon=True)
        t.start()
        logging.info(f"Thread started: {target.__name__}")

    # Start Flask health check + dashboard
    port = int(os.environ.get("PORT", 8080))
    logging.info(f"🚀 PolySniper Bot online — Dashboard at port {port}")
    app.run(host="0.0.0.0", port=port)