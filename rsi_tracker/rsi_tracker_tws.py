"""
Live Multi-Timeframe RSI Tracker  —  Interactive Brokers TWS Edition (Lean)
============================================================================
Mag 7 only · IBKR historical warmup + local cache · keepUpToDate + reqMktData

RSI uses TradingView-compatible Wilder smoothing (SMA seed + exponential).

Performance:
  - Tick updates:  O(1) incremental 1m RSI only (cached Wilder state)
  - Bar closes:    full resample + RSI across all timeframes
    - Startup:       cache-aware IBKR backfill with overlap repair

Requirements:
    pip install ib_insync pandas rich numpy pyarrow yfinance

Setup:
    1. Open IB Gateway (port 4001) or TWS (port 7496)
    2. Enable API: Configure → Settings → API → Settings
       - Uncheck Read-Only API
       - Socket port: 4001
       - Add 127.0.0.1 to Trusted IPs
    3. Run: python3 rsi_tracker_tws.py
"""

import asyncio
import json
import math
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, NamedTuple
from urllib import error, request
from zoneinfo import ZoneInfo

from ib_insync import IB, Stock, BarDataList, util

from rich.console import Console
from rich.table   import Table
from rich.live    import Live
from rich.panel   import Panel
from rich.text    import Text
from rich         import box


def load_dotenv_file(env_path: Path):
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue

        value = value.strip()
        if ((value.startswith('"') and value.endswith('"'))
                or (value.startswith("'") and value.endswith("'"))):
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_dotenv_file(Path(__file__).resolve().parent / ".env")


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
TWS_HOST  = "127.0.0.1"
TWS_PORT  = 4001        # 4001 = live IB Gateway | 4002 = paper IB Gateway
                         # 7496 = live TWS        | 7497 = paper TWS
CLIENT_ID = 17533939

# Magnificent 7 (alphabetical)
STOCKS = ["AAPL", "AMZN", "GOOG", "META", "MSFT", "NVDA", "TSLA"]

USE_RTH = True
HISTORY_SOURCE = os.getenv("HISTORY_SOURCE", "ibkr").strip().lower()

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_WEBHOOK_URLS_TEXT = os.getenv("DISCORD_WEBHOOK_URLS", "").strip()
DISCORD_WEBHOOK_URL_1 = os.getenv("DISCORD_WEBHOOK_URL_1", "").strip()
DISCORD_WEBHOOK_URL_2 = os.getenv("DISCORD_WEBHOOK_URL_2", "").strip()
DISCORD_WEBHOOK_URL_3 = os.getenv("DISCORD_WEBHOOK_URL_3", "").strip()
DISCORD_ALERT_COOLDOWN_TEXT = os.getenv("DISCORD_ALERT_COOLDOWN", "1h").strip()

RSI_PERIOD = 14

THRESHOLDS = {
    "1m":  {"overbought": 70, "oversold": 30},
    "5m":  {"overbought": 70, "oversold": 30},
    "15m": {"overbought": 70, "oversold": 30},
    "1h":  {"overbought": 70, "oversold": 30},
}

