"""
Live Multi-Timeframe RSI Tracker  —  Interactive Brokers TWS Edition
=====================================================================
RSI uses TradingView-compatible Wilder smoothing (SMA seed + exponential continuation).

Requirements:
    pip install ib_insync pandas rich numpy pyarrow

Setup:
    1. Open IB Gateway (port 4001) or TWS (port 7496)
    2. Enable API: Configure → Settings → API → Settings
       - Uncheck Read-Only API
       - Socket port: 4001
       - Add 127.0.0.1 to Trusted IPs
    3. Run: python3 rsi_tracker_tws.py
"""

import asyncio
import time
import threading
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
from zoneinfo import ZoneInfo

from ib_insync import IB, Stock, BarDataList, util

from rich.console import Console
from rich.table   import Table
from rich.live    import Live
from rich.panel   import Panel
from rich.text    import Text
from rich         import box
import os
from dotenv import load_dotenv

# ─────────────────────────────────────────────
#  CONFIG (loaded from environment when available)
# ─────────────────────────────────────────────
load_dotenv()

# IB / TWS connection defaults (override via .env)
TWS_HOST  = os.getenv("TWS_HOST", "127.0.0.1")
TWS_PORT  = int(os.getenv("TWS_PORT", os.getenv("TWS_SOCKET", "4001")))
CLIENT_ID = int(os.getenv("CLIENT_ID", "17533939"))

STOCKS = [
    "AAPL", "AMZN", "GOOG", "META",
    "NVDA", "MSFT", "RKLB",
    "ASTS", "NBIS", "TXN", "AMAT", "MU", "IREN",
]

REALTIME_MODE = "hybrid"  # "tickbytick" | "hybrid" | "mktdata"
TICK_BY_TICK_TICKERS = {"AAPL", "AMZN", "GOOG", "META", "NVDA", "MSFT"}
USE_RTH = True
INTRABAR_PRICE_SOURCE = "trade"  # "trade" | "market"

RSI_PERIOD = 14

THRESHOLDS = {
    "1m":  {"overbought": 70, "oversold": 30},
    "5m":  {"overbought": 70, "oversold": 30},
    "15m": {"overbought": 70, "oversold": 30},
    "1h":  {"overbought": 70, "oversold": 30},
}

WARMUP_DAYS    = 5
CACHE_DIR      = Path(__file__).parent / "cache"
RESAMPLE_RULES = {"5m": "5min", "15m": "15min", "1h": "1h"}
NYC            = ZoneInfo("America/New_York")
MARKET_OPEN    = "09:30"
EXCHANGE       = "SMART"
CURRENCY       = "USD"
# ─────────────────────────────────────────────

console    = Console()
CACHE_DIR.mkdir(exist_ok=True)

bar_cache:       Dict[str, pd.DataFrame] = {}
rsi_state:       Dict[str, dict]         = {}
last_update:     Dict[str, str]          = {}
live_bars:       Dict[str, BarDataList]  = {}
live_tickers:    Dict[str, object]       = {}
live_tickbytick: Dict[str, object]       = {}
cache_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    return df


def ensure_datetimeindex(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    elif df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    df.index.name = "t"
    return df


# ══════════════════════════════════════════════════════════════════════════════
#  RSI  — TradingView-compatible Wilder smoothing (SMA seed)
# ══════════════════════════════════════════════════════════════════════════════
def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """
    Wilder RSI matching TradingView:
      1. Compute gains and losses from close.diff()
      2. Seed avg_gain / avg_loss with SMA of first `period` changes
      3. Continue with exponential smoothing:
         avg = (prev_avg * (period - 1) + current) / period
    """
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)

    n = len(close)
    avg_gain = np.full(n, np.nan)
    avg_loss = np.full(n, np.nan)

    # Need at least period+1 bars (period changes) to seed
    if n < period + 1:
        return pd.Series(np.nan, index=close.index)

    # SMA seed over the first `period` changes (indices 1..period)
    avg_gain[period] = gain.iloc[1:period + 1].mean()
    avg_loss[period] = loss.iloc[1:period + 1].mean()

    # Wilder exponential smoothing
    for i in range(period + 1, n):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain.iloc[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss.iloc[i]) / period

    with np.errstate(divide="ignore", invalid="ignore"):
        rs  = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))

    rsi_series = pd.Series(rsi, index=close.index)
    # Where avg_loss is 0 and avg_gain > 0 → RSI = 100
    rsi_series[(avg_loss == 0) & (avg_gain > 0)] = 100.0
    # Where both are 0 → RSI = 50 (no movement)
    rsi_series[(avg_loss == 0) & (avg_gain == 0)] = 50.0

    return rsi_series


