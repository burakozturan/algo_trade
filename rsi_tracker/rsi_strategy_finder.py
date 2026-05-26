"""
RSI Strategy Finder  —  IBKR Edition
Tests every logical RSI combination across 1m/5m/15m and ranks by quality.
Both long and short sides, overbought and oversold.
"""

import warnings
warnings.filterwarnings("ignore")

import time
import numpy as np
import pandas as pd
from scipy import stats
from ib_insync import IB, Stock, util
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
TWS_HOST  = "127.0.0.1"
TWS_PORT  = 4001
CLIENT_ID = 17533942

MAG7 = ["AAPL", "MSFT", "AMZN", "META", "GOOGL", "NVDA", "TSLA"]

RSI_PERIOD    = 14
USE_RTH       = True
REQUEST_DELAY = 0.6

FORWARD_BARS_1M = 30   # 30 min hold

CHUNKS = {
    "1 min":   {"duration": "7 D",  "n_chunks": 4},
    "5 mins":  {"duration": "30 D", "n_chunks": 3},
    "15 mins": {"duration": "60 D", "n_chunks": 3},
}
# ─────────────────────────────────────────────────────────────────────────────


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def fetch_bars_single(ib, contract, bar_size, duration, end_dt=""):
    time.sleep(REQUEST_DELAY)
    bars = ib.reqHistoricalData(
        contract, endDateTime=end_dt, durationStr=duration,
        barSizeSetting=bar_size, whatToShow="TRADES",
        useRTH=USE_RTH, formatDate=1, keepUpToDate=False,
    )
    if not bars:
        return pd.DataFrame()
    df = util.df(bars)[["date", "close"]].rename(columns={"date": "ts", "close": "Close"})
    df["ts"] = pd.to_datetime(df["ts"])
    return df.set_index("ts").sort_index()


def fetch_bars(ib, ticker, bar_size):
    cfg      = CHUNKS[bar_size]
    contract = Stock(ticker, "SMART", "USD")
    ib.qualifyContracts(contract)
    chunks = []
    end_dt = ""
    for _ in range(cfg["n_chunks"]):
        chunk = fetch_bars_single(ib, contract, bar_size, cfg["duration"], end_dt)
        if chunk.empty:
            break
        chunks.append(chunk)
        end_dt = chunk.index.min().strftime("%Y%m%d %H:%M:%S")
    if not chunks:
        return pd.DataFrame()
    out = pd.concat(chunks).sort_index()
    return out[~out.index.duplicated(keep="last")]


def build_master(ib, ticker):
    """1m frame with 1m/5m/15m RSI columns, forward return attached."""
    df_1m  = fetch_bars(ib, ticker, "1 min")
    df_5m  = fetch_bars(ib, ticker, "5 mins")
    df_15m = fetch_bars(ib, ticker, "15 mins")
    if df_1m.empty or df_5m.empty or df_15m.empty:
        return pd.DataFrame()

    m = df_1m[["Close"]].copy()
    m["r1"]  = compute_rsi(df_1m["Close"],  RSI_PERIOD)
    m["r5"]  = compute_rsi(df_5m["Close"],  RSI_PERIOD).reindex(m.index, method="ffill")
    m["r15"] = compute_rsi(df_15m["Close"], RSI_PERIOD).reindex(m.index, method="ffill")
    m = m.dropna()

    # attach forward return
    closes = m["Close"].values
    fwd    = np.full(len(closes), np.nan)
    for i in range(len(closes) - FORWARD_BARS_1M):
        fwd[i] = (closes[i + FORWARD_BARS_1M] / closes[i] - 1) * 100
    m["fwd_ret"] = fwd
    return m


# ── Strategy definitions ──────────────────────────────────────────────────────
# Each strategy is a function: df → list of (bar_index, direction)
# direction: +1 = long, -1 = short

def _crossdown(series, level):
    """bars where series just crossed DOWN through level"""
    above = series > level
    return above.shift(1).fillna(False) & ~above

def _crossup(series, level):
    """bars where series just crossed UP through level"""
    below = series < level
    return below.shift(1).fillna(False) & ~below


