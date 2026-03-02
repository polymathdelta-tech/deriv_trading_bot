import asyncio
import httpx
import time
import json
import sys
import random
import csv
import os
import glob
from collections import defaultdict
import config
from deriv_api import DerivAPI
from indicators import LiveBollinger

# --- CONFIGURATION & DEFAULTS ---
SYMBOL = "1HZ10V"           # Volatility 10 (1s) Index
STAKE = 1.00
TARGET_PROFIT = 10.0
MAX_LOSS = -2.00
GROWTH_RATE = 0.01          # 1% Accumulator
COOLDOWN_SECONDS = 15       # Enforce statistical independence between trades

# --- GLOBAL SYSTEM STATE ---
BOT_STATE = {
    "mode": "STOPPED",      # ACTIVE, PAUSED, STOPPED
    "daily_profit": 0.0,
    "highest_profit": 0.0,
    "wins": 0,
    "losses": 0,
    "session_start_time": 0.0,
    "balance": 0.0
}

# Regime Tracking
regime_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "profit": 0.0})

class RegimeExplorer:
    """Deliberately samples bandwidth percentiles to discover empirical edge."""
    def __init__(self):
        # The percentile bins we want to test
        self.regimes = [
            (0.10, 0.30),
            (0.20, 0.40),
            (0.30, 0.50),
            (0.40, 0.60),
            (0.50, 0.70)
        ]
        self.current_regime = random.choice(self.regimes)
        self.trades_in_regime = 0
        self.max_trades_per_regime = 10 # Rotate every 10 trades to avoid time-of-day bias

    def should_trade(self, percentile):
        if percentile is None:
            return False
        low, high = self.current_regime
        return low <= percentile <= high

    def record_trade(self):
        self.trades_in_regime += 1
        if self.trades_in_regime >= self.max_trades_per_regime:
            self.current_regime = random.choice(self.regimes)
            self.trades_in_regime = 0
            return True # Indicates we rotated
        return False

def log_trade(data):
    """Writes raw trade data to disk for scientific analysis."""
    try:
        with open("trade_log.csv", "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=data.keys())
            if f.tell() == 0:
                writer.writeheader()
            writer.writerow(data)
    except Exception as e:
        print(f"Failed to write to CSV: {e}")

def format_time(seconds: float) -> str:
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours}h {mins}m {secs}s"
    return f"{mins}m {secs}s"

# --- TELEGRAM API FUNCTIONS ---
async def send_tg_alert(message: str):
    url = f"https://api.telegram.org/bot{config.TG_TOKEN}/sendMessage"
    payload = {"chat_id": config.TG_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, json=payload)
        except Exception as e:
            print(f"TG Alert Failed: {e}")

async def send_tg_document(filepath: str):
    """Uploads a file directly to the Telegram chat."""
    url = f"https://api.telegram.org/bot{config.TG_TOKEN}/sendDocument"
    try:
        async with httpx.AsyncClient() as client:
            with open(filepath, "rb") as f:
                files = {"document": (os.path.basename(filepath), f)}
                data = {"chat_id": config.TG_CHAT_ID}
                await client.post(url, data=data, files=files)
    except Exception as e:
        print(f"Document Upload Failed: {e}")
        await send_tg_alert(f"⚠️ Failed to upload `{filepath}`: {e}")

