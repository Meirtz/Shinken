import type { Action, ElementSource, PointNormTarget, PointPxTarget, Target } from "./types.js";

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
