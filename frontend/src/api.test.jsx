/* Where the live socket connects.

   The public build is served over HTTPS by a proxy that asks the owner to
   sign in, and it must reach the API on the page's own origin without the
   API key ever being in the browser. The tunnel build keeps talking to
   localhost:8000 exactly as before. */
import { describe, expect, it } from "vitest";

import { wsUrl } from "./api.js";

describe("the live socket's address", () => {
  it("is unchanged for the localhost desk", () => {
    expect(wsUrl("http://localhost:8000", "")).toBe("ws://localhost:8000/ws/signals");
    expect(wsUrl("http://localhost:8000", "a key/+")).toBe(
      "ws://localhost:8000/ws/signals?key=a%20key%2F%2B");
  });

  it("is wss on the page's own origin when the API is a path", () => {
    expect(wsUrl("/api", "", "https://desk.example.com")).toBe(
      "wss://desk.example.com/api/ws/signals");
  });

  it("carries no key when none was compiled in", () => {
    expect(wsUrl("/api", "", "https://desk.example.com")).not.toContain("key");
  });
});