WARMUP_DAYS    = 5
WARMUP_MAX_WORKERS = env_int("WARMUP_MAX_WORKERS", 4, minimum=1)
IB_HISTORY_OVERLAP_DAYS = env_int(
    "IB_HISTORY_OVERLAP_DAYS",
    max(2, WARMUP_DAYS + 2),
    minimum=1,
)
HISTORY_REQUEST_DELAY_SEC = env_float(
    "HISTORY_REQUEST_DELAY_SEC",
    0.25,
    minimum=0.0,
)
MAX_BAR_JUMP_PCT = env_float("MAX_BAR_JUMP_PCT", 0.20, minimum=0.01)
SUBSCRIBE_DELAY_SEC = env_float("SUBSCRIBE_DELAY_SEC", 0.35, minimum=0.1)
SUBSCRIBE_TIMEOUT   = env_int("SUBSCRIBE_TIMEOUT", 45, minimum=15)
SUBSCRIBE_RETRIES   = env_int("SUBSCRIBE_RETRIES", 1, minimum=0)
CACHE_DIR      = Path(__file__).parent / "cache"
RESAMPLE_RULES = {"5m": "5min", "15m": "15min", "1h": "1h"}
NYC            = ZoneInfo("America/New_York")
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
last_discord_alert: Dict[tuple, datetime] = {}
cache_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
#  WILDER RSI STATE  — for O(1) incremental tick updates
# ══════════════════════════════════════════════════════════════════════════════
class WilderState(NamedTuple):
    """Cached Wilder smoothing state: one step = O(1) RSI update.

    avg_gain / avg_loss are the Wilder averages computed up to the
    LAST CLOSED bar. prev_close is the close of that bar.
    This lets us estimate the next 1m bar's RSI from a live price
    in a single arithmetic step.
    """
    avg_gain: float
    avg_loss: float
    prev_close: float
    rsi: float


# Per-ticker cached Wilder state for 1m RSI
wilder_1m: Dict[str, Optional[WilderState]] = {}


def wilder_rsi_from_state(state: WilderState, new_close: float,
                           period: int = 14) -> tuple:
    """Single O(1) Wilder step. Returns (new_rsi, new_state)."""
    delta = new_close - state.prev_close
    gain = max(0.0, delta)
    loss = max(0.0, -delta)

    ag = (state.avg_gain * (period - 1) + gain) / period
    al = (state.avg_loss * (period - 1) + loss) / period

    if al == 0:
        rsi = 100.0 if ag > 0 else 50.0
    else:
        rs = ag / al
        rsi = 100.0 - (100.0 / (1.0 + rs))

    return round(rsi, 2), WilderState(ag, al, new_close, round(rsi, 2))


def build_wilder_state(close_series: pd.Series,
                        period: int = 14) -> Optional[WilderState]:
    """Build Wilder state from a full close series.

    Called once at warmup and on each bar close. Returns state
    positioned at the LAST CLOSED bar so the NEXT bar can be
    estimated via wilder_rsi_from_state().
    """
    n = len(close_series)
    if n < period + 1:
        return None

    delta = close_series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    # SMA seed
    ag = float(gain.iloc[1:period + 1].mean())
    al = float(loss.iloc[1:period + 1].mean())

    # Walk forward to the last closed bar
    for i in range(period + 1, n):
        ag = (ag * (period - 1) + float(gain.iloc[i])) / period
        al = (al * (period - 1) + float(loss.iloc[i])) / period

    if al == 0:
        rsi = 100.0 if ag > 0 else 50.0
    else:
        rs = ag / al
        rsi = 100.0 - (100.0 / (1.0 + rs))

    prev_close = float(close_series.iloc[-1])
    return WilderState(ag, al, prev_close, round(rsi, 2))


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


def is_rth_session_now() -> bool:
    now_local = datetime.now(NYC)
    if now_local.weekday() >= 5:
        return False
    hhmm = now_local.strftime("%H:%M")
    return "09:30" <= hhmm < "16:00"


def is_valid_price(value: Optional[float]) -> bool:
    if value is None:
        return False
    try:
        return np.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def last_close(df: Optional[pd.DataFrame]) -> Optional[float]:
    if df is None or df.empty or "close" not in df.columns:
        return None
    value = df["close"].iloc[-1]
    return float(value) if is_valid_price(value) else None


def is_plausible_price(value: Optional[float], reference_close: Optional[float]) -> bool:
    if not is_valid_price(value):
        return False
    if reference_close is None or not is_valid_price(reference_close):
        return True
    return (
        abs(float(value) - float(reference_close)) / float(reference_close)
        <= MAX_BAR_JUMP_PCT
    )


