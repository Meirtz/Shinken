import assert from "node:assert/strict";
import { test } from "node:test";

import { AciClient, connect } from "../dist/index.js";

const CAPS = {
  schema_version: 0,
  verbs: ["click", "type_text", "screenshot", "start_screencast", "stop_screencast", "wait"],
  targets: ["point_px"],
  observation_types: ["screenshot", "screencast"],
};

function fakeTransport() {
  const sent = [];
  return { transport: { send: (d) => sent.push(JSON.parse(d)), close: () => {} }, sent };
}

test("act() resolves on an ok ack and rejects on a not-ok ack", async () => {
  const { transport, sent } = fakeTransport();
  const c = new AciClient(transport, CAPS, "linux");

  const ok = c.click({ kind: "point_px", x: 1, y: 2 });
  const okId = sent.at(-1).call_id;
  assert.equal(sent.at(-1).action.verb, "click");
  c.feed(JSON.stringify({ type: "ack", call_id: okId, ok: true }));
  await ok;

  const bad = c.typeText("hi");
  const badId = sent.at(-1).call_id;
  c.feed(JSON.stringify({ type: "ack", call_id: badId, ok: false, error: "denied" }));
  await assert.rejects(bad, /denied/);
});

test("screenshot() resolves to the paired observation by cause", async () => {
  const { transport, sent } = fakeTransport();
  const c = new AciClient(transport, CAPS, "linux");
  const p = c.screenshot("screen");
  const id = sent.at(-1).call_id;
  c.feed(JSON.stringify({ type: "observation", obs_id: "o1", cause: id, image: { ref: "abc", w: 4, h: 4, scope: "screen" } }));
  const obs = await p;
  assert.equal(obs.image.ref, "abc");
});

test("ping() resolves to a non-negative rtt on pong", async () => {
  const { transport } = fakeTransport();
  const c = new AciClient(transport, CAPS, "linux");
  const p = c.ping();
  c.feed(JSON.stringify({ type: "pong", t: 1 }));
  const rtt = await p;
  assert.ok(rtt >= 0);
});

test("screencast: start, demux a frame, record scope, time out cleanly", async () => {
  const { transport, sent } = fakeTransport();
  const c = new AciClient(transport, CAPS, "linux");

  const startP = c.startScreencast({ fps: 10, scope: "active_window" });
  const sid = sent.at(-1).call_id;
  assert.equal(sent.at(-1).action.verb, "start_screencast");
  assert.equal(sent.at(-1).action.scope, "active_window");
  c.feed(JSON.stringify({ type: "ack", call_id: sid, ok: true }));
  assert.equal(await startP, sid);
  assert.equal(c.screencastScope, "active_window");

  c.feed(JSON.stringify({ type: "observation", obs_id: `${sid}-0`, stream: sid, seq: 0, image: { ref: "f", w: 1, h: 1 } }));
  const frame = await c.nextFrame(1000);
  assert.equal(frame.stream, sid);
  assert.equal(frame.seq, 0);

  assert.equal(await c.nextFrame(20), null); // no frame within the timeout
});

test("close() rejects in-flight calls and ends frame waits", async () => {
  const { transport } = fakeTransport();
  const c = new AciClient(transport, CAPS, "linux");
  const inflight = c.click({ kind: "point_px", x: 0, y: 0 });
  c.close();
  await assert.rejects(inflight);
  assert.equal(await c.nextFrame(), null);
});

test("connect() completes the hello -> welcome handshake", async () => {
  const listeners = {};
  const sent = [];
  const sock = {
    send: (d) => sent.push(d),
    close: () => {},
    addEventListener: (type, cb) => {
      (listeners[type] ??= []).push(cb);
    },
  };
  const fire = (type, ev) => (listeners[type] ?? []).forEach((cb) => cb(ev));

  const p = connect("127.0.0.1:8765", { token: "t", openSocket: () => sock });
  fire("open");
  const hello = JSON.parse(sent[0]);
  assert.equal(hello.type, "hello");
  assert.equal(hello.token, "t");

  fire("message", {
    data: JSON.stringify({
      type: "welcome",
      v: 0,
      server: { name: "mock", version: "0", platform: "linux" },
      capabilities: CAPS,
    }),
  });
  const client = await p;
  assert.equal(client.platform, "linux");
  assert.ok(client.capabilities.verbs.includes("click"));
});
