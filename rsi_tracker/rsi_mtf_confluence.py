"""
Multi-Timeframe RSI Confluence  —  IBKR Edition
Finds when 1m + 5m + 15m RSI are ALL > 70 simultaneously.
Tests 3 entry triggers and picks the best one.
"""

import warnings
warnings.filterwarnings("ignore")

import time
import numpy as np
import pandas as pd
from scipy import stats
from ib_insync import IB, Stock, util

# ── Config ────────────────────────────────────────────────────────────────────
TWS_HOST  = "127.0.0.1"
TWS_PORT  = 4001
CLIENT_ID = 17533941

MAG7 = ["AAPL", "MSFT", "AMZN", "META", "GOOGL", "NVDA", "TSLA"]

RSI_PERIOD    = 14
RSI_THRESHOLD = 70
USE_RTH       = True
REQUEST_DELAY = 0.6       # be conservative to avoid pacing violations

FORWARD_BARS_1M = 30      # measure return 30 min after entry

# How many chunks to fetch per timeframe (each chunk = one IBKR request)
# 1m:  7D per chunk  → 4 chunks = ~28 days
# 5m:  30D per chunk → 3 chunks = ~90 days
# 15m: 60D per chunk → 3 chunks = ~180 days
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


def fetch_bars_single(ib: IB, contract, bar_size: str, duration: str,
                      end_dt: str = "") -> pd.DataFrame:
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


def fetch_bars(ib: IB, ticker: str, bar_size: str) -> pd.DataFrame:
    """Fetch multiple chunks going backwards and concatenate."""
    cfg      = CHUNKS[bar_size]
    duration = cfg["duration"]
    n_chunks = cfg["n_chunks"]

    contract = Stock(ticker, "SMART", "USD")
    ib.qualifyContracts(contract)

    all_chunks = []
    end_dt     = ""   # empty = now for first request

    for chunk_i in range(n_chunks):
        chunk = fetch_bars_single(ib, contract, bar_size, duration, end_dt)
        if chunk.empty:
            break
        all_chunks.append(chunk)
        # next chunk ends just before the earliest bar we just got
        earliest = chunk.index.min()
        end_dt   = earliest.strftime("%Y%m%d %H:%M:%S")

    if not all_chunks:
        return pd.DataFrame()

    combined = pd.concat(all_chunks).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined


def build_confluence(ib: IB, ticker: str) -> pd.DataFrame:
    """
    Returns a 1m-indexed DataFrame with columns:
      rsi_1m, rsi_5m, rsi_15m, close
    All aligned to 1m timestamps.
    """
    df_1m  = fetch_bars(ib, ticker, "1 min")
    df_5m  = fetch_bars(ib, ticker, "5 mins")
    df_15m = fetch_bars(ib, ticker, "15 mins")

    if df_1m.empty or df_5m.empty or df_15m.empty:
        return pd.DataFrame()

    rsi_1m  = compute_rsi(df_1m["Close"],  RSI_PERIOD).rename("rsi_1m")
    rsi_5m  = compute_rsi(df_5m["Close"],  RSI_PERIOD).rename("rsi_5m")
    rsi_15m = compute_rsi(df_15m["Close"], RSI_PERIOD).rename("rsi_15m")

    # forward-fill higher TF RSI onto 1m index
    combined = df_1m[["Close"]].copy()
    combined["rsi_1m"]  = rsi_1m
    combined["rsi_5m"]  = rsi_5m.reindex(combined.index, method="ffill")
    combined["rsi_15m"] = rsi_15m.reindex(combined.index, method="ffill")

    return combined.dropna()


