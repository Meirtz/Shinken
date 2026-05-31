// Web control panel shell (#100). A pure, dependency-free renderer that
// turns the shared control-surface state (#102) into an HTML fragment: a session
// dashboard, an event timeline, and a selected-event
// detail pane. Rendering is kept entirely separate from state/protocol logic — this
// module imports only the shared model + selectors and returns a string, so it is fully
// unit-testable in CI and can be mounted into any page (`el.innerHTML = renderPanel(...)`)
// or server-rendered. Live media/WebRTC stays behind a feature flag for a later phase.

import type { ControlSurfaceState } from "./uistate.js";
import { currentEvent, selectedSession, visibleEvents } from "./uistate.js";

export interface PanelOptions {
  /** Mount the live media/WebRTC pane (deferred feature; default off). */
  media?: boolean;
}

function esc(value: unknown): string {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function dashboard(state: ControlSurfaceState): string {
  const rows = state.sessions
    .map((s) => {
      const sel = s.id === state.selectedSessionId ? ' class="selected" aria-selected="true"' : "";
      return `<li${sel} data-session="${esc(s.id)}">${esc(s.id)} · ${esc(s.provider)} · ${esc(s.status)} · eval:${esc(s.evalStatus)}</li>`;
    })
    .join("");
  const active = selectedSession(state);
  const detail = active
    ? `<dl class="active-session">
  <dt>backend</dt><dd>${esc(active.provider)}</dd>
  <dt>status</dt><dd>${esc(active.status)}</dd>
  <dt>event rate</dt><dd>${active.eventRate === undefined ? "—" : esc(active.eventRate.toFixed(2))}/s</dd>
  <dt>latest observation</dt><dd>${active.latestObservation === undefined ? "—" : esc(`${active.latestObservation.kind} @ ${active.latestObservation.dt.toFixed(2)}s`)}</dd>
  <dt>artifacts</dt><dd>${active.artifacts.length === 0 ? "—" : esc(active.artifacts.join(", "))}</dd>
  <dt>eval</dt><dd>${esc(active.evalStatus)}</dd>
</dl>`
    : `<p class="empty">no active session</p>`;
  return `<section class="dashboard"><h2>Sessions</h2><ul class="session-list">${rows}</ul>${detail}</section>`;
}

function timeline(state: ControlSurfaceState): string {
  const events = visibleEvents(state);
  const cur = currentEvent(state);
  const rows = events
    .map((e) => {
      const here = cur !== undefined && e.seq === cur.seq ? ' class="cursor" aria-current="true"' : "";
      return `<tr${here}><td>${esc(e.seq)}</td><td>${esc(e.kind)}</td><td>${esc(e.src)}</td><td>${esc(e.dt.toFixed(2))}s</td></tr>`;
    })
    .join("");
  const body = rows === "" ? '<tr class="empty"><td colspan="4">no events</td></tr>' : rows;
  return `<section class="timeline"><h2>Event timeline (${events.length})</h2><table><thead><tr><th>seq</th><th>kind</th><th>src</th><th>t</th></tr></thead><tbody>${body}</tbody></table></section>`;
}

function detailPane(state: ControlSurfaceState): string {
  const cur = currentEvent(state);
  if (cur === undefined) return '<section class="detail"><h2>Event</h2><p class="empty">none selected</p></section>';
  const payload = esc(JSON.stringify(cur.payload));
  return `<section class="detail"><h2>Event ${esc(cur.seq)} · ${esc(cur.src)}</h2><pre>${payload}</pre></section>`;
}

function mediaPane(enabled: boolean): string {
  return enabled
    ? '<section class="media"><h2>Live media</h2><div class="media-mount" data-media="on"></div></section>'
    : '<section class="media disabled"><h2>Live media</h2><p class="placeholder">disabled — WebRTC/NVENC media plane is behind a later feature flag</p></section>';
}

/** Render the control panel shell for a control-surface state as an HTML fragment. Pure
 *  and escaped; mount with `el.innerHTML = renderPanel(state)` or server-render. */
export function renderPanel(state: ControlSurfaceState, opts: PanelOptions = {}): string {
  return [
    '<div class="shinken-panel">',
    dashboard(state),
    timeline(state),
    detailPane(state),
    mediaPane(opts.media === true),
    "</div>",
  ].join("");
}
