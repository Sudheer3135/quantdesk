"""Headline sentiment, scored from a lexicon that says what it matched.

Why a lexicon and not a model: every other module on this desk outputs a
score, a label *and its reasons*, and a sentiment reading nobody can argue
with is a reading nobody should act on. A word list can be read, disputed
and corrected by the person whose money is on the line. A transformer on a
laptop cannot, and a paid API would put a vendor between the desk and its
own screen for a panel that is, at best, context.

What this is honestly good for
------------------------------
Telling a page of "Sebi bars promoter", "profit slumps", "rupee at record
low" apart from "record high", "beats estimates", "RBI cuts rates". That is
the whole claim. It is a coarse tone reading over a headline, useful for
noticing that the tape is running one way this morning.

What it is not
--------------
It is not an opinion about NIFTY, it does not read the article, and it has
no idea which company a headline is about or whether the desk is long it. A
bag of words cannot do sarcasm, cannot weigh a small company against an
index heavyweight, and will score "not as bad as feared" as negative.

**Nothing here feeds a trading decision.** The signal engine, the regime
classifier, the bias layer and the entry layer never see this module. It
exists to put context on a screen beside numbers that were computed without
it, and the dashboard says so.

Negation is handled because it inverts the answer rather than muddying it:
"fails to hold on to gains" contains *gains* and means the opposite.

The rule is **a negator flips everything after it, to the end of its
clause**. A fixed lookback window was tried first and was wrong for exactly
the headline that motivated this: seven words separate "fails" from "gains",
so any window wide enough to catch it was wide enough to invert half a
sentence it had no business touching. Clauses are the natural unit — a
newspaper headline negates a clause, not a neighbourhood of words — and
splitting on punctuation keeps "no fraud found, profits rise" from having
its second half inverted by its first.
"""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field

POSITIVE, NEGATIVE, NEUTRAL = "positive", "negative", "neutral"

# Terms and their weight. Weights are coarse on purpose — 1 for ordinary
# market language, 2 for words that only appear when something has really
# happened. Anything finer would be false precision over a headline.
#
# Indian-market vocabulary is deliberate: "bourses", "crore", "Sebi bars",
# "circuit" and "FII inflows" carry tone here that a generic English list
# would score at zero.
POSITIVE_TERMS: dict[str, float] = {
    "gain": 1, "gains": 1, "gained": 1, "rise": 1, "rises": 1, "rising": 1,
    "rose": 1, "jump": 1, "jumps": 1, "jumped": 1, "surge": 2, "surges": 2,
    "surged": 2, "rally": 2, "rallies": 2, "rallied": 2, "soar": 2,
    "soars": 2, "soared": 2, "climb": 1, "climbs": 1, "climbed": 1,
    "advance": 1, "advances": 1, "higher": 1, "up": 1, "upbeat": 1,
    "outperform": 1, "outperforms": 1, "beat": 1, "beats": 1, "topped": 1,
    "record high": 2, "all-time high": 2, "lifetime high": 2, "52-week high": 1,
    "profit": 1, "profits": 1, "boost": 1, "boosts": 1, "boosted": 1,
    "growth": 1, "expands": 1, "expansion": 1, "recovery": 1, "rebound": 2,
    "rebounds": 2, "rebounded": 2, "bullish": 2, "optimism": 1, "optimistic": 1,
    "strong": 1, "strength": 1, "robust": 1, "upgrade": 2, "upgrades": 2,
    "upgraded": 2, "buy rating": 2, "inflow": 1, "inflows": 1, "raises": 1,
    "hikes guidance": 2, "dividend": 1, "bonus issue": 1, "approval": 1,
    "approved": 1, "wins": 1, "won": 1, "order win": 2, "rate cut": 2,
    "cuts rates": 2, "eases": 1, "stimulus": 1, "revival": 1, "turnaround": 2,
    # Same reasoning as the negative additions below: common market
    # vocabulary the first pass happened not to contain.
    "bags": 1, "secures": 1, "clinches": 1, "signs": 1, "partnership": 1,
    "tailwinds": 1, "momentum": 1, "accelerates": 1, "outpaces": 1,
    "milestone": 1, "eases inflation": 2, "resilient": 1,
}

