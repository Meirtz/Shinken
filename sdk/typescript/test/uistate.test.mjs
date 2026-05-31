import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createInitialState,
  currentEvent,
  eventKindCounts,
  reduce,
  selectedSession,
  visibleEvents,
} from "../dist/index.js";

const ev = (seq, kind = "action") => ({ seq, dt: seq * 0.1, kind, src: `${kind}:x`, payload: {} });

test("sessions: select first by default, keep selection if still present", () => {
  let s = createInitialState();
  s = reduce(s, { type: "setSessions", sessions: [session("a"), session("b")] });
  assert.equal(s.selectedSessionId, "a");
  s = reduce(s, { type: "selectSession", id: "b" });
  assert.equal(selectedSession(s).id, "b");
  s = reduce(s, { type: "setSessions", sessions: [session("b"), session("c")] });
  assert.equal(s.selectedSessionId, "b"); // b still present -> kept
  s = reduce(s, { type: "setSessions", sessions: [session("c")] });
  assert.equal(s.selectedSessionId, "c"); // b gone -> fall back to first
});

test("appendEvents is incremental and bounded (oldest dropped past maxEvents)", () => {
  let s = createInitialState({ maxEvents: 1000 });
  // push 5000 events in 50 incremental batches of 100
  for (let b = 0; b < 50; b += 1) {
    const batch = Array.from({ length: 100 }, (_, i) => ev(b * 100 + i));
    s = reduce(s, { type: "appendEvents", events: batch });
  }
  assert.equal(s.events.length, 1000); // bounded
  assert.equal(s.events[0].seq, 4000); // oldest kept is the 4000th (5000 - 1000)
  assert.equal(s.events.at(-1).seq, 4999);
});

test("cursor stays anchored to the same event when the buffer drops oldest", () => {
  let s = createInitialState({ maxEvents: 3 });
  s = reduce(s, { type: "appendEvents", events: [ev(0), ev(1), ev(2)] });
  s = reduce(s, { type: "setCursor", cursor: 2 }); // points at seq 2
  assert.equal(currentEvent(s).seq, 2);
  s = reduce(s, { type: "appendEvents", events: [ev(3)] }); // drops seq 0
  assert.equal(currentEvent(s).seq, 2); // still seq 2, not shifted off
  assert.equal(s.events.length, 3);
});

test("play follows the live tail; step/setCursor pause and clamp", () => {
  let s = createInitialState();
  s = reduce(s, { type: "appendEvents", events: [ev(0), ev(1), ev(2)] });
  s = reduce(s, { type: "play" });
  assert.equal(s.playing, true);
  assert.equal(currentEvent(s).seq, 2);
  s = reduce(s, { type: "appendEvents", events: [ev(3)] });
  assert.equal(currentEvent(s).seq, 3); // tail-followed while playing
  s = reduce(s, { type: "step", delta: -1 });
  assert.equal(s.playing, false); // stepping pauses
  assert.equal(currentEvent(s).seq, 2);
  s = reduce(s, { type: "step", delta: -99 });
  assert.equal(currentEvent(s).seq, 0); // clamped, not negative
});

test("filter selector keeps state/protocol logic out of rendering", () => {
  let s = createInitialState();
  s = reduce(s, { type: "appendEvents", events: [ev(0, "action"), ev(1, "observation"), ev(2, "action")] });
  s = reduce(s, { type: "setFilter", filter: { kinds: ["observation"] } });
  assert.deepEqual(
    visibleEvents(s).map((e) => e.seq),
    [1],
  );
  assert.deepEqual(eventKindCounts(s), { action: 2, observation: 1 }); // counts ignore filter
});

test("eval status + diagnostics ring buffer", () => {
  let s = createInitialState({ maxDiagnostics: 2 });
  s = reduce(s, { type: "setSessions", sessions: [session("a")] });
  s = reduce(s, { type: "setEvalStatus", sessionId: "a", status: "pass" });
  assert.equal(selectedSession(s).evalStatus, "pass");
  for (const m of ["one", "two", "three"]) {
    s = reduce(s, { type: "pushDiagnostic", diagnostic: { level: "info", message: m } });
  }
  assert.equal(s.diagnostics.length, 2); // bounded
  assert.deepEqual(
    s.diagnostics.map((d) => d.message),
    ["two", "three"],
  );
});

function session(id) {
  return { id, provider: "docker-local", status: "ready", artifacts: [], evalStatus: "none" };
}
