"""
MAG7 RSI Overbought Signal
- Fetches 5-min OHLCV data via yfinance (free, no API key)
- Calculates RSI(14)
- For each RSI>70 episode: measures duration (minutes) and area under curve (AUC)
- Backtests forward returns to find the AUC/duration combo that actually works
"""

import yfinance as yf
import pandas as pd
import numpy as np
from scipy import stats

MAG7 = ["AAPL", "MSFT", "AMZN", "META", "GOOGL", "NVDA", "TSLA"]

RSI_PERIOD = 14
RSI_THRESHOLD = 70
FORWARD_BARS = 12          # how many bars ahead to measure return (12 x 5min = 1 hour)
INTERVAL = "5m"            # 1m / 5m / 15m / 1h
PERIOD = "60d"             # yfinance lookback


# ── RSI ──────────────────────────────────────────────────────────────────────

def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# ── Episode detector ─────────────────────────────────────────────────────────

def find_episodes(rsi: pd.Series, threshold: float = 70.0) -> pd.DataFrame:
    """
    Returns one row per continuous episode where RSI > threshold.
    Columns: start_idx, end_idx, duration_bars, area_above_threshold
    """
    above = rsi > threshold
    episodes = []
    in_ep = False

    for i, val in enumerate(above):
        if val and not in_ep:
            in_ep = True
            start = i
        elif not val and in_ep:
            in_ep = False
            end = i - 1
            ep_rsi = rsi.iloc[start:end + 1]
            area = float(np.trapz(ep_rsi - threshold))   # area ABOVE the 70 line
            episodes.append({
                "start_idx": start,
                "end_idx": end,
                "duration_bars": end - start + 1,
                "area_above": area,
                "peak_rsi": float(ep_rsi.max()),
                "entry_bar": end + 1,                     # bar just after RSI drops back <70
            })
        elif val and in_ep and i == len(above) - 1:       # episode still open at end
            end = i
            ep_rsi = rsi.iloc[start:end + 1]
            area = float(np.trapz(ep_rsi - threshold))
            episodes.append({
                "start_idx": start,
                "end_idx": end,
                "duration_bars": end - start + 1,
                "area_above": area,
                "peak_rsi": float(ep_rsi.max()),
                "entry_bar": None,
            })

    return pd.DataFrame(episodes)


# ── Forward return ────────────────────────────────────────────────────────────

def attach_forward_return(episodes: pd.DataFrame, close: pd.Series,
                          fwd_bars: int = FORWARD_BARS) -> pd.DataFrame:
    rows = []
    for _, ep in episodes.iterrows():
        entry = ep["entry_bar"]
        if entry is None or pd.isna(entry):
            continue
        entry = int(entry)
        exit_idx = entry + fwd_bars
        if exit_idx >= len(close):
            continue
        fwd_ret = (close.iloc[exit_idx] / close.iloc[entry] - 1) * 100
        rows.append({**ep.to_dict(), "fwd_ret_pct": fwd_ret})
    return pd.DataFrame(rows)


# ── Grid search for best thresholds ──────────────────────────────────────────

def grid_search(df: pd.DataFrame) -> pd.DataFrame:
    """
    Sweep duration_bars and area_above cutoffs.
    For each combo: show avg forward return and hit-rate (% negative = mean-reversion worked).
    """
    dur_cuts = range(1, 12)                            # 1..11 bars
    area_cuts = np.percentile(df["area_above"], [10, 25, 50, 75, 90])

    results = []
    for d in dur_cuts:
        for a in area_cuts:
            subset = df[(df["duration_bars"] >= d) & (df["area_above"] >= a)]
            if len(subset) < 5:
                continue
            avg_ret = subset["fwd_ret_pct"].mean()
            hit_rate = (subset["fwd_ret_pct"] < 0).mean() * 100  # % that fell (mean reversion)
            t_stat, p_val = stats.ttest_1samp(subset["fwd_ret_pct"], 0)
            results.append({
                "min_duration_bars": d,
                "min_area": round(a, 1),
                "n_signals": len(subset),
                "avg_fwd_ret_pct": round(avg_ret, 3),
                "hit_rate_pct": round(hit_rate, 1),
                "t_stat": round(t_stat, 2),
                "p_value": round(p_val, 3),
            })

    return pd.DataFrame(results).sort_values("hit_rate_pct", ascending=False)


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    all_results = []

    for ticker in MAG7:
        print(f"\n{'='*50}")
        print(f"  {ticker}")
        print(f"{'='*50}")

        df = yf.download(ticker, period=PERIOD, interval=INTERVAL,
                         auto_adjust=True, progress=False)
        if df.empty:
            print("  No data"); continue

        close = df["Close"].squeeze().dropna()
        rsi = compute_rsi(close, RSI_PERIOD)

        episodes = find_episodes(rsi, RSI_THRESHOLD)
        if episodes.empty:
            print("  No RSI>70 episodes"); continue

        episodes = attach_forward_return(episodes, close)
        if episodes.empty:
            print("  Not enough forward bars"); continue

        episodes["ticker"] = ticker
        all_results.append(episodes)

        print(f"  Total episodes:  {len(episodes)}")
        print(f"  Avg duration:    {episodes['duration_bars'].mean():.1f} bars  "
              f"({episodes['duration_bars'].mean() * int(INTERVAL[:-1]):.0f} min)")
        print(f"  Avg area above 70: {episodes['area_above'].mean():.1f}")
        print(f"  Avg fwd return:  {episodes['fwd_ret_pct'].mean():.3f}%")
        print(f"  Mean-reversion hit rate: {(episodes['fwd_ret_pct']<0).mean()*100:.1f}%")

    if not all_results:
        print("No data collected."); return

    combined = pd.concat(all_results, ignore_index=True)

    print(f"\n{'='*50}")
    print("  GRID SEARCH  —  best (duration, area) combos")
    print(f"{'='*50}")
    grid = grid_search(combined)
    print(grid.head(15).to_string(index=False))

    # ── Best single value ─────────────────────────────────────────────────────
    best = grid.iloc[0]
    print(f"\n>>> BEST COMBO <<<")
    print(f"  min_duration : {best['min_duration_bars']} bars  "
          f"= {int(best['min_duration_bars']) * int(INTERVAL[:-1])} minutes above RSI 70")
    print(f"  min_area     : {best['min_area']}  (RSI points × bars)")
    print(f"  signals      : {best['n_signals']}")
    print(f"  avg return   : {best['avg_fwd_ret_pct']}%  over next {FORWARD_BARS} bars "
          f"({FORWARD_BARS * int(INTERVAL[:-1])} min)")
    print(f"  hit rate     : {best['hit_rate_pct']}%  went down (mean-reversion)")
    print(f"  p-value      : {best['p_value']}  {'<< statistically significant' if best['p_value']<0.05 else '(not significant yet)'}")

    combined.to_csv("rsi_mag7_episodes.csv", index=False)
    grid.to_csv("rsi_mag7_grid.csv", index=False)
    print("\nSaved: rsi_mag7_episodes.csv  |  rsi_mag7_grid.csv")


if __name__ == "__main__":
    run()
