import assert from "node:assert/strict";
import { test } from "node:test";

import { createInitialState, reduce, renderPanel } from "../dist/index.js";

const ev = (seq, kind, src) => ({ seq, dt: seq * 0.5, kind, src: src ?? `${kind}:x`, payload: { seq } });

function stateWithData() {
  let s = createInitialState();
  s = reduce(s, {
    type: "setSessions",
    sessions: [
      { id: "sess-1", provider: "docker-local", status: "ready", artifacts: ["out.png"], evalStatus: "pass" },
      { id: "sess-2", provider: "external", status: "busy", artifacts: [], evalStatus: "none" },
    ],
  });
  s = reduce(s, {
    type: "appendEvents",
    events: [ev(0, "action", "click"), ev(1, "observation", "screenshot"), ev(2, "action", "type_text")],
  });
  s = reduce(s, { type: "setCursor", cursor: 1 });
  return s;
}

test("renderPanel shows the session dashboard with active-session detail", () => {
  const html = renderPanel(stateWithData());
  assert.match(html, /class="shinken-panel"/);
  assert.match(html, /sess-1/);
  assert.match(html, /docker-local/);
  assert.match(html, /eval:pass/);
  assert.match(html, /out\.png/);
  // selected session is marked
  assert.match(html, /aria-selected="true"[^>]*data-session="sess-1"|data-session="sess-1"/);
});

test("renderPanel renders the replay timeline and marks the cursor row", () => {
  const html = renderPanel(stateWithData());
  assert.match(html, /Replay timeline \(3\)/);
  assert.match(html, /screenshot/);
  // the cursor is at seq 1 (observation) -> a row marked aria-current
  assert.match(html, /class="cursor" aria-current="true"/);
});

test("renderPanel escapes event/session content (no HTML injection)", () => {
  let s = createInitialState();
  s = reduce(s, { type: "appendEvents", events: [ev(0, "marker", '<img src=x onerror="alert(1)">')] });
  const html = renderPanel(s);
  assert.ok(!html.includes("<img src=x"));
  assert.match(html, /&lt;img src=x/);
});

test("live media is a flag-gated placeholder by default, mountable when enabled", () => {
  const off = renderPanel(createInitialState());
  assert.match(off, /class="media disabled"/);
  assert.match(off, /behind a later feature flag/);
  const on = renderPanel(createInitialState(), { media: true });
  assert.match(on, /data-media="on"/);
});

test("renderPanel handles an empty state without throwing", () => {
  const html = renderPanel(createInitialState());
  assert.match(html, /no active session/);
  assert.match(html, /no events/);
});