STRATEGIES = {
    # ── Overbought setups ─────────────────────────────────────────────────────

    # All 3 > 70, 1m crosses back below 70 → LONG (momentum cont.)
    "OB_all3_1m_exit_LONG": lambda m: (
        _crossdown(m["r1"], 70) &
        (m["r5"]  > 70) &
        (m["r15"] > 70),
        +1
    ),

    # All 3 > 70, 1m crosses back below 70 → SHORT (mean reversion)
    "OB_all3_1m_exit_SHORT": lambda m: (
        _crossdown(m["r1"], 70) &
        (m["r5"]  > 70) &
        (m["r15"] > 70),
        -1
    ),

    # 5m + 15m > 70, 1m crosses above 70 → LONG (momentum entry on 1m breakout)
    "OB_5m15m_1m_cross_above_LONG": lambda m: (
        _crossup(m["r1"], 70) &
        (m["r5"]  > 70) &
        (m["r15"] > 70),
        +1
    ),

    # 15m > 70, 5m crosses back below 70 → SHORT (higher TF still hot, 5m exhausted)
    "OB_15m_5m_exit_SHORT": lambda m: (
        _crossdown(m["r5"], 70) &
        (m["r15"] > 70),
        -1
    ),

    # 15m > 70, 5m crosses back below 70 → LONG
    "OB_15m_5m_exit_LONG": lambda m: (
        _crossdown(m["r5"], 70) &
        (m["r15"] > 70),
        +1
    ),

    # Only 1m > 70, 5m < 70, 1m exits → SHORT (isolated 1m spike, no follow-through)
    "OB_1m_only_exit_SHORT": lambda m: (
        _crossdown(m["r1"], 70) &
        (m["r5"]  < 70) &
        (m["r15"] < 70),
        -1
    ),

    # Extreme OB: all 3 > 75, 1m exits 75 → SHORT
    "OB_extreme75_exit_SHORT": lambda m: (
        _crossdown(m["r1"], 75) &
        (m["r5"]  > 75) &
        (m["r15"] > 75),
        -1
    ),

    # Extreme OB: all 3 > 75, 1m exits 75 → LONG
    "OB_extreme75_exit_LONG": lambda m: (
        _crossdown(m["r1"], 75) &
        (m["r5"]  > 75) &
        (m["r15"] > 75),
        +1
    ),

    # ── Oversold setups ───────────────────────────────────────────────────────

    # All 3 < 30, 1m crosses back above 30 → LONG (oversold bounce)
    "OS_all3_1m_exit_LONG": lambda m: (
        _crossup(m["r1"], 30) &
        (m["r5"]  < 30) &
        (m["r15"] < 30),
        +1
    ),

    # 15m < 30, 5m crosses back above 30 → LONG
    "OS_15m_5m_exit_LONG": lambda m: (
        _crossup(m["r5"], 30) &
        (m["r15"] < 30),
        +1
    ),

    # 15m < 30, 5m crosses back above 30 → SHORT
    "OS_15m_5m_exit_SHORT": lambda m: (
        _crossup(m["r5"], 30) &
        (m["r15"] < 30),
        -1
    ),

    # Only 1m < 30, others not → SHORT (isolated dip, bounces fade)
    "OS_1m_only_exit_SHORT": lambda m: (
        _crossup(m["r1"], 30) &
        (m["r5"]  > 30) &
        (m["r15"] > 30),
        -1
    ),

    # ── Trend / mid-zone setups ───────────────────────────────────────────────

    # 15m > 60 (uptrend), 1m crosses above 50 → LONG (dip buy in trend)
    "TREND_15m60_1m_above50_LONG": lambda m: (
        _crossup(m["r1"], 50) &
        (m["r5"]  > 50) &
        (m["r15"] > 60),
        +1
    ),

    # 15m < 40 (downtrend), 1m crosses below 50 → SHORT
    "TREND_15m40_1m_below50_SHORT": lambda m: (
        _crossdown(m["r1"], 50) &
        (m["r5"]  < 50) &
        (m["r15"] < 40),
        -1
    ),
}


