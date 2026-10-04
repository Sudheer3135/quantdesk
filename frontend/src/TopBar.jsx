/* The market bar: the one strip that is true whether or not anything else
   on the page has loaded.

   Everything in it is a fact about the market or about this desk's link to
   it — never an opinion. Quotes the backend does not serve are named and
   marked unavailable rather than omitted, because a missing tile reads as
   "flat" and an absent feed is not a flat market.
*/
import { useEffect, useRef, useState } from "react";

import { QuoteCell } from "./panels.jsx";
import { UNAVAILABLE, istTime } from "./format.js";

/** Flash the tape green or red for a moment when the print changes.

    Lives here because the top bar is the only place the live price is
    rendered. It used to appear twice — once here and once in a detail panel
    — and a number that disagrees with itself for one render, or simply
    repeats, costs screen space without adding a reading. */
function useFlash(value) {
  const [flash, setFlash] = useState("");
  const last = useRef(null);

  useEffect(() => {
    if (value === null || value === undefined || value === last.current) return;
    const dir = last.current === null ? "" : value > last.current ? "up" : "down";
    last.current = value;
    if (!dir) return;
    setFlash(dir);
    const id = setTimeout(() => setFlash(""), 600);
    return () => clearTimeout(id);
  }, [value]);

  return flash;
}

const SESSION_LABEL = {
  open: "Market open", "pre-open": "Pre-open", closed: "Market closed",
};

/* Terse on purpose. The full sentence — "Data live", "Last print" — belongs
   to the freshness line in the status column, and having both say the same
   words made the eye read the phrase twice and learn nothing the second
   time. Here it is a status lamp; there it is an explanation. */
const AGE_LABEL = {
  live: "LIVE", delayed: "DELAYED", stale: "STALE",
  closed: "CLOSED", unknown: "AGE UNKNOWN",
};

/* The browser's link to the desk. Deliberately never the word "live": that
   word belongs to the freshness pill beside it, and two pills both reading
   LIVE — one about a socket, one about a price — let a connected socket pass
   for fresh data. */
const LINK_LABEL = {
  connecting: "connecting", connected: "connected",
  reconnecting: "reconnecting", polling: "polling",
};

const AGE_TONE = {
  live: "go", delayed: "wait", stale: "stop", closed: "flat", unknown: "wait",
};

export default function TopBar({
  price, vix, market, link, ageState, ageText, clock, onRefresh,
}) {
  const session = market?.session ?? null;
  const flash = useFlash(price?.price);

  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="M4 16L9 11L13 14L20 6M15 6H20V11" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg></span>
        <span className="brand-name">Quant<b>Desk</b></span>
        <span className="brand-sub">options signal platform</span>
      </div>

      <div className="quotes">
        <QuoteCell label="NIFTY 50" quote={price} flash={flash} />
        {/* Deliberately empty. `/market/price` serves the single configured
            watch symbol, and this desk has no second feed — so these are
            named and marked unavailable rather than quietly dropped or,
            worse, filled with the NIFTY number. */}
        <QuoteCell label="SENSEX" quote={null} />
        <QuoteCell label="BANK NIFTY" quote={null} />
        <div className="quote">
          <span className="quote-name">INDIA VIX</span>
          <span className="quote-price mono">
            {vix === null || vix === undefined ? UNAVAILABLE : vix.toFixed(2)}
          </span>
          <span className="quote-change mono dim">
            {vix === null || vix === undefined ? "" : vix < 15 ? "LOW" : vix < 22 ? "MID" : "HIGH"}
          </span>
        </div>
      </div>

      <div className="topbar-status">
        <span className={`pill ${market?.open ? "on" : "off"}`}>
          <i className="dot" />
          {session ? (SESSION_LABEL[session] ?? session) : "…"}
        </span>
        <span className={`pill tone-${AGE_TONE[ageState] ?? "wait"}`} title="Age of the latest NIFTY tick (feed freshness)">
          <i className="dot" />
          {AGE_LABEL[ageState] ?? ageState}
          {ageText ? ` ${ageText}` : ""}
        </span>
        <span className={`pill link-${link}`} title="Connection to the QuantDesk backend — not the age of the data">
          <i className="dot" />{LINK_LABEL[link] ?? link}
        </span>
        <span className="clock mono">{istTime(clock, true)} IST</span>
        <button className="ghost-btn" onClick={onRefresh}><svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none"><path d="M20 7v5h-5M19 12a7 7 0 1 0-2 5M20 12l-3-5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>refresh</button>
      </div>
    </header>
  );
}
