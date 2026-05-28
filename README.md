# algo_trade

Python research toolkit for mean-reversion signal backtesting, intraday RSI monitoring, IBKR/TWS utilities, and portfolio diagnostics.

This repository is an exploratory quantitative research and engineering project. It includes:

1. A **mean-reversion backtest workflow** for evaluating stock trading signals.
2. A **multi-timeframe RSI tracker** for intraday signal monitoring.
3. **IBKR / TWS utility scripts** for local scanning and live research workflows.
4. Optional **Discord alerting** for signal notifications.

> **Disclaimer:** This repository is for research, education, and engineering practice only. It is not financial advice and should not be interpreted as evidence of live trading profitability. Backtest results are exploratory and are not live trading performance.

---

## Branches

The repository keeps `mean_reversion` and `rsi_tracker` separate so backtesting and live-monitoring workflows can evolve independently.

---

## Performance at a Glance

The latest exploratory backtest on the most recent 200 signals after `2026-01-01` produced:

| Metric | Value |
|---|---:|
| Signals evaluated | 200 |
| Signal window | 2026-04-29 to 2026-05-22 |
| Average final PnL per signal | 0.79% |
| Median final PnL per signal | 1.39% |
| Win rate | 62.00% |
| Best timeframe | 1-hour |
| Best direction | Long |
| Portfolio entry balance | $100,000 |
| Portfolio final balance | $101,596.39 |
| Max drawdown | -0.57% |

These results are historical backtest outputs only. They do not include all real-world trading frictions and should not be interpreted as live performance.

## Real-life Estimate (with simple trading costs)

To give a rough idea of how trading costs would affect the exploratory results above, a quick sensitivity was applied to the generated portfolio assuming:

- **Position sizing**: `BET_MODE='fraction'` with `BET_SIZE=0.01` (1% of current balance per trade).
- **Round-trip cost (commission + slippage)**: **0.07%** of traded notional per trade (0.0007).

Under these conservative assumptions (costs charged on each executed position sized at 1% of account), the notebook produces an approximate adjusted outcome:

- **Start balance**: $100,000 (approx)
- **Final balance (adjusted)**: $102,270.42 (approx)
- **Net change**: +2.27% (approx)
- **Mean PnL per signal (adjusted)**: ~1.12% (was ~1.19% before costs)
- **Max drawdown (adjusted)**: ~-0.74%

These numbers are only an illustrative, order-of-magnitude adjustment. They assume you can fill at the reported `Signal Price` (the engine currently uses the supplied signal/optimum/SL price when provided), and that position sizing is `1%` per trade. Real-life slippage, market impact, exchange/clearing fees, latency misses, partial fills, and taxes can further reduce realized returns.

If you want, I can:

- Re-run the notebook with different cost scenarios (higher/lower round-trip cost).
- Use a per-trade fixed commission model instead of percentage-based costs.
- Simulate fills at next-bar open instead of the supplied `Signal Price` while still charging costs (a more conservative hybrid).

The raw artifacts used to compute these adjusted numbers are in `notebooks/backtest_portfolio.csv` and `notebooks/backtest_results_from_data_engine.csv`. A short summary was written to `notebooks/approx_real_life_summary.txt`.

---

## Repository Structure

```text
algo_trade/
├── data/                  # Local signal data; sample/redacted files only
├── notebooks/             # Backtest notebooks and exploratory analysis
├── rsi_tracker/           # RSI monitoring and multi-timeframe signal scripts
├── ibkr/                  # IBKR / TWS helper scripts
├── .github/               # Pull request and repo workflow templates
├── data_engine.py         # Signal mapping and backtest utilities
├── pyproject.toml         # Package configuration
├── requirements.txt       # Dependencies
├── .env.example           # Local environment variable template
└── README.md              # Project overview
```

---

## RSI Tracker / IBKR Scripts

The RSI tracker code is organized as standalone Python scripts for live monitoring and research workflows:

```text
rsi_tracker/rsi_tracker.py
rsi_tracker/rsi_tracker_tws.py
rsi_tracker/rsi_signal_monitor.py
rsi_tracker/rsi_strategy_finder.py
rsi_tracker/rsi_mtf_confluence.py
rsi_tracker/rsi_mag7_signal.py
rsi_tracker/rsi_mag7_ibkr.py
rsi_tracker/rsi_tracker_tws_test_plain.py
```

IBKR helper scripts live under `ibkr/`:

```text
ibkr/ibkr_fix_auth.py
ibkr/ibkr_scanner.py
ibkr/ibkr_short_tomorrow.py
```

