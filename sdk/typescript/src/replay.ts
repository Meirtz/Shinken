import type { ReplayTimeline, SknEvent, SknManifest } from "./types.js";

export function createReplayTimeline(manifest?: SknManifest, events: SknEvent[] = []): ReplayTimeline {
  const timeline: ReplayTimeline = {
    events: [],
    bySeq: new Map(),
    byActionId: new Map(),
  };
  if (manifest !== undefined) {
    timeline.manifest = manifest;
  }
  for (const event of events) {
    appendReplayEvent(timeline, event);
  }
  return timeline;
}

export function appendReplayEvent(timeline: ReplayTimeline, event: SknEvent): ReplayTimeline {
  if (timeline.bySeq.has(event.seq)) {
    throw new Error(`duplicate .skn event seq: ${event.seq}`);
  }
  timeline.events.push(event);
  timeline.bySeq.set(event.seq, event);
  if (event.action_id !== undefined) {
    const grouped = timeline.byActionId.get(event.action_id) ?? [];
    grouped.push(event);
    timeline.byActionId.set(event.action_id, grouped);
  }
  return timeline;
}

export function eventAt(timeline: ReplayTimeline, seq: number): SknEvent | undefined {
  return timeline.bySeq.get(seq);
}

export function eventsForAction(timeline: ReplayTimeline, actionId: string): SknEvent[] {
  return timeline.byActionId.get(actionId) ?? [];
}

export function summarizeTimeline(timeline: ReplayTimeline): {
  events: number;
  firstSeq?: number;
  lastSeq?: number;
  channels: string[];
} {
  const first = timeline.events[0];
  const last = timeline.events.at(-1);
  const channels = [...new Set(timeline.events.map((event) => event.kind))].sort();
  return {
    events: timeline.events.length,
    ...(first === undefined ? {} : { firstSeq: first.seq }),
    ...(last === undefined ? {} : { lastSeq: last.seq }),
    channels,
  };
}
