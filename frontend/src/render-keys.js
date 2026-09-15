/* When a panel's picture has actually changed.

   Kept out of the components themselves so the comparison can be imported
   without pulling in what it guards — OIProfile's is used to decide whether
   to render a Recharts chart, and importing OIProfile to get at it would
   load Recharts to avoid loading Recharts. */

/* The 25-point band a spot sits in, as (b-25, b].

   OIProfile reads the spot three ways: which 14 strikes are nearest, which
   strikes sit at or above it (`strike >= spot`), and which strike is closest.
   On a 50-point grid every one of those can only change where the spot
   crosses a strike or the midpoint between two — lines 25 points apart. The
   band is half-open on the lower side so a spot landing exactly on a strike
   stays with the spots just below it, which is the side `>=` puts it on.

   One exception, stated rather than hidden: a spot exactly on a midpoint is
   a tie for "closest strike", which the component breaks towards the higher
   strike, while its band-mates just below pick the lower one. A print at
   precisely 24,225.00 can therefore leave the ATM marker one strike low
   until the next move. */
export function spotBand(spot) {
  const n = Number(spot);
  return Number.isFinite(n) && n > 0 ? Math.ceil(n / 25) : null;
}

const pct2 = (summary) => (summary?.max_pain_distance_pct == null
  ? null : Number(summary.max_pain_distance_pct).toFixed(2));

/* Props equality for OIProfile: true means "the chart would look the same".

   A pushed chain arrives up to four times a second with a new `strikes`
   array every time, because premiums moved — but this chart draws open
   interest, which moves far more slowly. So the rows are compared on the
   three fields it draws, not by reference. A reordered array compares as
   different and costs a redraw, never a wrong picture. */
export function sameOIPicture(prev, next) {
  if (prev.span !== next.span) return false;
  if (spotBand(prev.spot) !== spotBand(next.spot)) return false;
  if (prev.summary?.max_pain !== next.summary?.max_pain) return false;
  if (pct2(prev.summary) !== pct2(next.summary)) return false;

  const a = prev.strikes;
  const b = next.strikes;
  if (a === b) return true;
  if (!a || !b || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i += 1) {
    if (a[i].strike !== b[i].strike
        || (a[i].call_oi || 0) !== (b[i].call_oi || 0)
        || (a[i].put_oi || 0) !== (b[i].put_oi || 0)) return false;
  }
  return true;
}
