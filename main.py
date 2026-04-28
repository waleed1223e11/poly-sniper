import os, time, logging, threading
from datetime import datetime, timezone
import requests
from flask import Flask

# ---------- Config ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

PAPER_TRADING = os.getenv("PAPER_TRADING", "True").lower() == "true"
BALANCE = float(os.getenv("INITIAL_CAPITAL", "5"))
PORT = int(os.getenv("PORT", "80"))

CHAINLINK_REST = "https://data.chain.link/streams/btc-usd"
POLYMARKET_BOOK = "https://clob.polymarket.com/book"

# ---------- Data Helpers ----------
def get_chainlink():
    try:
        r = requests.get(CHAINLINK_REST, timeout=5)
        return float(r.json()["price"])
    except:
        return None

def get_best_ask(token):
    try:
        r = requests.get(f"{POLYMARKET_BOOK}?token_id={token}", timeout=5).json()
        return float(r["asks"][0]["price"]) if r.get("asks") else None
    except:
        return None

def get_best_bid(token):
    try:
        r = requests.get(f"{POLYMARKET_BOOK}?token_id={token}", timeout=5).json()
        return float(r["bids"][0]["price"]) if r.get("bids") else None
    except:
        return None

# ---------- Phase Logic ----------
def get_phase():
    if BALANCE < 10:
        return 1
    elif BALANCE < 50:
        return 2
    elif BALANCE < 200:
        return 3
    else:
        return 4

# ---------- Order Validation ----------
def validate_order(notional):
    if notional < 1.0:
        return False, "Below $1 min"
    if notional > BALANCE * 0.2:
        return False, "Exceeds 20% risk cap"
    return True, "OK"

def place_order(token, shares, price, strategy_name):
    notional = shares * price
    valid, msg = validate_order(notional)
    if not valid:
        logging.warning(f"[{strategy_name}] SKIP: {msg}")
        return
    if PAPER_TRADING:
        logging.info(f"📄 [{strategy_name}] PAPER: {token} {shares:.1f} sh @ ${price:.2f} | notional ${notional:.2f}")

# ---------- Strategy Threads ----------
def maker_loop():
    while True:
        try:
            if get_phase() >= 1:
                for token in ["UP", "DOWN"]:
                    bid = get_best_bid(token)
                    if bid and validate_order(2.0):
                        place_order(token, 2.0, bid * 0.95, "MakerRebate")
        except Exception as e:
            logging.error(f"maker_loop error: {e}")
        time.sleep(60)

def arb_loop():
    while True:
        try:
            if get_phase() >= 2:
                up = get_best_ask("UP")
                down = get_best_ask("DOWN")
                if up and down and (up + down) < 1.0:
                    if validate_order(up) and validate_order(down):
                        place_order("UP", 1, up, "NegSpreadArb")
                        place_order("DOWN", 1, down, "NegSpreadArb")
                        logging.info(f"🎯 ARB: cost ${up+down:.4f}")
        except Exception as e:
            logging.error(f"arb_loop error: {e}")
        time.sleep(5)

def sniper_loop():
    global p0, last_cycle
    p0 = None
    last_cycle = None
    while True:
        try:
            if get_phase() < 3:
                time.sleep(1)
                continue
            now = datetime.now(timezone.utc)
            cycle = now.replace(second=0, microsecond=0)
            cycle_start = cycle.replace(minute=(cycle.minute // 5) * 5)
            if last_cycle != cycle_start:
                p0 = get_chainlink()
                last_cycle = cycle_start
                if p0:
                    logging.info(f"⚡ Cycle {cycle_start.time()}, P0=${p0:.2f}")
            curr = get_chainlink()
            if not p0 or not curr:
                time.sleep(0.5)
                continue
            sec_elapsed = (now.minute % 5) * 60 + now.second
            sec_remaining = 300 - sec_elapsed
            if sec_remaining > 15 or sec_remaining < 0:
                time.sleep(0.5)
                continue
            delta = (curr - p0) / p0
            if abs(delta) < 0.002:
                time.sleep(0.5)
                continue
            token = "UP" if delta > 0 else "DOWN"
            price = get_best_ask(token)
            if price and price > 0.85:
                place_order(token, 1, price, "WindowSniper")
        except Exception as e:
            logging.error(f"sniper_loop error: {e}")
        time.sleep(0.5)

# ---------- Flask App ----------
app = Flask(__name__)

@app.route("/")
def dashboard():
    phase = get_phase()
    phase_names = {1: "SURVIVAL", 2: "FOUNDATION", 3: "GROWTH", 4: "DOMINANCE"}
    return f"""
    <h1>PolySniper Bot</h1>
    <p>Paper Trading: {PAPER_TRADING}</p>
    <p>Balance: ${BALANCE:.2f}</p>
    <p>Phase: {phase_names.get(phase)}</p>
    <p>Maker: {'ACTIVE' if phase>=1 else 'LOCKED'}</p>
    <p>Arb: {'ACTIVE' if phase>=2 else 'LOCKED'}</p>
    <p>Sniper: {'ACTIVE' if phase>=3 else 'LOCKED'}</p>
    """

def run_bot():
    # Start strategy threads after a short delay to let Flask bind first
    time.sleep(2)
    for target in [maker_loop, arb_loop, sniper_loop]:
        t = threading.Thread(target=target, daemon=True)
        t.start()
        logging.info(f"Started {target.__name__}")

if __name__ == "__main__":
    # Start the bot threads in background
    threading.Thread(target=run_bot, daemon=True).start()
    # Start Flask immediately
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
