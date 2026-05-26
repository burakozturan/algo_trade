"""
MAG7 RSI Overbought Signal  —  IBKR Edition
Runs 1m / 5m / 15m analysis separately, then prints a single winner.

Run IB Gateway first:
  - Live:  port 4001  |  Paper: port 4002
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
TWS_PORT  = 4001          # 4001 live | 4002 paper | 7496 TWS live | 7497 TWS paper
CLIENT_ID = 17533940

MAG7 = ["AAPL", "MSFT", "AMZN", "META", "GOOGL", "NVDA", "TSLA"]

TIMEFRAMES = [
    # (bar_size,   duration,  fwd_bars,  label)
    ("1 min",   "7 D",   30, "1m"),    # 30 bars fwd = 30 min
    ("5 mins",  "30 D",  12, "5m"),    # 12 bars fwd = 60 min
    ("15 mins", "60 D",   8, "15m"),   # 8  bars fwd = 2 h
]

RSI_PERIOD    = 14
RSI_THRESHOLD = 70
USE_RTH       = True
WHAT_TO_SHOW  = "TRADES"
REQUEST_DELAY = 0.5       # seconds between IBKR requests (pacing)
# ─────────────────────────────────────────────────────────────────────────────


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def find_episodes(rsi: pd.Series, threshold: float = 70.0) -> pd.DataFrame:
    above    = rsi > threshold
    episodes = []
    in_ep    = False

    for i, val in enumerate(above):
        if val and not in_ep:
            in_ep = True
            start = i
        elif not val and in_ep:
            in_ep  = False
            end    = i - 1
            ep_rsi = rsi.iloc[start:end + 1]
            episodes.append({
                "start_idx":     start,
                "end_idx":       end,
                "duration_bars": end - start + 1,
                "area_above":    float(np.trapezoid(ep_rsi - threshold)),
                "peak_rsi":      float(ep_rsi.max()),
                "entry_bar":     end + 1,
            })
        elif val and in_ep and i == len(above) - 1:
            end    = i
            ep_rsi = rsi.iloc[start:end + 1]
            episodes.append({
                "start_idx":     start,
                "end_idx":       end,
                "duration_bars": end - start + 1,
                "area_above":    float(np.trapezoid(ep_rsi - threshold)),
                "peak_rsi":      float(ep_rsi.max()),
                "entry_bar":     None,
            })

    return pd.DataFrame(episodes)


def attach_forward_return(episodes: pd.DataFrame, close: pd.Series,
                          fwd_bars: int = 12) -> pd.DataFrame:
    rows = []
    for _, ep in episodes.iterrows():
        entry = ep["entry_bar"]
        if entry is None or pd.isna(entry):
            continue
        entry    = int(entry)
        exit_idx = entry + fwd_bars
        if exit_idx >= len(close):
            continue
        fwd_ret = (close.iloc[exit_idx] / close.iloc[entry] - 1) * 100
        rows.append({**ep.to_dict(), "fwd_ret_pct": fwd_ret})
    return pd.DataFrame(rows)


def grid_search(df: pd.DataFrame) -> pd.DataFrame:
    dur_cuts  = range(1, 16)
    area_cuts = np.percentile(df["area_above"], [0, 10, 25, 50, 75, 90])

    results = []
    for d in dur_cuts:
        for a in area_cuts:
            subset = df[(df["duration_bars"] >= d) & (df["area_above"] >= a)]
            if len(subset) < 8:
                continue
            avg_ret  = subset["fwd_ret_pct"].mean()
            hit_rate = (subset["fwd_ret_pct"] < 0).mean() * 100
            t_stat, p_val = stats.ttest_1samp(subset["fwd_ret_pct"], 0)
            results.append({
                "min_duration_bars": d,
                "min_area":          round(a, 1),
                "n_signals":         len(subset),
                "avg_fwd_ret_pct":   round(avg_ret, 3),
                "hit_rate_pct":      round(hit_rate, 1),
                "t_stat":            round(t_stat, 2),
                "p_value":           round(p_val, 4),
            })

    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values(
        ["hit_rate_pct", "p_value"], ascending=[False, True]
    )


def fetch_bars(ib: IB, ticker: str, bar_size: str, duration: str) -> pd.DataFrame:
    contract = Stock(ticker, "SMART", "USD")
    ib.qualifyContracts(contract)
    time.sleep(REQUEST_DELAY)

    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting=bar_size,
        whatToShow=WHAT_TO_SHOW,
        useRTH=USE_RTH,
        formatDate=1,
        keepUpToDate=False,
    )
    if not bars:
        return pd.DataFrame()

    df = util.df(bars)[["date", "close"]].rename(columns={"date": "ts", "close": "Close"})
    df["ts"] = pd.to_datetime(df["ts"])
    return df.set_index("ts").sort_index()


def analyse_timeframe(ib: IB, bar_size: str, duration: str,
                      fwd_bars: int, label: str) -> dict:
    """Fetch all MAG7, run episode detection + grid search, return summary dict."""

    bar_mins    = int(bar_size.split()[0])
    fwd_mins    = fwd_bars * bar_mins
    all_results = []

    print(f"\n{'━'*56}")
    print(f"  TIMEFRAME: {label}   ({bar_size}, {duration} history, "
          f"fwd={fwd_mins} min)")
    print(f"{'━'*56}")

    for ticker in MAG7:
        df = fetch_bars(ib, ticker, bar_size, duration)
        if df.empty:
            print(f"  {ticker}: no data"); continue

        close    = df["Close"].dropna()
        rsi      = compute_rsi(close, RSI_PERIOD)
        episodes = find_episodes(rsi, RSI_THRESHOLD)
        if episodes.empty:
            print(f"  {ticker}: no episodes"); continue

        episodes = attach_forward_return(episodes, close, fwd_bars)
        if episodes.empty:
            continue

        episodes["ticker"] = ticker
        all_results.append(episodes)
        print(f"  {ticker}: {len(episodes):3d} episodes | "
              f"avg {episodes['duration_bars'].mean():.1f} bars "
              f"({episodes['duration_bars'].mean()*bar_mins:.0f} min) | "
              f"hit {(episodes['fwd_ret_pct']<0).mean()*100:.0f}%")

    if not all_results:
        print("  No usable data for this timeframe.")
        return {"label": label, "grid": pd.DataFrame(), "best": None,
                "bar_mins": bar_mins, "fwd_mins": fwd_mins}

    combined = pd.concat(all_results, ignore_index=True)
    grid     = grid_search(combined)

    if grid.empty:
        print("  Not enough signals for grid search.")
        return {"label": label, "grid": grid, "best": None,
                "bar_mins": bar_mins, "fwd_mins": fwd_mins}

    best = grid.iloc[0]
    print(f"\n  Top 10 combos:")
    print(grid.head(10).to_string(index=False))

    print(f"\n  >> BEST [{label}]: "
          f"duration ≥ {best['min_duration_bars']:.0f} bars "
          f"({int(best['min_duration_bars'])*bar_mins} min), "
          f"area ≥ {best['min_area']}, "
          f"n={best['n_signals']:.0f}, "
          f"hit={best['hit_rate_pct']}%, "
          f"p={best['p_value']}")

    # save per-timeframe CSVs
    combined.to_csv(f"rsi_mag7_{label}_episodes.csv", index=False)
    grid.to_csv(f"rsi_mag7_{label}_grid.csv", index=False)

    return {"label": label, "grid": grid, "best": best,
            "bar_mins": bar_mins, "fwd_mins": fwd_mins,
            "n_total": len(combined)}


def print_winner(summaries: list):
    """Pick the single clearest setup across all timeframes."""
    candidates = [s for s in summaries if s["best"] is not None]
    if not candidates:
        print("\nNo valid results to compare."); return

    # Score = hit_rate - 50 (edge above coin flip) weighted by significance
    # Penalise p > 0.10 heavily so noise doesn't win
    def score(s):
        b = s["best"]
        edge = b["hit_rate_pct"] - 50.0
        sig  = max(0.0, 1.0 - b["p_value"] * 10)   # 0 when p≥0.10
        return edge * sig

    candidates.sort(key=score, reverse=True)
    winner = candidates[0]
    b      = winner["best"]
    bm     = winner["bar_mins"]

    print(f"\n{'═'*56}")
    print(f"  CLEAREST SETUP ACROSS ALL TIMEFRAMES")
    print(f"{'═'*56}")
    print(f"  Timeframe      : {winner['label']}")
    print(f"  Duration filter: RSI > 70 for ≥ {b['min_duration_bars']:.0f} consecutive bars")
    print(f"                   = {int(b['min_duration_bars'])*bm} minutes")
    print(f"  Area filter    : area above 70 ≥ {b['min_area']}  (RSI pts × bars)")
    print(f"  Signals (MAG7) : {b['n_signals']:.0f}")
    print(f"  Hit rate       : {b['hit_rate_pct']}%  sold off in next {winner['fwd_mins']} min")
    print(f"  Avg return     : {b['avg_fwd_ret_pct']}%")
    print(f"  t-stat / p     : {b['t_stat']} / {b['p_value']}")
    sig_label = ("SIGNIFICANT" if b["p_value"] < 0.05
                 else "marginal" if b["p_value"] < 0.10
                 else "not significant yet — need more data")
    print(f"  Significance   : {sig_label}")

    print(f"\n  All timeframe comparison:")
    print(f"  {'TF':<6} {'hit%':<8} {'p-val':<8} {'n':<6} {'score':<6}")
    for s in candidates:
        bb = s["best"]
        print(f"  {s['label']:<6} {bb['hit_rate_pct']:<8} "
              f"{bb['p_value']:<8} {bb['n_signals']:<6.0f} "
              f"{score(s):<6.2f}")
    print(f"{'═'*56}")


def run():
    ib = IB()
    print(f"Connecting to IB Gateway {TWS_HOST}:{TWS_PORT} ...")
    ib.connect(TWS_HOST, TWS_PORT, clientId=CLIENT_ID)
    print("Connected.")

    summaries = []
    for bar_size, duration, fwd_bars, label in TIMEFRAMES:
        s = analyse_timeframe(ib, bar_size, duration, fwd_bars, label)
        summaries.append(s)

    ib.disconnect()

    print_winner(summaries)


if __name__ == "__main__":
    run()
