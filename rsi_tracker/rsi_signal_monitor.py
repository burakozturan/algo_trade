"""
RSI Signal Monitor  —  Live IBKR Edition
=========================================
Watches MAG7 in real-time. Fires Discord alert when either setup triggers:

  SHORT : 15m RSI > 70  AND  5m RSI crosses back below 70
  LONG  : 15m RSI < 30  AND  5m RSI crosses back above 30

Signals fire on 5m bar close (confirmed candle, not tick noise).
"""

import warnings
warnings.filterwarnings("ignore")

import os, time, threading
from datetime import datetime, timedelta
from pathlib  import Path
from urllib   import request as urllib_request
from zoneinfo import ZoneInfo
import json

import numpy  as np
import pandas as pd
from ib_insync import IB, Stock, BarDataList, util
from rich.console import Console
from rich.table   import Table
from rich.live    import Live
from rich         import box

# ── Config ────────────────────────────────────────────────────────────────────
TWS_HOST  = "127.0.0.1"
TWS_PORT  = 4001
CLIENT_ID = 17533943

MAG7 = ["AAPL", "MSFT", "AMZN", "META", "GOOGL", "NVDA", "TSLA"]

RSI_PERIOD   = 14
OB_LEVEL     = 70     # overbought
OS_LEVEL     = 30     # oversold
HOLD_MINUTES = 30     # suggested hold time shown in alert

WARMUP_DAYS  = 10     # days of history to seed RSI
REQUEST_DELAY = 0.5

ALERT_COOLDOWN_MIN = 60   # don't re-alert same ticker+direction within X min

NYC = ZoneInfo("America/New_York")
# ─────────────────────────────────────────────────────────────────────────────

# load .env
def _load_env():
    p = Path(__file__).parent / ".env"
    if not p.exists(): return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)

_load_env()

DISCORD_URLS = [
    os.getenv("DISCORD_WEBHOOK_URL_1", ""),
    os.getenv("DISCORD_WEBHOOK_URL_2", ""),
    os.getenv("DISCORD_WEBHOOK_URL_3", ""),
]
DISCORD_URLS = [u for u in DISCORD_URLS if u]

console    = Console()
state_lock = threading.Lock()

# ── Per-ticker state ──────────────────────────────────────────────────────────
# rsi_5m[ticker]  = rolling list of last N 5m closes  → recompute RSI each bar
# rsi_15m[ticker] = rolling list of last N 15m closes
closes_5m:  dict[str, list] = {}
closes_15m: dict[str, list] = {}
last_rsi_5m:  dict[str, float] = {}   # RSI of the previous closed 5m bar
last_rsi_15m: dict[str, float] = {}
last_alert:   dict[str, datetime] = {}   # key = "AAPL_SHORT" etc.
signal_log:   list = []                  # for the live table
# ─────────────────────────────────────────────────────────────────────────────


def compute_rsi_scalar(closes: list, period: int = 14) -> float | None:
    """Returns RSI of the last close in the list, or None if not enough data."""
    if len(closes) < period + 2:
        return None
    s     = pd.Series(closes, dtype=float)
    delta = s.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    ag    = gain.ewm(com=period - 1, min_periods=period).mean().iloc[-1]
    al    = loss.ewm(com=period - 1, min_periods=period).mean().iloc[-1]
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return round(100 - 100 / (1 + ag / al), 2)


def send_discord(message: str):
    payload = json.dumps({"content": message}).encode()
    for url in DISCORD_URLS:
        try:
            req = urllib_request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib_request.urlopen(req, timeout=5)
        except Exception as e:
            console.print(f"[red]Discord error: {e}[/red]")


def cooldown_ok(ticker: str, direction: str) -> bool:
    key = f"{ticker}_{direction}"
    last = last_alert.get(key)
    if last is None:
        return True
    return (datetime.now() - last).total_seconds() > ALERT_COOLDOWN_MIN * 60


def fire_signal(ticker: str, direction: str, rsi_5m: float,
                rsi_15m: float, price: float):
    key = f"{ticker}_{direction}"
    last_alert[key] = datetime.now()

    emoji   = "🔴 SHORT" if direction == "SHORT" else "🟢 LONG"
    setup   = ("15m RSI overbought, 5m exits below 70"
               if direction == "SHORT"
               else "15m RSI oversold, 5m exits above 30")
    stop_p  = ("above entry + 0.3%"
               if direction == "SHORT"
               else "below entry - 0.3%")

    msg = (
        f"{emoji}  **{ticker}**\n"
        f"Setup    : {setup}\n"
        f"Price    : ${price:.2f}\n"
        f"RSI 5m   : {rsi_5m:.1f}  |  RSI 15m: {rsi_15m:.1f}\n"
        f"Hold     : {HOLD_MINUTES} min\n"
        f"Stop     : {stop_p}\n"
        f"Time     : {datetime.now(NYC).strftime('%H:%M:%S ET')}"
    )

    send_discord(msg)

    with state_lock:
        signal_log.append({
            "time":      datetime.now(NYC).strftime("%H:%M"),
            "ticker":    ticker,
            "direction": direction,
            "price":     f"${price:.2f}",
            "rsi_5m":    f"{rsi_5m:.1f}",
            "rsi_15m":   f"{rsi_15m:.1f}",
        })

    console.print(
        f"[bold {'red' if direction == 'SHORT' else 'green'}]"
        f"SIGNAL  {emoji}  {ticker}  @ ${price:.2f}  "
        f"RSI5={rsi_5m:.1f}  RSI15={rsi_15m:.1f}[/bold]"
    )


