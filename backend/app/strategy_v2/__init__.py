"""Strategy v2: weekly NIFTY option buying, run on paper beside the desk.

v2 takes the desk's signal and plan exactly as they are and changes only
what happens after them — which contract, when not to buy, and how the
position is closed. Nothing in this package alters the signal engine, the
plan, the regime classifier, the risk manager or the v1 backtest; it calls
them.
"""
