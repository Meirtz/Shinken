// TypeScript ACI client (#99). A thin, transport-agnostic client over the ACI v0
// wire protocol: handshake (hello → welcome), call_id-routed RPC for actions/queries,
// and a demuxed queue for server-pushed screencast frames. The protocol logic lives in
// `AciClient` and is driven by a `Transport`, so it is testable without a real socket;
// `connect()` wires a runtime WebSocket to it.

import * as build from "./builders.js";
import type { AciMessage, Action, Capabilities, ClientInfo, ExecResult, Observation, Platform, Target } from "./types.js";

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
  timer: ReturnType<typeof setTimeout>;
}

interface PongWaiter {
  resolve: (t: number | undefined) => void;
  reject: (err: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

const DEFAULT_RPC_TIMEOUT_MS = 30_000;
const DEFAULT_HANDSHAKE_TIMEOUT_MS = 10_000;

function randomProcessPrefix(): string {
  const crypto = globalThis.crypto as
    | { randomUUID?: () => string; getRandomValues?: (values: Uint32Array) => Uint32Array }
    | undefined;
  if (crypto?.randomUUID !== undefined) return crypto.randomUUID().replaceAll("-", "");
  if (crypto?.getRandomValues !== undefined) {
    const words = crypto.getRandomValues(new Uint32Array(4));
    return Array.from(words, (word) => word.toString(16).padStart(8, "0")).join("");
  }
  // Old embedders without Web Crypto still get a restart-specific namespace. This is an
  // identifier collision guard, not a credential or security token.
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
}

const PROCESS_CALL_PREFIX = randomProcessPrefix();
let processCallSequence = 0;

export interface AciClientOptions {
  /** Deadline for one action/query/ping reply. Defaults to 30 seconds. */
  rpcTimeoutMs?: number;
}

/** Drives the ACI v0 protocol over a {@link Transport}. Construct via {@link connect},
 *  or directly (with a known handshake result) for tests. */
export class AciClient {
  private readonly pending = new Map<string, Pending>();
  private readonly pongWaiters: PongWaiter[] = [];
  private readonly frames: Observation[] = [];
  private readonly frameWaiters: Array<(frame: Observation | null) => void> = [];
  private streamScope = "screen";
  private closed = false;
  private readonly rpcTimeoutMs: number;

  constructor(
    private readonly transport: Transport,
    readonly capabilities: Capabilities,
    readonly platform: Platform,
    opts: AciClientOptions = {},
  ) {
    this.rpcTimeoutMs = opts.rpcTimeoutMs ?? DEFAULT_RPC_TIMEOUT_MS;
    if (!Number.isFinite(this.rpcTimeoutMs) || this.rpcTimeoutMs <= 0) {
      throw new Error("rpcTimeoutMs must be a positive finite number");
    }
  }

  /** Feed one inbound wire message. The transport calls this for every frame received. */
  feed(data: string): void {
    if (this.closed) return;
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
      if (waiter) {
        clearTimeout(waiter.timer);
        waiter.resolve(msg.t);
      }
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
      clearTimeout(p.timer);
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
    processCallSequence += 1;
    return `${PROCESS_CALL_PREFIX}-c${processCallSequence}`;
  }

  private rpc(callId: string, payload: AciMessage): Promise<AciMessage> {
    if (this.closed) return Promise.reject(new Error("client is closed"));
    return new Promise<AciMessage>((resolve, reject) => {
      const timer = setTimeout(() => {
        const pending = this.pending.get(callId);
        if (pending === undefined) return;
        this.pending.delete(callId);
        pending.reject(new Error(`ACI RPC timed out after ${this.rpcTimeoutMs} ms`));
      }, this.rpcTimeoutMs);
      this.pending.set(callId, { resolve, reject, timer });
      try {
        this.transport.send(JSON.stringify(payload));
      } catch (error) {
        this.pending.delete(callId);
        clearTimeout(timer);
        reject(error instanceof Error ? error : new Error(String(error)));
      }
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

  /** Typed in-guest exec (buffered form): run argv (default, no shell) or an explicit
   * shell line inside the Sandbox; resolves to the typed ExecResult — a nonzero exit
   * code is the command's outcome, returned, not thrown. The streamed form
   * (`stream: true` + `exec_output`/`exec_exit` events) is not surfaced by this
   * client yet; the Python SDK's `exec_stream` is the reference consumer. */
  async exec(opts: {
    argv?: string[];
    shell?: string;
    cwd?: string;
    env?: Record<string, string>;
    timeout_ms?: number;
    stdin?: string;
  }): Promise<ExecResult> {
    if ((opts.argv === undefined) === (opts.shell === undefined)) {
      throw new Error("exec takes exactly one of argv (default) or shell (explicit opt-in)");
    }
    const callId = this.nextId();
    const reply = await this.rpc(callId, {
      type: "action",
      call_id: callId,
      action: { verb: "exec", ...opts },
    });
    if (reply.type === "result" && reply.ok) return reply.value as ExecResult;
    const error = reply.type === "result" || reply.type === "ack" ? reply.error : undefined;
    throw new Error(error ?? "exec failed");
  }

  /** Round-trip latency in milliseconds. */
  ping(): Promise<number> {
    if (this.closed) return Promise.reject(new Error("client is closed"));
    const t0 = Date.now();
    return new Promise<number>((resolve, reject) => {
      const timer = setTimeout(() => {
        const index = this.pongWaiters.indexOf(waiter);
        if (index >= 0) this.pongWaiters.splice(index, 1);
        reject(new Error(`ACI ping timed out after ${this.rpcTimeoutMs} ms`));
      }, this.rpcTimeoutMs);
      const waiter: PongWaiter = {
        resolve: () => resolve(Date.now() - t0),
        reject,
        timer,
      };
      this.pongWaiters.push(waiter);
      try {
        this.transport.send(JSON.stringify({ type: "ping", t: t0 }));
      } catch (error) {
        const index = this.pongWaiters.indexOf(waiter);
        if (index >= 0) this.pongWaiters.splice(index, 1);
        clearTimeout(timer);
        reject(error instanceof Error ? error : new Error(String(error)));
      }
    });
  }

  private async query(q: "platform" | "screen_size" | "ready"): Promise<unknown> {
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

  /** Guest-side boot readiness (S8): `{ready, x11_up, root_nonblack}` computed inside
   *  the guest in microseconds — poll this during boot instead of pulling screenshots.
   *  Older runtimes answer `unknown query: ready` (an Error here). */
  async ready(): Promise<{ ready: boolean; x11_up: boolean; root_nonblack: boolean | null }> {
    return (await this.query("ready")) as { ready: boolean; x11_up: boolean; root_nonblack: boolean | null };
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
      let timer: ReturnType<typeof setTimeout> | undefined;
      const done = (frame: Observation | null): void => {
        if (settled) return;
        settled = true;
        if (timer !== undefined) clearTimeout(timer);
        resolve(frame);
      };
      this.frameWaiters.push(done);
      if (timeoutMs !== undefined) {
        timer = setTimeout(() => {
          const index = this.frameWaiters.indexOf(done);
          if (index >= 0) this.frameWaiters.splice(index, 1);
          done(null);
        }, timeoutMs);
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
    this.fail(new Error("client closed"));
    this.transport.close();
  }

  /** Called by the socket adapter when an already-handshaken transport closes/errors. */
  transportFailed(error: Error): void {
    if (this.closed) return;
    this.fail(error);
  }

  private fail(error: Error): void {
    this.closed = true;
    for (const p of this.pending.values()) {
      clearTimeout(p.timer);
      p.reject(error);
    }
    this.pending.clear();
    for (const waiter of this.pongWaiters.splice(0)) {
      clearTimeout(waiter.timer);
      waiter.reject(error);
    }
    this.clearFrames();
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
  /** Deadline for hello -> welcome. Defaults to 10 seconds. */
  handshakeTimeoutMs?: number;
  /** Deadline passed to the connected client's RPCs/pings. Defaults to 30 seconds. */
  rpcTimeoutMs?: number;
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
    let failed = false;
    let client: AciClient | undefined;
    const handshakeTimeoutMs = opts.handshakeTimeoutMs ?? DEFAULT_HANDSHAKE_TIMEOUT_MS;
    if (!Number.isFinite(handshakeTimeoutMs) || handshakeTimeoutMs <= 0) {
      reject(new Error("handshakeTimeoutMs must be a positive finite number"));
      sock.close();
      return;
    }
    if (opts.rpcTimeoutMs !== undefined && (!Number.isFinite(opts.rpcTimeoutMs) || opts.rpcTimeoutMs <= 0)) {
      reject(new Error("rpcTimeoutMs must be a positive finite number"));
      sock.close();
      return;
    }
    const failHandshake = (error: Error, closeSocket: boolean): void => {
      if (handshaken || failed) return;
      failed = true;
      clearTimeout(handshakeTimer);
      if (closeSocket) sock.close();
      reject(error);
    };
    const handshakeTimer = setTimeout(
      () => failHandshake(new Error(`ACI handshake timed out after ${handshakeTimeoutMs} ms`), true),
      handshakeTimeoutMs,
    );
    const hello: Extract<AciMessage, { type: "hello" }> = { type: "hello", v: 0, client: opts.client ?? CLIENT_INFO };
    if (opts.token !== undefined) hello.token = opts.token;

    sock.addEventListener("open", () => {
      if (failed) return;
      try {
        sock.send(JSON.stringify(hello));
      } catch (error) {
        failHandshake(error instanceof Error ? error : new Error(String(error)), true);
      }
    });
    sock.addEventListener("error", () => {
      const error = new Error(handshaken ? "WebSocket error" : "WebSocket error during ACI handshake");
      if (handshaken) {
        client?.transportFailed(error);
        sock.close();
      } else failHandshake(error, true);
    });
    sock.addEventListener("close", () => {
      const error = new Error(
        handshaken ? "WebSocket connection closed" : "connection closed before ACI handshake completed",
      );
      if (handshaken) client?.transportFailed(error);
      else failHandshake(error, false);
    });
    sock.addEventListener("message", (ev: { data: unknown }) => {
      if (failed) return;
      const data = typeof ev.data === "string" ? ev.data : String(ev.data);
      if (handshaken) {
        client?.feed(data);
        return;
      }
      let msg: AciMessage;
      try {
        msg = JSON.parse(data) as AciMessage;
      } catch {
        failHandshake(new Error("malformed handshake reply"), true);
        return;
      }
      if (msg.type !== "welcome") {
        failHandshake(new Error(`expected 'welcome', got '${msg.type}'`), true);
        return;
      }
      clearTimeout(handshakeTimer);
      handshaken = true;
      const clientOptions: AciClientOptions = {};
      if (opts.rpcTimeoutMs !== undefined) clientOptions.rpcTimeoutMs = opts.rpcTimeoutMs;
      client = new AciClient(
        { send: (d) => sock.send(d), close: () => sock.close() },
        msg.capabilities,
        msg.server.platform,
        clientOptions,
      );
      resolve(client);
    });
  });
}