def last_rsi(df: pd.DataFrame) -> Optional[float]:
    if df is None or len(df) < RSI_PERIOD + 2:
        return None
    df = normalize(df)
    v  = calc_rsi(df["close"], RSI_PERIOD).dropna()
    return round(float(v.iloc[-1]), 2) if not v.empty else None


def compute_all_rsi(df1m: pd.DataFrame) -> dict:
    df1m = normalize(df1m)
    df1m = ensure_datetimeindex(df1m)
    results = {"1m": last_rsi(df1m)}

    for tf, rule in RESAMPLE_RULES.items():
        try:
            local = df1m.tz_convert(NYC)

            # Anchor resampling to market open (9:30 ET) so candle
            # boundaries match TradingView for all timeframes.
            resampled = local.resample(
                rule,
                label="right",
                closed="left",
                origin="epoch",
                offset="30min",        # 9:30 offset from midnight
            ).agg({
                "open":   "first",
                "high":   "max",
                "low":    "min",
                "close":  "last",
                "volume": "sum",
            }).dropna(subset=["close"]).tz_convert("UTC")

            results[tf] = last_rsi(resampled)
        except Exception:
            results[tf] = None

    return results


# ══════════════════════════════════════════════════════════════════════════════
#  CACHE
# ══════════════════════════════════════════════════════════════════════════════
def cache_path(ticker: str) -> Path:
    return CACHE_DIR / f"{ticker}_1m.parquet"


def load_cache(ticker: str) -> pd.DataFrame:
    p = cache_path(ticker)
    if not p.exists():
        return pd.DataFrame()
    try:
        df = pd.read_parquet(p)
        df = ensure_datetimeindex(df)
        return df.sort_index()
    except Exception:
        return pd.DataFrame()


def save_cache(ticker: str, df: pd.DataFrame):
    try:
        df.to_parquet(cache_path(ticker))
    except Exception:
        pass


def merge_bars(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    if existing.empty:
        return new
    if new.empty:
        return existing
    combined = pd.concat([existing, new])
    return combined[~combined.index.duplicated(keep="last")].sort_index()


# ══════════════════════════════════════════════════════════════════════════════
#  IB BAR CONVERSION
# ══════════════════════════════════════════════════════════════════════════════
def ib_bars_to_df(bars) -> pd.DataFrame:
    records = []
    for bar in bars:
        ts = pd.Timestamp(bar.date)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        records.append({
            "t":      ts,
            "open":   float(bar.open),
            "high":   float(bar.high),
            "low":    float(bar.low),
            "close":  float(bar.close),
            "volume": float(bar.volume),
        })
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records).set_index("t")
    return df.sort_index()


# ══════════════════════════════════════════════════════════════════════════════
#  WARMUP
# ══════════════════════════════════════════════════════════════════════════════
async def fetch_historical_async(ib: IB, ticker: str,
                                  cached: pd.DataFrame) -> tuple:
    contract = Stock(ticker, EXCHANGE, CURRENCY)
    await ib.qualifyContractsAsync(contract)
    try:
        bars = await ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=f"{WARMUP_DAYS} D",
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=USE_RTH,
            formatDate=2,
            keepUpToDate=False,
        )
        fresh = ib_bars_to_df(bars)
    except Exception as e:
        console.print(f"[red]  {ticker} fetch failed: {e}[/]")
        fresh = pd.DataFrame()

    merged = merge_bars(cached, fresh)
    return ticker, fresh, merged


async def warmup_async(ib: IB):
    console.print("\n[bold yellow]Warming up RSI — fetching all tickers in parallel…[/]\n")
    caches  = {t: load_cache(t) for t in STOCKS}
    tasks   = [fetch_historical_async(ib, t, caches[t]) for t in STOCKS]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            console.print(f"[red]Error: {result}[/]")
            continue
        ticker, fresh, merged = result
        if merged.empty:
            console.print(f"  [white]{ticker}[/] [red]no data[/]")
            with cache_lock:
                rsi_state[ticker] = {tf: None for tf in THRESHOLDS}
            continue
        with cache_lock:
            bar_cache[ticker] = merged
            rsi_state[ticker] = compute_all_rsi(merged)
        save_cache(ticker, merged)
        console.print(
            f"  [white]{ticker}[/] [green]{len(merged)} bars  "
            f"(+{len(fresh)} new)  "
            f"RSI 1m={rsi_state[ticker]['1m']}[/]"
        )

    console.print("\n[bold green]Warmup complete — subscribing to live bars…[/]\n")
    time.sleep(1)


def warmup(ib: IB):
    ib.run(warmup_async(ib))


