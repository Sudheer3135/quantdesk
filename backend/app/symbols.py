"""The symbol universe this platform carries.

Audit finding H-3 was fixed inside the free-data adapter, which left a gap:
validation that lives in one broker is not validation. Running with
BROKER=mock, an unsupported symbol sailed past the allowlist and the mock
happily generated candles for it — so the API's behaviour depended on which
adapter was loaded, which is exactly the kind of difference that shows up in
production and not in the tests.

The universe belongs to the platform, not to a feed. Adapters map these
names onto whatever their upstream calls them; the API validates against
this list before any adapter is involved.
"""
from __future__ import annotations

from .brokers.base import UnknownSymbol

# What the desk analyses. Adding one here is a deliberate act: every entry
# needs a mapping in each adapter that claims to serve it.
SUPPORTED: frozenset[str] = frozenset({
    "NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "INDIAVIX",
})


def normalise(symbol: str) -> str:
    """Canonical form: upper case, no surrounding whitespace."""
    return (symbol or "").strip().upper()


def validate(symbol: str) -> str:
    """Return the canonical symbol, or raise UnknownSymbol.

    Called at the API boundary so the answer is the same whichever broker
    is configured.
    """
    canonical = normalise(symbol)
    if canonical not in SUPPORTED:
        raise UnknownSymbol(
            f"{symbol!r} is not a supported symbol. This platform carries: "
            f"{', '.join(sorted(SUPPORTED))}."
        )
    return canonical
