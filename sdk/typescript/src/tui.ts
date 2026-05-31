// Local TUI control console (#94) — the render model + keyboard interaction model, kept
// pure and framework-agnostic so it runs (and is tested) without an interactive terminal.
// It renders the shared control-surface state (#102) to terminal lines and maps key
// presses to state actions; the thin raw-mode/ANSI run loop that wires stdin/stdout to
// these is the only non-pure part and is layered on top. Plain-text output keeps it
// legible in light/dark and low-color terminals; large event streams are handled by
// rendering only the visible window (no full-log re-render per frame).

import type { ControlSurfaceAction, ControlSurfaceState } from "./uistate.js";
import { currentEvent, selectedSession, visibleEvents } from "./uistate.js";

export interface TuiDims {
  width: number;
  height: number;
}

const DEFAULT_DIMS: TuiDims = { width: 80, height: 24 };

function fit(line: string, width: number): string {
  if (line.length <= width) return line;
  return width <= 1 ? line.slice(0, width) : `${line.slice(0, width - 1)}…`;
}

function dashboardLines(state: ControlSurfaceState): string[] {
  const active = selectedSession(state);
  const header = `Shinken Console — ${state.sessions.length} session(s)${
    state.selectedSessionId === undefined ? "" : ` · active: ${state.selectedSessionId}`
  }`;
  if (active === undefined) return [header, "  (no active session)"];
  const rate = active.eventRate === undefined ? "—" : `${active.eventRate.toFixed(1)}/s`;
  const obs =
    active.latestObservation === undefined
      ? "—"
      : `${active.latestObservation.kind}@${active.latestObservation.dt.toFixed(1)}s`;
  return [
    header,
    `  backend=${active.provider}  status=${active.status}  rate=${rate}  eval=${active.evalStatus}`,
    `  latest-obs=${obs}  artifacts=${active.artifacts.length}`,
  ];
}

/** Render the console to terminal lines for the given size. Only the cursor-centered
 *  window of the event timeline is rendered, so a 10k-event stream costs O(window),
 *  not O(stream), per frame. */
export function renderConsole(state: ControlSurfaceState, dims: TuiDims = DEFAULT_DIMS): string[] {
  const { width, height } = dims;
  const head = dashboardLines(state);
  const events = visibleEvents(state);
  const cur = currentEvent(state);
  const footerLines = 2;
  const eventsHeight = Math.max(1, height - head.length - footerLines - 1);

  // window the events around the cursor (incremental: slice, never map the whole stream)
  const cursorIdx = cur === undefined ? Math.max(0, events.length - 1) : events.findIndex((e) => e.seq === cur.seq);
  const start = Math.max(0, Math.min(cursorIdx - Math.floor(eventsHeight / 2), events.length - eventsHeight));
  const window = events.slice(Math.max(0, start), Math.max(0, start) + eventsHeight);

  const eventLines = window.map((e) => {
    const marker = cur !== undefined && e.seq === cur.seq ? "›" : " ";
    return fit(`${marker} ${String(e.seq).padStart(5)}  ${e.kind.padEnd(12)} ${e.dt.toFixed(2)}s  ${e.src}`, width);
  });
  if (eventLines.length === 0) eventLines.push("  (no events)");

  const lastDiag = state.diagnostics.at(-1);
  const status = lastDiag === undefined ? "ready" : `${lastDiag.level}: ${lastDiag.message}`;
  const footer = [
    fit(`events ${events.length}${state.playing ? " ▶" : " ⏸"}  ${status}`, width),
    fit("[j/k] step  [space] play/pause  [g/G] top/bottom  [/] filter  [:] palette  [q] quit", width),
  ];

  return [...head.map((l) => fit(l, width)), "─".repeat(Math.min(width, 40)), ...eventLines, ...footer];
}

export interface PaletteCommand {
  keys: string;
  label: string;
}

/** Discoverable command palette — the keyboard-first shortcuts the console exposes. */
export function paletteCommands(): PaletteCommand[] {
  return [
    { keys: "j / ↓", label: "next event" },
    { keys: "k / ↑", label: "previous event" },
    { keys: "space", label: "play / pause (follow live tail)" },
    { keys: "g / G", label: "jump to first / last event" },
    { keys: "/", label: "filter timeline by event kind" },
    { keys: "q", label: "quit console" },
  ];
}

/** Map a key press to a control-surface action (pure), or null if it is not a navigation
 *  key (e.g. quit / palette open, handled by the run loop). Keyboard-first by design. */
export function handleKey(key: string, _state: ControlSurfaceState): ControlSurfaceAction | null {
  switch (key) {
    case "j":
    case "down":
      return { type: "step", delta: 1 };
    case "k":
    case "up":
      return { type: "step", delta: -1 };
    case " ":
    case "space":
      return _state.playing ? { type: "pause" } : { type: "play" };
    case "g":
      return { type: "setCursor", cursor: 0 };
    case "G":
      return { type: "setCursor", cursor: Number.MAX_SAFE_INTEGER };
    default:
      return null;
  }
}
