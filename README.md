# Mean Reversion Backtest Repo

This repository contains a Jupyter notebook and the CSV data used to run a signal backtest from a local `data/` folder.

## Layout

- `data/TradeSignals - Stocks.csv` - source signal file
- `notebooks/backtest_data_engine.ipynb` - notebook for mapping, filtering, simulation, plots, and portfolio analysis

## What the notebook does

The notebook loads the CSV locally, maps the signal columns into the engine schema, filters the signals by date, and runs the simulation and portfolio workflow end to end. It also saves summary tables and Plotly charts for review.

## Mean Reversion Strategy Summary

The strategy is built around mean reversion: when price stretches too far away from a short-term equilibrium, the model looks for a reversal back toward the mean. The signal logic combines multiple filters, including:

- Stochastic oscillator extremes to detect overbought or oversold conditions
- Time-based filters so entries are taken only during more relevant market windows
- Direction-aware validation so long and short setups are handled differently
- Price-based checks such as signal price, take profit, and stop loss levels

In practice, the idea is to enter when the market looks extended, then capture the snap-back move while controlling risk with predefined exits.

If you are interested in the performance, please reach out.

## Notes

The notebook currently reads from `../data/TradeSignals - Stocks.csv` because it lives inside `notebooks/`.

## Run

Open `notebooks/backtest_data_engine.ipynb` in Jupyter and run the cells top to bottom.