# ══════════════════════════════════════════════════════════════════════════════
#  LIVE 1m BAR SUBSCRIPTION  (fires on bar close)
# ══════════════════════════════════════════════════════════════════════════════
def subscribe_live(ib: IB):
    for ticker in STOCKS:
        contract = Stock(ticker, EXCHANGE, CURRENCY)
        ib.qualifyContracts(contract)

        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr="1 D",
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=USE_RTH,
            formatDate=2,
            keepUpToDate=True,
        )

        def make_handler(sym):
            def on_bar_update(bars, hasNewBar):
                if not hasNewBar:
                    return
                bar = bars[-1]
                ts  = pd.Timestamp(bar.date)
                ts  = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
                new_row = pd.DataFrame(
                    [{
                        "open":   float(bar.open),
                        "high":   float(bar.high),
                        "low":    float(bar.low),
                        "close":  float(bar.close),
                        "volume": float(bar.volume),
                    }],
                    index=pd.DatetimeIndex([ts], name="t"),
                )
                with cache_lock:
                    existing          = bar_cache.get(sym, pd.DataFrame())
                    merged            = merge_bars(existing, new_row)
                    bar_cache[sym]    = merged
                    rsi_state[sym]    = compute_all_rsi(merged)
                    last_update[sym]  = ts.tz_convert(NYC).strftime("%H:%M:%S")
                save_cache(sym, merged)
            return on_bar_update

        bars.updateEvent += make_handler(ticker)
        live_bars[ticker] = bars
        console.print(f"  [green]Subscribed 1m bars: {ticker}[/]")
        time.sleep(0.3)


# ══════════════════════════════════════════════════════════════════════════════
#  REAL-TIME PRICE SUBSCRIPTION  (updates RSI on every tick)
# ══════════════════════════════════════════════════════════════════════════════
def subscribe_realtime(ib: IB):
    ib.reqMarketDataType(1)

    mode = REALTIME_MODE.strip().lower()
    valid_modes = {"tickbytick", "hybrid", "mktdata"}
    if mode not in valid_modes:
        raise ValueError(
            f"Invalid REALTIME_MODE={REALTIME_MODE!r}. "
            f"Use one of: {sorted(valid_modes)}"
        )

    price_source = INTRABAR_PRICE_SOURCE.strip().lower()
    valid_price_sources = {"trade", "market"}
    if price_source not in valid_price_sources:
        raise ValueError(
            f"Invalid INTRABAR_PRICE_SOURCE={INTRABAR_PRICE_SOURCE!r}. "
            f"Use one of: {sorted(valid_price_sources)}"
        )

    if mode == "tickbytick":
        tick_by_tick_symbols = set(STOCKS)
    elif mode == "mktdata":
        tick_by_tick_symbols = set()
    else:
        tick_by_tick_symbols = set(STOCKS).intersection(TICK_BY_TICK_TICKERS)

    console.print(f"[yellow]Realtime mode:[/] [bold]{mode}[/]")

    def apply_intrabar_price(sym: str, price: Optional[float]):
        if price is None or pd.isna(price) or price <= 0:
            return
        with cache_lock:
            df = bar_cache.get(sym)
            if df is None or df.empty:
                return
            df.iat[-1, df.columns.get_loc("close")] = float(price)
            rsi_state[sym] = compute_all_rsi(df)

    def make_price_handler(sym):
        def on_price_change(ticker, *_):
            price = None
            if price_source == "trade":
                for fallback in (ticker.last, ticker.close):
                    if (fallback is not None
                            and not pd.isna(fallback) and fallback > 0):
                        price = fallback
                        break
            else:
                price = ticker.marketPrice()
                if pd.isna(price) or price <= 0:
                    for fallback in (ticker.last, ticker.close,
                                     ticker.bid, ticker.ask):
                        if (fallback is not None
                                and not pd.isna(fallback) and fallback > 0):
                            price = fallback
                            break
            apply_intrabar_price(sym, price)
        return on_price_change

    def make_tick_handler(sym):
        def on_tick_change(ticks, *_):
            if not ticks:
                return
            price = getattr(ticks[-1], "price", None)
            apply_intrabar_price(sym, price)
        return on_tick_change

    for ticker in STOCKS:
        contract = Stock(ticker, EXCHANGE, CURRENCY)
        ib.qualifyContracts(contract)

        if ticker in tick_by_tick_symbols:
            ticks = ib.reqTickByTickData(contract, "Last", 0, False)
            ticks.updateEvent += make_tick_handler(ticker)
            live_tickbytick[ticker] = ticks
            console.print(f"  [green]Tick-by-tick: {ticker}[/]")
        else:
            t = ib.reqMktData(contract, "", False, False)
            t.updateEvent += make_price_handler(ticker)
            live_tickers[ticker] = t
            console.print(f"  [green]MktData tick: {ticker}[/]")
        time.sleep(0.2)

    console.print("[green]Real-time price subscriptions active[/]\n")


