/* The terminal panels, tested apart from the socket.

   These exist because the redesign moved the desk's most consequential
   sentence — enter, wait, or stand down, and whether risk allows it — into
   new components, and a panel that renders a plausible-looking number it was
   never given is the failure mode that matters here. A trading screen is
   read at a glance and acted on; a fabricated zero is indistinguishable from
   a measurement.

   So most of what follows checks a *refusal*: that an absent input renders
   as "Unavailable" rather than as 0, "—", or a stale neighbour's value.
*/
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  DecisionPanel, MarketOverview, NewsPanel, PerformanceStrip, SafetyMonitor,
  SignalFeed, explainPlan, performanceFrom,
} from "./panels.jsx";
import TopBar from "./TopBar.jsx";

const bias = (label, confidence = 0.73) => ({
  label, confidence, reasons: ["15m structure is bullish."],
});
const entry = (state, extra = {}) => ({
  state, confidence: 0.6, reasons: ["Price is extended."], ...extra,
});

const signalOf = (over = {}) => ({
  action: "BUY", confidence: 0.61, timestamp: "2026-08-25T04:50:00Z",
  entry: 24_200, stop_loss: 24_150, target: 24_320, risk_reward: 2.4,
  context: { vwap: 24_190, atr14: 32, trend: "bullish", threshold_used: 0.35 },
  plan: { bias: bias("BULLISH"), entry: entry("WAIT_PULLBACK") },
  risk: { state: "approved", evaluated: true, approved: true, quantity: 75,
          lots: 1, rupees_at_risk: 1000, reasons: [],
          evaluated_at: "2026-08-25T04:50:00Z" },
  ...over,
});

/* ------------------------------------------------------------ top bar */

describe("top market bar", () => {
  const bar = (over = {}) => render(
    <TopBar
      price={{ symbol: "NIFTY", price: 24_334.55, change: 12.4 }}
      vix={11.13} market={{ open: true, session: "open" }} link="live"
      ageState="live" ageText="3s ago" clock={Date.parse("2026-08-25T04:50:00Z")}
      onRefresh={() => {}} {...over}
    />);

  it("shows the NIFTY price and its change", () => {
    bar();
    expect(screen.getByText("24,334.55")).toBeTruthy();
    expect(screen.getByText("+12.40")).toBeTruthy();
  });

  it("names SENSEX and BANK NIFTY as unavailable rather than omitting them", () => {
    /* The backend serves one watch symbol. A missing tile reads as a flat
       market; an absent feed is not a flat market, so it says so. */
    bar();
    expect(screen.getByText("SENSEX")).toBeTruthy();
    expect(screen.getByText("BANK NIFTY")).toBeTruthy();
    expect(screen.getAllByText("Unavailable").length).toBe(2);
  });

  it("never invents a price when the feed has none", () => {
    bar({ price: null });
    expect(screen.queryByText("0")).toBeNull();
    expect(screen.queryByText("0.00")).toBeNull();
    expect(screen.getAllByText("Unavailable").length).toBe(3);
  });

  it("bands the VIX rather than leaving a bare number", () => {
    bar();
    expect(screen.getByText("11.13")).toBeTruthy();
    expect(screen.getByText("LOW")).toBeTruthy();
  });

  it("says the feed is stale in the status lamp", () => {
    bar({ ageState: "stale", ageText: "9m 12s ago" });
    expect(screen.getByText(/STALE/)).toBeTruthy();
  });
});

/* -------------------------------------------------------- performance */