These scripts use local IB Gateway / TWS connections and local environment variables. Credentials should never be committed.

---

## Methodology

The backtest maps raw signal records into a standardized schema with fields such as timestamp, symbol, direction, entry price, stop loss, take profit, and holding window. The simulation evaluates long and short setups separately and computes both per-signal and portfolio-level diagnostics.

Current metrics include:

- win rate,
- average and median final PnL per signal,
- portfolio final balance,
- maximum drawdown,
- timeframe-level performance,
- direction-level performance,
- PnL distribution,
- equity curve.

The mean-reversion logic is based on the idea that when price stretches too far away from a short-term equilibrium, it may revert toward the mean. Instead of relying on a single trigger, the workflow combines multiple filters:

- stochastic oscillator extremes,
- time-window filters,
- direction-aware validation for long and short setups,
- price-based checks such as signal price, take-profit, and stop-loss levels.

Note: To avoid obvious lookahead the simulation uses the next-bar Open as the execution price for any signal or event (i.e., entries are placed at the Open of the first bar strictly after the event timestamp). The exploratory runs in this repo do not apply transaction costs or explicit slippage models — real-life performance will be lower. These assumptions are documented here for transparency.

The RSI tracker extends this research workflow by monitoring intraday RSI conditions across multiple timeframes and producing signals for review.

---

## Example Outputs

Running the notebook produces:

- backtest summary CSV,
- portfolio equity CSV,
- Plotly HTML charts for:
  - PnL distribution,
  - timeframe split,
  - direction split,
  - equity curve.

Suggested screenshots to add under an `assets/` folder:

```text
assets/equity_curve.png
assets/drawdown_curve.png
assets/pnl_distribution.png
assets/timeframe_split.png
assets/direction_split.png
```

Then embed them in the README with:

```markdown
![Equity curve](assets/equity_curve.png)
![PnL distribution](assets/pnl_distribution.png)
```

---

## How to Reproduce the Backtest

Clone the repository and install the notebook dependencies:

```bash
git clone https://github.com/burakozturan/algo_trade.git
cd algo_trade
python -m venv .venv
source .venv/bin/activate
pip install -e ".[notebook]"
jupyter lab notebooks/backtest_data_engine.ipynb
```

Run the notebook cells from top to bottom.

The notebook currently reads from:

```text
../data/TradeSignals - Stocks.csv
```

because it lives inside the `notebooks/` directory.

---

## How to Run the RSI Tracker

Create a local `.env` file from the sample:

```bash
cp .env.example .env
# Edit .env locally with your own values.
```

Start IB Gateway or Trader Workstation first, then run one of the tracker scripts:

```bash
python rsi_tracker/rsi_tracker_tws.py
python rsi_tracker/rsi_tracker.py
python rsi_tracker/rsi_signal_monitor.py
```

Run the IBKR helper scripts only when needed:

```bash
python ibkr/ibkr_fix_auth.py
python ibkr/ibkr_scanner.py
python ibkr/ibkr_short_tomorrow.py
```

---

## Security

Credentials and account-specific settings are loaded from local `.env` files and are not committed. The `.env.example` file documents the required variables without exposing private information.

Recommended security practices:

- never commit `.env`,
- never commit API keys,
- never commit account IDs,
- keep broker credentials local,
- use read-only or paper-trading configurations for testing when possible.

---

## Limitations and Next Steps

Current limitations:

- Backtest results are exploratory and are not live trading results.
- Transaction costs, slippage, borrow constraints, latency, partial fills, and market impact are not fully modeled.
- Signal data is local and not redistributed in this repository.
- The current evaluation focuses on a recent signal window and should be expanded to longer out-of-sample periods.
- RSI monitoring scripts are research tools and do not constitute a full execution system.

Planned improvements:

- Add transaction-cost and slippage assumptions.
- Add walk-forward validation and train/test time splits.
- Add volatility-adjusted position sizing.
- Add Sharpe ratio, Sortino ratio, profit factor, exposure, turnover, and average holding period.
- Add performance breakdowns by ticker, timeframe, direction, and market regime.
- Add unit tests around the backtest engine and signal schema.
- Add screenshot examples of the generated Plotly charts.
- Expand documentation for IBKR/TWS setup and paper-trading workflows.

---

## Why I Built This

I built this project to practice the full quantitative research workflow: signal definition, data mapping, backtesting, portfolio diagnostics, intraday monitoring, and alerting. The project connects my background in econometrics, time-series modeling, statistical modeling, and Python-based data analysis with practical trading research workflows.
