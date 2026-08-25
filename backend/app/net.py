"""Budgets for outbound requests.

Two sessions of live data were lost to the same failure, and neither one
looked like a failure while it was happening.

The mechanism, from the logs of 24 and 25-Aug-2026:

  1. Container DNS stops resolving. `getaddrinfo` no longer returns
     promptly, so every outbound call gets slow rather than failing.
  2. Nothing bounded a *call*, only a single HTTP round trip. One option
     chain fetch is a contract-info request (3 attempts) followed by one
     chain request per published expiry (2 attempts each), each attempt
     preceded by a two-page cookie warm-up. At a 12-second per-request
     timeout and a dozen expiries that is over fourteen minutes of work for
     a job that is scheduled every sixty seconds.
  3. Every scheduled job carries `max_instances=1`, so while that call is
     in flight APScheduler skips the next run. And the next. The skip is
     logged by APScheduler's own logger and by nothing of ours.
  4. The price ticker keeps ticking, because it is a different job on a
     different endpoint. The dashboard stays live. The desk looks healthy
     while the option archive — the one dataset that cannot be rebuilt —
     silently stops growing.

The fix here is the missing bound in (2): a *deadline* shared by every
outbound call a single scheduled tick makes, sized from that job's own
interval. A tick that cannot finish its work in less time than it has
before it runs again must give the slot back, because the alternative is
not "a slower tick" — it is no ticks at all.

Two properties this is careful about:

  **The budget is per tick, not per request.** Retries, cookie warm-ups and
  per-expiry fallbacks all spend from the same pot, so no amount of nesting
  can multiply the wall clock beyond it. That is the property the old
  per-request timeout could not provide.

  **Running out is a refusal, not a truncation.** Where a wait exists for a
  reason — the NSE rate limit above all — a caller with no budget left is
  told so and gives up. Shortening the wait instead would turn a slow tick
  into a burst against an endpoint that blocks for bursts, which is the
  same data loss by a different route.

The residual, stated plainly: `socket.getaddrinfo` is a blocking C call and
no Python timeout interrupts it, so a request that is stuck *in name
resolution* returns when the resolver gives up and not before. This module
bounds everything on either side of that call and refuses to start new ones
once the budget is gone; the resolver itself is bounded where it is
configured, by `dns_opt` on the backend service in docker-compose.yml. The
honest guarantee is therefore: a tick returns within its budget plus at
most one in-flight name resolution.
"""
from __future__ import annotations

import contextvars
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

log = logging.getLogger(__name__)


class BudgetExhausted(TimeoutError):
    """The work ran out of the time its schedule allowed it.

    A `TimeoutError` subclass on purpose: every existing caller catches
    broad exceptions around network work and must keep treating this as the
    ordinary failure it is. The distinct type is for the code that wants to
    stop retrying rather than try the next fallback — there is no point
    falling back to a second source with no time left to call it.
    """


# What share of its own interval a scheduled job may spend on the network.
#
# Not 1.0. A job that runs for exactly its interval leaves nothing for the
# database write, the scheduler's own jitter, or the clock being a little
# unkind, and lands right back on the `max_instances` skip this exists to
# prevent. The remaining fifth is that margin.
BUDGET_FRACTION = 0.8

# Floor for a derived budget. Below a couple of seconds nothing over the
# public internet completes anyway, and a budget that can never be met just
# converts a slow source into a source that is never read.
MIN_BUDGET_SECONDS = 2.0

# Below this much remaining there is no point beginning another request; it
# would be abandoned mid-flight having spent the last of the budget and
# learned nothing.
MIN_REQUEST_SECONDS = 0.5


def budget_for(interval_seconds: float) -> float:
    """The network budget for a job that runs every `interval_seconds`.

    This is the whole design in one line: what a tick is allowed to spend
    is a property of how often it runs, not of how patient the person who
    wrote the request happened to feel.
    """
    return max(MIN_BUDGET_SECONDS, float(interval_seconds) * BUDGET_FRACTION)


class Deadline:
    """A shrinking allowance of wall-clock time.

    Monotonic throughout, so an NTP correction mid-session cannot hand a
    call more budget than it started with, or less.
    """

    def __init__(self, budget_seconds: float, *, label: str = "request") -> None:
        self.budget = float(budget_seconds)
        self.label = label
        self._expiry = time.monotonic() + self.budget

    def __repr__(self) -> str:                              # pragma: no cover
        return f"<Deadline {self.label} {self.remaining():.1f}s of {self.budget:.1f}s>"

    def remaining(self) -> float:
        return max(0.0, self._expiry - time.monotonic())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def spent(self) -> float:
        return self.budget - self.remaining()

    def check(self, what: str = "") -> None:
        """Raise if there is nothing left. For guarding non-network work."""
        if self.expired:
            raise BudgetExhausted(
                f"{self.label} exhausted its {self.budget:.1f}s budget"
                + (f" before {what}" if what else ""))

    def slice(self, want: float, *, minimum: float = MIN_REQUEST_SECONDS) -> float:
        """A timeout for one request: what it wants, or what is left.

        Refuses rather than returning a uselessly small timeout, so a caller
        cannot spend its last fraction of a second discovering that it had
        no time to make the call.
        """
        left = self.remaining()
        if left < minimum:
            raise BudgetExhausted(
                f"{self.label} has {left:.2f}s of its {self.budget:.1f}s budget "
                f"left, below the {minimum:.2f}s needed to start a request")
        return min(float(want), left)

    def wait(self, seconds: float) -> None:
        """Sleep for the full time asked, or refuse and raise.

        Never a short sleep. The waits this serves — the NSE rate limit,
        retry backoff — are protective, and a shortened one is worse than
        none: it produces exactly the burst the limit exists to prevent,
        against an endpoint that answers bursts with a block.
        """
        seconds = float(seconds)
        if seconds <= 0:
            return
        if self.remaining() < seconds:
            raise BudgetExhausted(
                f"{self.label} cannot afford a {seconds:.2f}s wait with "
                f"{self.remaining():.2f}s of its {self.budget:.1f}s budget left")
        time.sleep(seconds)


# The active budget, if a caller has set one.
#
# A ContextVar rather than an argument threaded through every signature.
# The call tree between "the collector ticked" and "httpx opened a socket"
# runs through the worker, the broker adapter and the NSE client, and none
# of the layers in between have any opinion about time budgets — passing a
# deadline through them would be four signature changes to express one fact
# that belongs to the caller at the top.
#
# ContextVars are per-thread when set inside a thread, which is exactly the
# shape needed here: APScheduler runs each job in its own pool thread, and
# two jobs must not share or overwrite each other's budget.
_active: contextvars.ContextVar[Deadline | None] = contextvars.ContextVar(
    "quantdesk_deadline", default=None)


@contextmanager
def budget(seconds: float, *, label: str = "job") -> Iterator[Deadline]:
    """Give everything called inside this block one shared time budget."""
    deadline = Deadline(seconds, label=label)
    token = _active.set(deadline)
    try:
        yield deadline
    finally:
        _active.reset(token)
        if deadline.expired:
            log.warning("%s used its whole %.1fs network budget",
                        label, deadline.budget)


def current() -> Deadline | None:
    """The budget in force, or None when nobody set one."""
    return _active.get()


def deadline_or(seconds: float, *, label: str = "request") -> Deadline:
    """The caller's budget if there is one, otherwise a fresh standalone one.

    The fallback matters as much as the sharing. An API request handler and
    a one-off script have no schedule to derive a budget from, and they must
    still not be able to hang forever — so they get their own default rather
    than an absent one.
    """
    return current() or Deadline(seconds, label=label)