def run_strategy(name, fn, master_all):
    """Apply strategy to all tickers, collect signed returns."""
    all_rets = []
    for ticker, m in master_all.items():
        try:
            mask, direction = fn(m)
        except Exception:
            continue
        bars = m[mask & m["fwd_ret"].notna()]
        if bars.empty:
            continue
        signed = bars["fwd_ret"] * direction
        all_rets.extend(signed.tolist())
    return all_rets


def score(rets):
    if len(rets) < 8:
        return None
    rets  = np.array(rets)
    n     = len(rets)
    hr    = (rets > 0).mean() * 100      # % winning (direction-adjusted)
    avg   = rets.mean()
    t, p  = stats.ttest_1samp(rets, 0)
    sharpe = avg / (rets.std() + 1e-9) * np.sqrt(252 * 390)  # annualised
    return {"n": n, "hit_rate": round(hr, 1), "avg_ret": round(avg, 3),
            "t": round(t, 2), "p": round(p, 4), "sharpe": round(sharpe, 2)}


def run():
    ib = IB()
    print(f"Connecting {TWS_HOST}:{TWS_PORT} ...")
    ib.connect(TWS_HOST, TWS_PORT, clientId=CLIENT_ID)
    print("Connected.\n")

    master_all = {}
    for ticker in MAG7:
        print(f"  Fetching {ticker} ...", end=" ", flush=True)
        m = build_master(ib, ticker)
        if m.empty:
            print("no data"); continue
        master_all[ticker] = m
        days = (m.index.max() - m.index.min()).days
        print(f"{len(m)} bars  ({days}d)")

    ib.disconnect()

    if not master_all:
        print("No data."); return

    print(f"\nRunning {len(STRATEGIES)} strategies across {len(master_all)} tickers...\n")

    rows = []
    for name, fn in STRATEGIES.items():
        rets = run_strategy(name, fn, master_all)
        s    = score(rets)
        if s is None:
            continue
        rows.append({"strategy": name, **s})

    results = (pd.DataFrame(rows)
               .sort_values(["hit_rate", "p"], ascending=[False, True]))

    print(f"{'═'*80}")
    print(f"  ALL STRATEGIES RANKED  (direction-adjusted: positive = correct call)")
    print(f"{'═'*80}")
    print(results.to_string(index=False))

    # ── Winner ────────────────────────────────────────────────────────────────
    sig = results[results["p"] < 0.05]
    winner = sig.iloc[0] if not sig.empty else results.iloc[0]

    direction = "LONG" if winner["strategy"].endswith("LONG") else "SHORT"
    print(f"\n{'═'*80}")
    print(f"  BEST STRATEGY")
    print(f"{'═'*80}")
    print(f"  Name      : {winner['strategy']}")
    print(f"  Direction : {direction}")
    print(f"  Signals   : {winner['n']}")
    print(f"  Hit rate  : {winner['hit_rate']}%")
    print(f"  Avg ret   : {winner['avg_ret']}%  per trade over {FORWARD_BARS_1M} min")
    print(f"  t / p     : {winner['t']} / {winner['p']}  "
          f"{'<< SIGNIFICANT' if winner['p'] < 0.05 else 'marginal'}")
    print(f"  Sharpe    : {winner['sharpe']}")

    # per-ticker breakdown for winner
    print(f"\n  Per-ticker (winner strategy):")
    fn = STRATEGIES[winner["strategy"]]
    for ticker, m in master_all.items():
        try:
            mask, direction_val = fn(m)
        except Exception:
            continue
        bars = m[mask & m["fwd_ret"].notna()]
        if len(bars) < 2:
            continue
        signed = bars["fwd_ret"] * direction_val
        hr = (signed > 0).mean() * 100
        print(f"    {ticker:5s}: {len(bars):4d} signals | hit {hr:.0f}% | "
              f"avg {signed.mean():.3f}%")

    results.to_csv("rsi_strategy_results.csv", index=False)
    print("\nSaved: rsi_strategy_results.csv")


if __name__ == "__main__":
    run()