# ══════════════════════════════════════════════════════════════════════════════
#  UI
# ══════════════════════════════════════════════════════════════════════════════
def classify(rsi: Optional[float], tf: str) -> tuple:
    if rsi is None:
        return "N/A", "grey50"
    ob  = THRESHOLDS[tf]["overbought"]
    os_ = THRESHOLDS[tf]["oversold"]
    if rsi >= ob:
        return f"{rsi} ▲OB", "bright_red"
    if rsi <= os_:
        return f"{rsi} ▼OS", "bright_cyan"
    return str(rsi), "white"


def overall_signal(rsi_map: dict) -> tuple:
    valid = {tf: v for tf, v in rsi_map.items() if v is not None}
    if not valid:
        return "NO DATA", "grey50"
    ob = sum(1 for tf, v in valid.items()
             if v >= THRESHOLDS[tf]["overbought"])
    os_ = sum(1 for tf, v in valid.items()
              if v <= THRESHOLDS[tf]["oversold"])
    n = len(valid)
    if ob == n:   return "🔴 SELL",      "bright_red"
    if os_ == n:  return "🟢 BUY",       "bright_green"
    if ob >= 2:   return "🟠 WEAK SELL", "dark_orange"
    if os_ >= 2:  return "🔵 WEAK BUY",  "cyan1"
    return "⚪ NEUTRAL", "grey70"


def build_table() -> Table:
    tbl = Table(
        title=(
            f"[bold white]RSI Signal Tracker · IBKR[/]  "
            f"[grey50]{datetime.now(NYC).strftime('%Y-%m-%d  %H:%M:%S ET')}[/]"
        ),
        box=box.SIMPLE_HEAVY,
        header_style="bold yellow",
        show_lines=False,
        min_width=115,
    )
    tbl.add_column("Ticker",   style="bold white", justify="left",  width=8)
    tbl.add_column("RSI 1m",   justify="center", width=14)
    tbl.add_column("RSI 5m",   justify="center", width=14)
    tbl.add_column("RSI 15m",  justify="center", width=14)
    tbl.add_column("RSI 1h",   justify="center", width=14)
    tbl.add_column("Signal",   justify="center", width=16)
    tbl.add_column("Last Bar", justify="right",  width=10, style="grey50")
    tbl.add_column("1m Bars",  justify="right",  width=9,  style="grey50")

    with cache_lock:
        snap_rsi   = dict(rsi_state)
        snap_cache = {t: len(df) for t, df in bar_cache.items()}

    for ticker in STOCKS:
        rsi_map = snap_rsi.get(ticker, {})
        row = [ticker]
        for tf in ("1m", "5m", "15m", "1h"):
            label, color = classify(rsi_map.get(tf), tf)
            row.append(f"[{color}]{label}[/]")
        sig_label, sig_color = overall_signal(rsi_map)
        row.append(f"[{sig_color}]{sig_label}[/]")
        row.append(last_update.get(ticker, "—"))
        row.append(str(snap_cache.get(ticker, 0)))
        tbl.add_row(*row)

    return tbl


def build_legend() -> Text:
    t = Text()
    t.append("  ▲OB", style="bright_red")
    t.append(" Overbought  ", style="grey50")
    t.append("▼OS", style="bright_cyan")
    t.append(" Oversold  ", style="grey50")
    t.append(
        f"Wilder RSI({RSI_PERIOD})  SIP  RTH only  live tick updates",
        style="grey50",
    )
    return t


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    util.patchAsyncio()

    ib = IB()

    def on_disconnected():
        console.print("[red]Disconnected — reconnecting in 5s…[/]")
        time.sleep(5)
        try:
            ib.connect(TWS_HOST, TWS_PORT, clientId=CLIENT_ID)
            console.print("[green]Reconnected![/]")
        except Exception as e:
            console.print(f"[red]Reconnect failed: {e}[/]")

    ib.disconnectedEvent += on_disconnected

    console.print(
        f"[yellow]Connecting to IB Gateway at {TWS_HOST}:{TWS_PORT}…[/]"
    )
    ib.connect(TWS_HOST, TWS_PORT, clientId=CLIENT_ID)
    console.print("[green]Connected![/]\n")

    warmup(ib)
    subscribe_live(ib)
    subscribe_realtime(ib)

    with Live(console=console, refresh_per_second=10, screen=True) as live:
        while True:
            ib.sleep(0.1)
            live.update(Panel(
                build_table(),
                subtitle=build_legend(),
                border_style="grey30",
            ))


if __name__ == "__main__":
    main()