describe("performance strip", () => {
  const report = (over = {}) => ({
    overall: { n: 175, wins: 35, resolved: 175, unresolved: 0,
               win_rate: 0.2, total_r: -118.5, avg_r: -0.677, ...over },
    outcomes: [
      { r_multiple: 1.8, won: true }, { r_multiple: 2.2, won: true },
      { r_multiple: -1.0, won: false }, { r_multiple: -1.2, won: false },
    ],
  });

  it("derives average win and average loss from the rows it was given", () => {
    const perf = performanceFrom(report());
    expect(perf.avgWinR).toBeCloseTo(2.0, 5);
    expect(perf.avgLossR).toBeCloseTo(-1.1, 5);
  });

  it("counts losses as resolved-minus-won, not as stop-outs", () => {
    /* Time-capped and session-end exits are losses too. Counting only
       stop-outs would shrink the denominator the win rate is read against. */
    expect(performanceFrom(report()).losses).toBe(140);
  });

  it("reports averages as unavailable when no rows were requested", () => {
    const perf = performanceFrom({ ...report(), outcomes: [] });
    expect(perf.avgWinR).toBeNull();
    expect(perf.avgLossR).toBeNull();

    render(<PerformanceStrip perf={perf} />);
    expect(screen.getAllByText("Unavailable").length).toBe(2);
  });

  it("is empty rather than zeroed when nothing has been evaluated", () => {
    expect(performanceFrom({ overall: { n: 0 } })).toBeNull();
    expect(performanceFrom(null)).toBeNull();

    render(<PerformanceStrip perf={null} />);
    expect(screen.getByText(/No evaluated signals yet/)).toBeTruthy();
    expect(screen.queryByText("0%")).toBeNull();
  });

  it("renders the headline figures", () => {
    render(<PerformanceStrip perf={performanceFrom(report())} />);
    expect(screen.getByText("175")).toBeTruthy();
    expect(screen.getByText("35")).toBeTruthy();
    expect(screen.getByText("20.0%")).toBeTruthy();
    expect(screen.getByText("−118.5")).toBeTruthy();
  });
});

/* ---------------------------------------------------------- decision */

describe("active decision panel", () => {
  const panel = (over = {}, rest = {}) => render(
    <DecisionPanel
      signal={signalOf(over)} regime={{ day: { label: "TREND_UP", confidence: 0.8 } }}
      riskNow={null} marketOpen {...rest}
    />);

  it("shows regime, bias and entry state as three separate readings", () => {
    panel();
    expect(screen.getByText("Regime")).toBeTruthy();
    expect(screen.getByText("Bias")).toBeTruthy();
    expect(screen.getByText("Entry state")).toBeTruthy();
    expect(screen.getByText("Trend up")).toBeTruthy();
    expect(screen.getByText("Bullish")).toBeTruthy();
  });

  it("makes WAIT visually distinct from the bias that produced it", () => {
    /* The entry layer borrows the bias's direction and has none of its own,
       so it must never be coloured as though it did. */
    const { container } = panel();
    expect(container.querySelector(".bias-BULLISH")).toBeTruthy();
    expect(container.querySelector(".entry-WAIT_PULLBACK")).toBeTruthy();
    expect(container.querySelector(".verdict-wait")).toBeTruthy();
  });

  it("colours ENTER_NOW as go and NO_ENTRY as stop", () => {
    const go = panel({ plan: { bias: bias("BULLISH"), entry: entry("ENTER_NOW") } });
    expect(go.container.querySelector(".verdict-go")).toBeTruthy();
    go.unmount();

    const stop = panel({ plan: { bias: bias("BEARISH"), entry: entry("NO_ENTRY") } });
    expect(stop.container.querySelector(".verdict-stop")).toBeTruthy();
  });

  it("writes the call out in plain English", () => {
    panel();
    expect(screen.getByText(/Market direction is bullish/)).toBeTruthy();
    expect(screen.getByText(/Wait for a pullback/)).toBeTruthy();
  });

  it("names the invalidation level explicitly", () => {
    panel();
    expect(screen.getByText("Close beyond 24,150.00")).toBeTruthy();
  });

  it("shows the blocking reasons as failed checks", () => {
    panel({}, { riskNow: { state: "blocked", evaluated: true, quantity: 0,
                           lots: 0, rupees_at_risk: 0,
                           reasons: ["Kill switch is on — no new entries."] } });
    expect(screen.getByText(/Kill switch is on/)).toBeTruthy();
    expect(screen.getByText("BLOCKED")).toBeTruthy();
  });

  it("never shows a risk verdict on a HOLD, which proposes no trade", () => {
    const { container } = panel({ action: "HOLD", plan: null });
    expect(container.querySelector(".risk-verdict")).toBeNull();
  });

  it("says so when the signal carries no two-layer read", () => {
    panel({ plan: null });
    expect(screen.getByText(/No two-layer read for this signal/)).toBeTruthy();
  });

  it("marks the regime unavailable rather than guessing one", () => {
    panel({}, { regime: null });
    expect(screen.getByText("Unavailable")).toBeTruthy();
  });
});