# ==========================================
# ENGINE 1: TELEGRAM COMMAND LISTENER
# ==========================================
async def listen_for_commands(explorer):
    last_update_id = 0
    url = f"https://api.telegram.org/bot{config.TG_TOKEN}/getUpdates"
    
    while True:
        try:
            async with httpx.AsyncClient() as client:
                res = await client.get(url, params={"offset": last_update_id + 1, "timeout": 30}, timeout=35.0)
                data = res.json()
                
                for item in data.get("result", []):
                    last_update_id = item["update_id"]
                    msg = item.get("message", {}).get("text", "").strip()
                    msg_lower = msg.lower()
                    
                    if msg_lower == "/start":
                        if BOT_STATE["mode"] == "STOPPED":
                            BOT_STATE["mode"] = "ACTIVE"
                            BOT_STATE["daily_profit"] = 0.0
                            BOT_STATE["highest_profit"] = 0.0
                            BOT_STATE["wins"] = 0
                            BOT_STATE["losses"] = 0
                            BOT_STATE["session_start_time"] = time.time()
                            await send_tg_alert("🟢 Scientific Engine Booting Up. Connecting to Deriv...")
                        else:
                            await send_tg_alert("Bot is already running. Use /pause or /stop.")
                            
                    elif msg_lower == "/stop":
                        BOT_STATE["mode"] = "STOPPED"
                        await send_tg_alert("🛑 Stopping trading. Bot in Standby Mode.")
                        
                    elif msg_lower == "/pause":
                        if BOT_STATE["mode"] == "ACTIVE":
                            BOT_STATE["mode"] = "PAUSED"
                            await send_tg_alert("⏸️ Paused. Monitoring charts but will not execute trades.")
                            
                    elif msg_lower == "/resume":
                        if BOT_STATE["mode"] == "PAUSED":
                            BOT_STATE["mode"] = "ACTIVE"
                            await send_tg_alert("▶️ Resumed. Resuming regime exploration.")
                            
                    elif msg_lower == "/stats":
                        uptime = time.time() - BOT_STATE["session_start_time"] if BOT_STATE["mode"] != "STOPPED" else 0
                        status_icon = "🟢" if BOT_STATE["mode"] == "ACTIVE" else "⏸️" if BOT_STATE["mode"] == "PAUSED" else "🛑"
                        
                        best_regime_str = "Needs more data"
                        if regime_stats:
                            # Find the regime with the highest Expected Value (EV)
                            best = max(regime_stats.items(), key=lambda x: x[1]["profit"] / max(1, x[1]["trades"]))
                            best_regime_str = f"{best[0]} (EV: ${(best[1]['profit'] / max(1, best[1]['trades'])):.2f})"

                        report = (
                            f"📊 EXPERIMENT DIAGNOSTICS\n"
                            f"------------------------\n"
                            f"Status: {status_icon} {BOT_STATE['mode']}\n"
                            f"Uptime: {format_time(uptime)}\n"
                            f"Balance: ${BOT_STATE['balance']:.2f}\n\n"
                            f"💰 Profit: ${BOT_STATE['daily_profit']:.2f} (Peak: ${BOT_STATE['highest_profit']:.2f})\n"
                            f"🎯 Win/Loss: {BOT_STATE['wins']} W / {BOT_STATE['losses']} L\n\n"
                            f"🔬 Current Testing Regime: {explorer.current_regime}\n"
                            f"🏆 Best Regime So Far: {best_regime_str}"
                        )
                        await send_tg_alert(report)

                    # --- NEW LOG FILE COMMANDS ---
                    elif msg_lower == "/logs":
                        # Scan the root directory for CSV and JSON files
                        log_files = glob.glob("*.csv") + glob.glob("*.json")
                        if not log_files:
                            await send_tg_alert("📭 No log files found on the server.")
                        else:
                            file_list = "\n".join([f"📄 `{f}`\nDownload: `/getlog {f}`\n" for f in log_files])
                            await send_tg_alert(f"🗂 **Available Logs:**\n\n{file_list}")

                    elif msg_lower.startswith("/getlog "):
                        # Extract the filename
                        filename = msg.split(" ", 1)[1].strip()
                        
                        # Security: Prevent traversing out of the directory
                        if "/" in filename or "\\" in filename:
                            await send_tg_alert("⚠️ Security blocked: Invalid file path.")
                        elif not os.path.exists(filename):
                            await send_tg_alert(f"⚠️ File `{filename}` does not exist.")
                        else:
                            await send_tg_alert(f"📤 Preparing to upload `{filename}`...")
                            await send_tg_document(filename)

                    elif msg_lower == "/kill":
                        await send_tg_alert("💀 Kill command received. Shutting down server process completely.")
                        sys.exit(0)
                        
        except Exception as e:
            await asyncio.sleep(5)