def is_plausible_bar(row: pd.Series, reference_close: Optional[float]) -> bool:
    prices = {name: row.get(name) for name in ("open", "high", "low", "close")}
    if any(not is_valid_price(value) for value in prices.values()):
        return False

    high = float(prices["high"])
    low = float(prices["low"])
    open_ = float(prices["open"])
    close = float(prices["close"])

    if low > high:
        return False
    if not (low <= open_ <= high and low <= close <= high):
        return False

    if reference_close is None or not is_valid_price(reference_close):
        return True

    max_move = max(
        abs(float(value) - float(reference_close)) / float(reference_close)
        for value in prices.values()
    )
    return max_move <= MAX_BAR_JUMP_PCT


# ══════════════════════════════════════════════════════════════════════════════
#  RSI  — full recompute (bar close and warmup only)
# ══════════════════════════════════════════════════════════════════════════════
def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)

    n = len(close)
    avg_gain = np.full(n, np.nan)
    avg_loss = np.full(n, np.nan)

    if n < period + 1:
        return pd.Series(np.nan, index=close.index)

    avg_gain[period] = gain.iloc[1:period + 1].mean()
    avg_loss[period] = loss.iloc[1:period + 1].mean()

    for i in range(period + 1, n):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain.iloc[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss.iloc[i]) / period

    with np.errstate(divide="ignore", invalid="ignore"):
        rs  = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))

    rsi_series = pd.Series(rsi, index=close.index)
    rsi_series[(avg_loss == 0) & (avg_gain > 0)] = 100.0
    rsi_series[(avg_loss == 0) & (avg_gain == 0)] = 50.0

    return rsi_series


def last_rsi(df: pd.DataFrame) -> Optional[float]:
    if df is None or len(df) < RSI_PERIOD + 1:
        return None
    df = normalize(df)
    v  = calc_rsi(df["close"], RSI_PERIOD).dropna()
    return round(float(v.iloc[-1]), 2) if not v.empty else None


