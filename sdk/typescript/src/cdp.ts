// TypeScript browser/CDP semantic adapter (#101). Normalizes a Chrome DevTools
// Protocol `Accessibility.getFullAXTree` response (optionally enriched with box bounds
// from `DOMSnapshot.captureSnapshot`) into the same flat ACI `Element[]` the other
// observation backends produce — mirroring the Python `shinken.cdp` reference path, so
// browser structure shares one normalized vocabulary.

import type { Element } from "./types.js";

/** A CDP `AXValue` — `{ value }` carrying the typed value of a role/name/property. */
export interface CdpAxValue {
  value?: unknown;
}

export interface CdpAxProperty {
  name: string;
  value?: CdpAxValue;
}

/** One node of a CDP `Accessibility.getFullAXTree` response. */
export interface CdpAxNode {
  nodeId: string;
  ignored?: boolean;
  role?: CdpAxValue;
  name?: CdpAxValue;
  value?: CdpAxValue;
  properties?: CdpAxProperty[];
  childIds?: string[];
  backendDOMNodeId?: number;
}

export type Bbox = [number, number, number, number];

// CDP AX property name -> normalized state token (matches the AT-SPI vocabulary where it
// overlaps, so downstream consumers see one state set regardless of backend).
const STATE_PROPS: Record<string, string> = {
  focusable: "focusable",
  focused: "focused",
  selected: "selected",
  checked: "checked",
  editable: "editable",
  expanded: "expanded",
  required: "required",
};

/** A CDP property value is truthy when it is `true` or a token like `"true"`/`"mixed"`
 *  (tristate checkboxes). */
function propTruthy(value: CdpAxValue | undefined): boolean {
  const v = value?.value;
  return v === true || (typeof v === "string" && (v.toLowerCase() === "true" || v.toLowerCase() === "mixed"));
}

function axString(field: CdpAxValue | undefined): string {
  return typeof field?.value === "string" ? field.value : "";
}

function statesOf(node: CdpAxNode): string[] {
  const states: string[] = [];
  let disabled = false;
  for (const prop of node.properties ?? []) {
    if (prop.name === "disabled") {
      disabled = propTruthy(prop.value);
    } else if (prop.name in STATE_PROPS && propTruthy(prop.value)) {
      const token = STATE_PROPS[prop.name];
      if (token !== undefined) states.push(token);
    }
  }
  if (!disabled) states.push("enabled");
  return states;
}

/** Extract `{ backendNodeId -> [x, y, w, h] }` from a `DOMSnapshot.captureSnapshot`
 *  result, so AX nodes (which carry a `backendDOMNodeId`) can be given a bbox. Bounds
 *  are document/CSS pixels (best-effort; a device-pixel-ratio scale may be needed to
 *  line up exactly with screenshots — left to the operator). */
export function boundsFromSnapshot(snapshot: unknown): Map<number, Bbox> {
  const out = new Map<number, Bbox>();
  const docs = (snapshot as { documents?: unknown[] } | null)?.documents;
  if (!Array.isArray(docs)) return out;
  for (const doc of docs) {
    const d = doc as { nodes?: { backendNodeId?: number[] }; layout?: { nodeIndex?: number[]; bounds?: number[][] } };
    const backend = d.nodes?.backendNodeId ?? [];
    const nodeIndex = d.layout?.nodeIndex ?? [];
    const boxes = d.layout?.bounds ?? [];
    for (let i = 0; i < nodeIndex.length; i += 1) {
      if (i >= boxes.length) break;
      const ni = nodeIndex[i];
      const box = boxes[i];
      if (ni === undefined || box === undefined || ni < 0 || ni >= backend.length || box.length < 4) continue;
      const id = backend[ni];
      const [x, y, w, h] = box;
      if (id === undefined || x === undefined || y === undefined || w === undefined || h === undefined) continue;
      out.set(id, [Math.round(x), Math.round(y), Math.round(w), Math.round(h)]);
    }
  }
  return out;
}

export interface AxToElementsOptions {
  bounds?: Map<number, Bbox>;
  /** Keep AX nodes CDP marks `ignored` (off-screen / not in the a11y tree). Default off. */
  includeIgnored?: boolean;
}

/** Normalize a CDP `Accessibility.getFullAXTree` node list into flat ACI `Element[]`
 *  (role/name/value/states/bbox + `source: "cdp"`). `ignored` nodes are dropped unless
 *  `includeIgnored` is set; a node gets a bbox when its `backendDOMNodeId` is in
 *  `bounds`, else a zero box (so it is roled/named but not box-addressable). */
export function axNodesToElements(axNodes: CdpAxNode[], opts: AxToElementsOptions = {}): Element[] {
  const bounds = opts.bounds ?? new Map<number, Bbox>();
  const elements: Element[] = [];
  for (const node of axNodes) {
    if (node.nodeId === undefined) continue;
    if (node.ignored && !opts.includeIgnored) continue;
    const bid = node.backendDOMNodeId;
    const bbox = (bid !== undefined ? bounds.get(bid) : undefined) ?? [0, 0, 0, 0];
    const name = axString(node.name);
    const value = axString(node.value);
    const element: Element = {
      ref: String(node.nodeId),
      role: axString(node.role) || "unknown",
      bbox,
      states: statesOf(node),
      source: "cdp",
    };
    if (name !== "") element.name = name;
    if (value !== "") element.value = value;
    elements.push(element);
  }
  return elements;
}