describe("the plain-English explanation", () => {
  it("pairs a bullish read with the wait it implies", () => {
    const text = explainPlan(bias("BULLISH"), entry("WAIT_PULLBACK",
      { trigger_level: 24_180.5 }));
    expect(text).toMatch(/bullish/);
    expect(text).toMatch(/Wait for a pullback toward 24,180.50/);
  });

  it("does not tell the desk to wait when the state says enter", () => {
    const text = explainPlan(bias("BULLISH"), entry("ENTER_NOW"));
    expect(text).toMatch(/this is the moment/);
    expect(text).not.toMatch(/Wait/);
  });

  it("returns nothing at all when a layer is missing", () => {
    expect(explainPlan(null, entry("ENTER_NOW"))).toBeNull();
    expect(explainPlan(bias("BULLISH"), null)).toBeNull();
  });
});

/* -------------------------------------------------------------- feed */

describe("signal feed", () => {
  const rows = [
    { id: 2, created_at: "2026-08-25T04:50:00Z", action: "BUY", confidence: 0.61,
      bias: "BULLISH", entry_state: "ENTER_NOW", regime_day: "TREND_UP",
      risk_state: "approved" },
    { id: 1, created_at: "2026-08-25T04:45:00Z", action: "HOLD", confidence: 0.03,
      bias: null, entry_state: null, regime_day: null, risk_state: null },
  ];

  it("shows bias, entry state, regime and risk on every row", () => {
    render(<SignalFeed rows={rows} outcomes={[]} />);
    expect(screen.getByText("BUY")).toBeTruthy();
    expect(screen.getByText("Bullish")).toBeTruthy();
    expect(screen.getByText("Enter now")).toBeTruthy();
    expect(screen.getByText("Trend up")).toBeTruthy();
    expect(screen.getByText("APPROVED")).toBeTruthy();
  });

  it("joins a resolved outcome onto its own signal", () => {
    render(<SignalFeed rows={rows}
      outcomes={[{ signal_id: 2, outcome: "target", r_multiple: 2.0, won: true }]} />);
    expect(screen.getByText(/target \+2.00R/)).toBeTruthy();
  });

  it("calls an unresolved signal open rather than flat", () => {
    render(<SignalFeed rows={rows} outcomes={[]} />);
    expect(screen.getAllByText("open").length).toBe(2);
  });

  it("leaves a row's missing layers blank instead of filling them", () => {
    render(<SignalFeed rows={[rows[1]]} outcomes={[]} />);
    expect(screen.queryByText("Neutral")).toBeNull();
    expect(screen.queryByText("No entry")).toBeNull();
  });

  it("says nothing has been recorded rather than showing an empty table", () => {
    render(<SignalFeed rows={[]} outcomes={[]} />);
    expect(screen.getByText(/No signals recorded yet/)).toBeTruthy();
  });
});

/* ------------------------------------------------------------ safety */

