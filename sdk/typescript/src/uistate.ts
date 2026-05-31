// Shared control-surface UI state model (#102). A framework-agnostic, pure state model
// + reducer + selectors that BOTH the web panel (#100) and the local TUI (#94) consume,
// so the two control surfaces cannot diverge. Rendering stays out of here entirely; this
// module holds no protocol/runtime/backend logic (no sockets, no I/O) — only state.

import type { SknEvent, SknEventKind } from "./types.js";

export type SessionStatus = "starting" | "ready" | "busy" | "ended" | "error";
export type EvalStatus = "none" | "running" | "pass" | "fail";
export type DiagnosticLevel = "info" | "warn" | "error";

export interface SessionView {
  id: string;
  provider: string;
  status: SessionStatus;
  /** Observed events/sec, if the surface tracks it. */
  eventRate?: number;
  /** Metadata of the latest observation (no pixel bytes — refs/dims only). */
  latestObservation?: { kind: SknEventKind; dt: number; ref?: string };
  artifacts: string[];
  evalStatus: EvalStatus;
}

export interface Diagnostic {
  level: DiagnosticLevel;
  message: string;
  dt?: number;
}

export interface TimelineFilter {
  /** Keep only these event kinds (empty/undefined = all). */
  kinds?: SknEventKind[];
}

export interface ControlSurfaceState {
  sessions: SessionView[];
  selectedSessionId?: string | undefined;
  /** Bounded ring of recent events (oldest dropped past `maxEvents`). */
  events: SknEvent[];
  maxEvents: number;
  /** Replay cursor as an index into `events` (-1 = no selection). */
  cursor: number;
  playing: boolean;
  filter: TimelineFilter;
  diagnostics: Diagnostic[];
  maxDiagnostics: number;
}

export type ControlSurfaceAction =
  | { type: "setSessions"; sessions: SessionView[] }
  | { type: "selectSession"; id: string | undefined }
  | { type: "appendEvents"; events: SknEvent[] }
  | { type: "setCursor"; cursor: number }
  | { type: "step"; delta: number }
  | { type: "play" }
  | { type: "pause" }
  | { type: "setFilter"; filter: TimelineFilter }
  | { type: "setEvalStatus"; sessionId: string; status: EvalStatus }
  | { type: "pushDiagnostic"; diagnostic: Diagnostic }
  | { type: "reset" };

const DEFAULT_MAX_EVENTS = 5000;
const DEFAULT_MAX_DIAGNOSTICS = 200;

export function createInitialState(opts: { maxEvents?: number; maxDiagnostics?: number } = {}): ControlSurfaceState {
  return {
    sessions: [],
    events: [],
    maxEvents: opts.maxEvents ?? DEFAULT_MAX_EVENTS,
    cursor: -1,
    playing: false,
    filter: {},
    diagnostics: [],
    maxDiagnostics: opts.maxDiagnostics ?? DEFAULT_MAX_DIAGNOSTICS,
  };
}

function clampCursor(cursor: number, length: number): number {
  if (length === 0) return -1;
  if (cursor < 0) return 0;
  if (cursor >= length) return length - 1;
  return cursor;
}

/** Pure reducer: returns a new state for an action (never mutates the input). Incremental
 *  `appendEvents` keeps `events` bounded to `maxEvents` by dropping the oldest, and shifts
 *  the cursor so it keeps pointing at the same logical event when possible. */
export function reduce(state: ControlSurfaceState, action: ControlSurfaceAction): ControlSurfaceState {
  switch (action.type) {
    case "setSessions": {
      const keep =
        state.selectedSessionId !== undefined && action.sessions.some((s) => s.id === state.selectedSessionId);
      const selectedSessionId = keep ? state.selectedSessionId : action.sessions[0]?.id;
      return { ...state, sessions: action.sessions, selectedSessionId };
    }
    case "selectSession":
      return { ...state, selectedSessionId: action.id };
    case "appendEvents": {
      if (action.events.length === 0) return state;
      const combined = state.events.concat(action.events);
      const overflow = Math.max(0, combined.length - state.maxEvents);
      const events = overflow > 0 ? combined.slice(overflow) : combined;
      // keep the cursor anchored to the same event after any drop
      let cursor = state.cursor;
      if (cursor < 0) {
        cursor = state.playing ? events.length - 1 : 0;
      } else {
        cursor = clampCursor(cursor - overflow, events.length);
      }
      if (state.playing) cursor = events.length - 1; // follow the live tail
      return { ...state, events, cursor };
    }
    case "setCursor":
      return { ...state, cursor: clampCursor(action.cursor, state.events.length), playing: false };
    case "step":
      return { ...state, cursor: clampCursor(state.cursor + action.delta, state.events.length), playing: false };
    case "play":
      return { ...state, playing: true, cursor: clampCursor(state.events.length - 1, state.events.length) };
    case "pause":
      return { ...state, playing: false };
    case "setFilter":
      return { ...state, filter: action.filter };
    case "setEvalStatus":
      return {
        ...state,
        sessions: state.sessions.map((s) => (s.id === action.sessionId ? { ...s, evalStatus: action.status } : s)),
      };
    case "pushDiagnostic": {
      const diagnostics = state.diagnostics.concat(action.diagnostic);
      const overflow = Math.max(0, diagnostics.length - state.maxDiagnostics);
      return { ...state, diagnostics: overflow > 0 ? diagnostics.slice(overflow) : diagnostics };
    }
    case "reset":
      return createInitialState({ maxEvents: state.maxEvents, maxDiagnostics: state.maxDiagnostics });
  }
}

// --- selectors (pure, render-agnostic) --------------------------------------------

export function selectedSession(state: ControlSurfaceState): SessionView | undefined {
  return state.sessions.find((s) => s.id === state.selectedSessionId);
}

export function visibleEvents(state: ControlSurfaceState): SknEvent[] {
  const kinds = state.filter.kinds;
  if (kinds === undefined || kinds.length === 0) return state.events;
  const allow = new Set(kinds);
  return state.events.filter((e) => allow.has(e.kind));
}

export function currentEvent(state: ControlSurfaceState): SknEvent | undefined {
  return state.cursor >= 0 ? state.events[state.cursor] : undefined;
}

export function eventKindCounts(state: ControlSurfaceState): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const e of state.events) counts[e.kind] = (counts[e.kind] ?? 0) + 1;
  return counts;
}
