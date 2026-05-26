
# Contributing

This repository is organized as a personal quantitative research and engineering project. The goal is to keep exploratory work reproducible while separating backtesting workflows from live-monitoring utilities.

## Branching Model

The repository intentionally separates major workflows:

- `mean_reversion`: backtesting notebooks, signal mapping, and portfolio diagnostics.
- `rsi_tracker`: live/intraday RSI monitoring, IBKR/TWS utilities, and signal alerting workflows.

When adding a new feature, create a descriptive branch:

```bash
git checkout -b feature/add-slippage-model
```

## Pull Request Checklist

Before opening a pull request, check that:

- no credentials or account-specific data are committed,
- `.env` files are not committed,
- code runs locally,
- notebooks are cleared of unnecessary outputs unless outputs are intentionally documented,
- README sections are updated if commands or project structure change,
- backtest results are clearly labeled as historical/exploratory.

## Data Policy

Do not commit private market data, account data, broker credentials, or proprietary datasets. Use local files or small redacted samples when needed.

## Research Standards

Backtest results should be presented with appropriate limitations. When reporting performance, specify:

- number of signals,
- sample window,
- assumptions,
- whether transaction costs and slippage are included,
- whether the result is historical backtest, paper trading, or live trading.