NEGATIVE_TERMS: dict[str, float] = {
    "fall": 1, "falls": 1, "fell": 1, "falling": 1, "drop": 1, "drops": 1,
    "dropped": 1, "decline": 1, "declines": 1, "declined": 1, "slump": 2,
    "slumps": 2, "slumped": 2, "plunge": 2, "plunges": 2, "plunged": 2,
    "crash": 2, "crashes": 2, "crashed": 2, "tumble": 2, "tumbles": 2,
    "tumbled": 2, "sink": 1, "sinks": 1, "sank": 1, "slide": 1, "slides": 1,
    "slid": 1, "lower": 1, "down": 1, "weak": 1, "weakness": 1, "weakens": 1,
    "record low": 2, "all-time low": 2, "52-week low": 1, "loss": 1,
    "losses": 1, "lost": 1, "deficit": 1, "shortfall": 1, "miss": 1,
    "misses": 1, "missed": 1, "bearish": 2, "pessimism": 1, "gloom": 1,
    "downgrade": 2, "downgrades": 2, "downgraded": 2, "sell rating": 2,
    "outflow": 1, "outflows": 1, "selloff": 2, "sell-off": 2, "sell off": 2,
    "correction": 1, "crisis": 2, "recession": 2, "slowdown": 1, "cuts": 1,
    "layoff": 2, "layoffs": 2, "fraud": 2, "probe": 1, "probes": 1,
    "investigation": 1, "penalty": 1, "penalises": 1, "fine": 1, "fined": 1,
    "bars": 2, "barred": 2, "ban": 2, "bans": 2, "banned": 2, "default": 2,
    "insolvency": 2, "bankruptcy": 2, "downturn": 1, "hikes rates": 1,
    "rate hike": 1, "inflation": 1, "tariff": 1, "tariffs": 1, "sanctions": 1,
    "tension": 1, "tensions": 1, "war": 1, "concern": 1, "concerns": 1,
    "worry": 1, "worries": 1, "fear": 1, "fears": 1, "risk": 1, "risks": 1,
    "halt": 1, "halted": 1, "suspends": 1, "suspended": 1, "resign": 1,
    "resigns": 1, "quits": 1, "scam": 2, "diversion": 2,
    # Ordinary financial-news vocabulary that the first pass simply lacked.
    # Added as dictionary completeness, not tuned against a day's headlines:
    # each is unambiguously negative in a market context regardless of what
    # the tape happened to be doing when it was noticed.
    "warns": 1, "warn": 1, "warning": 1, "caution": 1, "cautions": 1,
    "cautious": 1, "criticism": 1, "criticised": 1, "criticized": 1,
    "criticises": 1, "downbeat": 1, "sluggish": 1, "stalls": 1, "stalled": 1,
    "delays": 1, "delayed": 1, "curbs": 1, "restricts": 1, "freeze": 1,
    "writedown": 2, "write-down": 2, "impairment": 2, "dispute": 1,
    "lawsuit": 1, "sues": 1, "recall": 1, "shutdown": 1, "strike": 1,
    "unrest": 1, "volatility": 1, "uncertainty": 1, "headwinds": 1,
}

# Words that flip everything after them within their clause. Single tokens
# only: a two-word entry like "shrugs off" could never match, because the
# scan is per word — that was a real bug, and "oil shrugs off sanctions
# concerns" read as bad news because of it.
NEGATORS = frozenset({
    "no", "not", "never", "none", "cannot", "cant", "wont",
    "fails", "fail", "failed", "failing", "without", "despite", "unable",
    "lacks", "lack", "denies", "denied", "avoids", "avoided",
    "less", "fewer", "unlikely", "doubt", "doubts",
    "shrugs", "shrugged", "shrug", "ignores", "ignored", "defies", "defied",
    "erases", "erased", "erasing", "pares", "pared", "reverses", "reversed",
})

# Where one clause ends and the next begins. A headline negates a clause,
# not a span of words, so this is the unit negation is confined to.
_CLAUSE = re.compile(r"[,;:.!?\u2013\u2014]|\s-\s")

# Below this, a headline is called neutral. Without a dead band a single
# incidental "risk" turns a routine story negative, and a panel that colours
# every headline is a panel whose colours mean nothing.
NEUTRAL_BAND = 0.5

# What one headline's raw score is divided by to reach the -1..1 range. Four
# points of evidence is a strongly-worded headline; more than that is
# emphasis, not additional information.
SATURATION = 4.0

# Longest term in the lexicons, in words. Multi-word terms ("record high",
# "sell-off") have to be matched before their parts are.
_MAX_PHRASE = 3

_WORD = re.compile(r"[a-z0-9][a-z0-9'\-]*")


