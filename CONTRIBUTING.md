Branch strategy
---------------

This repository intentionally keeps `mean_reversion` (backtests, notebooks, releases) and
`rsi_tracker` (live-tracking, IBKR helpers) separate. They live in the same repo for
convenience but are allowed to diverge — there is deliberately no shared runtime library.

Guidelines
- Keep branches independent: do not merge `rsi_tracker` into `mean_reversion` unless you
  intend to bring tracker code into the release branch.
- Critical bugfixes: cherry-pick across branches when necessary; avoid automated merges.
- Documentation: update both branches if user-facing behavior or run instructions change.
- Sync cadence: decide a regular schedule (weekly/biweekly) for manual syncs where needed.

Pull requests
- Target the branch that matches intent (backtest changes → `mean_reversion`; live tools → `rsi_tracker`).
- Explain cross-branch impacts in the PR description when relevant.

Contact
- For questions about branch policy, open a PR discussion or file an issue.
