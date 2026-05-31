import assert from "node:assert/strict";
import { test } from "node:test";

import { axNodesToElements, boundsFromSnapshot } from "../dist/index.js";

const AX_NODES = [
  { nodeId: "1", role: { value: "RootWebArea" }, name: { value: "Demo" }, childIds: ["2", "3"] },
  {
    nodeId: "2",
    role: { value: "button" },
    name: { value: "Save" },
    backendDOMNodeId: 42,
    properties: [
      { name: "focusable", value: { value: true } },
      { name: "disabled", value: { value: false } },
    ],
  },
  {
    nodeId: "3",
    role: { value: "textbox" },
    name: { value: "Title" },
    value: { value: "hello" },
    backendDOMNodeId: 43,
    properties: [
      { name: "editable", value: { value: "true" } },
      { name: "disabled", value: { value: true } },
    ],
  },
  { nodeId: "4", role: { value: "presentation" }, ignored: true },
];

test("axNodesToElements normalizes role/name/value/states and drops ignored", () => {
  const bounds = new Map([
    [42, [10, 20, 30, 40]],
    [43, [0, 50, 200, 24]],
  ]);
  const els = axNodesToElements(AX_NODES, { bounds });

  assert.equal(els.length, 3); // node 4 is ignored
  assert.ok(els.every((e) => e.source === "cdp"));

  const btn = els.find((e) => e.ref === "2");
  assert.equal(btn.role, "button");
  assert.equal(btn.name, "Save");
  assert.deepEqual(btn.bbox, [10, 20, 30, 40]);
  assert.ok(btn.states.includes("focusable"));
  assert.ok(btn.states.includes("enabled")); // disabled=false -> enabled

  const box = els.find((e) => e.ref === "3");
  assert.equal(box.value, "hello");
  assert.ok(box.states.includes("editable"));
  assert.ok(!box.states.includes("enabled")); // disabled=true -> no enabled token
});

test("axNodesToElements can keep ignored nodes and defaults missing bbox to zero", () => {
  const els = axNodesToElements(AX_NODES, { includeIgnored: true });
  assert.equal(els.length, 4);
  const root = els.find((e) => e.ref === "1");
  assert.deepEqual(root.bbox, [0, 0, 0, 0]); // no backendDOMNodeId / no bounds
});

test("boundsFromSnapshot maps backendNodeId -> rounded [x,y,w,h]", () => {
  const snapshot = {
    documents: [
      {
        nodes: { backendNodeId: [100, 101, 102] },
        layout: {
          nodeIndex: [0, 2],
          bounds: [
            [1.4, 2.6, 3.5, 4.5],
            [9, 9, 9, 9],
          ],
        },
      },
    ],
  };
  const map = boundsFromSnapshot(snapshot);
  assert.deepEqual(map.get(100), [1, 3, 4, 5]); // rounded
  assert.deepEqual(map.get(102), [9, 9, 9, 9]); // nodeIndex 2 -> backendNodeId[2]
  assert.equal(map.has(101), false); // backendNodeId[1] is never referenced by nodeIndex
  assert.equal(map.size, 2);
});
