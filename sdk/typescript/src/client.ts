// TypeScript ACI client (#99). A thin, transport-agnostic client over the ACI v0
// wire protocol: handshake (hello → welcome), call_id-routed RPC for actions/queries,
// and a demuxed queue for server-pushed screencast frames. The protocol logic lives in
// `AciClient` and is driven by a `Transport`, so it is testable without a real socket;
// `connect()` wires a runtime WebSocket to it.

import * as build from "./builders.js";
import type { AciMessage, Action, Capabilities, ClientInfo, Observation, Platform, Target } from "./types.js";

/** A bidirectional text channel the client sends on; inbound frames are delivered to
 *  {@link AciClient.feed}. Abstracted so the client can be driven by a real WebSocket
 *  or an in-memory fake in tests. */
export interface Transport {
  send(data: string): void;
  close(): void;
}

const CLIENT_INFO: ClientInfo = { name: "shinken-ts", version: "0.0.0" };

interface Pending {
  resolve: (msg: AciMessage) => void;
  reject: (err: Error) => void;
}

/** Drives the ACI v0 protocol over a {@link Transport}. Construct via {@link connect},
 *  or directly (with a known handshake result) for tests. */
export class AciClient {
  private seq = 0;
  private readonly pending = new Map<string, Pending>();
  private readonly pongWaiters: Array<(t: number | undefined) => void> = [];
  private readonly frames: Observation[] = [];
  private readonly frameWaiters: Array<(frame: Observation | null) => void> = [];
  private streamScope = "screen";
  private closed = false;

  constructor(
    private readonly transport: Transport,
    readonly capabilities: Capabilities,
    readonly platform: Platform,
  ) {}

  /** Feed one inbound wire message. The transport calls this for every frame received. */
  feed(data: string): void {
    let msg: AciMessage;
    try {
      msg = JSON.parse(data) as AciMessage;
    } catch {
      return; // ignore unparseable frames rather than crash the reader
    }
    if (msg.type === "observation") {
      if (msg.stream !== undefined && msg.stream !== null) {
        this.pushFrame(msg);
        return;
      }
      if (msg.cause !== undefined) {
        this.settle(msg.cause, msg);
      }
      return;
    }
    if (msg.type === "pong") {
      const waiter = this.pongWaiters.shift();
      if (waiter) waiter(msg.t);
      return;
    }
    if (msg.type === "ack" || msg.type === "result") {
      this.settle(msg.call_id, msg);
    }
  }

  private settle(callId: string, msg: AciMessage): void {
    const p = this.pending.get(callId);
    if (p) {
      this.pending.delete(callId);
      p.resolve(msg);
    }
  }

  private pushFrame(obs: Observation): void {
    const waiter = this.frameWaiters.shift();
    if (waiter) {
      waiter(obs);
    } else {
      this.frames.push(obs);
    }
  }

  private nextId(): string {
    this.seq += 1;
    return `c${this.seq}`;
  }

  private rpc(callId: string, payload: AciMessage): Promise<AciMessage> {
    if (this.closed) return Promise.reject(new Error("client is closed"));
    return new Promise<AciMessage>((resolve, reject) => {
      this.pending.set(callId, { resolve, reject });
      this.transport.send(JSON.stringify(payload));
    });
  }

  /** Dispatch one typed action and await its `ack` (throws on a not-ok ack). */
  async act(action: Action): Promise<void> {
    const callId = this.nextId();
    const reply = await this.rpc(callId, { type: "action", call_id: callId, action });
    if (reply.type === "ack" && !reply.ok) {
      throw new Error(reply.error ?? `action ${action.verb} was rejected`);
    }
  }

  click(target: Target): Promise<void> {
    return this.act(build.click(target));
  }
  doubleClick(target: Target): Promise<void> {
    return this.act(build.doubleClick(target));
  }
  rightClick(target: Target): Promise<void> {
    return this.act(build.rightClick(target));
  }
  move(target: Target): Promise<void> {
    return this.act(build.move(target));
  }
  scroll(target: Target | undefined, dy: number, dx?: number): Promise<void> {
    return this.act(build.scroll(target, dy, dx));
  }
  typeText(text: string): Promise<void> {
    return this.act(build.typeText(text));
  }
  key(keys: string): Promise<void> {
    return this.act(build.key(keys));
  }

  /** One-shot screenshot; resolves to the paired `observation`. */
  async screenshot(scope: Action["scope"] = "screen"): Promise<Observation> {
    const callId = this.nextId();
    const reply = await this.rpc(callId, {
      type: "action",
      call_id: callId,
      action: { verb: "screenshot", scope },
    });
    if (reply.type !== "observation") {
      throw new Error(reply.type === "ack" ? (reply.error ?? "screenshot failed") : "screenshot: unexpected reply");
    }
    return reply;
  }

  /** Round-trip latency in milliseconds. */
  ping(): Promise<number> {
    if (this.closed) return Promise.reject(new Error("client is closed"));
    const t0 = Date.now();
    return new Promise<number>((resolve) => {
      this.pongWaiters.push(() => resolve(Date.now() - t0));
      this.transport.send(JSON.stringify({ type: "ping", t: t0 }));
    });
  }

