/* The net under the chart.

   The bug that motivated this was total: one throw inside lightweight-
   charts unmounted the whole React tree and the dashboard went black —
   no price, no chain, no signals, no risk — while the feed underneath
   was perfectly healthy. A charting defect became a total loss of the
   desk. These tests hold the blast radius. */
import { render, screen, fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import PanelBoundary from "./PanelBoundary.jsx";

function Boom({ fail }) {
  if (fail) throw new Error("Cannot update oldest data, last time=1, new time=0");
  return <p>chart drew fine</p>;
}

afterEach(() => vi.restoreAllMocks());

describe("PanelBoundary", () => {
  it("renders its child when nothing is wrong", () => {
    render(<PanelBoundary label="NIFTY 50 · 5m"><Boom fail={false} /></PanelBoundary>);
    expect(screen.getByText("chart drew fine")).toBeTruthy();
  });

  it("contains a throw instead of taking the page down", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});

    render(
      <div>
        <PanelBoundary label="NIFTY 50 · 5m"><Boom fail /></PanelBoundary>
        <p>the rest of the desk</p>
      </div>);

    // the panel says it failed...
    expect(screen.getByText(/stopped drawing/i)).toBeTruthy();
    // ...and every other panel is still on screen, which is the whole point
    expect(screen.getByText("the rest of the desk")).toBeTruthy();
  });

  it("shows what broke rather than failing silently", () => {
    /* A panel that quietly renders nothing is how a broken feed gets
       mistaken for a quiet market — the failure this platform exists to
       avoid. The reason has to be on screen. */
    vi.spyOn(console, "error").mockImplementation(() => {});

    render(<PanelBoundary label="NIFTY 50 · 5m"><Boom fail /></PanelBoundary>);

    expect(screen.getByText(/Cannot update oldest data/)).toBeTruthy();
    expect(screen.getByText("NIFTY 50 · 5m")).toBeTruthy();
  });

  it("keeps the failure on the console so it stays reportable", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(<PanelBoundary label="NIFTY 50 · 5m"><Boom fail /></PanelBoundary>);
    expect(spy.mock.calls.some(
      (c) => String(c[0]).includes("NIFTY 50 · 5m failed"))).toBe(true);
  });

  it("can be retried once the cause has passed", () => {
    /* The crash cleared itself when the bucket rolled over, so a retry is
       not theoretical — it is how the desk came back without a reload. */
    vi.spyOn(console, "error").mockImplementation(() => {});

    function Flaky({ state }) {
      if (state.fail) throw new Error("boom");
      return <p>recovered</p>;
    }
    const state = { fail: true };

    render(<PanelBoundary label="chart"><Flaky state={state} /></PanelBoundary>);
    expect(screen.getByText(/stopped drawing/i)).toBeTruthy();

    state.fail = false;
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(screen.getByText("recovered")).toBeTruthy();
  });
});
