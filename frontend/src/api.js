/* Where the backend is, and the one way to talk to it.

   Pulled out of App so the chart can page history without a second copy of
   the base URL and the key handling — two copies drift, and the one that
   drifts is always the one that stops sending the key. */

/* An absolute URL for the localhost desk reached over an SSH tunnel, or a
   path on the page's own origin (`/api`) for the public build, where the
   desk sits behind an authenticating HTTPS proxy (scripts/native/caddy). */
export const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

/* Set VITE_API_KEY when the backend has API_KEY set. Reads stay open, so
   this is only needed for the live socket; leaving it unset is correct for
   a localhost desk running without a key.

   Never set for the public build: anything named VITE_* is compiled into
   the JavaScript every visitor downloads, and this one would also sit in
   the socket's URL. There the proxy adds the key to the request it forwards,
   after the owner has signed in, and the browser never holds it
   (scripts/native/build_web.sh refuses a build that carries one). */
export const API_KEY = import.meta.env.VITE_API_KEY || "";

/* The live socket's address. A websocket needs an absolute ws:// or wss://
   URL, so a same-origin API path is resolved against the page it was
   served from: an https page gets wss, and the handshake goes to the same
   host, through the same sign-in, as every other request. */
export function wsUrl(api = API, key = API_KEY, origin = window.location.origin) {
  const base = api.startsWith("/") ? origin + api : api;
  return base.replace(/^http/, "ws") + "/ws/signals"
    + (key ? `?key=${encodeURIComponent(key)}` : "");
}

export const WS_URL = wsUrl();

export async function getJSON(path) {
  const res = await fetch(`${API}${path}`);
  if (!res.ok) throw new Error(`${path} returned ${res.status}`);
  return res.json();
}