def find_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    All three RSI > 70 at the same time = confluence zone.
    Test 3 entry triggers (all on 1m bars):

    A) 1m drops back below 70  (5m + 15m still above 70) — earliest entry
    B) 1m + 5m both below 70   (15m still above 70)       — confirmed entry
    C) all three drop below 70                             — latest / safest
    """
    signals = []
    in_confluence = False

    for i in range(1, len(df)):
        prev = df.iloc[i - 1]
        curr = df.iloc[i]

        all_above = (curr["rsi_1m"]  > RSI_THRESHOLD and
                     curr["rsi_5m"]  > RSI_THRESHOLD and
                     curr["rsi_15m"] > RSI_THRESHOLD)

        if all_above:
            in_confluence = True

        if in_confluence:
            # Trigger A: 1m just crossed back below 70
            if (prev["rsi_1m"] > RSI_THRESHOLD and
                    curr["rsi_1m"] <= RSI_THRESHOLD and
                    curr["rsi_5m"]  > RSI_THRESHOLD and
                    curr["rsi_15m"] > RSI_THRESHOLD):
                signals.append({"bar": i, "trigger": "A", "close": curr["Close"]})
                in_confluence = False   # don't double-fire

            # Trigger B: 1m + 5m both just below 70, 15m still above
            elif (prev["rsi_5m"] > RSI_THRESHOLD and
                      curr["rsi_5m"]  <= RSI_THRESHOLD and
                      curr["rsi_1m"]  <= RSI_THRESHOLD and
                      curr["rsi_15m"] > RSI_THRESHOLD):
                signals.append({"bar": i, "trigger": "B", "close": curr["Close"]})
                in_confluence = False

            # Trigger C: all three just dropped below 70
            elif (prev["rsi_15m"] > RSI_THRESHOLD and
                      curr["rsi_15m"] <= RSI_THRESHOLD and
                      curr["rsi_1m"]  <= RSI_THRESHOLD and
                      curr["rsi_5m"]  <= RSI_THRESHOLD):
                signals.append({"bar": i, "trigger": "C", "close": curr["Close"]})
                in_confluence = False

    return pd.DataFrame(signals)


def attach_returns(signals: pd.DataFrame, df: pd.DataFrame,
                   fwd_bars: int = FORWARD_BARS_1M) -> pd.DataFrame:
    rows = []
    closes = df["Close"].values
    for _, sig in signals.iterrows():
        entry_i  = int(sig["bar"])
        exit_i   = entry_i + fwd_bars
        if exit_i >= len(closes):
            continue
        fwd_ret = (closes[exit_i] / closes[entry_i] - 1) * 100
        rows.append({**sig.to_dict(), "fwd_ret_pct": fwd_ret})
    return pd.DataFrame(rows)


def summarise(all_signals: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for trigger in ["A", "B", "C"]:
        sub = all_signals[all_signals["trigger"] == trigger]
        if len(sub) < 5:
            continue
        hit_rate = (sub["fwd_ret_pct"] < 0).mean() * 100
        avg_ret  = sub["fwd_ret_pct"].mean()
        t, p     = stats.ttest_1samp(sub["fwd_ret_pct"], 0)
        rows.append({
            "trigger":       trigger,
            "n":             len(sub),
            "hit_rate_pct":  round(hit_rate, 1),
            "avg_ret_pct":   round(avg_ret, 3),
            "t_stat":        round(t, 2),
            "p_value":       round(p, 4),
        })
    return pd.DataFrame(rows).sort_values("hit_rate_pct", ascending=False)


def run():
    ib = IB()
    print(f"Connecting {TWS_HOST}:{TWS_PORT} ...")
    ib.connect(TWS_HOST, TWS_PORT, clientId=CLIENT_ID)
    print("Connected.\n")

    all_signals = []

    for ticker in MAG7:
        print(f"  {ticker} (fetching 3 timeframes × up to chunks) ...", end=" ", flush=True)
        df = build_confluence(ib, ticker)
        if df.empty:
            print("no data"); continue

        sigs = find_signals(df)
        if sigs.empty:
            print("no confluence signals"); continue

        sigs = attach_returns(sigs, df)
        if sigs.empty:
            print("not enough fwd bars"); continue

        sigs["ticker"] = ticker
        all_signals.append(sigs)

        for t, grp in sigs.groupby("trigger"):
            hr = (grp["fwd_ret_pct"] < 0).mean() * 100
            print(f"[{t}:{len(grp)}×{hr:.0f}%]", end=" ")
        print()

    ib.disconnect()

    if not all_signals:
        print("No signals found."); return

    combined = pd.concat(all_signals, ignore_index=True)
    summary  = summarise(combined)

    print(f"\n{'═'*56}")
    print("  MULTI-TIMEFRAME CONFLUENCE RESULTS")
    print(f"  (all 3 RSI > 70 at once, then entry on trigger)")
    print(f"  Forward window: {FORWARD_BARS_1M} minutes")
    print(f"{'═'*56}")
    print(summary.to_string(index=False))

    # ── Winner ────────────────────────────────────────────────────────────────
    best = summary.iloc[0]
    trigger_desc = {
        "A": "1m RSI drops back below 70  (5m + 15m still above 70)  ← earliest",
        "B": "1m + 5m both drop below 70  (15m still above 70)       ← confirmed",
        "C": "all three drop below 70                                 ← safest",
    }
    print(f"\n  BEST ENTRY TRIGGER: {best['trigger']}")
    print(f"  {trigger_desc[best['trigger']]}")
    print(f"  Hit rate : {best['hit_rate_pct']}%  ({best['n']} signals across MAG7)")
    print(f"  Avg ret  : {best['avg_ret_pct']}%  over next {FORWARD_BARS_1M} min")
    print(f"  p-value  : {best['p_value']}  "
          f"{'<< significant' if best['p_value']<0.05 else 'marginal' if best['p_value']<0.10 else '— need more data'}")

    print(f"\n  Per-ticker breakdown (trigger {best['trigger']}):")
    for ticker, grp in combined[combined["trigger"]==best["trigger"]].groupby("ticker"):
        hr = (grp["fwd_ret_pct"] < 0).mean() * 100
        print(f"    {ticker}: {len(grp):3d} signals | hit {hr:.0f}%")

    combined.to_csv("rsi_mtf_confluence.csv", index=False)
    print("\nSaved: rsi_mtf_confluence.csv")


if __name__ == "__main__":
    run()
