## Summary

Briefly describe the change.

## Type of change

- [ ] Backtest workflow
- [ ] RSI tracker
- [ ] IBKR / TWS utility
- [ ] Documentation
- [ ] Packaging / CI
- [ ] Other

## Checklist

- [ ] No credentials or account-specific data are committed.
- [ ] `.env` is not committed.
- [ ] Code runs locally.
- [ ] README updated if commands or structure changed.
- [ ] Backtest results are labeled as exploratory/historical.
- [ ] Limitations are documented when reporting performance.

## Notes

Add any implementation notes, assumptions, or follow-up tasks.
## Summary

Describe the purpose of this PR and which branch it targets.

## Checklist
- [ ] Is the PR targeting the correct branch (`mean_reversion` or `rsi_tracker`)?
- [ ] Does this change intentionally keep code separate from the other branch?
- [ ] If this PR fixes a bug that must be applied to the other branch, mention how it will be backported.
- [ ] CI green (if applicable)

## Notes for reviewers
If the change impacts runtime config or deployment, ensure the `README.md` and `.env.example` are updated in the same PR.
