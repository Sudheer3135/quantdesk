/* Open interest the source did not send is missing, not zero (OC-5).

   `Number(null) || 0` and `s.call_oi || 0` both turn a missing reading into
   a genuine-looking zero — a plotted bar, a caption, a wall candidate. These
   helpers keep the two apart: `oiValue` is a number (zero included) or null,
   and `heaviest` never picks a row whose reading is null. */
export const oiValue = (v) => {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};

/* The row with the largest recorded value, or null when no row has one. */
export const heaviest = (rows, pick) => rows.reduce((best, row) => {
  const value = oiValue(pick(row));
  if (value === null) return best;
  return best === null || value > oiValue(pick(best)) ? row : best;
}, null);
