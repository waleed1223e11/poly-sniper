import os, time, logging, threading, json, random, requests
from datetime import datetime, timezone
from collections import deque
from flask import Flask

# ---------- Configuration ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

PORT = int(os.getenv("PORT", "80"))
PAPER_TRADING = os.getenv("PAPER_TRADING", "True").lower() == "true"
PAPER_BALANCE = float(os.getenv("INITIAL_CAPITAL", "10"))

# Polymarket L2 credentials (for live trading)
POLY_API_KEY = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE", "")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# ---------- Dashboard state ----------
trade_log = deque(maxlen=20)
balance_history = deque(maxlen=200)

def add_trade(strategy, token, shares, price, pnl=0.0):
    global PAPER_BALANCE
    trade = {
        "time": datetime.utcnow().strftime("%H:%M:%S"),
        "strategy": strategy,
        "token": token,
        "shares": round(shares, 1),
        "price": round(price, 4),
        "notional": round(shares * price, 2),
        "pnl": round(pnl, 4)
    }
    trade_log.append(trade)
    if PAPER_TRADING and pnl != 0:
        PAPER_BALANCE += pnl
    balance_history.append((datetime.utcnow().isoformat(), round(PAPER_BALANCE, 4)))

# ---------- Token ID discovery (Gamma API) ----------
def get_token_ids():
    now = datetime.now(timezone.utc)
    interval = 300
    timestamp = int(now.timestamp())
    rounded = (timestamp // interval) * interval
    slug = f"btc-updown-5m-{rounded}"
    try:
        r = requests.get(f"{GAMMA_API}/events/slug/{slug}", timeout=5)
        r.raise_for_status()
        data = r.json()
        if "markets" in data and len(data["markets"]) > 0:
            token_ids_raw = data["markets"][0].get("clobTokenIds", "[]")
            token_ids = json.loads(token_ids_raw)
            if len(token_ids) >= 2:
                return {"up": token_ids[0], "down": token_ids[1]}
    except Exception as e:
        logging.error(f"Token ID fetch: {e}")
    return None

# ---------- Price helpers ----------
def get_best_ask(token_id):
    try:
        r = requests.get(f"{CLOB_API}/price", params={"token_id": token_id, "side": "BUY"}, timeout=5)
        if r.status_code == 200:
            return float(r.json()["price"])
    except:
        pass
    return None

def get_best_bid(token_id):
    try:
        r = requests.get(f"{CLOB_API}/price", params={"token_id": token_id, "side": "SELL"}, timeout=5)
        if r.status_code == 200:
            return float(r.json()["price"])
    except:
        pass
    return None

# ---------- Phase ----------
def get_phase():
    if PAPER_BALANCE < 10:
        return 1
    elif PAPER_BALANCE < 50:
        return 2
    elif PAPER_BALANCE < 200:
        return 3
    return 4

def validate_order(notional):
    if notional < 1.0:
        return False, "Below $1 min"
    if notional > PAPER_BALANCE * 0.2:
        return False, "Exceeds 20% risk cap"
    return True, "OK"

# ---------- Live order via L2 API ----------
def submit_live_order(token_id, price, size, side):
    if not POLY_API_KEY or not POLY_API_SECRET or not POLY_API_PASSPHRASE:
        logging.error("L2 credentials missing – cannot place live order")
        return False
    try:
        headers = {
            "POLY-API-KEY": POLY_API_KEY,
            "POLY-API-SECRET": POLY_API_SECRET,
            "POLY-API-PASSPHRASE": POLY_API_PASSPHRASE
        }
        order = {
            "token_id": token_id,
            "price": str(price),
            "size": str(size),
            "side": side
        }
        resp = requests.post(f"{CLOB_API}/order", json=order, headers=headers, timeout=10)
        if resp.status_code == 200:
            logging.info(f"✅ LIVE ORDER: {token_id} {side} {size} @ {price}")
            return True
        else:
            logging.error(f"Live order failed: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        logging.error(f"Live order exception: {e}")
        return False

def place_order(token_label, shares, price, strategy, token_id=None):
    notional = shares * price
    valid, msg = validate_order(notional)
    if not valid:
        logging.warning(f"[{strategy}] SKIP: {msg}")
        return
    if PAPER_TRADING:
        pnl = 0.0
        if strategy == "MakerRebate":
            pnl = notional * 0.0005
        elif strategy == "WindowSniper":
            pnl = (1.0 - price) * shares if random.random() < 0.85 else -notional
        add_trade(strategy, token_label, shares, price, pnl)
        logging.info(f"📄 [{strategy}] PAPER: {token_label} {shares:.1f} sh @ ${price:.2f} | sim PnL ${pnl:+.4f}")
    else:
        if token_id:
            submit_live_order(token_id, price, shares, "BUY")
            add_trade(strategy, token_label, shares, price, 0.0)  # log without PnL

# ---------- Strategy threads ----------
def maker_loop():
    while True:
        try:
            if get_phase() >= 1:
                ids = get_token_ids()
                if ids:
                    for label, tid in [("UP", ids["up"]), ("DOWN", ids["down"])]:
                        bid = get_best_bid(tid)
                        if bid:
                            limit_price = bid * 0.95
                            # Determine shares so that notional >= $1
                            q = max(int(1.0 / limit_price) + 1, 1)
                            if validate_order(q * limit_price):
                                place_order(label, q, limit_price, "MakerRebate", tid)
        except Exception as e:
            logging.error(f"maker_loop: {e}")
        time.sleep(60)

def arb_loop():
    while True:
        try:
            if get_phase() >= 2:
                ids = get_token_ids()
                if ids:
                    up_ask = get_best_ask(ids["up"])
                    down_ask = get_best_ask(ids["down"])
                    if up_ask and down_ask and (up_ask + down_ask) < 1.0:
                        # Calculate shares needed to meet $1 notional for each leg
                        q_up = max(int(1.0 / up_ask) + 1, 1)   # ceil(1/up_ask)
                        q_down = max(int(1.0 / down_ask) + 1, 1)
                        q = max(q_up, q_down)   # same quantity for both sides
                        
                        total_cost = q * (up_ask + down_ask)
                        if total_cost > PAPER_BALANCE * 0.2:
                            logging.warning(f"ARB cost ${total_cost:.2f} exceeds 20% risk cap, skipping")
                            continue
                        
                        place_order("UP", q, up_ask, "NegSpreadArb", ids["up"])
                        place_order("DOWN", q, down_ask, "NegSpreadArb", ids["down"])
                        
                        profit = q * (1.0 - (up_ask + down_ask))
                        add_trade("NegSpreadArb", "ARB", q, up_ask + down_ask, profit)
                        logging.info(f"🎯 ARB: qty {q} | cost ${total_cost:.2f} | profit ${profit:.4f}")
        except Exception as e:
            logging.error(f"arb_loop: {e}")
        time.sleep(5)

def sniper_loop():
    global p0, last_cycle
    while True:
        try:
            if get_phase() < 3:
                time.sleep(1)
                continue
            now = datetime.now(timezone.utc)
            ids = get_token_ids()
            if not ids:
                time.sleep(0.5)
                continue
            try:
                r = requests.get("https://data.chain.link/streams/btc-usd", timeout=5)
                curr = float(r.json()["price"])
            except:
                time.sleep(0.5)
                continue
            cycle = now.replace(second=0, microsecond=0)
            cycle_start = cycle.replace(minute=(cycle.minute // 5) * 5)
            if last_cycle != cycle_start:
                p0 = curr
                last_cycle = cycle_start
                logging.info(f"⚡ Cycle {cycle_start.time()}, P0=${p0:.2f}")
            if not p0:
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
            label = "UP" if delta > 0 else "DOWN"
            tid = ids["up"] if label == "UP" else ids["down"]
            price = get_best_ask(tid)
            if price and price > 0.85:
                place_order(label, 1, price, "WindowSniper", tid)
        except Exception as e:
            logging.error(f"sniper_loop: {e}")
        time.sleep(0.5)

# ---------- Terminal Dashboard ----------
app = Flask(__name__)

@app.route("/")
def dashboard():
    phase = get_phase()
    phase_names = {1: "SURVIVAL", 2: "FOUNDATION", 3: "GROWTH", 4: "DOMINANCE"}
    balance_json = json.dumps([{"t": t, "y": y} for t, y in balance_history])
    trades_json = json.dumps(list(trade_log))
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>PolySniper Terminal</title>
        <meta charset="UTF-8">
        <style>
            body {{ background: #0a0a0a; color: #00ff00; font-family: 'Courier New', monospace; margin: 20px; }}
            h1 {{ color: #00ff00; border-bottom: 1px solid #00ff00; padding-bottom: 10px; }}
            .panel {{ background: #111; border: 1px solid #00ff00; padding: 15px; margin: 10px 0; border-radius: 5px; }}
            .status {{ display: flex; flex-wrap: wrap; gap: 15px; }}
            .status span {{ background: #0a0a0a; padding: 5px 10px; border: 1px solid #00ff00; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
            th, td {{ border: 1px solid #00ff00; padding: 5px; text-align: left; }}
            th {{ background: #111; }}
            .profit {{ color: #00ff00; }} .loss {{ color: #ff0000; }}
            canvas {{ width: 100%; max-height: 300px; margin-top: 10px; }}
        </style>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    </head>
    <body>
        <h1>⚡ PolySniper Terminal</h1>
        <div class="panel status">
            <span>Mode: {('LIVE' if not PAPER_TRADING else 'PAPER').upper()}</span>
            <span>Balance: ${PAPER_BALANCE:.2f}</span>
            <span>Phase: {phase_names.get(phase)}</span>
            <span>Maker: {'ACTIVE' if phase>=1 else 'LOCKED'}</span>
            <span>Arb: {'ACTIVE' if phase>=2 else 'LOCKED'}</span>
            <span>Sniper: {'ACTIVE' if phase>=3 else 'LOCKED'}</span>
        </div>
        <div class="panel">
            <h2>📈 Portfolio</h2>
            <canvas id="chart"></canvas>
        </div>
        <div class="panel">
            <h2>📋 Recent Trades</h2>
            <table id="trades">
                <tr><th>Time</th><th>Strategy</th><th>Token</th><th>Shares</th><th>Price</th><th>Notional</th><th>PnL</th></tr>
            </table>
        </div>
        <script>
            const balanceData = JSON.parse('{balance_json}');
            const trades = JSON.parse('{trades_json}');
            const ctx = document.getElementById('chart').getContext('2d');
            if (balanceData.length > 0) {{
                new Chart(ctx, {{
                    type: 'line',
                    data: {{
                        labels: balanceData.map(p => new Date(p.t).toLocaleTimeString()),
                        datasets: [{{
                            label: 'Balance ($)',
                            data: balanceData.map(p => p.y),
                            borderColor: '#00ff00',
                            backgroundColor: 'rgba(0,255,0,0.1)',
                            fill: true,
                            tension: 0.1
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        scales: {{
                            x: {{ ticks: {{ color: '#00ff00' }} }},
                            y: {{ ticks: {{ color: '#00ff00' }}, beginAtZero: false }}
                        }},
                        plugins: {{ legend: {{ labels: {{ color: '#00ff00' }} }} }}
                    }}
                }});
            }}
            const table = document.getElementById('trades');
            if (trades.length === 0) {{
                const row = table.insertRow();
                const cell = row.insertCell();
                cell.colSpan = 7;
                cell.textContent = 'Waiting for trades...';
                cell.style.textAlign = 'center';
            }}
            trades.forEach(t => {{
                const row = table.insertRow();
                row.insertCell().textContent = t.time;
                row.insertCell().textContent = t.strategy;
                row.insertCell().textContent = t.token;
                row.insertCell().textContent = t.shares.toFixed(1);
                row.insertCell().textContent = '$' + t.price.toFixed(4);
                row.insertCell().textContent = '$' + t.notional.toFixed(2);
                const pnlCell = row.insertCell();
                pnlCell.textContent = (t.pnl >= 0 ? '+' : '') + '$' + t.pnl.toFixed(4);
                pnlCell.className = t.pnl >= 0 ? 'profit' : 'loss';
            }});
        </script>
    </body>
    </html>
    """

# ---------- Start ----------
def run_bot():
    time.sleep(2)
    for target in [maker_loop, arb_loop, sniper_loop]:
        t = threading.Thread(target=target, daemon=True)
        t.start()
        logging.info(f"Started {target.__name__}")

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
