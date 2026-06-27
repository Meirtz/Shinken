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
  | "set_value"
  // typed in-guest exec channel (G1): argv-default, shell opt-in.
  | "exec"
  // desktop verbs (G2+G3): clipboard + app launch/activate. clipboard_get is a
  // READ answered with a `result` whose value is {text} (v1 text-only).
  | "clipboard_get"
  | "clipboard_set"
  | "launch_app"
  | "activate_window";

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
  /** exec: program + arguments, run directly (no shell) — the DEFAULT form.
   * Exactly one of argv/shell. */
  argv?: string[];
  /** exec: a shell line run via the guest's `/bin/sh -c` — the explicit opt-in. */
  shell?: string;
  /** exec: the child's working directory (guest path). */
  cwd?: string;
  /** exec: extra environment merged over the runtime's. */
  env?: Record<string, string>;
  /** exec: kill-the-process-group deadline (ms, runtime-clamped; default 60 s). */
  timeout_ms?: number;
  /** exec: text written to the child's stdin, then closed. */
  stdin?: string;
  /** exec: streamed form — ack, then `exec_output` events + one `exec_exit`. */
  stream?: boolean;
  /** exec: RESERVED (PTY follow-up) — only false is accepted. */
  pty?: false;
  scope?: "screen" | "window" | "region" | "active_window" | `window:${string}`;
  fps?: number;
  max_long_edge?: number;
  /** launch_app: executable name (guest PATH) or absolute path, spawned detached on
   * the session display; activate_window: title selector (first case-insensitive
   * substring match wins). */
  app?: string;
  /** launch_app: argv tail, passed verbatim (never through a shell). */
  args?: string[];
  /** activate_window: a window id from the `list_windows` query. */
  window_id?: number;
  /** Wire codec for screenshot/start_screencast frames; omitted = png (lossless). */
  format?: "png" | "jpeg";
  /** JPEG quality 1-100 (ignored for png). */
  quality?: number;
}

export interface CoordinateRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface CoordinateSize {
  w: number;
  h: number;
}

export interface CoordinateSpace {
  origin: "top-left";
  /** Full global point_px action-space dimensions. */
  w: number;
  h: number;
  dpr: number;
  /** Captured pre-downscale region in global point_px coordinates. */
  source_rect: CoordinateRect;
  /** Image dimensions actually delivered to the client/model. */
  delivered: CoordinateSize;
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

/** The typed value of the `result` answering a buffered `exec` ($defs.ExecResult).
 * stdout/stderr are UTF-8 with lossy replacement, capped with honest truncation
 * flags; a timeout group-kill reports `timed_out: true` with a null exit_code. */
export interface ExecResult {
  exit_code: number | null;
  signal?: number | null;
  timed_out: boolean;
  stdout: string;
  stderr: string;
  stdout_truncated: boolean;
  stderr_truncated: boolean;
  duration_ms: number;
}

/** One stdout/stderr chunk of a streamed exec (`exec` with stream: true). */
export interface ExecOutputMessage {
  type: "exec_output";
  /** the exec action's call_id */
  cause: string;
  /** monotonic chunk index across BOTH channels of one exec */
  seq: number;
  channel: "stdout" | "stderr";
  /** base64 chunk bytes (binary-negotiated sessions carry raw bytes instead) */
  data_b64: string;
}

/** The terminal event of a streamed exec — exactly one per stream:true action. */
export interface ExecExitMessage {
  type: "exec_exit";
  cause: string;
  exit_code: number | null;
  signal?: number | null;
  timed_out: boolean;
  duration_ms: number;
  truncated: boolean;
  /** spawn/runtime failure: the run produced no process */
  error?: string;
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
  | ({ type: "observation" } & Observation)
  | ExecOutputMessage
  | ExecExitMessage;

export type EventKind = "action" | "observation" | "decision" | "permission" | "marker" | "meta";

export interface ControlEvent<TPayload = unknown> {
  seq: number;
  dt: number;
  kind: EventKind;
  src: string;
  payload: TPayload;
  action_id?: string;
}