def compute_all_rsi(df1m: pd.DataFrame) -> dict:
    """Full recompute of all timeframes. ONLY called on bar close."""
    df1m = normalize(df1m)
    df1m = ensure_datetimeindex(df1m)
    results = {"1m": last_rsi(df1m)}

    for tf, rule in RESAMPLE_RULES.items():
        try:
            local = df1m.tz_convert(NYC)
            resampled = local.resample(
                rule,
                label="right",
                closed="left",
                origin="epoch",
                offset="30min",
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


def full_rsi_update(sym: str, df: pd.DataFrame):
    """Full recompute: all timeframes + rebuild Wilder state.
    Called on bar close and warmup only."""
    rsi_state[sym] = compute_all_rsi(df)

    # Rebuild incremental state for fast tick path
    ndf = normalize(df)
    if "close" in ndf.columns and len(ndf) >= RSI_PERIOD + 1:
        wilder_1m[sym] = build_wilder_state(ndf["close"], RSI_PERIOD)
    else:
        wilder_1m[sym] = None


def tick_rsi_update(sym: str, new_close: float):
    """O(1) incremental: only 1m RSI. Called on every tick.

    Does NOT persist the new Wilder state — the intrabar close is
    speculative and the state gets properly rebuilt on bar close.
    """
    state = wilder_1m.get(sym)
    if state is None:
        return

    new_rsi, _ = wilder_rsi_from_state(state, new_close, RSI_PERIOD)
    if pd.isna(new_rsi) or not np.isfinite(float(new_rsi)):
        return
    rsi_state[sym]["1m"] = new_rsi


# ══════════════════════════════════════════════════════════════════════════════
#  CACHE
# ══════════════════════════════════════════════════════════════════════════════
def cache_path(ticker: str) -> Path:
    return CACHE_DIR / f"{ticker}_1m.parquet"


def sanitize_ohlcv_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    cleaned = df.copy()
    cleaned.columns = [str(c).strip().lower() for c in cleaned.columns]
    cleaned = cleaned.loc[:, ~cleaned.columns.duplicated(keep="last")]

    keep_cols = [c for c in ("open", "high", "low", "close", "volume")
                 if c in cleaned.columns]
    if keep_cols:
        cleaned = cleaned[keep_cols]

    for col in keep_cols:
        cleaned[col] = pd.to_numeric(cleaned[col], errors="coerce")

    # A missing close makes RSI state invalid; drop those rows early.
    if "close" in cleaned.columns:
        cleaned = cleaned.dropna(subset=["close"])

    cleaned = ensure_datetimeindex(cleaned)
    cleaned = cleaned[~cleaned.index.duplicated(keep="last")]
    return cleaned.sort_index()


def choose_yf_price_level(columns: pd.MultiIndex) -> int:
    expected = {"open", "high", "low", "close", "adj close", "volume"}
    best_level = 0
    best_score = -1

    for level in range(columns.nlevels):
        labels = [str(v).strip().lower() for v in columns.get_level_values(level)]
        score = sum(label in expected for label in labels)
        if score > best_score:
            best_score = score
            best_level = level

    return best_level


def load_cache(ticker: str) -> pd.DataFrame:
    p = cache_path(ticker)
    if not p.exists():
        return pd.DataFrame()
    try:
        df = pd.read_parquet(p)
        return sanitize_ohlcv_df(df)
    except Exception:
        return pd.DataFrame()


def save_cache(ticker: str, df: pd.DataFrame):
    try:
        df.to_parquet(cache_path(ticker))
    except Exception:
        pass


def merge_bars(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    existing = sanitize_ohlcv_df(existing)
    new = sanitize_ohlcv_df(new)

    if existing.empty:
        return new
    if new.empty:
        return existing

    preferred_cols = ["open", "high", "low", "close", "volume"]
    common_cols = [c for c in preferred_cols
                   if c in existing.columns and c in new.columns]
    if common_cols:
        existing = existing[common_cols]
        new = new[common_cols]

    combined = pd.concat([existing, new], axis=0)
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
#  WARMUP  — cache-aware historical backfill
# ══════════════════════════════════════════════════════════════════════════════
def history_duration_for_cache(cached: pd.DataFrame) -> str:
    if cached is None or cached.empty:
        return f"{WARMUP_DAYS} D"

    last_ts = pd.Timestamp(cached.index[-1])
    last_ts = (
        last_ts.tz_localize("UTC")
        if last_ts.tzinfo is None else last_ts.tz_convert("UTC")
    )

    overlap_start = last_ts - timedelta(days=IB_HISTORY_OVERLAP_DAYS)
    now_utc = datetime.now(timezone.utc)
    request_seconds = max(3600, int((now_utc - overlap_start).total_seconds()))

    if request_seconds <= 86400:
        return f"{request_seconds} S"
    return f"{math.ceil(request_seconds / 86400)} D"


def fetch_ibkr_1m(ib: IB, ticker: str, duration_str: str) -> pd.DataFrame:
    contract = Stock(ticker, EXCHANGE, CURRENCY)
    ib.qualifyContracts(contract)
    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=duration_str,
        barSizeSetting="1 min",
        whatToShow="TRADES",
        useRTH=USE_RTH,
        formatDate=2,
        timeout=SUBSCRIBE_TIMEOUT,
    )
    return ib_bars_to_df(bars)


def warmup_ibkr(ib: IB):
    console.print(
        "\n[bold yellow]Warming up RSI via IBKR history "
        f"(cache overlap {IB_HISTORY_OVERLAP_DAYS}d)…[/]\n"
    )

    for ticker in STOCKS:
        cached = load_cache(ticker)
        duration_str = history_duration_for_cache(cached)

        try:
            fresh = fetch_ibkr_1m(ib, ticker, duration_str)
            merged = merge_bars(cached, fresh)
            if merged.empty:
                console.print(f"  [white]{ticker}[/] [red]no IBKR history data[/]")
                with cache_lock:
                    rsi_state[ticker] = {tf: None for tf in THRESHOLDS}
                    wilder_1m[ticker] = None
                continue

            with cache_lock:
                bar_cache[ticker] = merged
                full_rsi_update(ticker, merged)
                last_ts = merged.index[-1]
                last_update[ticker] = last_ts.tz_convert(NYC).strftime("%H:%M:%S")

            save_cache(ticker, merged)
            console.print(
                f"  [white]{ticker}[/] [green]{len(merged)} bars  "
                f"(+{len(fresh)} from IB, request {duration_str})  "
                f"RSI 1m={format_rsi_value(rsi_state[ticker]['1m'])}[/]"
            )
        except Exception as e:
            console.print(f"  [white]{ticker}[/] [red]IBKR history error: {e}[/]")
            with cache_lock:
                if not cached.empty:
                    bar_cache[ticker] = cached
                    full_rsi_update(ticker, cached)
                    last_ts = cached.index[-1]
                    last_update[ticker] = last_ts.tz_convert(NYC).strftime("%H:%M:%S")
                else:
                    rsi_state[ticker] = {tf: None for tf in THRESHOLDS}
                    wilder_1m[ticker] = None

        time.sleep(HISTORY_REQUEST_DELAY_SEC)

    console.print("\n[bold green]Warmup complete — subscribing to live bars…[/]\n")


def fetch_yfinance_1m(ticker: str) -> pd.DataFrame:
    data = yf.download(
        ticker,
        period=f"{WARMUP_DAYS}d",
        interval="1m",
        prepost=False,
        progress=False,
        threads=False,
    )
    if data.empty:
        return pd.DataFrame()

    if isinstance(data.columns, pd.MultiIndex):
        level = choose_yf_price_level(data.columns)
        data.columns = data.columns.get_level_values(level)

    fresh = data.rename(columns=lambda c: str(c).strip().lower())
    fresh = fresh.loc[:, ~fresh.columns.duplicated(keep="last")]

    for col in ("adj close", "adjclose", "adj_close"):
        if col in fresh.columns:
            fresh = fresh.drop(columns=[col])

    return sanitize_ohlcv_df(fresh)


def warmup_yfinance():
    console.print(
        f"\n[bold yellow]Warming up RSI via yfinance "
        f"({WARMUP_DAYS}d of 1m bars)…[/]\n"
    )

    caches = {ticker: load_cache(ticker) for ticker in STOCKS}
    max_workers = min(max(1, len(STOCKS)), WARMUP_MAX_WORKERS)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            ticker: pool.submit(fetch_yfinance_1m, ticker)
            for ticker in STOCKS
        }

    for ticker in STOCKS:
        cached = caches[ticker]
        try:
            fresh = futures[ticker].result()
            if fresh.empty:
                console.print(f"  [white]{ticker}[/] [red]no yfinance data[/]")
                with cache_lock:
                    rsi_state[ticker] = {tf: None for tf in THRESHOLDS}
                continue

            merged = merge_bars(cached, fresh)

            with cache_lock:
                bar_cache[ticker] = merged
                full_rsi_update(ticker, merged)

            save_cache(ticker, merged)
            console.print(
                f"  [white]{ticker}[/] [green]{len(merged)} bars  "
                f"(+{len(fresh)} new)  "
                f"RSI 1m={rsi_state[ticker]['1m']}[/]"
            )

        except Exception as e:
            console.print(f"  [white]{ticker}[/] [red]yfinance error: {e}[/]")
            with cache_lock:
                if not cached.empty:
                    bar_cache[ticker] = cached
                    full_rsi_update(ticker, cached)
                else:
                    rsi_state[ticker] = {tf: None for tf in THRESHOLDS}

    console.print("\n[bold green]Warmup complete — subscribing to live bars…[/]\n")


