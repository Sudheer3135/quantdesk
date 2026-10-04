"""Dated option contract specifications (OC-4).

For any contract a study trades, research has to know — for the date it was
traded — its underlying, strike, side, listed expiry, contract identifier and
the lot size that applied. Two shortcuts used to stand in for that:

  **A weekday for the expiry.** "NIFTY weeklies expire on Tuesday" is true
  until it is not: NSE has moved the day before, and a holiday moves a single
  week. An expiry computed from a weekday is a contract that may never have
  been listed. Wherever the archive lists the contract, the listed expiry is
  the answer; the weekday calendar survives only for a fully modelled run
  that has no archive, and is labelled as synthetic there.

  **A configured lot size.** `settings.lot_size` is an undated default, and
  the collector used to write it onto every contract row as if the source had
  published it. It did not — NSE's public chain carries no lot size — so the
  archive's 75s and 65s are configuration, not contract metadata.

So each lot size carries its basis, and a basis alone never verifies it:

  SOURCE_PUBLISHED           the contract observation carries its own
                             auditable lot-size evidence (below)
  CITED_SCHEDULE             a dated `LOT_SIZE_SCHEDULE` entry whose evidence
                             is complete
  CITED_SCHEDULE_UNVERIFIED  a schedule entry missing part of that evidence
  CONFIGURED_UNVERIFIED      a number is on the row with no evidence for it —
                             whichever source's row it is
  UNAVAILABLE                nothing — reported as `contract metadata unavailable`

Verified means auditable: the value, the date range it was in force, who
published it and the document that says so, all present and all consistent
with the contract date. A source *name* on the row is not evidence. The
archive's rows tagged `angel` or `kite` hold whatever the collector wrote,
and the collector wrote the configured default; the tag says who served the
quote, not who established the lot. Those stay unverified.

Nothing here invents a lot size. `LOT_SIZE_SCHEDULE` is empty until someone
enters a revision with the exchange circular that established it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

SOURCE_PUBLISHED = "source_published"
CITED_SCHEDULE = "cited_schedule"
CITED_SCHEDULE_UNVERIFIED = "cited_schedule_unverified"
CONFIGURED_UNVERIFIED = "configured_unverified"
UNAVAILABLE = "contract_metadata_unavailable"
VERIFIED = (SOURCE_PUBLISHED, CITED_SCHEDULE)

ARCHIVE_LISTED = "archive_listed"
SYNTHETIC_WEEKDAY = "synthetic_weekday_modelled"


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


@dataclass(frozen=True)
class LotSizeEvidence:
    """What makes one lot size auditable.

    `source` is who published it (the exchange, a broker's instrument master
    as retrieved), `reference` the document or file that says so (a circular
    number, a master file's retrieval id). Both are required; neither is ever
    filled in by this module.
    """
    lot_size: int | None
    effective_from: date | None
    source: str | None
    reference: str | None
    effective_to: date | None = None

    def problems(self, *, lot_size: int | None, on: date | None) -> list[str]:
        found = []
        if self.lot_size is None or self.lot_size <= 0:
            found.append("no lot size in the evidence")
        elif lot_size is not None and int(lot_size) != int(self.lot_size):
            found.append("evidence names a different lot size")
        if self.effective_from is None:
            found.append("no effective date")
        if _blank(self.source):
            found.append("no source identity")
        if _blank(self.reference):
            found.append("no source reference")
        if on is None:
            found.append("no contract date to check the evidence against")
        elif self.effective_from is not None and (
                on < self.effective_from
                or (self.effective_to is not None and on > self.effective_to)):
            found.append("evidence not in force on the contract date")
        return found

    def auditable(self, *, lot_size: int | None, on: date | None) -> bool:
        return not self.problems(lot_size=lot_size, on=on)


@dataclass(frozen=True)
class LotSizeEntry:
    """One exchange lot-size revision.

    A missing citation is allowed to exist so that it can be reported: an
    entry without one resolves as CITED_SCHEDULE_UNVERIFIED, never as cited.
    """
    underlying: str
    effective_from: date
    lot_size: int
    citation: str | None
    source: str | None = "NSE"
    effective_to: date | None = None

    def __post_init__(self) -> None:
        if self.lot_size <= 0:
            raise ValueError("a lot size must be positive")

    def evidence(self) -> LotSizeEvidence:
        return LotSizeEvidence(
            lot_size=self.lot_size, effective_from=self.effective_from,
            source=self.source,
            reference=None if _blank(self.citation) else self.citation,
            effective_to=self.effective_to)


# Deliberately empty. Populate only from an exchange circular, with its
# reference in `citation`; an entry typed from memory is exactly the
# unverified number this module exists to keep out of research.
LOT_SIZE_SCHEDULE: tuple[LotSizeEntry, ...] = ()


def scheduled_lot_size(underlying: str, on: date,
                       schedule: tuple[LotSizeEntry, ...] | None = None
                       ) -> LotSizeEntry | None:
    """The revision in force on `on`, or None."""
    entries = sorted((e for e in (LOT_SIZE_SCHEDULE if schedule is None else schedule)
                      if e.underlying == underlying and e.effective_from <= on
                      and (e.effective_to is None or on <= e.effective_to)),
                     key=lambda e: e.effective_from)
    return entries[-1] if entries else None


@dataclass(frozen=True)
class ContractSpec:
    underlying: str
    strike: float
    option_type: str
    expiry: date
    expiry_basis: str
    contract_id: int | None
    tradingsymbol: str | None
    lot_size: int | None
    lot_size_basis: str
    source: str | None = None
    citation: str | None = None
    evidence: LotSizeEvidence | None = None
    as_of: date | None = None

    @property
    def lot_size_verified(self) -> bool:
        """Re-derived from the evidence every time, never from the basis
        label alone — a spec built by hand as `cited_schedule` with no
        citation is not verified."""
        return (self.lot_size_basis in VERIFIED and self.lot_size is not None
                and self.evidence is not None
                and self.evidence.auditable(lot_size=self.lot_size, on=self.as_of))

    def to_dict(self) -> dict:
        return asdict(self) | {"lot_size_verified": self.lot_size_verified}


def resolve(*, underlying: str, key, meta, on: date, expiry_basis: str,
            schedule: tuple[LotSizeEntry, ...] | None = None) -> ContractSpec:
    """Everything research knows about one contract on one date.

    `key` is the contract (expiry, strike, side). `meta` is its archive
    record, or None when there is none. The lot size is taken from the most
    trustworthy basis available, and the basis is always reported. A
    recorded lot size is source-published only when `meta.lot_size_evidence`
    is auditable for `on`; the row's source name is not consulted.
    """
    lot, basis, evidence = None, UNAVAILABLE, None
    recorded = getattr(meta, "lot_size", None) if meta is not None else None
    source = getattr(meta, "source", None) if meta is not None else None
    observed = getattr(meta, "lot_size_evidence", None) if meta is not None else None

    if recorded and observed is not None \
            and observed.auditable(lot_size=recorded, on=on):
        lot, basis, evidence = int(recorded), SOURCE_PUBLISHED, observed
    else:
        entry = scheduled_lot_size(underlying, on, schedule)
        if entry is not None:
            evidence = entry.evidence()
            lot = entry.lot_size
            basis = (CITED_SCHEDULE if evidence.auditable(lot_size=lot, on=on)
                     else CITED_SCHEDULE_UNVERIFIED)
        elif recorded:
            lot, basis = int(recorded), CONFIGURED_UNVERIFIED

    return ContractSpec(
        underlying=underlying, strike=float(key.strike),
        option_type=key.option_type, expiry=key.expiry,
        expiry_basis=expiry_basis,
        contract_id=getattr(meta, "contract_id", None) if meta is not None else None,
        tradingsymbol=getattr(meta, "tradingsymbol", None) if meta is not None else None,
        lot_size=lot, lot_size_basis=basis, source=source,
        citation=evidence.reference if evidence is not None else None,
        evidence=evidence, as_of=on)
