"""Headline tone, and what the scorer refuses to claim.

The module is a lexicon, which means every reading it produces can be
checked by reading the word list. These pin the cases that are easy to get
wrong and were got wrong on the way here.

Two of them are regressions from the first implementation, and both were
found by scoring real headlines off the live feeds rather than invented ones:

  `test_a_negator_reaches_the_whole_clause` — a fixed three-word lookback
  could not connect "fails" to "gains" seven words later in
  "Market fails to hold on to day's gains", so the headline scored neutral.

  `test_a_multi_word_negator_still_negates` — "shrugs off" sat in the negator
  set as a two-word string while the scan was per word, so it could never
  match, and "oil shrugs off sanctions concerns" read as bad news.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.news import sentiment  # noqa: E402

POSITIVE, NEGATIVE, NEUTRAL = (
    sentiment.POSITIVE, sentiment.NEGATIVE, sentiment.NEUTRAL)


# ---------------------------------------------------------------- the read

@pytest.mark.parametrize("headline,expected", [
    ("Sensex surges 800 points to record high as FII inflows return", POSITIVE),
    ("RBI cuts rates by 25 bps, stocks rally", POSITIVE),
    ("Nifty tumbles 2% as selloff deepens on tariff fears", NEGATIVE),
    ("IPO fund diversion: Sebi bars promoter for 7 years", NEGATIVE),
    ("RBI working to give banknotes in your pocket a longer life", NEUTRAL),
    ("HEG demerger becomes effective from 07 Sep 2026", NEUTRAL),
])
def test_it_reads_ordinary_market_headlines(headline, expected):
    assert sentiment.score(headline).label == expected


def test_a_negator_reaches_the_whole_clause():
    """The headline that broke a fixed lookback window.

    Seven words separate "fails" from "gains". Any window wide enough to
    catch that is wide enough to invert things it has no business touching,
    so negation is scoped to the clause instead.
    """
    reading = sentiment.score(
        "Market fails to hold on to day's gains, ends marginally lower")
    assert reading.label == NEGATIVE
    assert "gains" in reading.negated


def test_a_multi_word_negator_still_negates():
    """"shrugs off" as a two-word entry could never match a per-word scan."""
    reading = sentiment.score(
        "India bonds rise as oil shrugs off US sanctions concerns")
    assert reading.label == POSITIVE
    assert {"sanctions", "concerns"} <= set(reading.negated)


def test_negation_does_not_leak_across_a_clause_boundary():
    """"No fraud found" must not invert the clause after the comma."""
    reading = sentiment.score("No fraud found, profits rise")
    assert reading.label == POSITIVE
    assert "profits" not in reading.negated
    assert "rise" not in reading.negated


# ------------------------------------------------------------- the working

def test_every_reading_carries_the_words_that_produced_it():
    """A tone label with no visible evidence is one nobody can argue with."""
    reading = sentiment.score("Sensex surges to a record high")
    assert reading.matched
    assert "record high" in dict(reading.matched)
    assert "record high" in reading.reason
    assert "surges" in reading.reason


def test_an_unmatched_headline_says_undetermined_not_neutral():
    """Silence and balance are different findings, and the panel says so."""
    reading = sentiment.score("Company announces board meeting date")
    assert reading.matched == []
    assert "undetermined" in reading.reason


# ------------------------------------------------------------ the mechanics

def test_a_phrase_beats_its_parts():
    """"record high" must score once as a phrase, not as "high" plus a
    stray "record" — and never as both."""
    reading = sentiment.score("Nifty hits a record high")
    assert [t for t, _ in reading.matched] == ["record high"]


def test_html_entities_are_decoded_before_scoring():
    """RSS titles arrive encoded, sometimes twice. `day&#39;s` must not
    become a token that hides the word beside it."""
    assert sentiment.normalise("Market&#39;s gains") == "market's gains"
    assert sentiment.score("Profit &amp; growth surge").label == POSITIVE


def test_typographic_quotes_do_not_split_a_word():
    assert "bessent's" in sentiment.normalise("Scott Bessent’s strategy")


def test_the_score_saturates_rather_than_running_away():
    """Emphasis is not additional information. Ten bad words is not ten
    times worse than one."""
    piled = sentiment.score(
        "crash plunge slump crisis fraud bankruptcy recession selloff")
    assert piled.score == -1.0
    assert piled.raw < -1.0


def test_an_empty_headline_is_neutral_and_says_nothing():
    for text in ("", "   ", None):
        reading = sentiment.score(text)
        assert reading.label == NEUTRAL
        assert reading.matched == []


def test_clauses_split_on_punctuation_not_on_spaces():
    assert sentiment.clauses("Nifty falls; Sensex rises, rupee steady") == [
        ["nifty", "falls"], ["sensex", "rises"], ["rupee", "steady"]]


# ------------------------------------------------------------- the average

def test_unmatched_headlines_are_excluded_from_the_mean():
    """Averaging them in as zeros would drag every reading toward neutral and
    make a genuinely one-sided morning look balanced."""
    readings = [sentiment.score("Sensex surges to record high"),
                sentiment.score("Board meeting scheduled"),
                sentiment.score("Company announces AGM date")]
    tone = sentiment.aggregate(readings)

    assert tone["total"] == 3
    assert tone["scored"] == 1
    assert tone["label"] == POSITIVE          # not diluted to neutral by two zeros


def test_the_aggregate_counts_every_label_even_the_unscored():
    tone = sentiment.aggregate([
        sentiment.score("Nifty rallies"), sentiment.score("Nifty tumbles"),
        sentiment.score("Board meeting scheduled")])
    assert tone["counts"] == {POSITIVE: 1, NEGATIVE: 1, NEUTRAL: 1}


def test_an_empty_page_has_no_score_rather_than_a_zero():
    tone = sentiment.aggregate([])
    assert tone["score"] is None
    assert tone["label"] == NEUTRAL


def test_the_aggregate_disclaims_what_it_is_not():
    tone = sentiment.aggregate([sentiment.score("Nifty rallies")])
    assert "not a view on NIFTY" in tone["note"]
    assert "signal engine" in tone["note"]


# ------------------------------------------------------- lexicon hygiene

def test_no_term_appears_in_both_lexicons():
    """A word in both lists scores whichever the loop reaches first, which
    is an ordering accident rather than a decision."""
    overlap = set(sentiment.POSITIVE_TERMS) & set(sentiment.NEGATIVE_TERMS)
    assert overlap == set(), overlap


def test_no_term_is_also_a_negator():
    """It would invert itself and everything after it in the clause."""
    terms = set(sentiment.POSITIVE_TERMS) | set(sentiment.NEGATIVE_TERMS)
    assert terms & sentiment.NEGATORS == set()


def test_no_phrase_is_longer_than_the_matcher_looks():
    """A four-word entry would sit in the lexicon and never fire — silently,
    because nothing checks that a term is reachable."""
    terms = set(sentiment.POSITIVE_TERMS) | set(sentiment.NEGATIVE_TERMS)
    too_long = [t for t in terms if len(t.split()) > sentiment._MAX_PHRASE]
    assert too_long == []


def test_every_term_is_lower_case_and_matchable():
    """Tokens are lower-cased before matching, so a capitalised entry is
    dead weight."""
    terms = set(sentiment.POSITIVE_TERMS) | set(sentiment.NEGATIVE_TERMS)
    assert [t for t in terms if t != t.lower()] == []
    # Every single-word term must survive the tokeniser that will look it up.
    unmatchable = [t for t in terms
                   if len(t.split()) == 1 and sentiment._tokens(t) != [t]]
    assert unmatchable == []
