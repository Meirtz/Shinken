export type Platform = "linux" | "windows" | "macos";

export type Verb =
  | "click"
  | "double_click"
  | "right_click"
  | "move"
  | "scroll"
  | "type_text"
  | "key"
  | "screenshot"
  | "start_screencast"
  | "stop_screencast"
  | "wait";

export type TargetKind = "point_px" | "point_norm" | "element_ref";
export type ObservationType = "a11y" | "screenshot" | "video" | "som" | "screencast";
export type ElementSource = "atspi" | "uia" | "ax" | "cdp" | "som";

export interface ClientInfo {
  name: string;
  version: string;
}

export interface ServerInfo {
  name: string;
  version: string;
  platform: Platform;
}

export interface Capabilities {
  schema_version: 0;
  verbs: Verb[];
  targets: TargetKind[];
  observation_types: ObservationType[];
  max_long_edge?: number;
}

export interface PointPxTarget {
  kind: "point_px";
  x: number;
  y: number;
}

export interface PointNormTarget {
  kind: "point_norm";
  x: number;
  y: number;
}

export interface ElementRefTarget {
  kind: "element_ref";
  ref: string;
  source?: ElementSource;
}

export type Target = PointPxTarget | PointNormTarget | ElementRefTarget;

export interface Action {
  verb: Verb;
  target?: Target;
  text?: string;
  keys?: string;
  dx?: number;
  dy?: number;
  ms?: number;
  scope?: "screen" | "window" | "region" | "active_window" | `window:${string}`;
  fps?: number;
  max_long_edge?: number;
}

export interface CoordinateSpace {
  origin?: "top-left";
  w: number;
  h: number;
  dpr?: number;
}

export interface Element {
  ref: string;
  role: string;
  name?: string;
  value?: string;
  states?: string[];
  bbox: [number, number, number, number];
  source?: ElementSource;
}

export interface ImageRef {
  ref: string;
  w: number;
  h: number;
  scope?: "screen" | "window" | "region";
}

export interface ObservationDelta {
  added?: Element[];
  removed?: string[];
  changed?: Element[];
}

export interface Observation {
  obs_id: string;
  cause?: string;
  display?: CoordinateSpace;
  tree?: "full" | "diff";
  elements?: Element[];
  delta?: ObservationDelta;
  image?: ImageRef;
  stream?: string;
  seq?: number;
}

export type AciMessage =
  | { type: "hello"; v: 0; client: ClientInfo; accept?: { observation_types?: ObservationType[] }; token?: string }
  | { type: "welcome"; v: 0; server: ServerInfo; capabilities: Capabilities }
  | { type: "ping"; t?: number }
  | { type: "pong"; t?: number }
  | { type: "query"; call_id: string; q: "platform" | "screen_size" }
  | { type: "result"; call_id: string; ok: boolean; value?: unknown; error?: string }
  | { type: "action"; call_id: string; action: Action }
  | { type: "ack"; call_id: string; ok: boolean; error?: string }
  | ({ type: "observation" } & Observation);

export type EventKind = "action" | "observation" | "decision" | "permission" | "marker" | "meta";

export interface ControlEvent<TPayload = unknown> {
  seq: number;
  dt: number;
  kind: EventKind;
  src: string;
  payload: TPayload;
  action_id?: string;
}