  private async query(q: "platform" | "screen_size"): Promise<unknown> {
    const callId = this.nextId();
    const reply = await this.rpc(callId, { type: "query", call_id: callId, q });
    if (reply.type !== "result" || !reply.ok) {
      throw new Error(reply.type === "result" ? (reply.error ?? `query ${q} failed`) : `query ${q}: unexpected reply`);
    }
    return reply.value;
  }

  async screenSize(): Promise<{ w: number; h: number }> {
    return (await this.query("screen_size")) as { w: number; h: number };
  }

  /** Start a server-pushed screencast; returns the stream id (its `start` call_id). */
  async startScreencast(opts: { fps?: number; maxLongEdge?: number; scope?: Action["scope"] } = {}): Promise<string> {
    this.clearFrames();
    this.streamScope = opts.scope ?? "screen";
    const action: Action = { verb: "start_screencast" };
    if (opts.fps !== undefined) action.fps = opts.fps;
    if (opts.maxLongEdge !== undefined) action.max_long_edge = opts.maxLongEdge;
    if (opts.scope !== undefined) action.scope = opts.scope;
    const callId = this.nextId();
    const reply = await this.rpc(callId, { type: "action", call_id: callId, action });
    if (reply.type === "ack" && !reply.ok) {
      throw new Error(reply.error ?? "start_screencast failed");
    }
    return callId;
  }

  /** Await the next screencast frame, or null if `timeoutMs` elapses with no frame. */
  nextFrame(timeoutMs?: number): Promise<Observation | null> {
    const queued = this.frames.shift();
    if (queued !== undefined) return Promise.resolve(queued);
    if (this.closed) return Promise.resolve(null);
    return new Promise<Observation | null>((resolve) => {
      let settled = false;
      const done = (frame: Observation | null): void => {
        if (settled) return;
        settled = true;
        resolve(frame);
      };
      this.frameWaiters.push(done);
      if (timeoutMs !== undefined) {
        setTimeout(() => done(null), timeoutMs);
      }
    });
  }

  async stopScreencast(): Promise<void> {
    const callId = this.nextId();
    await this.rpc(callId, { type: "action", call_id: callId, action: { verb: "stop_screencast" } }).catch(() => undefined);
    this.clearFrames();
  }

  /** The capture region of the active screencast. */
  get screencastScope(): string {
    return this.streamScope;
  }

  private clearFrames(): void {
    this.frames.length = 0;
    for (const waiter of this.frameWaiters.splice(0)) waiter(null);
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    for (const p of this.pending.values()) p.reject(new Error("client closed"));
    this.pending.clear();
    this.clearFrames();
    this.transport.close();
  }
}

/** A minimal structural view of a runtime WebSocket (the Node ≥22 / browser global). */
interface RuntimeSocket {
  send(data: string): void;
  close(): void;
  addEventListener(type: "open", listener: () => void): void;
  addEventListener(type: "message", listener: (ev: { data: unknown }) => void): void;
  addEventListener(type: "close", listener: () => void): void;
  addEventListener(type: "error", listener: (ev: unknown) => void): void;
}

export interface ConnectOptions {
  token?: string;
  client?: ClientInfo;
  /** Override the socket factory (defaults to the global `WebSocket`); useful for tests. */
  openSocket?: (uri: string) => RuntimeSocket;
}

function toUri(addr: string): string {
  if (addr.startsWith("ws://") || addr.startsWith("wss://")) return addr;
  return `ws://${addr}`;
}

function defaultSocketFactory(uri: string): RuntimeSocket {
  const ctor = (globalThis as { WebSocket?: new (url: string) => RuntimeSocket }).WebSocket;
  if (ctor === undefined) {
    throw new Error("no global WebSocket available (Node >= 22 or a WebSocket polyfill is required)");
  }
  return new ctor(uri);
}

/** Open an ACI session: connect, complete the `hello` → `welcome` handshake, and
 *  resolve to a ready {@link AciClient}. */
export function connect(addr: string, opts: ConnectOptions = {}): Promise<AciClient> {
  const factory = opts.openSocket ?? defaultSocketFactory;
  const sock = factory(toUri(addr));
  return new Promise<AciClient>((resolve, reject) => {
    let handshaken = false;
    const hello: Extract<AciMessage, { type: "hello" }> = { type: "hello", v: 0, client: opts.client ?? CLIENT_INFO };
    if (opts.token !== undefined) hello.token = opts.token;

    sock.addEventListener("open", () => sock.send(JSON.stringify(hello)));
    sock.addEventListener("error", () => reject(new Error("WebSocket error during ACI handshake")));
    sock.addEventListener("close", () => {
      if (!handshaken) reject(new Error("connection closed before ACI handshake completed"));
    });
    sock.addEventListener("message", (ev: { data: unknown }) => {
      const data = typeof ev.data === "string" ? ev.data : String(ev.data);
      if (handshaken) {
        client.feed(data);
        return;
      }
      let msg: AciMessage;
      try {
        msg = JSON.parse(data) as AciMessage;
      } catch {
        reject(new Error("malformed handshake reply"));
        return;
      }
      if (msg.type !== "welcome") {
        reject(new Error(`expected 'welcome', got '${msg.type}'`));
        return;
      }
      handshaken = true;
      client = new AciClient({ send: (d) => sock.send(d), close: () => sock.close() }, msg.capabilities, msg.server.platform);
      resolve(client);
    });

    let client: AciClient;
  });
}
