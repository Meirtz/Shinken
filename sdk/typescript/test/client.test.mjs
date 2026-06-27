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

test("a timed-out frame waiter is removed and cannot swallow the next frame", async () => {
  const { transport } = fakeTransport();
  const c = new AciClient(transport, CAPS, "linux");
  assert.equal(await c.nextFrame(20), null);

  c.feed(JSON.stringify({ type: "observation", obs_id: "late-0", stream: "s", seq: 0 }));
  const frame = await c.nextFrame(100);
  assert.equal(frame?.obs_id, "late-0");
});

test("RPC and ping deadlines reject and remove stale waiters", async () => {
  const { transport, sent } = fakeTransport();
  const c = new AciClient(transport, CAPS, "linux", { rpcTimeoutMs: 20 });

  await assert.rejects(c.click({ kind: "point_px", x: 1, y: 2 }), /RPC timed out/);
  const expiredId = sent.at(-1).call_id;
  c.feed(JSON.stringify({ type: "ack", call_id: expiredId, ok: true })); // ignored late reply

  const live = c.typeText("still usable");
  const liveId = sent.at(-1).call_id;
  c.feed(JSON.stringify({ type: "ack", call_id: liveId, ok: true }));
  await live;
  await assert.rejects(c.ping(), /ping timed out/);
});

test("call ids are unique across clients in one process", async () => {
  const a = fakeTransport();
  const b = fakeTransport();
  const ca = new AciClient(a.transport, CAPS, "linux");
  const cb = new AciClient(b.transport, CAPS, "linux");
  const pa = ca.typeText("a");
  const pb = cb.typeText("b");
  const aid = a.sent.at(-1).call_id;
  const bid = b.sent.at(-1).call_id;
  assert.notEqual(aid, bid);
  ca.feed(JSON.stringify({ type: "ack", call_id: aid, ok: true }));
  cb.feed(JSON.stringify({ type: "ack", call_id: bid, ok: true }));
  await Promise.all([pa, pb]);
});

test("exec() resolves to the typed ExecResult and validates argv/shell exclusivity", async () => {
  const { transport, sent } = fakeTransport();
  const c = new AciClient(transport, CAPS, "linux");

  const p = c.exec({ argv: ["echo", "hi"], cwd: "/tmp", timeout_ms: 5000 });
  const id = sent.at(-1).call_id;
  assert.equal(sent.at(-1).action.verb, "exec");
  assert.deepEqual(sent.at(-1).action.argv, ["echo", "hi"]);
  assert.equal(sent.at(-1).action.cwd, "/tmp");
  const value = {
    exit_code: 3, // a nonzero exit code is RETURNED, not thrown
    signal: null,
    timed_out: false,
    stdout: "hi\n",
    stderr: "",
    stdout_truncated: false,
    stderr_truncated: false,
    duration_ms: 4.2,
  };
  c.feed(JSON.stringify({ type: "result", call_id: id, ok: true, value }));
  assert.deepEqual(await p, value);

  // a nack (e.g. saturation / validation) rejects with the server's reason
  const bad = c.exec({ shell: "ls | wc -l" });
  const badId = sent.at(-1).call_id;
  assert.equal(sent.at(-1).action.shell, "ls | wc -l");
  c.feed(JSON.stringify({ type: "ack", call_id: badId, ok: false, error: "max concurrent execs reached (4)" }));
  await assert.rejects(bad, /max concurrent execs/);

  // exactly one of argv/shell — locally typed, nothing sent
  const before = sent.length;
  await assert.rejects(c.exec({ argv: ["ls"], shell: "ls" }), /exactly one/);
  await assert.rejects(c.exec({}), /exactly one/);
  assert.equal(sent.length, before);
});

test("feed() ignores streamed exec events it does not consume (no crash, no settle)", () => {
  const { transport } = fakeTransport();
  const c = new AciClient(transport, CAPS, "linux");
  c.feed(JSON.stringify({ type: "exec_output", cause: "x", seq: 0, channel: "stdout", data_b64: "aGk=" }));
  c.feed(JSON.stringify({ type: "exec_exit", cause: "x", exit_code: 0, timed_out: false, duration_ms: 1, truncated: false }));
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

test("connect() has a handshake deadline and closes the stalled socket", async () => {
  const listeners = {};
  let closed = 0;
  const sock = {
    send: () => {},
    close: () => {
      closed += 1;
    },
    addEventListener: (type, cb) => {
      (listeners[type] ??= []).push(cb);
    },
  };
  await assert.rejects(
    connect("127.0.0.1:8765", { handshakeTimeoutMs: 20, openSocket: () => sock }),
    /handshake timed out/,
  );
  assert.equal(closed, 1);
});

test("post-handshake socket close rejects RPCs and ends frame waits", async () => {
  const listeners = {};
  const sent = [];
  const sock = {
    send: (data) => sent.push(data),
    close: () => {},
    addEventListener: (type, cb) => {
      (listeners[type] ??= []).push(cb);
    },
  };
  const fire = (type, ev) => (listeners[type] ?? []).forEach((cb) => cb(ev));
  const connected = connect("127.0.0.1:8765", { openSocket: () => sock });
  fire("open");
  fire("message", {
    data: JSON.stringify({
      type: "welcome",
      v: 0,
      server: { name: "mock", version: "0", platform: "linux" },
      capabilities: CAPS,
    }),
  });
  const client = await connected;
  const inflight = client.typeText("pending");
  const frame = client.nextFrame();
  fire("close");
  await assert.rejects(inflight, /connection closed/);
  assert.equal(await frame, null);
  await assert.rejects(client.ping(), /closed/);
});