# ══════════════════════════════════════════════════════════════════════════════
#  LIVE 1m BAR SUBSCRIPTION  (keepUpToDate — fires on bar close)
#  Full RSI recompute on new bar only.
# ══════════════════════════════════════════════════════════════════════════════
def subscribe_live(ib: IB):
    for ticker in STOCKS:
        contract = Stock(ticker, EXCHANGE, CURRENCY)
        ib.qualifyContracts(contract)

        bars = None
        for attempt in range(1, SUBSCRIBE_RETRIES + 2):
            try:
                bars = ib.reqHistoricalData(
                    contract,
                    endDateTime="",
                    durationStr="120 S",   # minimal — warmup already loaded history
                    barSizeSetting="1 min",
                    whatToShow="TRADES",
                    useRTH=USE_RTH,
                    formatDate=2,
                    keepUpToDate=True,
                    timeout=SUBSCRIBE_TIMEOUT,
                )
                if USE_RTH and is_rth_session_now() and len(bars) == 0:
                    raise TimeoutError(
                        "empty 1m snapshot during RTH (timeout/cancel)"
                    )
                break
            except Exception as e:
                if attempt > SUBSCRIBE_RETRIES:
                    console.print(
                        f"  [red]Subscribe 1m failed: {ticker} ({e})[/]"
                    )
                else:
                    console.print(
                        f"  [yellow]Retry {attempt}/{SUBSCRIBE_RETRIES}: "
                        f"{ticker} ({e})[/]"
                    )
                    time.sleep(SUBSCRIBE_DELAY_SEC * 2)

        if bars is None:
            continue

        # Seed cache from the initial closed-bar snapshot only.
        initial_df = ib_bars_to_df(bars[:-1]) if len(bars) > 1 else pd.DataFrame()
        if not initial_df.empty:
            with cache_lock:
                existing = bar_cache.get(ticker, pd.DataFrame())
                merged = merge_bars(existing, initial_df)
                bar_cache[ticker] = merged
                full_rsi_update(ticker, merged)
                last_ts = merged.index[-1]
                last_update[ticker] = last_ts.tz_convert(NYC).strftime("%H:%M:%S")
            save_cache(ticker, merged)

        def make_handler(sym):
            def on_bar_update(bars, hasNewBar):
                if not hasNewBar or len(bars) < 2:
                    return
                bar = bars[-2]
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

                merged = None
                warning = None
                with cache_lock:
                    existing = bar_cache.get(sym, pd.DataFrame())
                    reference_close = last_close(existing)
                    if not is_plausible_bar(new_row.iloc[0], reference_close):
                        prev_text = f"{reference_close:.2f}" if reference_close is not None else "N/A"
                        warning = (
                            f"[yellow]Ignored suspicious closed bar:[/] {sym} "
                            f"{ts.tz_convert(NYC).strftime('%H:%M:%S')} "
                            f"close={float(new_row['close'].iloc[0]):.2f} prev={prev_text}"
                        )
                    else:
                        merged = merge_bars(existing, new_row)
                        bar_cache[sym] = merged
                        # ── FULL recompute: closed 1m bar only ──
                        full_rsi_update(sym, merged)
                        last_update[sym] = ts.tz_convert(NYC).strftime("%H:%M:%S")

                if warning is not None:
                    console.print(f"  {warning}")
                    return

                if merged is not None:
                    save_cache(sym, merged)
            return on_bar_update

        bars.updateEvent += make_handler(ticker)
        live_bars[ticker] = bars
        if len(bars) == 0:
            console.print(
                f"  [yellow]Subscribed 1m bars: {ticker} "
                f"(empty snapshot; waiting for next bar)[/]"
            )
        else:
            console.print(f"  [green]Subscribed 1m bars: {ticker}[/]")
        time.sleep(SUBSCRIBE_DELAY_SEC)


