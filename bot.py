import time
import os
import json
from datetime import datetime
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from config import Config
from kalshi_client import KalshiClient
from discord_notifier import DiscordNotifier

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is healthy and running!")

    def log_message(self, format, *args):
        # Silence standard HTTP logs to keep console clean
        pass

def start_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        print(f"[Web Server] Health check server listening on 0.0.0.0:{port}")
        server.serve_forever()
    except Exception as e:
        print(f"[Web Server Error] Failed to start HTTP server: {e}")

def load_state() -> dict:
    default_state = {
        "last_fill_time": "",
        "processed_fill_ids": [],
        "last_settlement_time": "",
        "processed_settlement_ids": [],
        "monthly_stats": {}
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for k, v in default_state.items():
                    if k not in loaded:
                        loaded[k] = v
                return loaded
        except Exception as e:
            print(f"[State Warning] Failed to load state: {e}. Resetting state.")
    return default_state

def save_state(state: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4)
    except Exception as e:
        print(f"[State Error] Failed to save state: {e}")

def main():
    print("==================================================")
    print("        KALSHI TO DISCORD COPY-BETTING BOT        ")
    print("==================================================")
    
    # 1. Initialize configuration and clients
    config = Config()
    client = KalshiClient(config)
    notifier = DiscordNotifier(config.discord_webhook_url)

    # Start health check server in background thread for free hosting (Render / UptimeRobot)
    web_thread = threading.Thread(target=start_health_check_server, daemon=True)
    web_thread.start()

    print(f"Environment: {config.environment.upper()}")
    print(f"Base URL: {config.base_url}")
    print(f"Polling Interval: {config.poll_interval} seconds")
    print(f"State File: {STATE_FILE}")

    # 2. Load state
    state = load_state()
    last_fill_time = state.get("last_fill_time", "")
    processed_fill_ids = set(state.get("processed_fill_ids", []))
    processed_settlement_ids = set(state.get("processed_settlement_ids", []))
    last_settlement_time = state.get("last_settlement_time", "")
    
    # In-memory cache for market details to avoid duplicate requests
    market_cache = {}

    # 3. Synchronize on first run to avoid backfilling old trades
    print("\nSynchronizing with Kalshi API...")
    resp = client.get_fills(limit=50)
    fills = resp.get("fills", [])
    
    if not last_fill_time and fills:
        # First run: Mark all recent fills as already processed
        print("First-time run detected. Indexing recent fills to prevent duplicate spam...")
        latest_fill = fills[0]
        state["last_fill_time"] = latest_fill.get("created_time", "")
        for f in fills:
            fid = f.get("fill_id")
            if fid:
                processed_fill_ids.add(fid)
        state["processed_fill_ids"] = list(processed_fill_ids)
        save_state(state)
        print(f"Synchronized! Marked {len(fills)} past fills as processed.")
        print(f"Monitoring starting from fill timestamp: {state['last_fill_time']}")
    else:
        print("Sync complete. Bot is actively monitoring for new trades.")

    # Synchronize settlements
    if config.wins_webhook_url or config.loss_webhook_url:
        print("Synchronizing settlements with Kalshi API...")
        s_resp = client.get_settlements(limit=50)
        settlements = s_resp.get("settlements", [])
        if not last_settlement_time and settlements:
            print("First-time settlement run detected. Indexing recent settlements and backfilling monthly stats...")
            latest_s = settlements[0]
            state["last_settlement_time"] = latest_s.get("settled_time", "")
            
            # Initialize monthly_stats in state if not exists
            if "monthly_stats" not in state:
                state["monthly_stats"] = {}
                
            for s in settlements:
                ticker = s.get("ticker")
                settled_time = s.get("settled_time", "")
                sid = f"{ticker}_{settled_time}"
                processed_settlement_ids.add(sid)
                
                # Calculate stats for historical settlements
                yes_cost = float(s.get("yes_total_cost_dollars", 0.0))
                no_cost = float(s.get("no_total_cost_dollars", 0.0))
                total_cost = yes_cost + no_cost
                fee_cost = float(s.get("fee_cost", 0.0))
                revenue = float(s.get("revenue", 0)) / 100.0
                net_profit = revenue - total_cost - fee_cost
                
                is_win = net_profit > 0
                month_key = settled_time[:7]  # YYYY-MM
                if month_key not in state["monthly_stats"]:
                    state["monthly_stats"][month_key] = {
                        "month": month_key,
                        "wins": 0,
                        "losses": 0,
                        "net_profit": 0.0
                    }
                
                m_stats = state["monthly_stats"][month_key]
                if is_win:
                    m_stats["wins"] += 1
                else:
                    m_stats["losses"] += 1
                m_stats["net_profit"] += net_profit
                
            state["processed_settlement_ids"] = list(processed_settlement_ids)
            save_state(state)
            
            # Send initial scorecard update for the current month if webhook is set
            current_month = datetime.utcnow().strftime("%Y-%m")
            if current_month in state["monthly_stats"] and config.monthly_stats_webhook_url:
                print(f"Sending initial monthly stats scorecard for {current_month}...")
                notifier.send_monthly_stats_notification(config.monthly_stats_webhook_url, state["monthly_stats"][current_month])
                
            print(f"Synchronized! Marked {len(settlements)} past settlements as processed and backfilled monthly stats.")
            print(f"Monitoring settlements starting from timestamp: {state['last_settlement_time']}")
        else:
            print("Settlements sync complete. Active monitoring for wins/losses enabled.")

    print("\nBot is running. Press Ctrl+C to exit.")

    # 4. Main Polling Loop
    while True:
        try:
            time.sleep(config.poll_interval)
            
            # Poll settlements for wins/losses if configured
            if config.wins_webhook_url or config.loss_webhook_url:
                s_resp = client.get_settlements(limit=10)
                settlements = s_resp.get("settlements", [])
                settlements_sorted = sorted(settlements, key=lambda x: x.get("settled_time", ""))
                
                new_settlements_processed = False
                for s in settlements_sorted:
                    ticker = s.get("ticker")
                    settled_time = s.get("settled_time", "")
                    sid = f"{ticker}_{settled_time}"
                    
                    if sid in processed_settlement_ids:
                        continue
                    if last_settlement_time and settled_time < last_settlement_time:
                        continue
                        
                    # Calculate net profit to determine if it is a win/loss
                    yes_cost = float(s.get("yes_total_cost_dollars", 0.0))
                    no_cost = float(s.get("no_total_cost_dollars", 0.0))
                    total_cost = yes_cost + no_cost
                    fee_cost = float(s.get("fee_cost", 0.0))
                    revenue = float(s.get("revenue", 0)) / 100.0
                    net_profit = revenue - total_cost - fee_cost
                    
                    is_win = net_profit > 0
                    notification_success = False
                    
                    if is_win:
                        if config.wins_webhook_url:
                            # Fetch market details
                            if ticker not in market_cache:
                                print(f"Retrieving market details for settled market {ticker}...")
                                market_cache[ticker] = client.get_market(ticker)
                            market = market_cache[ticker]
                            
                            success = notifier.send_win_notification(config.wins_webhook_url, s, market)
                            if success:
                                notification_success = True
                            else:
                                print(f"[Retry Warning] Discord win notification failed for {ticker}. Will retry.")
                        else:
                            notification_success = True
                    else:
                        if config.loss_webhook_url:
                            # Fetch market details
                            if ticker not in market_cache:
                                print(f"Retrieving market details for settled market {ticker}...")
                                market_cache[ticker] = client.get_market(ticker)
                            market = market_cache[ticker]
                            
                            success = notifier.send_loss_notification(config.loss_webhook_url, s, market)
                            if success:
                                notification_success = True
                            else:
                                print(f"[Retry Warning] Discord loss notification failed for {ticker}. Will retry.")
                        else:
                            notification_success = True
                            
                    if notification_success:
                        processed_settlement_ids.add(sid)
                        if not last_settlement_time or settled_time > last_settlement_time:
                            last_settlement_time = settled_time
                        new_settlements_processed = True
                        
                        # Update monthly stats in state
                        month_key = settled_time[:7]  # YYYY-MM
                        if "monthly_stats" not in state:
                            state["monthly_stats"] = {}
                        if month_key not in state["monthly_stats"]:
                            state["monthly_stats"][month_key] = {
                                "month": month_key,
                                "wins": 0,
                                "losses": 0,
                                "net_profit": 0.0
                            }
                        
                        m_stats = state["monthly_stats"][month_key]
                        if is_win:
                            m_stats["wins"] += 1
                        else:
                            m_stats["losses"] += 1
                        m_stats["net_profit"] += net_profit
                        
                        # Trigger monthly scorecard update to Discord
                        if config.monthly_stats_webhook_url:
                            notifier.send_monthly_stats_notification(config.monthly_stats_webhook_url, m_stats)
                        
                if new_settlements_processed:
                    # Keep processed IDs set reasonably sized
                    if len(processed_settlement_ids) > 500:
                        sorted_sids = sorted(list(processed_settlement_ids))[:300]
                        for osid in sorted_sids:
                            processed_settlement_ids.discard(osid)
                            
                    state["last_settlement_time"] = last_settlement_time
                    state["processed_settlement_ids"] = list(processed_settlement_ids)
                    save_state(state)

            resp = client.get_fills(limit=20)
            fills = resp.get("fills", [])
            if not fills:
                continue

            # Sort fills in chronological order (oldest first)
            # Fills from API are sorted newest first, so we reverse it
            fills_sorted = sorted(fills, key=lambda x: x.get("created_time", ""))

            # Filter out already processed fills
            new_fills = []
            for fill in fills_sorted:
                fill_id = fill.get("fill_id")
                created_time = fill.get("created_time", "")
                if fill_id in processed_fill_ids:
                    continue
                if last_fill_time and created_time < last_fill_time:
                    continue
                new_fills.append(fill)

            if not new_fills:
                continue

            # Aggregate fills of the same market, action, and side that come in the same batch
            # This prevents multiple notifications for partial fills of a single order.
            aggregated_fills = {}
            new_fills_processed = False

            for fill in new_fills:
                ticker = fill.get("ticker") or fill.get("market_ticker")
                action = fill.get("action", "buy").upper()
                side = fill.get("side", "").upper()
                if not side:
                    if fill.get("yes_price_dollars"): side = "YES"
                    elif fill.get("no_price_dollars"): side = "NO"
                    else: side = "YES"
                
                key = (ticker, action, side)
                
                # Get price safely
                price = 0.0
                yes_str = fill.get("yes_price_dollars")
                no_str = fill.get("no_price_dollars")
                if side == "YES" and yes_str: price = float(yes_str)
                elif side == "NO" and no_str: price = float(no_str)
                elif yes_str: 
                    price = float(yes_str)
                    if side == "NO": price = 1.0 - price
                
                count = float(fill.get("count_fp", 0))

                if key not in aggregated_fills:
                    aggregated_fills[key] = {
                        "base_fill": fill.copy(),
                        "original_fills": [fill],
                        "total_cost": price * count,
                        "total_count": count
                    }
                else:
                    agg = aggregated_fills[key]
                    agg["original_fills"].append(fill)
                    agg["total_cost"] += (price * count)
                    agg["total_count"] += count

            for key, agg in aggregated_fills.items():
                fill = agg["base_fill"]
                count = agg["total_count"]
                
                if count > 0:
                    avg_price = agg["total_cost"] / count
                else:
                    avg_price = 0.0
                    
                fill["count_fp"] = count
                
                if key[2] == "YES":
                    fill["yes_price_dollars"] = str(avg_price)
                    fill["no_price_dollars"] = str(1.0 - avg_price)
                else:
                    fill["no_price_dollars"] = str(avg_price)
                    fill["yes_price_dollars"] = str(1.0 - avg_price)

                ticker = key[0]
                
                print(f"\n[New Trade Fill] Processing aggregated fill for {ticker} ({int(count)} contracts)")

                if ticker not in market_cache:
                    print(f"Retrieving market details for {ticker}...")
                    market_cache[ticker] = client.get_market(ticker)
                
                market = market_cache[ticker]

                # Post notification to Discord (always single fill)
                success = notifier.send_fill_notification(fill, market, config.environment)
                
                if success:
                    for orig_fill in agg["original_fills"]:
                        fid = orig_fill.get("fill_id")
                        ctime = orig_fill.get("created_time", "")
                        processed_fill_ids.add(fid)
                        if not last_fill_time or ctime > last_fill_time:
                            last_fill_time = ctime
                    new_fills_processed = True
                else:
                    print(f"[Retry Warning] Discord notification failed for aggregated fill {ticker}. Will retry next poll.")


            if new_fills_processed:
                # Keep processed IDs set reasonably sized (keep last 500)
                if len(processed_fill_ids) > 500:
                    sorted_ids = sorted(list(processed_fill_ids), key=lambda x: x)[:300]
                    for oid in sorted_ids:
                        processed_fill_ids.discard(oid)
                        
                state["last_fill_time"] = last_fill_time
                state["processed_fill_ids"] = list(processed_fill_ids)
                save_state(state)

        except KeyboardInterrupt:
            print("\nShutting down bot. Goodbye!")
            break
        except Exception as e:
            print(f"[Loop Exception] Unexpected error: {e}")
            time.sleep(10) # Wait a bit before retrying after a crash

if __name__ == "__main__":
    main()