describe("safety monitor", () => {
  const monitor = (over = {}) => render(
    <SafetyMonitor
      ageState="live" priceAge={3}
      market={{ open: true, session: "open" }}
      quality={{ options: { coverage: 96.2, verdict: "clean" }, findings: [] }}
      scheduler={{ healthy: true, problems: [] }}
      riskNow={{ state: "approved" }}
      coverage={{ rows: 5336, sessions: 71 }}
      feed={{ live_price_source: "angel", transport: "stream",
              angel: { enabled: true, healthy: true } }}
      {...over}
    />);

  it("reports every monitored subsystem", () => {
    monitor();
    for (const label of ["Data freshness", "Market session", "Option collector",
                         "Scheduler", "Risk manager", "Option coverage",
                         "Index archive", "Price feed"]) {
      expect(screen.getByText(label)).toBeTruthy();
    }
  });

  it("marks a stale feed as a stop, not a warning", () => {
    const { container } = monitor({ ageState: "stale", priceAge: 540 });
    expect(container.querySelector(".monitor-stop")).toBeTruthy();
    expect(screen.getByText("STALE")).toBeTruthy();
  });

  it("shows a degraded scheduler loudly", () => {
    monitor({ scheduler: { healthy: false,
                           problems: ["option-collector: 12 consecutive skips"] } });
    expect(screen.getByText("DEGRADED")).toBeTruthy();
    expect(screen.getByText(/12 consecutive skips/)).toBeTruthy();
  });

  it("says the scheduler is unknown when the endpoint gave nothing", () => {
    /* An unreachable endpoint must not read as healthy. Scoped to its own
       row: more than one subsystem can be unavailable at once, and an
       unscoped match would pass for the wrong reason. */
    const { container } = monitor({ scheduler: null });
    const row = [...container.querySelectorAll(".monitor")].find(
      (r) => r.textContent.includes("Scheduler"));
    expect(row.textContent).toContain("Unavailable");
  });

  it("names the source the price on screen actually came from", () => {
    /* A desk reading a four-second age wants to know whether it is looking
       at the push feed or at the poller that took over — the number alone
       does not say, and the two fail differently. */
    const { container } = monitor();
    const row = [...container.querySelectorAll(".monitor")].find(
      (r) => r.textContent.includes("Price feed"));

    expect(row.textContent).toContain("ANGEL");
    expect(row.textContent).toContain("streaming");
    expect(row.querySelector(".monitor-go")
      || row.className.includes("go")).toBeTruthy();
  });

  it("says so when the desk has fallen back to polling", () => {
    const { container } = monitor({
      feed: { live_price_source: "free", transport: "poll",
              angel: { enabled: true, healthy: false } } });
    const row = [...container.querySelectorAll(".monitor")].find(
      (r) => r.textContent.includes("Price feed"));

    expect(row.textContent).toContain("FREE");
    expect(row.textContent).toContain("Angel feed is quiet");
  });

  it("does not blame Angel when Angel was never switched on", () => {
    const { container } = monitor({
      feed: { live_price_source: "free", transport: "poll",
              angel: { enabled: false, healthy: false } } });
    const row = [...container.querySelectorAll(".monitor")].find(
      (r) => r.textContent.includes("Price feed"));

    expect(row.textContent).toContain("polling");
    expect(row.textContent).not.toContain("quiet");
  });

  it("reports the price feed as unknown when the endpoint gave nothing", () => {
    const { container } = monitor({ feed: null });
    const row = [...container.querySelectorAll(".monitor")].find(
      (r) => r.textContent.includes("Price feed"));
    expect(row.textContent).toContain("Unavailable");
  });

  it("grades thin option coverage as a stop", () => {
    const { container } = monitor({
      quality: { options: { coverage: 43.3, verdict: "unusable" }, findings: [] } });
    expect(screen.getByText("43.3%")).toBeTruthy();
    expect(container.querySelectorAll(".monitor-stop").length).toBeGreaterThan(0);
  });

  it("surfaces the collector when it has stalled", () => {
    monitor({ quality: { options: { coverage: 90 }, findings: [
      { check: "option_collector_stalled", severity: "error",
        summary: "The newest option snapshot is 41.0 minutes old." }] } });
    expect(screen.getByText("ERROR")).toBeTruthy();
    expect(screen.getByText(/41.0 minutes old/)).toBeTruthy();
  });
});

/* ---------------------------------------------------- news & overview */