@dataclass
class Reading:
    """One headline's tone, and the evidence for it."""

    label: str
    score: float                       # -1..1, saturating
    raw: float                         # unsaturated sum, for debugging
    # Every term that fired, with its signed contribution. This is the
    # "show your working" half — a reading whose evidence you cannot see is
    # one you cannot argue with.
    matched: list[tuple[str, float]] = field(default_factory=list)
    negated: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        if not self.matched:
            return "No lexicon terms matched — tone is undetermined, not neutral."
        parts = [f"{term} {value:+.0f}" for term, value in self.matched[:5]]
        note = f" ({', '.join(self.negated)} negated)" if self.negated else ""
        return f"Matched {', '.join(parts)}{note}."

    def to_dict(self) -> dict:
        return {"label": self.label, "score": round(self.score, 3),
                "matched": [t for t, _ in self.matched],
                "reason": self.reason}


def normalise(text: str) -> str:
    """Lower-case, entity-decoded, punctuation-flattened text.

    RSS titles arrive with HTML entities intact and sometimes double-encoded
    — Moneycontrol serves `day#39;s` — and with typographic quotes that would
    otherwise split a word in two.
    """
    if not text:
        return ""
    decoded = html.unescape(html.unescape(text))
    decoded = unicodedata.normalize("NFKD", decoded)
    decoded = decoded.replace("’", "'").replace("‘", "'")
    decoded = decoded.replace("“", '"').replace("”", '"')
    return decoded.lower()


def _tokens(text: str) -> list[str]:
    return _WORD.findall(normalise(text))


def clauses(text: str) -> list[list[str]]:
    """The headline as clauses of words, punctuation removed.

    Empty clauses are dropped rather than kept: "Sebi bars X, promoter" has
    a trailing fragment that carries no terms, and an empty list of words in
    the middle of the sequence would end a negation early for no reason.
    """
    return [words for words in
            (_WORD.findall(part) for part in _CLAUSE.split(normalise(text)))
            if words]


def score(text: str) -> Reading:
    """Read one headline's tone.

    Clause by clause. Within a clause, longest phrase first so "record high"
    is not scored as "high" plus a stray "record", and a matched phrase
    consumes its words so nothing is counted twice. Once a negator has been
    seen in a clause, every later term in that clause is inverted.
    """
    matched: list[tuple[str, float]] = []
    negated: list[str] = []
    total = 0.0

    for words in clauses(text):
        # Where the first negator sits. Everything at or after this index
        # is flipped; everything before it is read as written.
        flip_from = next((i for i, w in enumerate(words) if w in NEGATORS), None)
        consumed = [False] * len(words)

        for size in range(_MAX_PHRASE, 0, -1):
            for i in range(len(words) - size + 1):
                if any(consumed[i:i + size]):
                    continue
                phrase = " ".join(words[i:i + size])
                weight = POSITIVE_TERMS.get(phrase)
                sign = 1.0
                if weight is None:
                    weight = NEGATIVE_TERMS.get(phrase)
                    sign = -1.0
                if weight is None:
                    continue

                if flip_from is not None and i > flip_from:
                    sign = -sign
                    negated.append(phrase)

                for j in range(i, i + size):
                    consumed[j] = True
                contribution = sign * weight
                total += contribution
                matched.append((phrase, contribution))

    if not matched:
        return Reading(NEUTRAL, 0.0, 0.0)

    matched.sort(key=lambda pair: -abs(pair[1]))
    saturated = max(-1.0, min(1.0, total / SATURATION))

    if total >= NEUTRAL_BAND:
        label = POSITIVE
    elif total <= -NEUTRAL_BAND:
        label = NEGATIVE
    else:
        label = NEUTRAL

    return Reading(label, saturated, total, matched, negated)


def aggregate(readings: list[Reading]) -> dict:
    """The tone of a whole page of headlines.

    The mean of the *scored* items, with the count of each label beside it.
    Headlines that matched nothing are excluded from the mean and reported
    separately: averaging them in as zeros would drag every reading toward
    neutral and make a genuinely one-sided morning look balanced.
    """
    counts = {POSITIVE: 0, NEGATIVE: 0, NEUTRAL: 0}
    for reading in readings:
        counts[reading.label] += 1

    scored = [r.score for r in readings if r.matched]
    mean = sum(scored) / len(scored) if scored else None

    if mean is None:
        label = NEUTRAL
    elif mean >= 0.12:
        label = POSITIVE
    elif mean <= -0.12:
        label = NEGATIVE
    else:
        label = NEUTRAL

    return {
        "label": label,
        "score": round(mean, 3) if mean is not None else None,
        "counts": counts,
        "scored": len(scored),
        "total": len(readings),
        "note": (
            "Lexicon tone over headlines, not a view on NIFTY. Headlines that "
            "matched no term are excluded from the mean rather than counted "
            "as zero. Nothing here feeds the signal engine."
        ),
    }