def check_signals(ticker: str, prev_rsi_5m: float | None,
                  curr_rsi_5m: float, curr_rsi_15m: float, price: float):
    """Called on every confirmed 5m bar close."""

    if prev_rsi_5m is None:
        return

    # SHORT: 15m overbought, 5m just crossed back below 70
    if (curr_rsi_15m > OB_LEVEL and
            prev_rsi_5m > OB_LEVEL and curr_rsi_5m <= OB_LEVEL):
        if cooldown_ok(ticker, "SHORT"):
            fire_signal(ticker, "SHORT", curr_rsi_5m, curr_rsi_15m, price)

    # LONG: 15m oversold, 5m just crossed back above 30
    if (curr_rsi_15m < OS_LEVEL and
            prev_rsi_5m < OS_LEVEL and curr_rsi_5m >= OS_LEVEL):
        if cooldown_ok(ticker, "LONG"):
            fire_signal(ticker, "LONG", curr_rsi_5m, curr_rsi_15m, price)


# ── Warmup ────────────────────────────────────────────────────────────────────

def warmup(ib: IB):
    console.print("[yellow]Warming up RSI from IBKR history...[/yellow]")
    contracts = {t: Stock(t, "SMART", "USD") for t in MAG7}
    for t, c in contracts.items():
        ib.qualifyContracts(c)

    for ticker, contract in contracts.items():
        # 5m warmup
        time.sleep(REQUEST_DELAY)
        bars5 = ib.reqHistoricalData(
            contract, endDateTime="",
            durationStr=f"{WARMUP_DAYS} D", barSizeSetting="5 mins",
            whatToShow="TRADES", useRTH=True, formatDate=1, keepUpToDate=False,
        )
        # 15m warmup
        time.sleep(REQUEST_DELAY)
        bars15 = ib.reqHistoricalData(
            contract, endDateTime="",
            durationStr=f"{WARMUP_DAYS} D", barSizeSetting="15 mins",
            whatToShow="TRADES", useRTH=True, formatDate=1, keepUpToDate=False,
        )

        if bars5:
            closes_5m[ticker]  = [b.close for b in bars5]
            last_rsi_5m[ticker] = compute_rsi_scalar(closes_5m[ticker]) or 50.0
        if bars15:
            closes_15m[ticker]  = [b.close for b in bars15]
            last_rsi_15m[ticker] = compute_rsi_scalar(closes_15m[ticker]) or 50.0

        console.print(
            f"  {ticker}: 5m bars={len(bars5 or [])}  "
            f"RSI5={last_rsi_5m.get(ticker, '?'):.1f}  "
            f"RSI15={last_rsi_15m.get(ticker, '?'):.1f}"
        )

    console.print("[green]Warmup complete.[/green]\n")


# ── Live bar callbacks ────────────────────────────────────────────────────────

def make_bar_handler(ticker: str):
    def on_bar_update(bars: BarDataList, has_new_bar: bool):
        if not has_new_bar:
            return
        if len(bars) < 2:
            return

        closed_bar  = bars[-2]   # last CONFIRMED closed bar
        latest_open = bars[-1]   # currently forming bar

        close_price = closed_bar.close

        with state_lock:
            # update 5m closes
            cl5 = closes_5m.get(ticker, [])
            cl5.append(close_price)
            if len(cl5) > 200:
                cl5 = cl5[-200:]
            closes_5m[ticker] = cl5

            prev_r5 = last_rsi_5m.get(ticker)
            curr_r5 = compute_rsi_scalar(cl5)
            if curr_r5 is not None:
                last_rsi_5m[ticker] = curr_r5

            # 15m RSI — recompute from stored closes (updated by 15m subscription)
            curr_r15 = last_rsi_15m.get(ticker, 50.0)

        if curr_r5 is not None:
            check_signals(ticker, prev_r5, curr_r5, curr_r15, close_price)

    return on_bar_update


def make_bar15_handler(ticker: str):
    def on_bar_update(bars: BarDataList, has_new_bar: bool):
        if not has_new_bar or len(bars) < 2:
            return
        closed_bar = bars[-2]
        with state_lock:
            cl15 = closes_15m.get(ticker, [])
            cl15.append(closed_bar.close)
            if len(cl15) > 200:
                cl15 = cl15[-200:]
            closes_15m[ticker] = cl15
            r15 = compute_rsi_scalar(cl15)
            if r15 is not None:
                last_rsi_15m[ticker] = r15
    return on_bar_update