describe("news panel", () => {
  const item = (over = {}) => ({
    id: "a1", headline: "Sensex surges to record high", source: "Economic Times",
    label: "markets", published_at: "2026-08-25T13:40:00Z",
    url: "https://example.test/story", sentiment: "positive",
    sentiment_score: 1.0, sentiment_reason: "Matched record high +2, surges +2.",
    ...over,
  });

  const feed = (over = {}) => ({
    available: true, items: [item()],
    sentiment: { label: "positive", score: 0.42,
                 counts: { positive: 7, negative: 5, neutral: 12 },
                 scored: 12, total: 24 },
    sources: [{ source: "Economic Times", items: 50, problem: null }],
    ...over,
  });

  it("renders a headline with its source, time and tone", () => {
    render(<NewsPanel news={feed()} />);
    expect(screen.getByText("Sensex surges to record high")).toBeTruthy();
    expect(screen.getByText("Economic Times")).toBeTruthy();
    expect(screen.getByText("markets")).toBeTruthy();
    expect(screen.getAllByText("Positive").length).toBeGreaterThan(0);
  });

  it("links the headline out to the publisher, safely", () => {
    const { container } = render(<NewsPanel news={feed()} />);
    const link = container.querySelector("a.news-headline");
    expect(link.getAttribute("href")).toBe("https://example.test/story");
    // The desk should not announce itself to every site it reads.
    expect(link.getAttribute("rel")).toContain("noreferrer");
    expect(link.getAttribute("target")).toBe("_blank");
  });

  it("carries the scoring evidence on the tone tag", () => {
    /* A sentiment label whose reasoning cannot be seen is a number to argue
       with and no way to argue. */
    const { container } = render(<NewsPanel news={feed()} />);
    expect(container.querySelector(".news-sent").getAttribute("title"))
      .toMatch(/Matched record high/);
  });

  it("summarises the page as counts, never as a bare decimal", () => {
    /* The score is a lexicon count over a headline; rendering "0.42" would
       lend it a precision it does not have. */
    render(<NewsPanel news={feed()} />);
    expect(screen.getByText("7+")).toBeTruthy();
    expect(screen.getByText("5−")).toBeTruthy();
    expect(screen.getByText("12=")).toBeTruthy();
    expect(screen.getByText(/12 of 24 scored/)).toBeTruthy();
    expect(screen.queryByText("0.42")).toBeNull();
  });

  it("says nothing here feeds a trading decision", () => {
    render(<NewsPanel news={feed()} />);
    expect(screen.getByText(/not an input to any signal/)).toBeTruthy();
  });

  it("colours a negative headline apart from a positive one", () => {
    const { container } = render(<NewsPanel news={feed({ items: [
      item({ id: "a", sentiment: "positive" }),
      item({ id: "b", sentiment: "negative", headline: "Nifty tumbles" }),
    ] })} />);
    expect(container.querySelector(".news-item.sent-positive")).toBeTruthy();
    expect(container.querySelector(".news-item.sent-negative")).toBeTruthy();
  });

  it("says the feeds are unavailable rather than inventing headlines", () => {
    render(<NewsPanel news={{ available: false, items: [], sources: [] }} />);
    expect(screen.getByText(/No headlines right now/)).toBeTruthy();
    expect(screen.getByText(/context only/)).toBeTruthy();
  });

  it("names the publisher that failed, and why", () => {
    /* A dark panel that will not say what went wrong is indistinguishable
       from a quiet news day. */
    render(<NewsPanel news={{ available: false, items: [], sources: [
      { source: "Livemint", items: 0, problem: "HTTP 503" },
      { source: "Economic Times", items: 12, problem: null },
    ] }} />);
    expect(screen.getByText("Livemint")).toBeTruthy();
    expect(screen.getByText(/HTTP 503/)).toBeTruthy();
  });

  it("handles never having been given a payload at all", () => {
    render(<NewsPanel news={null} />);
    expect(screen.getByText(/No headlines right now/)).toBeTruthy();
  });
});

describe("market overview", () => {
  const chain = { summary: { pcr_oi: 1.084, max_pain: 24_300, bias: "neutral",
    iv_skew: 3.28, atm_strike: 24_350,
    resistance_strikes: [24_350, 24_500], support_strikes: [24_300] } };

  it("shows the option and structure readings together", () => {
    const { container } = render(<MarketOverview signal={signalOf()} chain={chain}
      regime={null} vix={11.13} price={{ price: 24_334 }} />);
    // Scoped to the grid: 24,300 is legitimately both max pain and a support
    // strike, and the panel showing it twice under two headings is correct.
    const grid = container.querySelector(".overview-grid");
    expect(within(grid).getByText("1.08")).toBeTruthy();
    expect(within(grid).getByText("24,300")).toBeTruthy();
    expect(within(grid).getByText("11.13")).toBeTruthy();
    expect(within(grid).getByText("24,190.00")).toBeTruthy();
  });

  it("marks every chain reading unavailable when no chain has loaded", () => {
    render(<MarketOverview signal={{ context: {} }} chain={null} regime={null}
      vix={null} price={null} />);
    const cells = screen.getAllByText("Unavailable");
    expect(cells.length).toBeGreaterThanOrEqual(4);
    expect(screen.queryByText("0.00")).toBeNull();
  });

  it("strikes through liquidity that has already been swept", () => {
    const { container } = render(<MarketOverview
      signal={signalOf({ context: { ...signalOf().context,
        liquidity_pools: [{ level: 24_180, side: "buyside", swept: true }] } })}
      chain={chain} regime={null} vix={11} price={null} />);
    expect(container.querySelector(".swept")).toBeTruthy();
  });

  it("lists support and resistance from the chain", () => {
    const { container } = render(<MarketOverview signal={signalOf()} chain={chain}
      regime={null} vix={11} price={null} />);
    expect(container.querySelectorAll(".lvl-res").length).toBe(2);
    expect(within(container).getByText("24,350")).toBeTruthy();
  });
});
