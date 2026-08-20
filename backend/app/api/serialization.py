"""Turning DataFrames into JSON that is actually valid JSON.

`DataFrame.to_dict()` hands back NaN and infinity as Python floats, and
those have no representation in the JSON spec. FastAPI refuses them:

    ValueError: Out of range float values are not JSON compliant: nan

which surfaces as a 500 on an endpoint whose data was perfectly fine. That
is what `/market/candles` was doing on every request — relative volume is
NaN for the whole series whenever the source publishes no volume, which on
the free path is always, so the endpoint failed permanently rather than
occasionally.

NaN becomes `null`, which is what it means: not a number, no value here.
The alternative fixes are both worse. Dropping the column hides that the
indicator exists; substituting 0 or 1 invents a reading, and a client
cannot tell an invented neutral value from a real one.

Infinity becomes `null` too. It only arises from a division that should not
have happened, and a client has no more use for `Infinity` than for `NaN`.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def jsonable_records(df: pd.DataFrame) -> list[dict]:
    """DataFrame rows as JSON-safe dicts, with NaN and inf as None.

    Timestamps are stringified: a tz-aware Timestamp survives FastAPI's
    encoder but arrives in a shape that varies with pandas versions, and
    every consumer here wants the ISO string anyway.
    """
    if df is None or len(df) == 0:
        return []

    out = df.copy()
    for column in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[column]):
            out[column] = out[column].astype(str)

    # Replace before the object cast: comparing against inf is only
    # meaningful while the column is still numeric.
    out = out.replace([np.inf, -np.inf], np.nan)
    return out.astype(object).where(pd.notna(out), None).to_dict(orient="records")


def has_unserialisable_floats(records: list[dict]) -> bool:
    """True if any value would break a strict JSON encoder.

    Exists for tests: asserting on the absence of NaN is clearer than
    asserting that a request happened not to 500.
    """
    for row in records:
        for value in row.values():
            if isinstance(value, float) and (np.isnan(value) or np.isinf(value)):
                return True
    return False