# ══════════════════════════════════════════════════════════════════════════════
#  REAL-TIME PRICE  — reqMktData (free with NP bundle)
#  O(1) incremental 1m RSI per tick. No resampling, no higher TF recompute.
# ══════════════════════════════════════════════════════════════════════════════
def subscribe_realtime(ib: IB):
    ib.reqMarketDataType(1)  # Live data (NP bundle)

    def apply_intrabar_price(sym: str, price: Optional[float]) -> bool:
        with cache_lock:
            reference_close = last_close(bar_cache.get(sym))
            if reference_close is None or not is_plausible_price(price, reference_close):
                return False
            # ── FAST path: O(1) 1m RSI only ──
            tick_rsi_update(sym, float(price))
        return True

    def make_price_handler(sym):
        def on_price_change(ticker, *_):
            candidates = [ticker.last]

            bid = getattr(ticker, "bid", None)
            ask = getattr(ticker, "ask", None)
            if is_valid_price(bid) and is_valid_price(ask):
                candidates.append((float(bid) + float(ask)) / 2.0)

            try:
                candidates.append(ticker.marketPrice())
            except Exception:
                pass

            for candidate in candidates:
                if apply_intrabar_price(sym, candidate):
                    break
        return on_price_change

    for ticker in STOCKS:
        contract = Stock(ticker, EXCHANGE, CURRENCY)
        ib.qualifyContracts(contract)

        ticker_obj = ib.reqMktData(contract, "", False, False)
        ticker_obj.updateEvent += make_price_handler(ticker)
        live_tickers[ticker] = ticker_obj
        console.print(f"  [green]MktData: {ticker}[/]")
        time.sleep(0.2)

    console.print("[green]Real-time price subscriptions active (NP bundle)[/]\n")


