import type { Action, ElementSource, PointerButton, PointNormTarget, PointPxTarget, Target } from "./types.js";

export function pointPx(x: number, y: number): PointPxTarget {
  return { kind: "point_px", x, y };
}

export function pointNorm(x: number, y: number): PointNormTarget {
  return { kind: "point_norm", x, y };
}

export function elementRef(ref: string, source?: ElementSource): Target {
  return source === undefined ? { kind: "element_ref", ref } : { kind: "element_ref", ref, source };
}

export function click(target: Target): Action {
  return { verb: "click", target };
}

export function doubleClick(target: Target): Action {
  return { verb: "double_click", target };
}

export function rightClick(target: Target): Action {
  return { verb: "right_click", target };
}

export function move(target: Target): Action {
  return { verb: "move", target };
}

export function drag(target: Target, to: Target, durationMs?: number, button?: PointerButton): Action {
  const action: Action = { verb: "drag", target, to };
  if (durationMs !== undefined) action.duration_ms = durationMs;
  if (button !== undefined) action.button = button;
  return action;
}

export function mouseDown(target?: Target, button?: PointerButton): Action {
  const action: Action = { verb: "mouse_down" };
  if (target !== undefined) action.target = target;
  if (button !== undefined) action.button = button;
  return action;
}

export function mouseUp(target?: Target, button?: PointerButton): Action {
  const action: Action = { verb: "mouse_up" };
  if (target !== undefined) action.target = target;
  if (button !== undefined) action.button = button;
  return action;
}

export function scroll(target: Target | undefined, dy: number, dx?: number): Action {
  const action: Action = { verb: "scroll", dy };
  if (target !== undefined) action.target = target;
  if (dx !== undefined) action.dx = dx;
  return action;
}

export function typeText(text: string): Action {
  return { verb: "type_text", text };
}

export function key(keys: string): Action {
  return { verb: "key", keys };
}

export function screenshot(scope: Action["scope"] = "screen"): Action {
  return { verb: "screenshot", scope };
}

export function wait(ms?: number): Action {
  return ms === undefined ? { verb: "wait" } : { verb: "wait", ms };
}

// ---- desktop verbs (G2+G3) ----

/** Read the guest clipboard (answered with a `result` carrying {text}). */
export function clipboardGet(): Action {
  return { verb: "clipboard_get" };
}

export function clipboardSet(text: string): Action {
  return { verb: "clipboard_set", text };
}

export function launchApp(app: string, args?: string[]): Action {
  const action: Action = { verb: "launch_app", app };
  if (args !== undefined) action.args = args;
  return action;
}

/** Activate by window id (from `list_windows`) or by app/title selector. */
export function activateWindow(selector: number | string): Action {
  return typeof selector === "number"
    ? { verb: "activate_window", window_id: selector }
    : { verb: "activate_window", app: selector };
}
