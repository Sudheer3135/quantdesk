/* Where the backend is, and the one way to talk to it.

   Pulled out of App so the chart can page history without a second copy of
   the base URL and the key handling — two copies drift, and the one that
   drifts is always the one that stops sending the key. */

export const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

/* Set VITE_API_KEY when the backend has API_KEY set. Reads stay open, so
   this is only needed for the live socket; leaving it unset is correct for
   a localhost desk running without a key. */
export const API_KEY = import.meta.env.VITE_API_KEY || "";

export const WS_URL = API.replace(/^http/, "ws") + "/ws/signals"
  + (API_KEY ? `?key=${encodeURIComponent(API_KEY)}` : "");

export async function getJSON(path) {
  const res = await fetch(`${API}${path}`);
  if (!res.ok) throw new Error(`${path} returned ${res.status}`);
  return res.json();
}
