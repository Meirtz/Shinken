import assert from "node:assert/strict";
import { test } from "node:test";

import { createInitialState, handleKey, paletteCommands, reduce, renderConsole } from "../dist/index.js";

const ev = (seq, kind = "action") => ({ seq, dt: seq * 0.1, kind, src: `${kind}:x`, payload: {} });

test("renderConsole shows dashboard, events, and a keyboard hint footer", () => {
  let s = createInitialState();
  s = reduce(s, {
    type: "setSessions",
    sessions: [{ id: "s1", provider: "docker-local", status: "ready", artifacts: [], evalStatus: "pass" }],
  });
  s = reduce(s, { type: "appendEvents", events: [ev(0), ev(1, "observation")] });
  const lines = renderConsole(s, { width: 80, height: 16 });
  const text = lines.join("\n");
  assert.match(text, /Shinken Console — 1 session/);
  assert.match(text, /backend=docker-local/);
  assert.match(text, /observation/);
  assert.match(text, /\[j\/k\] step/);
});

test("renderConsole is bounded per frame for large streams (windowed, not full-log)", () => {
  let s = createInitialState({ maxEvents: 100000 });
  // 10k events; render must produce only ~height lines, not 10k
  s = reduce(s, { type: "appendEvents", events: Array.from({ length: 10000 }, (_, i) => ev(i)) });
  s = reduce(s, { type: "setCursor", cursor: 5000 });
  const lines = renderConsole(s, { width: 80, height: 24 });
  assert.ok(lines.length <= 24, `expected <=24 lines, got ${lines.length}`);
  // the window is centered on the cursor (seq 5000 marked)
  assert.match(lines.join("\n"), /›\s+5000/);
});

test("renderConsole truncates long lines to the terminal width", () => {
  let s = createInitialState();
  s = reduce(s, { type: "appendEvents", events: [{ seq: 0, dt: 0, kind: "action", src: "x".repeat(200), payload: {} }] });
  const lines = renderConsole(s, { width: 40, height: 10 });
  assert.ok(lines.every((l) => l.length <= 40), "all lines fit the width");
});

test("handleKey maps keys to control-surface actions (keyboard-first)", () => {
  const s = reduce(createInitialState(), { type: "appendEvents", events: [ev(0), ev(1), ev(2)] });
  assert.deepEqual(handleKey("j", s), { type: "step", delta: 1 });
  assert.deepEqual(handleKey("k", s), { type: "step", delta: -1 });
  assert.deepEqual(handleKey("g", s), { type: "setCursor", cursor: 0 });
  assert.equal(handleKey("space", s).type, "play");
  assert.equal(handleKey("space", { ...s, playing: true }).type, "pause");
  assert.equal(handleKey("x", s), null); // unmapped
});

test("palette exposes discoverable shortcuts", () => {
  const cmds = paletteCommands();
  assert.ok(cmds.length >= 5);
  assert.ok(cmds.some((c) => /play/.test(c.label)));
});

test("renderConsole handles an empty state without throwing", () => {
  const lines = renderConsole(createInitialState());
  assert.match(lines.join("\n"), /no active session/);
  assert.match(lines.join("\n"), /no events/);
});