# ── Live dashboard ────────────────────────────────────────────────────────────

def make_table() -> Table:
    t = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan")
    t.add_column("Ticker", width=7)
    t.add_column("RSI 5m",  width=8)
    t.add_column("RSI 15m", width=9)
    t.add_column("5m zone", width=12)
    t.add_column("15m zone", width=12)
    t.add_column("Watch", width=22)

    def zone(v):
        if v is None: return "—", "white"
        if v > OB_LEVEL: return "OVERBOUGHT", "red"
        if v < OS_LEVEL: return "OVERSOLD",   "green"
        return "neutral", "white"

    with state_lock:
        for ticker in MAG7:
            r5  = last_rsi_5m.get(ticker)
            r15 = last_rsi_15m.get(ticker)
            z5,  c5  = zone(r5)
            z15, c15 = zone(r15)

            watch = ""
            if r5 is not None and r15 is not None:
                if r15 > OB_LEVEL and r5 > OB_LEVEL:
                    watch = "[red]⚡ Both OB — watch SHORT[/red]"
                elif r15 < OS_LEVEL and r5 < OS_LEVEL:
                    watch = "[green]⚡ Both OS — watch LONG[/green]"
                elif r15 > OB_LEVEL and r5 <= OB_LEVEL:
                    watch = "[red]🔴 SHORT signal fired![/red]"
                elif r15 < OS_LEVEL and r5 >= OS_LEVEL:
                    watch = "[green]🟢 LONG signal fired![/green]"

            t.add_row(
                ticker,
                f"[{c5}]{r5:.1f}[/{c5}]"  if r5  is not None else "—",
                f"[{c15}]{r15:.1f}[/{c15}]" if r15 is not None else "—",
                f"[{c5}]{z5}[/{c5}]",
                f"[{c15}]{z15}[/{c15}]",
                watch,
            )
    return t


def make_log_table() -> Table:
    t = Table(box=box.SIMPLE, show_header=True, header_style="bold white",
              title="Recent Signals")
    t.add_column("Time",  width=6)
    t.add_column("Ticker", width=7)
    t.add_column("Side",  width=7)
    t.add_column("Price", width=8)
    t.add_column("RSI5",  width=6)
    t.add_column("RSI15", width=7)
    with state_lock:
        for row in signal_log[-8:]:
            color = "red" if row["direction"] == "SHORT" else "green"
            t.add_row(
                row["time"], row["ticker"],
                f"[{color}]{row['direction']}[/{color}]",
                row["price"], row["rsi_5m"], row["rsi_15m"],
            )
    return t


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    ib = IB()
    console.print(f"[cyan]Connecting {TWS_HOST}:{TWS_PORT}...[/cyan]")
    ib.connect(TWS_HOST, TWS_PORT, clientId=CLIENT_ID)
    console.print("[green]Connected.[/green]\n")

    warmup(ib)

    console.print("[yellow]Subscribing to live 5m + 15m bars...[/yellow]")
    live_5m:  dict[str, BarDataList] = {}
    live_15m: dict[str, BarDataList] = {}

    for ticker in MAG7:
        contract = Stock(ticker, "SMART", "USD")
        ib.qualifyContracts(contract)
        time.sleep(0.3)

        bars5 = ib.reqRealTimeBars(contract, 5, "TRADES", useRTH=False)
        # realTimeBars gives 5-sec bars; we use reqHistoricalData keepUpToDate for 5m
        bars5.updateEvent += make_bar_handler(ticker)
        live_5m[ticker] = bars5

        b5m = ib.reqHistoricalData(
            contract, endDateTime="", durationStr="2 D",
            barSizeSetting="5 mins", whatToShow="TRADES",
            useRTH=False, formatDate=1, keepUpToDate=True,
        )
        b5m.updateEvent += make_bar_handler(ticker)

        b15m = ib.reqHistoricalData(
            contract, endDateTime="", durationStr="5 D",
            barSizeSetting="15 mins", whatToShow="TRADES",
            useRTH=False, formatDate=1, keepUpToDate=True,
        )
        b15m.updateEvent += make_bar15_handler(ticker)

        live_5m[ticker]  = b5m
        live_15m[ticker] = b15m
        console.print(f"  {ticker} subscribed")

    console.print("\n[green]Live. Watching for signals...[/green]")
    console.print(f"  SHORT trigger: 15m RSI > {OB_LEVEL}  AND  5m crosses below {OB_LEVEL}")
    console.print(f"  LONG  trigger: 15m RSI < {OS_LEVEL}  AND  5m crosses above {OS_LEVEL}")
    console.print(f"  Alerts → Discord  |  Cooldown: {ALERT_COOLDOWN_MIN} min per ticker\n")

    with Live(console=console, refresh_per_second=0.2) as live:
        while True:
            from rich.columns import Columns
            from rich.padding  import Padding
            display = Columns([
                Padding(make_table(),     (0, 2)),
                Padding(make_log_table(), (0, 2)),
            ])
            live.update(display)
            ib.sleep(5)


if __name__ == "__main__":
    run()
