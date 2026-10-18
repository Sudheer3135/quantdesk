"""Research methodology (Repair Pass 2D).

What research needs in order not to fool itself, kept apart from strategy
code:

  events      the append-only, hash-chained log every record below lives in
  registry    which sessions research has seen (OS-1), the prospective
              holdout lock, exposure, and one-time final evaluation
  folds       session-based expanding-window folds with an embargo (OS-2/3)
  trials      preregistered trials, results and multiple-testing counts (OS-4)
  benchmarks  frozen benchmark definitions and the primary metric (OS-5)
  sample      sample adequacy and session-clustered bootstrap (OS-6)
  mtm         the daily marked-to-market research ledger (OS-7)
  readiness   one methodology-readiness summary, one status per question

None of it tunes, searches or scores a strategy.
"""