# ══════════════════════════════════════════════════════════════════════════════
#  DISCORD ALERTS
# ══════════════════════════════════════════════════════════════════════════════
def classify(rsi: Optional[float], tf: str) -> tuple:
    if rsi is None or pd.isna(rsi) or not np.isfinite(float(rsi)):
        return "N/A", "grey50"
    rsi = float(rsi)
    ob  = THRESHOLDS[tf]["overbought"]
    os_ = THRESHOLDS[tf]["oversold"]
    if rsi >= ob:
        return f"{rsi} ▲OB", "bright_red"
    if rsi <= os_:
        return f"{rsi} ▼OS", "bright_cyan"
    return str(rsi), "white"


def overall_signal(rsi_map: dict) -> tuple:
    valid = {
        tf: float(v)
        for tf, v in rsi_map.items()
        if v is not None and not pd.isna(v) and np.isfinite(float(v))
    }
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


def format_rsi_value(value: Optional[float]) -> str:
    if value is None or pd.isna(value) or not np.isfinite(float(value)):
        return "N/A"
    return f"{float(value):.2f}"


def parse_discord_cooldown(value: str) -> timedelta:
    default = timedelta(hours=1)
    text = (value or "").strip().lower()
    if not text:
        return default

    unit_seconds = {"s": 1, "m": 60, "h": 3600}
    unit = text[-1]
    multiplier = unit_seconds.get(unit)
    amount_text = text[:-1] if multiplier is not None else text
    if multiplier is None:
        multiplier = 60

    try:
        amount = float(amount_text)
    except ValueError:
        return default

    if amount <= 0:
        return default

    return timedelta(seconds=int(amount * multiplier))