# ==========================================
# ENGINE 2: CORE TRADING LOOP
# ==========================================
async def run_bot(explorer):
    api = DerivAPI(config.APP_ID, config.API_TOKEN)
    bollinger = LiveBollinger(window=20, stds=2.0)

    trade_state = "IDLE"  # IDLE, BUY_SENT, OPEN, COOLDOWN
    buy_request_time = 0.0
    cooldown_end = 0.0
    target_hit_flag = False
    trade_start_time = 0.0
    last_trade_attempt = 0.0
    MIN_TRADE_INTERVAL = 3

    open_trades = {}  # contract_id -> entry_data

    await send_tg_alert("⚙️ GAT Server Online. Standing by. Send `/start` to begin experiment.")

    while True:
        # 1. STANDBY MODE HANDLER
        if BOT_STATE["mode"] == "STOPPED":
            if not api.ws or api.ws.closed:
                await api.disconnect()
            trade_state = "IDLE"
            await asyncio.sleep(1)
            continue

        # 2. CONNECTION MANAGER
        if not api.ws or api.ws.closed:
            try:
                BOT_STATE["balance"] = await api.connect()
                await api.send({"ticks": SYMBOL, "subscribe": 1})
                # After reconnect, request open contracts for reconciliation
                await api.send({"portfolio": 1})
                print(f"Connected. Subscribed to {SYMBOL}.")
            except Exception as e:
                await send_tg_alert(f"⚠️ Connection Failed: {e}. Retrying in 5s...")
                await asyncio.sleep(5)
                continue

        # 3. SHIELD & GOAL CHECKER
        dynamic_stop = max(MAX_LOSS, BOT_STATE["highest_profit"] - 2.00)
        if BOT_STATE["daily_profit"] <= dynamic_stop and BOT_STATE["highest_profit"] > 0:
            BOT_STATE["mode"] = "STOPPED"
            trade_state = "IDLE"
            await send_tg_alert(f"🛡️ Shield Activated! Locked in: ${BOT_STATE['daily_profit']:.2f}. Standby Mode.")
            continue
        elif BOT_STATE["daily_profit"] <= MAX_LOSS:
            BOT_STATE["mode"] = "STOPPED"
            trade_state = "IDLE"
            await send_tg_alert(f"🛑 Max Loss Reached (-$2.00). Standby Mode.")
            continue

        # 4. WEBSOCKET LISTENER
        try:
            msg = await asyncio.wait_for(api.recv(), timeout=2.0)
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            print(f"WS Error: {e}")
            await api.disconnect()
            continue

        # --- Error Handling ---
        if 'error' in msg:
            print(f"API Error: {msg['error']}")
            trade_state = "IDLE"
            continue

        # --- Portfolio Reconciliation ---
        if 'portfolio' in msg:
            contracts = msg['portfolio'].get('contracts', [])
            if contracts:
                trade_state = "OPEN"
            else:
                trade_state = "IDLE"
            continue

        # --- BUY ACK TIMEOUT ---
        if trade_state == "BUY_SENT" and time.time() - buy_request_time > 10:
            print("Buy confirmation timeout. Resetting state.")
            trade_state = "IDLE"

        # --- COOLDOWN HANDLER ---
        if trade_state == "COOLDOWN" and time.time() >= cooldown_end:
            bollinger.prices.clear()
            trade_state = "IDLE"

        # --- ROUTE A: NEW TICK ---
        if 'tick' in msg:
            if trade_state != "IDLE":
                continue
            price = msg['tick']['quote']
            bollinger_data = bollinger.update(price)
            if bollinger_data and bollinger_data.get("percentile") is not None:
                percentile = bollinger_data["percentile"]
                if explorer.should_trade(percentile):
                    if BOT_STATE["mode"] == "ACTIVE":
                        # Rate limit protection
                        if time.time() - last_trade_attempt < MIN_TRADE_INTERVAL:
                            continue
                        last_trade_attempt = time.time()
                        print(f"Regime Match {explorer.current_regime}! Percentile: {percentile:.2f}. Executing...")
                        entry_data = {
                            "bandwidth": bollinger_data["bandwidth"],
                            "percentile": percentile,
                            "ma": bollinger_data["ma"],
                            "sd": bollinger_data["sd"]
                        }
                        buy_payload = {
                            "buy": 1,
                            "price": STAKE,
                            "parameters": {
                                "amount": STAKE,
                                "contract_type": "ACCU",
                                "symbol": SYMBOL,
                                "currency": "USD",
                                "growth_rate": GROWTH_RATE,
                                "limit_order": {"take_profit": 1.00}
                            }
                        }
                        await api.send(buy_payload)
                        trade_state = "BUY_SENT"
                        buy_request_time = time.time()
                        # Store entry data for this buy attempt (will be keyed by contract_id on confirmation)
                        pending_entry_data = entry_data
                    elif BOT_STATE["mode"] == "PAUSED":
                        print(f"Regime match found, but bot is PAUSED.", end="\r")

        # --- ROUTE B: TRADE OPENED ---
        elif 'buy' in msg:
            contract_id = msg['buy']['contract_id']
            trade_start_time = time.time()
            BOT_STATE["balance"] = msg['buy']['balance_after']
            await send_tg_alert(f"📈 Trade Opened! Staked ${STAKE}. ID: {contract_id}")
            await api.send({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1})
            # Store entry data keyed by contract_id
            open_trades[contract_id] = pending_entry_data.copy() if 'pending_entry_data' in locals() else {}
            trade_state = "OPEN"

        # --- ROUTE C: LIVE CONTRACT UPDATE ---
        elif 'proposal_open_contract' in msg:
            contract_info = msg['proposal_open_contract']
            contract_id = contract_info.get('contract_id')
            if contract_info.get('is_sold') == 1:
                trade_lifespan = time.time() - trade_start_time
                profit = contract_info.get('profit', 0)
                BOT_STATE["daily_profit"] += profit
                if BOT_STATE["daily_profit"] > BOT_STATE["highest_profit"]:
                    BOT_STATE["highest_profit"] = BOT_STATE["daily_profit"]
                # --- 1. UPDATE STATS ---
                regime_key = explorer.current_regime
                regime_stats[regime_key]["trades"] += 1
                regime_stats[regime_key]["profit"] += profit
                if profit > 0:
                    BOT_STATE["wins"] += 1
                    regime_stats[regime_key]["wins"] += 1
                    alert = f"✅ WIN in {trade_lifespan:.1f}s: +${profit:.2f} | Total: ${BOT_STATE['daily_profit']:.2f}"
                else:
                    BOT_STATE["losses"] += 1
                    exit_tick = contract_info.get('exit_tick_display_value', 'Unknown')
                    alert = f"❌ LOSS in {trade_lifespan:.1f}s (-${abs(profit):.2f})\nExit: {exit_tick} | Total: ${BOT_STATE['daily_profit']:.2f}"
                # Target check
                if BOT_STATE["daily_profit"] >= TARGET_PROFIT and not target_hit_flag:
                    target_hit_flag = True
                    elapsed = time.time() - BOT_STATE["session_start_time"]
                    alert += f"\n\n🎉 TARGET REACHED in {format_time(elapsed)}! Runner Mode Active."
                await send_tg_alert(alert)
                # --- 2. LOG TO CSV ---
                entry_data = open_trades.pop(contract_id, {})
                log_trade({
                    "timestamp": time.time(),
                    "regime_low": explorer.current_regime[0],
                    "regime_high": explorer.current_regime[1],
                    "bandwidth": entry_data.get("bandwidth", 0),
                    "percentile": entry_data.get("percentile", 0),
                    "ma": entry_data.get("ma", 0),
                    "sd": entry_data.get("sd", 0),
                    "profit": profit,
                    "duration": trade_lifespan,
                    "daily_profit": BOT_STATE["daily_profit"]
                })
                # --- 3. REGIME ROTATION CHECK ---
                rotated = explorer.record_trade()
                if rotated:
                    await send_tg_alert(f"🔄 Rotating Explorer Engine to new regime: {explorer.current_regime}")
                # --- 4. INDEPENDENCE COOLDOWN ---
                await send_tg_alert(f"⏱️ Initiating {COOLDOWN_SECONDS}s statistical cooldown...")
                cooldown_end = time.time() + COOLDOWN_SECONDS
                trade_state = "COOLDOWN"

# ==========================================
# LAUNCH SEQUENCE
# ==========================================
async def main_system():
    explorer = RegimeExplorer()
    await asyncio.gather(
        listen_for_commands(explorer),
        run_bot(explorer)
    )

if __name__ == "__main__":
    try:
        print("Starting GAT Scientific Daemon...")
        asyncio.run(main_system())
    except KeyboardInterrupt:
        print("\nSystem shut down manually via terminal.")