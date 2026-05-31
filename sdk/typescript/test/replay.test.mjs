import assert from "node:assert/strict";
import test from "node:test";

import {
  appendReplayEvent,
  click,
  createReplayTimeline,
  eventAt,
  eventsForAction,
  pointPx,
  summarizeTimeline,
} from "../dist/index.js";

test("action builders produce typed ACI actions", () => {
  assert.deepEqual(click(pointPx(10, 20)), {
    verb: "click",
    target: { kind: "point_px", x: 10, y: 20 },
  });
});

test("replay timeline indexes events by seq and action id", () => {
  const timeline = createReplayTimeline({ skn_version: 0, session_id: "s", run_id: "r" });
  appendReplayEvent(timeline, {
    seq: 0,
    dt: 0,
    kind: "action",
    src: "click",
    action_id: "a1",
    payload: { verb: "click" },
  });
  appendReplayEvent(timeline, {
    seq: 1,
    dt: 0.1,
    kind: "observation",
    src: "image",
    action_id: "a1",
    payload: { image: { ref: "sha", w: 2, h: 2 } },
  });

  assert.equal(eventAt(timeline, 0)?.kind, "action");
  assert.equal(eventsForAction(timeline, "a1").length, 2);
  assert.deepEqual(summarizeTimeline(timeline), {
    events: 2,
    firstSeq: 0,
    lastSeq: 1,
    channels: ["action", "observation"],
  });
});

test("replay timeline rejects duplicate sequence numbers", () => {
  const timeline = createReplayTimeline();
  appendReplayEvent(timeline, { seq: 0, dt: 0, kind: "meta", src: "test", payload: {} });
  assert.throws(
    () => appendReplayEvent(timeline, { seq: 0, dt: 1, kind: "meta", src: "again", payload: {} }),
    /duplicate/,
  );
});
