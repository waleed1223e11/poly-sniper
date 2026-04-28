import os, time, logging, threading, json, random
from datetime import datetime, timezone
from collections import deque
import requests
from flask import Flask

# ---------- Config ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

PAPER_TRADING = os.getenv("PAPER_TRADING", "True").lower() == "true"
PORT = int(os.getenv("PORT", "80"))

# Paper balance is dynamic for demo
PAPER_BALANCE = float(os.getenv("INITIAL_CAPITAL", "5"))

CHAINLINK_REST = "https://data.chain.link/streams/btc-usd"
POLYMARKET_BOOK = "https://clob.polymarket.com/book"

# ---------- State for Dashboard ----------
trade_log = deque(maxlen=20)          # stores dicts of recent trades
balance_history = deque(maxlen=200)   # stores (timestamp, balance) points

def add_trade(strategy, token, shares, price, pnl=0):
    """Record a paper trade and optionally adjust paper balance."""
    global PAPER_BALANCE
    trade = {
        "time": datetime.utcnow().strftime("%H:%M:%S"),
        "strategy": strategy,
        "token": token,
        "shares": shares,
        "price": price,
        "notional": shares * price,
        "pnl": pnl
    }
    trade_log.append(trade)
    if PAPER_TRADING and pnl != 0:
        PAPER_BALANCE += pnl
    # record balance snapshot
    balance_history.append((datetime.utcnow().isoformat(), PAPER_BALANCE))

# ---------- Data helpers ----------
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

# ---------- Phase detection ----------
def get_phase():
    if PAPER_BALANCE < 10:
        return 1
    elif PAPER_BALANCE < 50:
        return 2
    elif PAPER_BALANCE < 200:
        return 3
    else:
        return 4

# ---------- Order validation ----------
def validate_order(notional):
    if notional < 1.0:
        return False, "Below $1 min"
    if notional > PAPER_BALANCE * 0.2:
        return False, "Exceeds 20% risk cap"
    return True, "OK"

def place_order(token, shares, price, strategy_name):
    """Place a paper order and simulate P&L."""
    notional = shares * price
    valid, msg = validate_order(notional)
    if not valid:
        logging.warning(f"[{strategy_name}] SKIP: {msg}")
        return
    if PAPER_TRADING:
        pnl = 0
        # Simulate profit/loss based on strategy
        if strategy_name == "MakerRebate":
            pnl = notional * 0.0005  # tiny rebate
        elif strategy_name == "NegSpreadArb":
            pnl = 0  # handled in arb_loop after both orders
        elif strategy_name == "WindowSniper":
            # Simulate win with 85% probability if token matches direction (approx)
            win_prob = 0.85
            if random.random() < win_prob:
                pnl = (1.0 - price) * shares  # profit per share
            else:
                pnl = -notional
        add_trade(strategy_name, token, shares, price, pnl)
        logging.info(f"📄 [{strategy_name}] PAPER: {token} {shares:.1f} sh @ ${price:.2f} | notional ${notional:.2f} | simulated PnL ${pnl:+.4f}")
    else:
        # Real order would go here
        pass

# ---------- Strategy threads ----------
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
                        # Place both and simulate arb profit
                        place_order("UP", 1, up, "NegSpreadArb")
                        place_order("DOWN", 1, down, "NegSpreadArb")
                        profit = 1.0 - (up + down)
                        add_trade("NegSpreadArb", "ARB", 1, up+down, profit)  # combined trade
                        logging.info(f"🎯 ARB: cost ${up+down:.4f} | guaranteed profit ${profit:.4f}")
        except Exception as e:
            logging.error(f"arb_loop error: {e}")
        time.sleep(5)

p0 = None
last_cycle = None

def sniper_loop():
    global p0, last_cycle
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

# ---------- Flask dashboard ----------
app = Flask(__name__)

@app.route("/")
def dashboard():
    phase = get_phase()
    phase_names = {1: "SURVIVAL", 2: "FOUNDATION", 3: "GROWTH", 4: "DOMINANCE"}
    # Prepare data for chart
    balance_json = json.dumps([{"t": t, "y": b} for t, b in balance_history])
    trades_json = json.dumps(list(trade_log))
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>PolySniper Terminal</title>
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
            <span>Paper: {str(PAPER_TRADING).upper()}</span>
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
                <tr><th>Time</th><th>Strategy</th><th>Token</th><th>Shares</th><th>Price</th><th>Notional</th><th>Sim. PnL</th></tr>
            </table>
        </div>

        <script>
            const balanceData = {balance_json};
            const trades = {trades_json};

            // Chart
            const ctx = document.getElementById('chart').getContext('2d');
            new Chart(ctx, {{
                type: 'line',
                data: {{
                    labels: balanceData.map(p => new Date(p.t).toLocaleTimeString()),
                    datasets: [{{
                        label: 'Paper Balance ($)',
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

            // Trade table
            const table = document.getElementById('trades');
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

# ---------- Start bot ----------
def run_bot():
    time.sleep(2)
    for target in [maker_loop, arb_loop, sniper_loop]:
        t = threading.Thread(target=target, daemon=True)
        t.start()
        logging.info(f"Started {target.__name__}")

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
