# algo_trade

Branch: `rsi_tracker`

This repository contains a Jupyter notebook and the CSV data used to run a signal backtest from a local `data/` folder.

It also includes the RSI tracker and IBKR utility scripts on the `rsi_tracker` branch for live tracking, scanning, and signal research.

## Performance at a Glance

The latest backtest on the most recent 200 signals after `2026-01-01` produced:

| Metric | Value |
| --- | ---: |
| Average final PnL per signal | `0.79%` |
| Median final PnL per signal | `1.39%` |
| Win rate | `62.00%` |
| Best timeframe | `1-hour` |
| Best direction | `Long` |
| Portfolio final balance | `$101,596.39` |
| Max drawdown | `-0.57%` |

## Layout

- `data/TradeSignals - Stocks.csv` - source signal file
- `notebooks/backtest_data_engine.ipynb` - notebook for mapping, filtering, simulation, plots, and portfolio analysis
- `rsi_tracker/` - RSI tracker and confluence research scripts
- `ibkr/` - IBKR utility scripts and auth/scanner helpers

## RSI Tracker / IBKR

The RSI tracker code is organized as standalone Python scripts for live and research workflows:

- `rsi_tracker/rsi_tracker.py`
- `rsi_tracker/rsi_tracker_tws.py`
- `rsi_tracker/rsi_signal_monitor.py`
- `rsi_tracker/rsi_strategy_finder.py`
- `rsi_tracker/rsi_mtf_confluence.py`
- `rsi_tracker/rsi_mag7_signal.py`
- `rsi_tracker/rsi_mag7_ibkr.py`
- `rsi_tracker/rsi_tracker_tws_test_plain.py`

IBKR helpers live under `ibkr/`:

- `ibkr/ibkr_fix_auth.py`
- `ibkr/ibkr_scanner.py`
- `ibkr/ibkr_short_tomorrow.py`

These scripts use local IB Gateway / TWS connections and do not store credentials in the repo.

## Backtest Results

The backtest used the latest 200 signals after `2026-01-01`.

| Metric | Value |
| --- | ---: |
| Signals evaluated | `200` |
| Signal window | `2026-04-29` to `2026-05-22` |
| Average final PnL per signal | `0.79%` |
| Median final PnL per signal | `1.39%` |
| Win rate | `62.00%` |
| Best timeframe | `1-hour` |
| Best direction | `Long` |
| Portfolio final balance | `$101,596.39` |
| Max drawdown | `-0.57%` |

## What the notebook does

The notebook loads the CSV locally, maps the signal columns into the engine schema, filters the signals by date, and runs the simulation and portfolio workflow end to end. It also saves summary tables and Plotly charts for review.

## Outputs

Running the notebook produces:

- Backtest summary CSV
- Portfolio equity CSV
- Plotly HTML charts for the distribution, timeframe split, direction split, and equity curve

## Mean Reversion Strategy Summary

The strategy is built around mean reversion: when price stretches too far away from a short-term equilibrium, the model looks for a reversal back toward the mean. Rather than relying on a single trigger, the signal logic combines several filters:

- Stochastic oscillator extremes to detect overbought or oversold conditions
- Time-based filters so entries are taken only during more relevant market windows
- Direction-aware validation so long and short setups are handled differently
- Price-based checks such as signal price, take profit, and stop loss levels

In practice, the idea is to enter when the market looks extended, then capture the snap-back move while controlling risk with predefined exits. The signals are not taken blindly from one indicator; they are built from a confluence of conditions, with the stochastic oscillator and time window acting as key confirmation filters. This gives the strategy a more selective, event-driven profile rather than a generic oscillation trade.

If you are interested in the performance, please reach out.

## Notes

The notebook currently reads from `../data/TradeSignals - Stocks.csv` because it lives inside `notebooks/`.

## Run

Open `notebooks/backtest_data_engine.ipynb` in Jupyter and run the cells top to bottom.