def format_discord_cooldown(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    if total_seconds % 3600 == 0:
        return f"{total_seconds // 3600}h"
    if total_seconds % 60 == 0:
        return f"{total_seconds // 60}m"
    return f"{total_seconds}s"


def parse_discord_webhooks(
        single_value: str,
        multi_value: str,
        line_values: Optional[list[str]] = None) -> list[str]:
    urls = []

    def add_value(raw: str):
        for part in raw.replace("\n", ",").split(","):
            url = part.strip()
            if url and url not in urls:
                urls.append(url)

    add_value(single_value)
    for line_value in (line_values or []):
        add_value(line_value)
    add_value(multi_value)
    return urls


DISCORD_ALERT_COOLDOWN = parse_discord_cooldown(DISCORD_ALERT_COOLDOWN_TEXT)
DISCORD_WEBHOOK_URLS = parse_discord_webhooks(
    DISCORD_WEBHOOK_URL,
    DISCORD_WEBHOOK_URLS_TEXT,
    [DISCORD_WEBHOOK_URL_1, DISCORD_WEBHOOK_URL_2, DISCORD_WEBHOOK_URL_3],
)


def send_discord_message(message: str) -> bool:
    if not DISCORD_WEBHOOK_URLS:
        return False

    payload = {"content": message}
    data = json.dumps(payload).encode("utf-8")
    success = False

    for webhook_url in DISCORD_WEBHOOK_URLS:
        req = request.Request(
            webhook_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
                              " AppleWebKit/537.36 (KHTML, like Gecko)"
                              " Chrome/122.0.0.0 Safari/537.36",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=5) as response:
                success = success or (200 <= response.status < 300)
        except error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
            console.print(
                f"[red]Discord webhook HTTP {exc.code}[/]"
                + (f" [grey50]{body[:160]}[/]" if body else "")
            )
        except Exception as exc:
            console.print(f"[red]Discord webhook failed: {exc}[/]")

    return success


def maybe_send_discord_alerts():
    if not DISCORD_WEBHOOK_URLS:
        return

    now_utc = datetime.now(timezone.utc)
    with cache_lock:
        snap_rsi = dict(rsi_state)

    for ticker in STOCKS:
        rsi_map = snap_rsi.get(ticker, {})
        signal, _ = overall_signal(rsi_map)
        if signal in {"⚪ NEUTRAL", "NO DATA"}:
            continue

        cooldown_key = (ticker, signal)
        last_sent = last_discord_alert.get(cooldown_key)
        if (last_sent is not None
                and now_utc - last_sent < DISCORD_ALERT_COOLDOWN):
            continue

        message = (
            f"**{ticker}** {signal}\n"
            f"RSI 1m={format_rsi_value(rsi_map.get('1m'))}  "
            f"5m={format_rsi_value(rsi_map.get('5m'))}  "
            f"15m={format_rsi_value(rsi_map.get('15m'))}  "
            f"1h={format_rsi_value(rsi_map.get('1h'))}\n"
            f"{datetime.now(NYC).strftime('%Y-%m-%d %H:%M:%S ET')}"
        )
        if send_discord_message(message):
            last_discord_alert[cooldown_key] = now_utc
            console.print(f"[magenta]Discord alert sent:[/] {ticker} {signal}")


# ══════════════════════════════════════════════════════════════════════════════
#  UI
# ══════════════════════════════════════════════════════════════════════════════
def build_table() -> Table:
    tbl = Table(
        title=(
            f"[bold white]RSI Signal Tracker · IBKR (Mag 7)[/]  "
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
        f"Wilder RSI({RSI_PERIOD})  NP bundle  RTH only  "
        f"O(1) tick · full recompute on bar close",
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

    # ── Phase 1: connect to IB ──
    console.print(
        f"[yellow]Connecting to IB Gateway at {TWS_HOST}:{TWS_PORT}…[/]"
    )
    ib.connect(TWS_HOST, TWS_PORT, clientId=CLIENT_ID)
    console.print("[green]Connected![/]\n")

    # ── Phase 2: historical warmup ──
    if HISTORY_SOURCE == "yfinance":
        warmup_yfinance()
    else:
        warmup_ibkr(ib)

    subscribe_live(ib)       # keepUpToDate 1m bars  (7 data lines)
    subscribe_realtime(ib)   # reqMktData             (7 data lines, free w/ NP)

    if DISCORD_WEBHOOK_URLS:
        console.print(
            "[magenta]Discord alerts enabled:[/] non-neutral signals, "
            f"max 1 per symbol per {format_discord_cooldown(DISCORD_ALERT_COOLDOWN)}, "
            f"sent to {len(DISCORD_WEBHOOK_URLS)} webhook(s)"
        )
    else:
        console.print(
            "[grey50]Discord alerts disabled.[/] "
            "Set DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URLS to enable"
        )

    console.print(
        f"[grey50]Data lines: 7 keepUpToDate + 7 reqMktData = 14 total "
        f"(all covered by NP bundle)[/]\n"
    )

    with Live(console=console, refresh_per_second=10, screen=True) as live:
        while True:
            ib.sleep(0.1)
            maybe_send_discord_alerts()
            live.update(Panel(
                build_table(),
                subtitle=build_legend(),
                border_style="grey30",
            ))


if __name__ == "__main__":
    main()
