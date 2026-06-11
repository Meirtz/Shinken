export type Platform = "linux" | "windows" | "macos";

export type Verb =
  | "click"
  | "double_click"
  | "right_click"
  | "move"
  | "drag"
  | "mouse_down"
  | "mouse_up"
  | "scroll"
  | "type_text"
  | "key"
  | "screenshot"
  | "start_screencast"
  | "stop_screencast"
  | "wait"
  | "observe"
  | "invoke_action"
  | "set_value";

export type PointerButton = "left" | "middle" | "right";

/** Act-returns-observation parameters (schema `$defs.ObserveSpec`): ask the runtime to
 * follow a mutating action's ack with a fresh observation (`cause` = the call_id).
 * Requires the welcome's `capabilities.observe_after_act`. */
export interface ObserveSpec {
  scope?: "screen" | "active_window" | `window:${string}`;
  format?: "png" | "jpeg";
  quality?: number;
  max_long_edge?: number;
}

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
  /** Whether the runtime honors the per-action `observe` argument (act-returns-observation).
   * Absent on older welcomes = false; clients must not send `observe` then. */
  observe_after_act?: boolean;
  /** Whether the runtime ships the guest-side structured-observation engine (the
   * `observe` verb with stable element refs + tree_text diff, guest-resolved
   * `element_ref` targets, `invoke_action`/`set_value`). Absent = false. */
  structured_observation?: boolean;
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
  /** Drag destination: pointer down at `target`, interpolated moves, up at `to`. */
  to?: Target;
  /** Pointer button for drag/mouse_down/mouse_up; omitted = left. */
  button?: PointerButton;
  /** Drag gesture duration (ms); omitted = fastest (runtime-clamped). */
  duration_ms?: number;
  /** Act-returns-observation: only on mutating verbs. */
  observe?: ObserveSpec;
  /** observe: capture the structured (a11y) tree — omitted defaults to true
   * (observe IS the structured verb; pixels are `screenshot`). */
  structured?: boolean;
  /** observe: render tree_text as a diff against this session's previous revision. */
  diff?: boolean;
  /** observe: debounce a11y change notifications (ms, runtime-clamped) before walking. */
  settle_ms?: number;
  text?: string;
  keys?: string;
  dx?: number;
  dy?: number;
  ms?: number;
  scope?: "screen" | "window" | "region" | "active_window" | `window:${string}`;
  fps?: number;
  max_long_edge?: number;
  /** Wire codec for screenshot/start_screencast frames; omitted = png (lossless). */
  format?: "png" | "jpeg";
  /** JPEG quality 1-100 (ignored for png). */
  quality?: number;
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
  /** Codec of `ref` bytes; absent = png. */
  format?: "png" | "jpeg";
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
  | { type: "query"; call_id: string; q: "platform" | "screen_size" | "ready" | "list_windows" }
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
