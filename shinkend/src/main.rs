//! shinkend — the Shinken Guest Runtime.
//!
//! A WebSocket server speaking the ACI: handshake (`hello`→`welcome`) + pointer/
//! keyboard action execution + screenshot capture, via a backend-pluggable
//! [`executor::Executor`]. Connections run a [`connection::Session`] state machine
//! (handshake-first, mandatory dev-token auth). Listens on `$SHINKEND_ADDR`
//! (default `127.0.0.1:8765`); every TCP listener requires `$SHINKEND_TOKEN`.
//! Browser-originated WebSocket upgrades are rejected unless their exact origin is
//! listed in `$SHINKEND_ALLOWED_ORIGINS`.
//! `$SHINKEND_EXECUTOR` (or the `--backend` flag, which wins) selects the action
//! backend (`auto`, `x11_xtest`, `virtual`, `pyautogui`, `macos`). On macOS with
//! no `$DISPLAY`, `auto` picks the native CoreGraphics backend.

#[cfg(target_os = "linux")]
mod atspi_source;
mod clipboard;
mod connection;
mod exec;
mod executor;
#[cfg(all(target_os = "macos", feature = "macos-native"))]
mod executor_macos;
mod observe;
mod protocol;
mod pyautogui;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::Result;
use futures_util::{SinkExt, StreamExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, Semaphore};
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::handshake::server::{
    Callback, ErrorResponse, Request as WsRequest, Response as WsResponse,
};
use tokio_tungstenite::tungstenite::http::StatusCode;
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;
use tokio_tungstenite::tungstenite::Message as WsMessage;

use connection::{Reply, ScreencastSpec, Session, StreamCtl};
use executor::Executor;

/// Map a session [`Reply`] onto the WebSocket message kind it travels as.
fn ws_reply(reply: Reply) -> WsMessage {
    match reply {
        Reply::Text(t) => WsMessage::Text(t),
        Reply::Binary(b) => WsMessage::Binary(b),
    }
}

const DEFAULT_ADDR: &str = "127.0.0.1:8765";
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
const AUTH_IDLE_TIMEOUT: Duration = Duration::from_secs(15 * 60);
const MAX_CONNECTION_LIFETIME: Duration = Duration::from_secs(4 * 60 * 60);
const MAX_CONNECTIONS: usize = 64;
const MAX_SCREENCASTS: usize = 8;
/// Cap inbound WS message/frame size (#136). 16 MiB fits 4K screenshots / large a11y
/// trees while bounding a peer from forcing unbounded buffering (tungstenite's default
/// max_message_size is 64 MiB); matches the SDK's client-side cap.
const MAX_WS_MESSAGE: usize = 16 * 1024 * 1024;
/// Outbound writer-channel depth. Bounds buffered frames+replies so a slow client
/// cannot grow memory without bound; screencast frames beyond this are dropped.
const OUTBOUND_CAP: usize = 32;
/// Per-message write deadline. A stalled peer (e.g. TCP zero-window) must not be able
/// to park the writer task — and through it the read loop's awaited sends — forever,
/// which would make MAX_CONNECTION_LIFETIME unenforceable.
const WRITE_TIMEOUT: Duration = Duration::from_secs(30);
/// How long a logical screencast stream stays resumable after its last delivered frame.
/// Long enough to ride out a reconnect; short enough that stale state can't pile up.
const STREAM_RESUME_TTL: Duration = Duration::from_secs(60);
/// Hard cap on remembered logical streams; past it the stalest entry is evicted.
const STREAM_REGISTRY_MAX: usize = 64;

/// Exact browser origins admitted at the HTTP upgrade boundary. Native SDK clients do
/// not send `Origin` and are admitted; browser clients always do and are denied unless
/// the operator opts an exact origin in via `SHINKEND_ALLOWED_ORIGINS` (comma-separated).
/// Wildcards are deliberately unsupported: this is the CSWSH boundary for a daemon that
/// can capture the screen and inject input.
#[derive(Debug, Clone, Default)]
struct OriginPolicy {
    allowed: Vec<String>,
}

impl OriginPolicy {
    fn parse(value: Option<&str>) -> Result<Self> {
        let allowed: Vec<String> = value
            .unwrap_or_default()
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .collect();
        if allowed.iter().any(|origin| origin == "*") {
            anyhow::bail!(
                "SHINKEND_ALLOWED_ORIGINS does not accept '*'; list exact origins instead"
            );
        }
        Ok(Self { allowed })
    }

    fn allows(&self, request: &WsRequest) -> bool {
        let mut origins = request.headers().get_all("origin").iter();
        let Some(origin) = origins.next() else {
            return true; // non-browser SDK/CLI client
        };
        // Multiple Origin headers are malformed/ambiguous and fail closed.
        if origins.next().is_some() {
            return false;
        }
        origin
            .to_str()
            .ok()
            .is_some_and(|candidate| self.allowed.iter().any(|allowed| allowed == candidate))
    }
}

fn forbidden_origin() -> ErrorResponse {
    tokio_tungstenite::tungstenite::http::Response::builder()
        .status(StatusCode::FORBIDDEN)
        .body(Some(
            "browser WebSocket origin is not allowed by SHINKEND_ALLOWED_ORIGINS".to_string(),
        ))
        .expect("static origin-rejection response is valid")
}

struct OriginCallback(Arc<OriginPolicy>);

impl Callback for OriginCallback {
    // Tungstenite's public Callback trait requires the large ErrorResponse by value.
    // The shape is external and this path runs once per connection, not per frame.
    fn on_request(
        self,
        request: &WsRequest,
        response: WsResponse,
    ) -> std::result::Result<WsResponse, ErrorResponse> {
        if self.0.allows(request) {
            Ok(response)
        } else {
            Err(forbidden_origin())
        }
    }
}

/// Clone-cheap, server-wide services handed to each accepted connection. Grouping the
/// policy and bounded shared resources makes it difficult for a new connection path to
/// forget one of the security controls.
#[derive(Clone)]
struct ConnectionServices {
    exec: Arc<dyn Executor>,
    token: Option<String>,
    origin_policy: Arc<OriginPolicy>,
    exec_enabled: bool,
    screencasts: Arc<Semaphore>,
    execs: Arc<Semaphore>,
    streams: Arc<StreamRegistry>,
    tree: Option<Arc<dyn observe::TreeSource>>,
}

/// Parse an explicit opt-in feature flag. Only `1` and `true` enable a privileged
/// surface; typos therefore fail closed.
fn enabled(value: Option<&str>) -> bool {
    value.is_some_and(|v| v == "1" || v.eq_ignore_ascii_case("true"))
}

fn require_token(value: Option<String>) -> Result<String> {
    value.filter(|token| !token.is_empty()).ok_or_else(|| {
        anyhow::anyhow!(
            "SHINKEND_TOKEN is required for every TCP listener, including loopback; \
             set it to a high-entropy bearer token"
        )
    })
}

/// Server-wide screencast resume registry (#56): logical stream id → (next seq, last
/// activity, owning generation). Shared across connections so a client that reconnects
/// can ask `start_screencast` + `resume_stream` to continue the SAME logical stream —
/// frames keep the old `stream` id and `seq` carries on, making the dropped-frame gap
/// readable off the first resumed frame instead of silently restarting at 0. The
/// generation is an ownership token: a resume bumps it and hands the new value to the
/// resumed task, so a zombie task still streaming into a half-open old connection
/// (which holds the stale generation) can no longer advance seq/last_seen and corrupt
/// the entry under the new owner. Entries expire after [`STREAM_RESUME_TTL`] (pruned
/// opportunistically on access — no background task) and the map is bounded at
/// [`STREAM_REGISTRY_MAX`], so a client minting stream ids cannot grow it without bound.
#[derive(Default)]
struct StreamRegistry(Mutex<RegistryInner>);

#[derive(Default)]
struct RegistryInner {
    streams: HashMap<String, StreamEntry>,
    next_gen: u64,
}

struct StreamEntry {
    next_seq: u64,
    last_seen: Instant,
    generation: u64,
}

impl StreamRegistry {
    /// Lock the registry, recovering from poisoning: entries are plain values that
    /// cannot be torn mid-update, so recovering beats cascading panics across every
    /// connection sharing the registry.
    fn locked(&self) -> std::sync::MutexGuard<'_, RegistryInner> {
        self.0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// Register a NEW logical stream `id` at `now`; returns its owning generation.
    fn register_at(&self, id: &str, next_seq: u64, now: Instant) -> u64 {
        let mut inner = self.locked();
        inner
            .streams
            .retain(|_, e| now.duration_since(e.last_seen) < STREAM_RESUME_TTL);
        if inner.streams.len() >= STREAM_REGISTRY_MAX && !inner.streams.contains_key(id) {
            // Eviction policy: when the registry is full of fresh entries, a new stream
            // evicts the stalest LIVE entry — acceptable for a 64-connection local
            // runtime; revisit with session namespacing if multi-tenant.
            let stalest = inner
                .streams
                .iter()
                .min_by_key(|(_, e)| e.last_seen)
                .map(|(k, _)| k.clone());
            if let Some(k) = stalest {
                inner.streams.remove(&k);
            }
        }
        inner.next_gen += 1;
        let generation = inner.next_gen;
        inner.streams.insert(
            id.to_string(),
            StreamEntry {
                next_seq,
                last_seen: now,
                generation,
            },
        );
        generation
    }

    /// Resume `id` if its state is still live at `now`: bump the generation (the
    /// resumed task takes ownership) and return `(next_seq, new generation)`.
    fn resume_at(&self, id: &str, now: Instant) -> Option<(u64, u64)> {
        let mut inner = self.locked();
        let inner = &mut *inner;
        inner
            .streams
            .retain(|_, e| now.duration_since(e.last_seen) < STREAM_RESUME_TTL);
        let entry = inner.streams.get_mut(id)?;
        inner.next_gen += 1;
        entry.generation = inner.next_gen;
        Some((entry.next_seq, entry.generation))
    }

    /// Record that `id`'s next frame will carry `next_seq` (called on every delivered
    /// frame). Ignored if `generation` is stale — only the current owner advances seq.
    fn record_at(&self, id: &str, next_seq: u64, generation: u64, now: Instant) {
        let mut inner = self.locked();
        if let Some(e) = inner.streams.get_mut(id) {
            if e.generation == generation {
                e.next_seq = next_seq;
                e.last_seen = now;
            }
        }
    }

    /// Refresh `id`'s liveness without advancing seq (gen-guarded). Called every
    /// capture tick so idle suppression / drop-on-full (which deliver no frames and
    /// thus never `record`) can't starve the TTL and silently lose resumability of a
    /// healthy stream over a static screen.
    fn touch_at(&self, id: &str, generation: u64, now: Instant) {
        let mut inner = self.locked();
        if let Some(e) = inner.streams.get_mut(id) {
            if e.generation == generation {
                e.last_seen = now;
            }
        }
    }

    fn register(&self, id: &str, next_seq: u64) -> u64 {
        self.register_at(id, next_seq, Instant::now())
    }

    fn resume(&self, id: &str) -> Option<(u64, u64)> {
        self.resume_at(id, Instant::now())
    }

    fn record(&self, id: &str, next_seq: u64, generation: u64) {
        self.record_at(id, next_seq, generation, Instant::now());
    }

    fn touch(&self, id: &str, generation: u64) {
        self.touch_at(id, generation, Instant::now());
    }
}

/// Server WebSocket config with bounded inbound message/frame size (#136).
fn ws_config() -> WebSocketConfig {
    WebSocketConfig {
        max_message_size: Some(MAX_WS_MESSAGE),
        max_frame_size: Some(MAX_WS_MESSAGE),
        ..WebSocketConfig::default()
    }
}

/// Whether a connection has blown the pre-auth handshake deadline (#134). Checked every
/// loop iteration so raw WS pings (handled outside the `Session`) can't keep an
/// unauthenticated connection alive past `HANDSHAKE_TIMEOUT` and squat a connection slot.
fn handshake_expired(authenticated: bool, elapsed: Duration) -> bool {
    !authenticated && elapsed >= HANDSHAKE_TIMEOUT
}

/// Extract `--backend <name>` / `--backend=<name>` from argv (it overrides
/// `$SHINKEND_EXECUTOR`). Other args are ignored — shinkend stays env-configured.
fn backend_arg(args: impl Iterator<Item = String>) -> Option<String> {
    let mut args = args;
    let mut found = None;
    while let Some(a) = args.next() {
        if a == "--backend" {
            found = args.next();
        } else if let Some(v) = a.strip_prefix("--backend=") {
            found = Some(v.to_string());
        }
    }
    found
}

#[tokio::main]
async fn main() -> Result<()> {
    let addr = std::env::var("SHINKEND_ADDR").unwrap_or_else(|_| DEFAULT_ADDR.to_string());
    let backend = backend_arg(std::env::args().skip(1));
    let token = require_token(std::env::var("SHINKEND_TOKEN").ok())?;
    let origin_policy = Arc::new(OriginPolicy::parse(
        std::env::var("SHINKEND_ALLOWED_ORIGINS").ok().as_deref(),
    )?);
    let exec_enabled = enabled(std::env::var("SHINKEND_ENABLE_EXEC").ok().as_deref());

    let listener = TcpListener::bind(&addr).await?;
    let exec = executor::executor_for(backend.as_deref())?;
    let connections = Arc::new(Semaphore::new(MAX_CONNECTIONS));
    let screencasts = Arc::new(Semaphore::new(MAX_SCREENCASTS));
    // In-guest exec channel (G1): bound concurrently running children runtime-wide.
    let execs = Arc::new(Semaphore::new(exec::MAX_EXECS));
    let streams = Arc::new(StreamRegistry::default());
    // Structured observation (M1b): ONE AT-SPI worker thread shared by every
    // session (per-session ids/diff state lives in each Session). It dials the
    // a11y bus lazily, so this never delays the listener. Linux-only v1: on other
    // hosts (e.g. the macOS engine) no tree source is attached, so the observe
    // family answers the typed `structured_observation_unavailable` error.
    #[cfg(target_os = "linux")]
    let tree: Option<Arc<dyn observe::TreeSource>> =
        Some(Arc::new(atspi_source::AtspiHandle::spawn()));
    #[cfg(not(target_os = "linux"))]
    let tree: Option<Arc<dyn observe::TreeSource>> = None;
    eprintln!(
        "shinkend v{} listening on ws://{addr} (platform: {}, backend: {}, auth: token)",
        env!("CARGO_PKG_VERSION"),
        protocol::platform(),
        exec.backend(),
    );
    eprintln!(
        "browser origins: {}; in-guest process spawning: {}",
        if origin_policy.allowed.is_empty() {
            "denied".to_string()
        } else {
            origin_policy.allowed.join(",")
        },
        if exec_enabled { "enabled" } else { "disabled" }
    );
    let services = ConnectionServices {
        exec,
        token: Some(token),
        origin_policy,
        exec_enabled,
        screencasts,
        execs,
        streams,
        tree,
    };

    loop {
        // shinkend is the sole control path into the sandbox, so a transient accept()
        // error (ECONNABORTED on a reset-during-accept, EMFILE/ENFILE fd pressure,
        // ENOBUFS) must not tear down the whole session: log, back off briefly, retry.
        let (stream, peer) = match listener.accept().await {
            Ok(pair) => pair,
            Err(e) => {
                eprintln!("accept error ({e}); backing off 100ms and continuing");
                tokio::time::sleep(Duration::from_millis(100)).await;
                continue;
            }
        };
        let Ok(conn_permit) = connections.clone().try_acquire_owned() else {
            eprintln!("connection {peer} rejected: max connections reached ({MAX_CONNECTIONS})");
            continue;
        };
        let services = services.clone();
        tokio::spawn(async move {
            let _conn_permit = conn_permit;
            if let Err(e) = serve(stream, services).await {
                eprintln!("connection {peer} closed with error: {e}");
            }
        });
    }
}

/// Handle one client connection through its [`Session`] state machine.
///
/// The socket's write half is owned by a single **writer task** fed over an mpsc
/// channel, so both request replies and unsolicited server-pushed frames (e.g. a
/// screencast) can be sent without interleaving. The read loop stays a thin driver
/// over the synchronous [`Session`]; streaming side effects arrive as [`StreamCtl`].
async fn serve(stream: TcpStream, services: ConnectionServices) -> Result<()> {
    let ConnectionServices {
        exec,
        token,
        origin_policy,
        exec_enabled,
        screencasts,
        execs,
        streams,
        tree,
    } = services;
    // Bound the WS HTTP upgrade itself by the pre-auth deadline. The connection permit
    // is already held by the caller, so an upgrade that never completes would squat a
    // slot forever — exactly the slot-exhaustion DoS #134's handshake deadline (which
    // only starts AFTER the upgrade) cannot otherwise see.
    let ws = tokio::time::timeout(
        HANDSHAKE_TIMEOUT,
        tokio_tungstenite::accept_hdr_async_with_config(
            stream,
            OriginCallback(origin_policy),
            Some(ws_config()),
        ),
    )
    .await
    .map_err(|_| anyhow::anyhow!("websocket upgrade timed out"))??;
    let (mut tx, mut rx) = ws.split();

    // Single writer behind a BOUNDED channel: replies are sent reliably (awaited),
    // but screencast frames are dropped when the client falls behind (see
    // spawn_screencast) so a slow/stalled reader cannot grow memory without bound.
    // Each send is bounded by WRITE_TIMEOUT so a stalled peer can't wedge the writer.
    let (out, mut out_rx) = mpsc::channel::<WsMessage>(OUTBOUND_CAP);
    let writer: JoinHandle<()> = tokio::spawn(async move {
        while let Some(msg) = out_rx.recv().await {
            match tokio::time::timeout(WRITE_TIMEOUT, tx.send(msg)).await {
                Ok(Ok(())) => {}
                _ => break, // send error or peer stalled past WRITE_TIMEOUT
            }
        }
        let _ = tx.close().await;
    });

    let mut session = Session::new(token, exec.clone()).with_exec_enabled(exec_enabled);
    if let Some(tree) = tree {
        session = session.with_tree_source(tree);
    }
    let mut screencast: Option<JoinHandle<()>> = None;
    // In-flight exec tasks (G1). Tracked so a connection drop aborts them — the
    // children are spawned with kill_on_drop, so aborting reaps the process too.
    let mut exec_tasks: Vec<JoinHandle<()>> = Vec::new();
    let started = tokio::time::Instant::now();

    let outcome: Result<()> = loop {
        if started.elapsed() >= MAX_CONNECTION_LIFETIME {
            let _ = out
                .send(WsMessage::Text(protocol::error_result_text(
                    "?",
                    "connection max lifetime exceeded",
                )))
                .await;
            break Ok(());
        }
        // Hard pre-auth deadline (#134): close an unauthenticated connection past
        // HANDSHAKE_TIMEOUT regardless of any pings it sent (pings don't reset it).
        if handshake_expired(session.is_authenticated(), started.elapsed()) {
            let _ = out
                .send(WsMessage::Text(protocol::error_result_text(
                    "?",
                    "handshake timeout",
                )))
                .await;
            break Ok(());
        }
        // Bound the time a client may stay unauthenticated.
        let next = if session.is_authenticated() {
            match tokio::time::timeout(AUTH_IDLE_TIMEOUT, rx.next()).await {
                Ok(frame) => frame,
                Err(_) => {
                    let _ = out
                        .send(WsMessage::Text(protocol::error_result_text(
                            "?",
                            "authenticated connection idle timeout",
                        )))
                        .await;
                    break Ok(());
                }
            }
        } else {
            // wait only until the handshake deadline (not a fresh window per frame), so a
            // ping flood can't extend the pre-auth phase (#134)
            let remaining = HANDSHAKE_TIMEOUT.saturating_sub(started.elapsed());
            match tokio::time::timeout(remaining, rx.next()).await {
                Ok(frame) => frame,
                Err(_) => {
                    let _ = out
                        .send(WsMessage::Text(protocol::error_result_text(
                            "?",
                            "handshake timeout",
                        )))
                        .await;
                    break Ok(());
                }
            }
        };
        let Some(frame) = next else { break Ok(()) };
        let frame = match frame {
            Ok(f) => f,
            Err(e) => break Err(e.into()),
        };
        match frame {
            WsMessage::Text(text) => {
                // The executor work inside on_text (one-shot screenshot capture, pointer/
                // keyboard synthesis, the pyautogui subprocess spawn) is blocking I/O; run
                // the whole step on a blocking thread so it never stalls a runtime worker
                // and starve other connections (#137 did this only for the screencast path).
                let step = match tokio::task::spawn_blocking(move || {
                    let step = session.on_text(&text);
                    (session, step)
                })
                .await
                {
                    Ok((s, step)) => {
                        session = s;
                        step
                    }
                    Err(e) => break Err(anyhow::anyhow!("session task failed: {e}")),
                };
                // wait.ms (#140): sleep (async, bounded by MAX_WAIT_MS) so the ack lands
                // after the delay; yields to other connections, never blocks the runtime.
                if step.delay_ms > 0 {
                    tokio::time::sleep(Duration::from_millis(step.delay_ms)).await;
                }
                // Typed in-guest exec (G1): run the validated spec in a bounded async
                // task feeding the same writer channel. Exclusive with StreamCtl —
                // an exec action never carries a stream side effect.
                if let Some(espec) = step.exec {
                    match execs.clone().try_acquire_owned() {
                        Ok(permit) => {
                            // The streamed form's ack must hit the writer BEFORE the
                            // task can emit its first exec_output, so send it here,
                            // then spawn. (Buffered form: reply is None — the task's
                            // typed `result` is the only answer.)
                            if let Some(reply) = step.reply {
                                if out.send(ws_reply(reply)).await.is_err() {
                                    break Ok(());
                                }
                            }
                            exec_tasks.retain(|h| !h.is_finished());
                            exec_tasks.push(spawn_exec(espec, out.clone(), permit));
                        }
                        Err(_) => {
                            // Build via serde so a hostile call_id can't break the JSON.
                            let nack = serde_json::to_string(&protocol::Message::Ack {
                                call_id: espec.call_id.clone(),
                                ok: false,
                                error: Some(format!(
                                    "max concurrent execs reached ({})",
                                    exec::MAX_EXECS
                                )),
                            })
                            .unwrap_or_else(|_| {
                                protocol::error_result_text(
                                    &espec.call_id,
                                    "max concurrent execs reached",
                                )
                            });
                            let _ = out.send(WsMessage::Text(nack)).await;
                        }
                    }
                    if step.close {
                        break Ok(());
                    }
                    continue;
                }
                match step.stream {
                    StreamCtl::Start(mut spec) => {
                        if let Some(h) = screencast.take() {
                            h.abort();
                        }
                        // The saturation nack below must answer the REQUEST: a resume
                        // rewrites spec.stream_id to the OLD logical id, and the SDK
                        // correlates replies strictly by call_id — a nack addressed to
                        // the resumed id would never match and the client would hang.
                        let request_call_id = spec.stream_id.clone();
                        // Resolve a requested resume against the registry here — the
                        // Session is pure and never sees it. A live entry continues the
                        // SAME logical stream (old id, seq carrying on) under a fresh
                        // generation, so the resumed task owns the entry and a zombie
                        // task from a half-open old connection can no longer advance
                        // it. A missing or expired one leaves the fresh stream the spec
                        // already describes, so the client sees the new id + seq 0 and
                        // knows continuity was lost.
                        let generation = match spec.resume_stream.take() {
                            Some(id) => match streams.resume(&id) {
                                Some((next_seq, generation)) => {
                                    spec.stream_id = id;
                                    spec.start_seq = next_seq;
                                    generation
                                }
                                None => streams.register(&spec.stream_id, spec.start_seq),
                            },
                            None => streams.register(&spec.stream_id, spec.start_seq),
                        };
                        match screencasts.clone().try_acquire_owned() {
                            Ok(permit) => {
                                screencast = Some(spawn_screencast(
                                    exec.clone(),
                                    out.clone(),
                                    spec,
                                    permit,
                                    streams.clone(),
                                    generation,
                                ));
                                if let Some(reply) = step.reply {
                                    if out.send(ws_reply(reply)).await.is_err() {
                                        break Ok(());
                                    }
                                }
                            }
                            Err(_) => {
                                // Build via serde so a client-supplied call_id containing
                                // a quote/backslash can't produce a malformed JSON frame.
                                let nack = serde_json::to_string(&protocol::Message::Ack {
                                    call_id: request_call_id.clone(),
                                    ok: false,
                                    error: Some(format!(
                                        "max concurrent screencasts reached ({MAX_SCREENCASTS})"
                                    )),
                                })
                                .unwrap_or_else(|_| {
                                    protocol::error_result_text(
                                        &request_call_id,
                                        "max concurrent screencasts reached",
                                    )
                                });
                                let _ = out.send(WsMessage::Text(nack)).await;
                            }
                        }
                    }
                    StreamCtl::Stop => {
                        if let Some(h) = screencast.take() {
                            h.abort();
                        }
                        if let Some(reply) = step.reply {
                            if out.send(ws_reply(reply)).await.is_err() {
                                break Ok(());
                            }
                        }
                    }
                    StreamCtl::None => {
                        if let Some(reply) = step.reply {
                            if out.send(ws_reply(reply)).await.is_err() {
                                break Ok(());
                            }
                        }
                        // Act-returns-observation: the fresh observation (or its typed
                        // capture error) lands right after the ack, same ordered writer.
                        if let Some(followup) = step.followup {
                            if out.send(ws_reply(followup)).await.is_err() {
                                break Ok(());
                            }
                        }
                    }
                }
                if step.close {
                    break Ok(());
                }
            }
            WsMessage::Ping(payload) => {
                let _ = out.send(WsMessage::Pong(payload)).await;
            }
            WsMessage::Close(_) => break Ok(()),
            _ => {}
        }
    };

    if let Some(h) = screencast.take() {
        h.abort();
    }
    // Abort in-flight execs: kill_on_drop reaps their children with them.
    for h in exec_tasks {
        h.abort();
    }
    drop(out); // let the writer task drain and finish
               // Bound the drain: a stalled peer must not let the writer (and thus the connection
               // slot) outlive the connection — abort if it can't flush within WRITE_TIMEOUT.
    let mut writer = writer;
    if tokio::time::timeout(WRITE_TIMEOUT, &mut writer)
        .await
        .is_err()
    {
        writer.abort();
    }
    outcome
}

/// Run one validated in-guest exec (G1) as an async task feeding the connection's
/// writer channel: the buffered form answers with one typed `result`
/// ([`protocol::exec_result`] — ok even on a nonzero exit code; spawn/wait failures
/// are error results); the streamed form pushes `exec_output` events and one
/// terminal `exec_exit` (see [`exec::run_streamed`]). The semaphore permit rides
/// the task, releasing on completion or abort.
fn spawn_exec(
    spec: exec::ExecSpec,
    out: mpsc::Sender<WsMessage>,
    permit: tokio::sync::OwnedSemaphorePermit,
) -> JoinHandle<()> {
    tokio::spawn(async move {
        let _permit = permit;
        if spec.stream {
            exec::run_streamed(spec, out).await;
            return;
        }
        let call_id = spec.call_id.clone();
        let text = match exec::run_buffered(spec).await {
            Ok(outcome) => serde_json::to_string(&protocol::exec_result(&call_id, &outcome))
                .unwrap_or_else(|_| {
                    protocol::error_result_text(&call_id, "failed to encode exec result")
                }),
            Err(e) => protocol::error_result_text(&call_id, &e),
        };
        let _ = out.send(WsMessage::Text(text)).await;
    })
}

/// Stream screencast frames for `spec` into the connection's writer channel until
/// the task is aborted (on `stop_screencast`, a new stream, or disconnect). Frames
/// identical to the previous one are dropped (idle-frame suppression) — the first
/// bandwidth lever; resolution/codec controls follow. Every delivered frame updates
/// the resume registry so the logical stream survives a connection drop (#56); every
/// capture tick `touch`es it so idle suppression can't starve the resume TTL. All
/// registry writes carry `generation` — a stale (resumed-away) task can't advance it.
fn spawn_screencast(
    exec: Arc<dyn Executor>,
    out: mpsc::Sender<WsMessage>,
    spec: ScreencastSpec,
    _permit: tokio::sync::OwnedSemaphorePermit,
    streams: Arc<StreamRegistry>,
    generation: u64,
) -> JoinHandle<()> {
    if spec.delta {
        return spawn_delta_screencast(exec, out, spec, _permit, streams, generation);
    }
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(Duration::from_secs_f64(1.0 / spec.fps));
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let encode_opts = crate::executor::EncodeOpts {
            max_long_edge: spec.max_long_edge,
            format: spec.format,
            quality: spec.quality,
        };
        let scope = spec.scope.clone();
        let mut seq: u64 = spec.start_seq;
        let mut last_hash: Option<u64> = None;
        // XDamage-driven capture (root window → full-screen scope only): when the
        // executor tracks damage, a tick with no accumulated damage captures
        // NOTHING — idle costs ~zero guest CPU instead of a full GetImage+encode.
        // `pending` accumulates damage across ticks whose frame was not delivered
        // (dropped on a full writer queue), so a change can never be skipped.
        let mut cursor = if spec.scope == "screen" {
            exec.damage_cursor()
        } else {
            None
        };
        let mut pending = PendingDamage::Full; // first tick always captures
        loop {
            tick.tick().await;
            // Capture off the runtime's worker threads — X11 GetImage is blocking. Awaiting
            // the blocking task here serializes captures: at most one is in flight per
            // screencast, and MissedTickBehavior::Skip drops ticks that arrive while busy,
            // so slow capture can't pile up blocking tasks/buffers behind the X11 mutex (#137).
            let exec2 = exec.clone();
            let scope2 = scope.clone();
            let (cur, pend) = (cursor, pending);
            let step = tokio::task::spawn_blocking(move || {
                let (ncur, pend) = advance_damage(exec2.as_ref(), cur, pend);
                if pend == PendingDamage::Clean {
                    return Ok((ncur, pend, None)); // damage says: nothing changed
                }
                exec2
                    .capture(&scope2, encode_opts)
                    .map(|img| (ncur, pend, Some(img)))
            })
            .await;
            let (ncur, pend, img) = match step {
                Ok(Ok(t)) => t,
                _ => break, // capture failed or the blocking pool is gone
            };
            cursor = ncur;
            pending = pend;
            // Keep the resume entry alive on EVERY capture tick, before any
            // suppression/delivery decision: idle-suppressed and dropped-on-full
            // frames never `record`, and a healthy stream over a static screen must
            // not lose resumability after STREAM_RESUME_TTL.
            streams.touch(&spec.stream_id, generation);
            let Some(img) = img else {
                continue; // clean tick — nothing was captured
            };
            // Idle suppression hashes the RAW encoded bytes — no base64 detour.
            let hash = executor::content_hash64(&img.data);
            if last_hash == Some(hash) {
                // The screen matches the delivered frame: the pending damage was
                // visually a no-op (e.g. redraw with identical pixels) — clear it.
                pending = PendingDamage::Clean;
                continue;
            }
            let Some(msg) = full_frame_msg(&spec, seq, &img) else {
                break;
            };
            // Drop the frame if the client is behind (bounded memory); a live preview
            // wants recent frames, not a backlog. Only stop if the client is gone.
            // Commit last_hash/seq ONLY on a successful send: a frame dropped on Full
            // must be re-attempted next tick, else idle-suppression would treat the
            // never-delivered change as "already sent" and leave the view stale forever.
            match out.try_send(msg) {
                Ok(()) => {
                    last_hash = Some(hash);
                    seq += 1;
                    streams.record(&spec.stream_id, seq, generation);
                    pending = PendingDamage::Clean; // this capture is delivered
                }
                Err(mpsc::error::TrySendError::Full(_)) => continue,
                Err(mpsc::error::TrySendError::Closed(_)) => break,
            }
        }
    })
}

/// Damage accumulated across capture ticks that have not yet resulted in a
/// DELIVERED frame. Distinct from [`executor::DamageSince`] (one query's verdict):
/// this is the loop-side accumulator that survives dropped/suppressed ticks.
#[derive(Debug, Clone, Copy, PartialEq)]
enum PendingDamage {
    /// The screen matches the last delivered/verified state — skip capture.
    Clean,
    /// Everything since then fits this bounding region (root coordinates).
    Region(executor::DamageRect),
    /// Unknown extent (no damage tracking, stale cursor, tracker error) — capture
    /// the full frame. Always the safe answer.
    Full,
}

impl PendingDamage {
    fn merge(self, verdict: executor::DamageSince) -> PendingDamage {
        use executor::DamageSince as V;
        match (self, verdict) {
            (p, V::Clean) => p,
            (PendingDamage::Full, _) | (_, V::Full) => PendingDamage::Full,
            (PendingDamage::Clean, V::Region(r)) => PendingDamage::Region(r),
            (PendingDamage::Region(a), V::Region(b)) => {
                PendingDamage::Region(executor::union_rect(a, b))
            }
        }
    }
}

/// Advance the damage cursor one tick: poll the executor's tracker (if any) and
/// fold the verdict into the loop's pending accumulator. Without tracking
/// (`cursor` None or the backend stopped answering) the result is always `Full` —
/// the pre-damage poll-capture behavior.
fn advance_damage(
    exec: &dyn Executor,
    cursor: Option<u64>,
    pending: PendingDamage,
) -> (Option<u64>, PendingDamage) {
    match cursor {
        Some(c) => match exec.damage_since(c) {
            Some((nc, v)) => (Some(nc), pending.merge(v)),
            None => (None, PendingDamage::Full),
        },
        None => (None, PendingDamage::Full),
    }
}

/// The dirty-tile delta variant of [`spawn_screencast`] (B2): capture RAW pixels
/// (downscaled BEFORE tiling so tiles align with the delivered resolution), diff
/// against the previous delivered frame in 64px tiles, and push only the changed
/// tiles; a full keyframe goes out first (also right after a resume — this task
/// restarts with an empty baseline) and every `KEYFRAME_INTERVAL`th delivered frame.
/// An unchanged frame emits nothing (the same idle suppression as the full-frame
/// path), and the baseline/seq advance ONLY on a successful send, so a frame dropped
/// on a full writer queue is re-diffed next tick instead of being lost.
///
/// Memory bound: the baseline is ONE raw RGB frame (`w*h*3` ≈ 6 MB at 1080p) held in
/// [`executor::DeltaState`] per active screencast — see its docs.
fn spawn_delta_screencast(
    exec: Arc<dyn Executor>,
    out: mpsc::Sender<WsMessage>,
    spec: ScreencastSpec,
    _permit: tokio::sync::OwnedSemaphorePermit,
    streams: Arc<StreamRegistry>,
    generation: u64,
) -> JoinHandle<()> {
    use executor::{DeltaFrame, DeltaState};
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(Duration::from_secs_f64(1.0 / spec.fps));
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        // capture_raw already downscales, so encode must not downscale again.
        let encode_opts = crate::executor::EncodeOpts {
            max_long_edge: None,
            format: spec.format,
            quality: spec.quality,
        };
        let max_long_edge = spec.max_long_edge;
        let scope = spec.scope.clone();
        let mut seq: u64 = spec.start_seq;
        let mut state = DeltaState::default();
        // XDamage-driven capture: a clean tick captures NOTHING; a damaged tick
        // GetImages only the damage-bounding region and composes it onto the
        // committed baseline before the (unchanged) tile diff/encode machinery.
        // Region capture is sound only when the delivered frame is the live screen
        // 1:1 — full-screen scope AND no active downscale — otherwise damage still
        // drives the idle-skip but a damaged tick falls back to full capture.
        let mut cursor = if spec.scope == "screen" {
            exec.damage_cursor()
        } else {
            None
        };
        let region_ok = spec.scope == "screen" && {
            let (sw, sh) = exec.screen_size();
            max_long_edge.is_none_or(|m| u32::from(sw.max(sh)) <= m)
        };
        let mut pending = PendingDamage::Full; // first tick: full keyframe capture
        loop {
            tick.tick().await;
            // Capture + diff + encode all run off the runtime's worker threads (X11
            // GetImage is blocking; tile diff/encode is real CPU work on a limited
            // guest). Awaiting serializes ticks like the full-frame path (#137). The
            // state moves through the closure and back, like serve() does its Session.
            let exec2 = exec.clone();
            let scope2 = scope.clone();
            let st = state;
            let (cur, pend) = (cursor, pending);
            let (st, outcome) = match tokio::task::spawn_blocking(move || {
                let (ncur, pend) = advance_damage(exec2.as_ref(), cur, pend);
                if pend == PendingDamage::Clean {
                    return (st, Ok((ncur, pend, None))); // idle — capture nothing
                }
                let captured =
                    delta_capture(exec2.as_ref(), &scope2, max_long_edge, &st, pend, region_ok);
                let outcome = captured.and_then(|(rgb, w, h)| {
                    let frame = st.tick(&rgb, w, h, encode_opts)?;
                    Ok((ncur, pend, Some((frame, rgb, w, h))))
                });
                (st, outcome)
            })
            .await
            {
                Ok(pair) => pair,
                Err(_) => break, // the blocking pool is gone
            };
            state = st;
            let Ok((ncur, pend, item)) = outcome else {
                break; // capture or encode failed
            };
            cursor = ncur;
            pending = pend;
            // Keep the resume entry alive on EVERY capture tick (see the full-frame
            // loop): suppressed/dropped frames never `record`.
            streams.touch(&spec.stream_id, generation);
            let Some((frame, rgb, w, h)) = item else {
                continue; // clean tick — nothing was captured
            };
            let (msg, was_key) = match frame {
                DeltaFrame::Unchanged => {
                    // The composed/captured frame equals the baseline: the pending
                    // damage was visually a no-op — clear it (idle suppression).
                    pending = PendingDamage::Clean;
                    continue;
                }
                DeltaFrame::Key(img) => {
                    let Some(msg) = full_frame_msg(&spec, seq, &img) else {
                        break;
                    };
                    (msg, true)
                }
                DeltaFrame::Tiles(tiles) => {
                    let Some(msg) = tiles_frame_msg(&spec, seq, &tiles) else {
                        break;
                    };
                    (msg, false)
                }
            };
            // Commit the baseline/cadence/seq ONLY on a successful send (mirrors the
            // full-frame loop's last_hash semantics): a frame dropped on Full must be
            // re-diffed against the SAME baseline next tick, or its tiles would be
            // treated as already delivered and the client's view would stay stale.
            match out.try_send(msg) {
                Ok(()) => {
                    state.commit(rgb, w, h, was_key);
                    seq += 1;
                    streams.record(&spec.stream_id, seq, generation);
                    pending = PendingDamage::Clean; // this capture is delivered
                }
                Err(mpsc::error::TrySendError::Full(_)) => continue,
                Err(mpsc::error::TrySendError::Closed(_)) => break,
            }
        }
    })
}

/// One damaged delta tick's capture: region-capture + compose when damage bounds
/// the change and the baseline matches the live screen 1:1 (`region_ok`), full
/// `capture_raw` otherwise. Any region-path failure (rect off-screen after a
/// resize, geometry drift) falls back to the full capture rather than erroring.
fn delta_capture(
    exec: &dyn Executor,
    scope: &str,
    max_long_edge: Option<u32>,
    st: &executor::DeltaState,
    pending: PendingDamage,
    region_ok: bool,
) -> Result<(Vec<u8>, u16, u16)> {
    if let (PendingDamage::Region(r), true) = (pending, region_ok) {
        if let Some((bw, bh)) = st.baseline_dims() {
            if let Some(rect) = executor::clamp_rect(r, bw, bh) {
                if let Ok(pixels) = exec.capture_raw_region(rect) {
                    if let Some(frame) = st.compose_partial(rect, &pixels) {
                        return Ok(frame);
                    }
                }
            }
        }
    }
    exec.capture_raw(scope, max_long_edge)
}

/// Build one full-image screencast frame in the stream's negotiated wire form:
/// a binary WS message (raw codec bytes after the JSON header) on a binary session,
/// the legacy base64-in-JSON text observation otherwise. `None` = encoding failed
/// (the loop breaks, matching the old serde failure path).
fn full_frame_msg(
    spec: &ScreencastSpec,
    seq: u64,
    img: &executor::CapturedImage,
) -> Option<WsMessage> {
    if spec.binary {
        return Some(WsMessage::Binary(protocol::binary_image_frame(
            &format!("{}-{seq}", spec.stream_id),
            None,
            Some(&spec.stream_id),
            Some(seq),
            None,
            None, // stream frames carry no pointer metadata
            protocol::BinaryImageMeta {
                w: img.w,
                h: img.h,
                scope: &spec.scope,
                format: img.format.as_str(),
            },
            &img.data,
        )));
    }
    let frame = protocol::stream_frame(
        &spec.stream_id,
        seq,
        protocol::ImageRef {
            data: img.to_base64(),
            w: img.w,
            h: img.h,
            scope: spec.scope.clone(),
            format: img.format.as_str().to_string(),
        },
    );
    serde_json::to_string(&frame).ok().map(WsMessage::Text)
}

/// Build one dirty-tile screencast frame in the stream's negotiated wire form
/// (see [`full_frame_msg`]).
fn tiles_frame_msg(
    spec: &ScreencastSpec,
    seq: u64,
    tiles: &[executor::EncodedTile],
) -> Option<WsMessage> {
    if spec.binary {
        let refs: Vec<protocol::BinaryTileRef<'_>> = tiles
            .iter()
            .map(|t| protocol::BinaryTileRef {
                x: t.x,
                y: t.y,
                w: t.w,
                h: t.h,
                data: &t.data,
            })
            .collect();
        return Some(WsMessage::Binary(protocol::binary_tiles_frame(
            &spec.stream_id,
            seq,
            &refs,
        )));
    }
    let msg = protocol::stream_tiles_frame(
        &spec.stream_id,
        seq,
        tiles
            .iter()
            .map(|t| protocol::TileRef {
                x: t.x,
                y: t.y,
                w: t.w,
                h: t.h,
                data: t.to_base64(),
            })
            .collect(),
    );
    serde_json::to_string(&msg).ok().map(WsMessage::Text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ws_config_bounds_inbound_size() {
        let c = ws_config();
        // bounded, and tighter than tungstenite's 64 MiB default
        assert_eq!(c.max_message_size, Some(MAX_WS_MESSAGE));
        assert_eq!(c.max_frame_size, Some(MAX_WS_MESSAGE));
    }

    #[test]
    fn handshake_deadline_fires_regardless_of_pings() {
        assert!(handshake_expired(false, HANDSHAKE_TIMEOUT)); // at the deadline → expired
        assert!(handshake_expired(
            false,
            HANDSHAKE_TIMEOUT + Duration::from_secs(1)
        ));
        assert!(!handshake_expired(false, Duration::from_secs(1))); // still within the window
        assert!(!handshake_expired(true, Duration::from_secs(100_000))); // authed never expires here
    }

    #[test]
    fn backend_arg_parses_both_flag_forms() {
        let args = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        assert_eq!(
            backend_arg(args(&["--backend", "macos"]).into_iter()),
            Some("macos".into())
        );
        assert_eq!(
            backend_arg(args(&["--backend=virtual"]).into_iter()),
            Some("virtual".into())
        );
        // last one wins; unrelated args are ignored; missing value is None
        assert_eq!(
            backend_arg(args(&["--backend=x11", "-v", "--backend", "macos"]).into_iter()),
            Some("macos".into())
        );
        assert_eq!(backend_arg(args(&["-v"]).into_iter()), None);
        assert_eq!(backend_arg(args(&["--backend"]).into_iter()), None);
    }

    #[test]
    fn privileged_flags_fail_closed() {
        assert!(!enabled(None));
        assert!(!enabled(Some("")));
        assert!(!enabled(Some("yes")));
        assert!(enabled(Some("1")));
        assert!(enabled(Some("TRUE")));
    }

    #[test]
    fn token_is_mandatory_even_for_the_default_listener() {
        assert!(require_token(None).is_err());
        assert!(require_token(Some(String::new())).is_err());
        assert_eq!(require_token(Some("secret".into())).unwrap(), "secret");
    }

    #[test]
    fn origin_policy_rejects_wildcards() {
        assert!(OriginPolicy::parse(Some("*")).is_err());
        let policy =
            OriginPolicy::parse(Some("https://console.example, http://127.0.0.1:3000")).unwrap();
        assert_eq!(
            policy.allowed,
            ["https://console.example", "http://127.0.0.1:3000"]
        );
    }

    const HELLO: &str = r#"{"type":"hello","v":0,"client":{"name":"t","version":"0"}}"#;

    /// A test server that keeps accepting connections — every `serve` shares one
    /// executor and the GIVEN screencast semaphore / resume registry, like the real
    /// main loop — so reconnect/saturation tests can shape the shared state.
    async fn spawn_server_with(
        screencasts: Arc<Semaphore>,
        streams: Arc<StreamRegistry>,
        execs: Arc<Semaphore>,
    ) -> std::net::SocketAddr {
        spawn_server_with_policy(
            screencasts,
            streams,
            execs,
            Arc::new(OriginPolicy::default()),
            true,
        )
        .await
    }

    async fn spawn_server_with_policy(
        screencasts: Arc<Semaphore>,
        streams: Arc<StreamRegistry>,
        execs: Arc<Semaphore>,
        origin_policy: Arc<OriginPolicy>,
        exec_enabled: bool,
    ) -> std::net::SocketAddr {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let exec: Arc<dyn Executor> = Arc::new(executor::VirtualExecutor::default());
        // A scripted tree source so observe-family verbs work over a real WS e2e.
        let tree: Arc<dyn observe::TreeSource> = Arc::new(observe::tests::FakeSource::new(
            observe::tests::sample_tree(),
        ));
        tokio::spawn(async move {
            loop {
                let (stream, _) = listener.accept().await.unwrap();
                let exec = exec.clone();
                let screencasts = screencasts.clone();
                let execs = execs.clone();
                let streams = streams.clone();
                let tree = tree.clone();
                let origin_policy = origin_policy.clone();
                tokio::spawn(async move {
                    let _ = serve(
                        stream,
                        ConnectionServices {
                            exec,
                            token: None,
                            origin_policy,
                            exec_enabled,
                            screencasts,
                            execs,
                            streams,
                            tree: Some(tree),
                        },
                    )
                    .await;
                });
            }
        });
        addr
    }

    async fn spawn_server() -> std::net::SocketAddr {
        spawn_server_with(
            Arc::new(Semaphore::new(MAX_SCREENCASTS)),
            Arc::new(StreamRegistry::default()),
            Arc::new(Semaphore::new(exec::MAX_EXECS)),
        )
        .await
    }

    #[tokio::test]
    async fn browser_origin_is_rejected_unless_exactly_allowlisted() {
        use tokio_tungstenite::tungstenite::client::IntoClientRequest;
        use tokio_tungstenite::tungstenite::http::HeaderValue;

        let origins = Arc::new(OriginPolicy::parse(Some("https://trusted.example")).unwrap());
        let addr = spawn_server_with_policy(
            Arc::new(Semaphore::new(MAX_SCREENCASTS)),
            Arc::new(StreamRegistry::default()),
            Arc::new(Semaphore::new(exec::MAX_EXECS)),
            origins,
            true,
        )
        .await;

        let mut evil = format!("ws://{addr}").into_client_request().unwrap();
        evil.headers_mut()
            .insert("origin", HeaderValue::from_static("https://evil.example"));
        let err = tokio_tungstenite::connect_async(evil).await.unwrap_err();
        match err {
            tokio_tungstenite::tungstenite::Error::Http(response) => {
                assert_eq!(response.status(), StatusCode::FORBIDDEN)
            }
            other => panic!("expected an HTTP rejection, got {other:?}"),
        }

        let mut trusted = format!("ws://{addr}").into_client_request().unwrap();
        trusted.headers_mut().insert(
            "origin",
            HeaderValue::from_static("https://trusted.example"),
        );
        let (mut ws, _) = tokio_tungstenite::connect_async(trusted).await.unwrap();
        ws.send(WsMessage::Text(HELLO.into())).await.unwrap();
        assert!(text(&mut ws).await.contains("\"type\":\"welcome\""));
    }

    async fn text(
        ws: &mut (impl StreamExt<Item = tokio_tungstenite::tungstenite::Result<WsMessage>> + Unpin),
    ) -> String {
        tokio::time::timeout(Duration::from_secs(3), ws.next())
            .await
            .expect("frame within 3s")
            .expect("stream open")
            .expect("ws ok")
            .into_text()
            .expect("text frame")
    }

    /// End-to-end: a real WS client starts a screencast and receives a sequence of
    /// distinct, server-pushed frames with increasing `seq`; after `stop_screencast`
    /// the frames cease.
    #[tokio::test]
    async fn screencast_streams_distinct_frames_then_stops() {
        let addr = spawn_server().await;

        let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}"))
            .await
            .unwrap();

        ws.send(WsMessage::Text(HELLO.into())).await.unwrap();
        assert!(text(&mut ws).await.contains("\"type\":\"welcome\""));

        // Start a fast screencast; the first reply must be the ack.
        ws.send(WsMessage::Text(
            r#"{"type":"action","call_id":"sc1","action":{"verb":"start_screencast","fps":30}}"#
                .into(),
        ))
        .await
        .unwrap();
        let ack = text(&mut ws).await;
        assert!(ack.contains("\"type\":\"ack\"") && ack.contains("\"ok\":true"));

        // Collect four pushed frames: stream id matches, seq advances 0..3.
        let mut seqs = Vec::new();
        for _ in 0..4 {
            let v: serde_json::Value = serde_json::from_str(&text(&mut ws).await).unwrap();
            assert_eq!(v["type"], "observation");
            assert_eq!(v["stream"], "sc1");
            assert!(v["image"]["ref"].is_string());
            seqs.push(v["seq"].as_u64().unwrap());
        }
        assert_eq!(seqs, vec![0, 1, 2, 3]);

        // Stop, then confirm the stream goes quiet (a read eventually times out).
        ws.send(WsMessage::Text(
            r#"{"type":"action","call_id":"sc2","action":{"verb":"stop_screencast"}}"#.into(),
        ))
        .await
        .unwrap();
        let mut saw_stop_ack = false;
        let mut went_quiet = false;
        for _ in 0..60 {
            match tokio::time::timeout(Duration::from_millis(250), ws.next()).await {
                Ok(Some(Ok(msg))) => {
                    let t = msg.into_text().unwrap_or_default();
                    if t.contains("\"type\":\"ack\"") && t.contains("sc2") {
                        saw_stop_ack = true;
                    }
                    // otherwise it's an in-flight frame — drain it
                }
                _ => {
                    went_quiet = true;
                    break;
                }
            }
        }
        assert!(saw_stop_ack, "stop_screencast must be acked");
        assert!(went_quiet, "frames must cease after stop_screencast");
    }

    #[tokio::test]
    async fn screencast_runs_at_most_one_capture_at_a_time() {
        use std::sync::atomic::{AtomicUsize, Ordering};

        use executor::{ActionSpec, CapturedImage, EncodeOpts, ImageFormat, DEFAULT_JPEG_QUALITY};

        // A slow executor that records the peak number of concurrent captures.
        #[derive(Default)]
        struct SlowCounter {
            inflight: AtomicUsize,
            max: AtomicUsize,
        }
        impl Executor for SlowCounter {
            fn execute(&self, _: &ActionSpec) -> anyhow::Result<String> {
                Ok(String::new())
            }
            fn backend(&self) -> &'static str {
                "slow-counter"
            }
            fn capture(&self, _: &str, _: EncodeOpts) -> anyhow::Result<CapturedImage> {
                let n = self.inflight.fetch_add(1, Ordering::SeqCst) + 1;
                self.max.fetch_max(n, Ordering::SeqCst);
                std::thread::sleep(Duration::from_millis(20)); // slow capture
                self.inflight.fetch_sub(1, Ordering::SeqCst);
                Ok(CapturedImage {
                    data: format!("f{n}").into_bytes(),
                    format: ImageFormat::Png,
                    w: 1,
                    h: 1,
                })
            }
        }

        let exec = Arc::new(SlowCounter::default());
        let (out, _rx) = mpsc::channel::<WsMessage>(8);
        let permit = Arc::new(Semaphore::new(1)).try_acquire_owned().unwrap();
        let spec = ScreencastSpec {
            stream_id: "s".into(),
            fps: 50.0, // 20ms ticks vs 20ms captures — would pile up if not serialized
            max_long_edge: None,
            format: ImageFormat::Png,
            quality: DEFAULT_JPEG_QUALITY,
            scope: "screen".into(),
            start_seq: 0,
            resume_stream: None,
            delta: false,
            binary: false,
        };
        let handle = spawn_screencast(
            exec.clone(),
            out,
            spec,
            permit,
            Arc::new(StreamRegistry::default()),
            0,
        );
        tokio::time::sleep(Duration::from_millis(120)).await; // several ticks
        handle.abort();
        // the loop awaits each blocking capture before the next tick → never concurrent (#137)
        assert_eq!(exec.max.load(Ordering::SeqCst), 1);
    }

    /// End-to-end delta screencast (B2) against the VirtualExecutor (whose synthetic
    /// frame changes every capture): the FIRST frame is a full keyframe (`image`),
    /// the following frames are dirty tiles (`tiles`, no `image`), and the keyframe
    /// cadence re-sends a full `image` as every `KEYFRAME_INTERVAL`th delivered frame.
    #[tokio::test]
    async fn delta_screencast_keyframe_then_tiles_then_cadence_keyframe() {
        use executor::KEYFRAME_INTERVAL;

        let addr = spawn_server().await;
        let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}"))
            .await
            .unwrap();
        ws.send(WsMessage::Text(HELLO.into())).await.unwrap();
        assert!(text(&mut ws).await.contains("\"type\":\"welcome\""));
        ws.send(WsMessage::Text(
            r#"{"type":"action","call_id":"d1","action":{"verb":"start_screencast","fps":30,"delta":true}}"#
                .into(),
        ))
        .await
        .unwrap();
        let ack = text(&mut ws).await;
        assert!(ack.contains("\"type\":\"ack\"") && ack.contains("\"ok\":true"));

        // Collect one full cadence cycle + 1: seq 0..=KEYFRAME_INTERVAL.
        let n = KEYFRAME_INTERVAL as usize + 1;
        let mut frames = Vec::with_capacity(n);
        for _ in 0..n {
            frames.push(next_observation(&mut ws).await);
        }
        // seq advances 0..=N with no gaps (every tick changes, none suppressed/dropped)
        for (i, v) in frames.iter().enumerate() {
            assert_eq!(v["stream"], "d1");
            assert_eq!(v["seq"].as_u64().unwrap(), i as u64);
        }
        // frame 0: keyframe — image, no tiles
        assert!(frames[0]["image"]["ref"].is_string());
        assert!(frames[0].get("tiles").is_none());
        // frames 1..KEYFRAME_INTERVAL-1: tiles only (the 2x2 frame is one edge tile)
        for v in &frames[1..KEYFRAME_INTERVAL as usize] {
            assert!(v.get("image").is_none(), "tile frame must carry no image");
            let tiles = v["tiles"].as_array().unwrap();
            assert_eq!(tiles.len(), 1);
            assert_eq!(tiles[0]["x"], 0);
            assert_eq!(tiles[0]["y"], 0);
            assert_eq!(tiles[0]["w"], 2);
            assert_eq!(tiles[0]["h"], 2);
            assert!(tiles[0]["ref"].is_string());
        }
        // the KEYFRAME_INTERVALth delivered frame is a cadence keyframe again
        let key = &frames[KEYFRAME_INTERVAL as usize];
        assert!(
            key["image"]["ref"].is_string() && key.get("tiles").is_none(),
            "every {KEYFRAME_INTERVAL}th delta frame must be a full keyframe"
        );
    }

    /// Delta + idle interplay: a static screen emits exactly ONE keyframe and then
    /// nothing — no tile frames, no periodic keyframes (idle suppression beats the
    /// cadence, which only counts DELIVERED frames).
    #[tokio::test]
    async fn delta_screencast_suppresses_unchanged_frames() {
        use executor::{ActionSpec, CapturedImage, EncodeOpts};

        /// A backend whose raw frame never changes.
        struct StaticExec;
        impl Executor for StaticExec {
            fn execute(&self, _: &ActionSpec) -> anyhow::Result<String> {
                Ok(String::new())
            }
            fn backend(&self) -> &'static str {
                "static"
            }
            fn capture(&self, _: &str, _: EncodeOpts) -> anyhow::Result<CapturedImage> {
                anyhow::bail!("unused")
            }
            fn capture_raw(&self, _: &str, _: Option<u32>) -> anyhow::Result<(Vec<u8>, u16, u16)> {
                Ok((vec![7u8; 4 * 4 * 3], 4, 4))
            }
            fn supports_raw_capture(&self) -> bool {
                true
            }
        }

        let (out, mut rx) = mpsc::channel::<WsMessage>(8);
        let permit = Arc::new(Semaphore::new(1)).try_acquire_owned().unwrap();
        let spec = ScreencastSpec {
            stream_id: "st".into(),
            fps: 30.0,
            max_long_edge: None,
            format: executor::ImageFormat::Png,
            quality: executor::DEFAULT_JPEG_QUALITY,
            scope: "screen".into(),
            start_seq: 0,
            resume_stream: None,
            delta: true,
            binary: false,
        };
        let handle = spawn_screencast(
            Arc::new(StaticExec),
            out,
            spec,
            permit,
            Arc::new(StreamRegistry::default()),
            0,
        );
        // First frame: the keyframe.
        let first = tokio::time::timeout(Duration::from_secs(3), rx.recv())
            .await
            .expect("keyframe within 3s")
            .expect("channel open");
        let v: serde_json::Value = serde_json::from_str(&first.into_text().unwrap()).unwrap();
        assert_eq!(v["seq"], 0);
        assert!(v["image"]["ref"].is_string());
        // Then: silence — many ticks pass, no further message.
        assert!(
            tokio::time::timeout(Duration::from_millis(400), rx.recv())
                .await
                .is_err(),
            "a static screen must be idle-suppressed after the keyframe"
        );
        handle.abort();
    }

    const HELLO_BINARY: &str = r#"{"type":"hello","v":0,"client":{"name":"t","version":"0"},"accept":{"binary_frames":true}}"#;

    /// Split one binary media frame into (header JSON, payload bytes).
    fn split_binary(frame: &[u8]) -> (serde_json::Value, &[u8]) {
        let hlen = u32::from_le_bytes(frame[..4].try_into().unwrap()) as usize;
        let header: serde_json::Value = serde_json::from_slice(&frame[4..4 + hlen]).unwrap();
        (header, &frame[4 + hlen..])
    }

    /// End-to-end binary negotiation: a hello with `accept.binary_frames` gets a
    /// welcome advertising the capability, a one-shot screenshot as a WS Binary
    /// message (header carries off/len, payload is the verbatim codec bytes), and
    /// screencast frames as WS Binary with advancing seq.
    #[tokio::test]
    async fn binary_session_gets_binary_screenshot_and_screencast() {
        let addr = spawn_server().await;
        let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}"))
            .await
            .unwrap();
        ws.send(WsMessage::Text(HELLO_BINARY.into())).await.unwrap();
        let welcome = text(&mut ws).await;
        assert!(welcome.contains("\"binary_frames\":true"));

        // One-shot screenshot → one WS Binary frame answering the call_id via `cause`.
        ws.send(WsMessage::Text(
            r#"{"type":"action","call_id":"shot1","action":{"verb":"screenshot"}}"#.into(),
        ))
        .await
        .unwrap();
        let frame = tokio::time::timeout(Duration::from_secs(3), ws.next())
            .await
            .expect("frame within 3s")
            .unwrap()
            .unwrap();
        let WsMessage::Binary(bytes) = frame else {
            panic!("expected a binary screenshot frame, got {frame:?}");
        };
        let (header, payload) = split_binary(&bytes);
        assert_eq!(header["type"], "observation");
        assert_eq!(header["cause"], "shot1");
        assert_eq!(header["image"]["off"], 0);
        assert_eq!(header["image"]["len"], payload.len());
        assert_eq!(header["image"]["format"], "png");
        // VirtualExecutor encodes a real PNG — check the signature travelled raw.
        assert_eq!(&payload[..4], b"\x89PNG");

        // Screencast frames arrive as WS Binary with stream id + advancing seq.
        ws.send(WsMessage::Text(
            r#"{"type":"action","call_id":"sc1","action":{"verb":"start_screencast","fps":30}}"#
                .into(),
        ))
        .await
        .unwrap();
        let ack = text(&mut ws).await;
        assert!(ack.contains("\"type\":\"ack\"") && ack.contains("\"ok\":true"));
        for want_seq in 0..3u64 {
            let frame = tokio::time::timeout(Duration::from_secs(3), ws.next())
                .await
                .expect("frame within 3s")
                .unwrap()
                .unwrap();
            let WsMessage::Binary(bytes) = frame else {
                panic!("expected a binary screencast frame, got {frame:?}");
            };
            let (header, payload) = split_binary(&bytes);
            assert_eq!(header["stream"], "sc1");
            assert_eq!(header["seq"], want_seq);
            assert_eq!(header["image"]["len"], payload.len());
        }
    }

    /// A binary-negotiated DELTA stream pushes the keyframe and the tile frames as
    /// WS Binary; tile off/len index contiguous payload slices.
    #[tokio::test]
    async fn binary_delta_screencast_sends_binary_tiles() {
        let addr = spawn_server().await;
        let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}"))
            .await
            .unwrap();
        ws.send(WsMessage::Text(HELLO_BINARY.into())).await.unwrap();
        let _ = text(&mut ws).await; // welcome
        ws.send(WsMessage::Text(
            r#"{"type":"action","call_id":"d1","action":{"verb":"start_screencast","fps":30,"delta":true}}"#
                .into(),
        ))
        .await
        .unwrap();
        let _ = text(&mut ws).await; // ack
        let mut frames = Vec::new();
        while frames.len() < 3 {
            let msg = tokio::time::timeout(Duration::from_secs(3), ws.next())
                .await
                .expect("frame within 3s")
                .unwrap()
                .unwrap();
            if let WsMessage::Binary(b) = msg {
                frames.push(b);
            }
        }
        // frame 0: keyframe (image), frames 1..: tiles
        let (h0, p0) = split_binary(&frames[0]);
        assert_eq!(h0["seq"], 0);
        assert_eq!(h0["image"]["len"], p0.len());
        for (i, frame) in frames[1..].iter().enumerate() {
            let (h, p) = split_binary(frame);
            assert_eq!(h["seq"], (i + 1) as u64);
            assert!(h.get("image").is_none(), "tile frame carries no image");
            let tiles = h["tiles"].as_array().unwrap();
            assert_eq!(tiles.len(), 1, "the 2x2 virtual frame is one edge tile");
            assert_eq!(tiles[0]["off"], 0);
            assert_eq!(tiles[0]["len"], p.len());
        }
    }

    /// A client that does NOT opt in keeps the legacy text frames — even though the
    /// runtime advertises binary_frames (old-client back-compat).
    #[tokio::test]
    async fn non_binary_session_keeps_text_frames() {
        let addr = spawn_server().await;
        let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}"))
            .await
            .unwrap();
        ws.send(WsMessage::Text(HELLO.into())).await.unwrap();
        let _ = text(&mut ws).await; // welcome
        ws.send(WsMessage::Text(
            r#"{"type":"action","call_id":"shot1","action":{"verb":"screenshot"}}"#.into(),
        ))
        .await
        .unwrap();
        let obs = text(&mut ws).await; // a TEXT observation with base64 ref
        let v: serde_json::Value = serde_json::from_str(&obs).unwrap();
        assert_eq!(v["type"], "observation");
        assert!(v["image"]["ref"].is_string(), "text path keeps base64 ref");
    }

    // ---- typed in-guest exec channel (G1) ----

    /// End-to-end buffered exec over a real WS: argv echo answers one typed
    /// `result` (ok=true, ExecResult value) — no executor backend involved, so it
    /// runs on the virtual backend like any other substrate.
    #[cfg(unix)]
    #[tokio::test]
    async fn exec_buffered_answers_typed_result_over_websocket() {
        let addr = spawn_server().await;
        let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}"))
            .await
            .unwrap();
        ws.send(WsMessage::Text(HELLO.into())).await.unwrap();
        assert!(
            text(&mut ws).await.contains("\"exec\""),
            "welcome advertises exec"
        );
        ws.send(WsMessage::Text(
            r#"{"type":"action","call_id":"x1","action":{"verb":"exec","argv":["echo","over","ws"]}}"#
                .into(),
        ))
        .await
        .unwrap();
        let v: serde_json::Value = serde_json::from_str(&text(&mut ws).await).unwrap();
        assert_eq!(v["type"], "result");
        assert_eq!(v["call_id"], "x1");
        assert_eq!(v["ok"], true);
        assert_eq!(v["value"]["exit_code"], 0);
        assert_eq!(v["value"]["stdout"], "over ws\n");
        assert_eq!(v["value"]["timed_out"], false);
    }

    /// End-to-end streamed exec: ack first, then seq-ordered exec_output events,
    /// terminated by exactly one exec_exit.
    #[cfg(unix)]
    #[tokio::test]
    async fn exec_streamed_acks_then_pushes_output_and_exit() {
        let addr = spawn_server().await;
        let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}"))
            .await
            .unwrap();
        ws.send(WsMessage::Text(HELLO.into())).await.unwrap();
        let _ = text(&mut ws).await; // welcome
        ws.send(WsMessage::Text(
            r#"{"type":"action","call_id":"x2","action":{"verb":"exec","shell":"echo one; echo two 1>&2","stream":true}}"#
                .into(),
        ))
        .await
        .unwrap();
        let ack = text(&mut ws).await;
        assert!(
            ack.contains("\"type\":\"ack\"") && ack.contains("\"ok\":true"),
            "the ack must precede any exec_output: {ack}"
        );
        let mut last_seq: i64 = -1;
        let mut channels = Vec::new();
        loop {
            let v: serde_json::Value = serde_json::from_str(&text(&mut ws).await).unwrap();
            if v["type"] == "exec_exit" {
                assert_eq!(v["cause"], "x2");
                assert_eq!(v["exit_code"], 0);
                assert_eq!(v["truncated"], false);
                break;
            }
            assert_eq!(v["type"], "exec_output");
            assert_eq!(v["cause"], "x2");
            let seq = v["seq"].as_i64().unwrap();
            assert!(seq > last_seq, "seq must be monotonic");
            last_seq = seq;
            channels.push(v["channel"].as_str().unwrap().to_string());
        }
        assert!(channels.contains(&"stdout".to_string()));
        assert!(channels.contains(&"stderr".to_string()));
    }

    /// Exec saturation: with the exec semaphore exhausted, the action is nacked
    /// (addressed to the request call_id), never silently queued.
    #[tokio::test]
    async fn exec_saturation_is_nacked() {
        let addr = spawn_server_with(
            Arc::new(Semaphore::new(MAX_SCREENCASTS)),
            Arc::new(StreamRegistry::default()),
            Arc::new(Semaphore::new(0)), // exec slots exhausted
        )
        .await;
        let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}"))
            .await
            .unwrap();
        ws.send(WsMessage::Text(HELLO.into())).await.unwrap();
        let _ = text(&mut ws).await; // welcome
        ws.send(WsMessage::Text(
            r#"{"type":"action","call_id":"x3","action":{"verb":"exec","argv":["true"]}}"#.into(),
        ))
        .await
        .unwrap();
        let v: serde_json::Value = serde_json::from_str(&text(&mut ws).await).unwrap();
        assert_eq!(v["type"], "ack");
        assert_eq!(v["call_id"], "x3");
        assert_eq!(v["ok"], false);
        assert!(v["error"]
            .as_str()
            .unwrap()
            .contains("max concurrent execs"));
    }

    // ---- XDamage-driven capture (change-proportional pipeline, half B) ----

    #[test]
    fn pending_damage_merge_accumulates() {
        use executor::DamageSince as V;
        let r1 = (0, 0, 10, 10);
        let r2 = (20, 20, 5, 5);
        assert_eq!(PendingDamage::Clean.merge(V::Clean), PendingDamage::Clean);
        assert_eq!(
            PendingDamage::Clean.merge(V::Region(r1)),
            PendingDamage::Region(r1)
        );
        assert_eq!(
            PendingDamage::Region(r1).merge(V::Region(r2)),
            PendingDamage::Region((0, 0, 25, 25))
        );
        // Full is sticky in both directions, and Clean never downgrades pending
        assert_eq!(
            PendingDamage::Full.merge(V::Region(r1)),
            PendingDamage::Full
        );
        assert_eq!(
            PendingDamage::Region(r1).merge(V::Full),
            PendingDamage::Full
        );
        assert_eq!(
            PendingDamage::Region(r1).merge(V::Clean),
            PendingDamage::Region(r1)
        );
    }

    /// A damage-capable backend over a scriptable screen: full-frame raw capture,
    /// region capture, and a damage log the test pokes. Counts captures so tests
    /// can prove idle ticks capture NOTHING and damaged ticks fetch only a region.
    struct DamageExec {
        w: u16,
        h: u16,
        screen: Mutex<Vec<u8>>,
        log: Mutex<executor::DamageLog>,
        full_captures: std::sync::atomic::AtomicUsize,
        region_captures: std::sync::atomic::AtomicUsize,
    }

    impl DamageExec {
        fn new(w: u16, h: u16) -> Self {
            Self {
                w,
                h,
                screen: Mutex::new(vec![0u8; w as usize * h as usize * 3]),
                log: Mutex::new(executor::DamageLog::default()),
                full_captures: Default::default(),
                region_captures: Default::default(),
            }
        }

        /// Paint a rect and record the matching damage, like a real X server would.
        fn paint(&self, rect: executor::DamageRect, value: u8) {
            let mut screen = self.screen.lock().unwrap();
            for row in rect.1..rect.1 + rect.3 {
                for col in rect.0..rect.0 + rect.2 {
                    let i = ((row * self.w as u32 + col) * 3) as usize;
                    screen[i..i + 3].copy_from_slice(&[value, value, value]);
                }
            }
            self.log.lock().unwrap().record(rect);
        }
    }

    impl Executor for DamageExec {
        fn execute(&self, _: &executor::ActionSpec) -> anyhow::Result<String> {
            Ok(String::new())
        }
        fn backend(&self) -> &'static str {
            "damage-test"
        }
        fn screen_size(&self) -> (u16, u16) {
            (self.w, self.h)
        }
        fn capture_raw(&self, _: &str, _: Option<u32>) -> anyhow::Result<(Vec<u8>, u16, u16)> {
            self.full_captures
                .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            Ok((self.screen.lock().unwrap().clone(), self.w, self.h))
        }
        fn supports_raw_capture(&self) -> bool {
            true
        }
        fn damage_cursor(&self) -> Option<u64> {
            Some(self.log.lock().unwrap().epoch())
        }
        fn damage_since(&self, cursor: u64) -> Option<(u64, executor::DamageSince)> {
            let log = self.log.lock().unwrap();
            Some((log.epoch(), log.since(cursor)))
        }
        fn capture_raw_region(&self, rect: executor::DamageRect) -> anyhow::Result<Vec<u8>> {
            self.region_captures
                .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            let screen = self.screen.lock().unwrap();
            let mut out = Vec::with_capacity((rect.2 * rect.3 * 3) as usize);
            for row in rect.1..rect.1 + rect.3 {
                let start = ((row * self.w as u32 + rect.0) * 3) as usize;
                out.extend_from_slice(&screen[start..start + (rect.2 * 3) as usize]);
            }
            Ok(out)
        }
    }

    /// delta_capture: a damaged tick with a usable baseline fetches ONLY the region
    /// and composes it onto the baseline; Full / no-baseline ticks capture the
    /// whole frame.
    #[test]
    fn delta_capture_region_path_composes_without_full_capture() {
        use executor::DeltaState;
        let exec = DamageExec::new(128, 64);
        let mut st = DeltaState::default();

        // no baseline yet → full capture even for a Region verdict
        let (rgb, w, h) = delta_capture(
            &exec,
            "screen",
            None,
            &st,
            PendingDamage::Region((0, 0, 4, 4)),
            true,
        )
        .unwrap();
        assert_eq!(
            exec.full_captures.load(std::sync::atomic::Ordering::SeqCst),
            1
        );
        st.commit(rgb, w, h, true);

        // paint a region; Region verdict + baseline → region capture only
        exec.paint((10, 10, 6, 6), 200);
        let (frame, _, _) = delta_capture(
            &exec,
            "screen",
            None,
            &st,
            PendingDamage::Region((10, 10, 6, 6)),
            true,
        )
        .unwrap();
        assert_eq!(
            exec.full_captures.load(std::sync::atomic::Ordering::SeqCst),
            1,
            "region path must not full-capture"
        );
        assert_eq!(
            exec.region_captures
                .load(std::sync::atomic::Ordering::SeqCst),
            1
        );
        // the composed frame equals the live screen
        assert_eq!(frame, *exec.screen.lock().unwrap());

        // region_ok=false (e.g. active downscale) falls back to full capture
        let _ = delta_capture(
            &exec,
            "screen",
            Some(32),
            &st,
            PendingDamage::Region((0, 0, 2, 2)),
            false,
        )
        .unwrap();
        assert_eq!(
            exec.full_captures.load(std::sync::atomic::Ordering::SeqCst),
            2
        );
    }

    /// End-to-end idle-zero: a delta screencast over a damage-tracking backend with
    /// NO damage captures nothing after the first keyframe — zero GetImage work,
    /// not merely zero bytes. Painting damage wakes it up with a tile frame fetched
    /// via region capture.
    #[tokio::test]
    async fn damage_driven_delta_screencast_skips_capture_when_idle() {
        let exec = Arc::new(DamageExec::new(128, 64));
        let (out, mut rx) = mpsc::channel::<WsMessage>(8);
        let permit = Arc::new(Semaphore::new(1)).try_acquire_owned().unwrap();
        let spec = ScreencastSpec {
            stream_id: "dmg".into(),
            fps: 30.0,
            max_long_edge: None,
            format: executor::ImageFormat::Png,
            quality: executor::DEFAULT_JPEG_QUALITY,
            scope: "screen".into(),
            start_seq: 0,
            resume_stream: None,
            delta: true,
            binary: false,
        };
        let handle = spawn_screencast(
            exec.clone(),
            out,
            spec,
            permit,
            Arc::new(StreamRegistry::default()),
            0,
        );
        // keyframe arrives (the first tick is always a full capture)
        let first = tokio::time::timeout(Duration::from_secs(3), rx.recv())
            .await
            .expect("keyframe within 3s")
            .expect("channel open");
        let v: serde_json::Value = serde_json::from_str(&first.into_text().unwrap()).unwrap();
        assert_eq!(v["seq"], 0);
        assert!(v["image"]["ref"].is_string());
        let full_after_key = exec.full_captures.load(std::sync::atomic::Ordering::SeqCst);

        // idle: many ticks pass — no messages AND no further captures of any kind
        assert!(
            tokio::time::timeout(Duration::from_millis(400), rx.recv())
                .await
                .is_err(),
            "idle ticks must emit nothing"
        );
        assert_eq!(
            exec.full_captures.load(std::sync::atomic::Ordering::SeqCst),
            full_after_key,
            "idle ticks must not full-capture"
        );
        assert_eq!(
            exec.region_captures
                .load(std::sync::atomic::Ordering::SeqCst),
            0,
            "idle ticks must not region-capture"
        );

        // damage wakes it: one tile frame, fetched via the region path
        exec.paint((64, 0, 8, 8), 99);
        let frame = tokio::time::timeout(Duration::from_secs(3), rx.recv())
            .await
            .expect("tile frame within 3s")
            .expect("channel open");
        let v: serde_json::Value = serde_json::from_str(&frame.into_text().unwrap()).unwrap();
        assert_eq!(v["seq"], 1);
        assert!(v.get("image").is_none(), "damage wake must be a tile frame");
        assert!(v["tiles"].as_array().is_some_and(|t| !t.is_empty()));
        assert!(
            exec.region_captures
                .load(std::sync::atomic::Ordering::SeqCst)
                >= 1,
            "the wake capture must use the damaged region"
        );
        assert_eq!(
            exec.full_captures.load(std::sync::atomic::Ordering::SeqCst),
            full_after_key,
            "the wake capture must not be a full GetImage"
        );
        handle.abort();
    }

    /// The FULL-FRAME loop also goes idle-zero under damage tracking: after the
    /// first frame, clean ticks skip the capture entirely.
    #[tokio::test]
    async fn damage_driven_full_screencast_skips_capture_when_idle() {
        // full-frame loop uses exec.capture() — route it through capture_raw counts
        struct FullExec(DamageExec);
        impl Executor for FullExec {
            fn execute(&self, a: &executor::ActionSpec) -> anyhow::Result<String> {
                self.0.execute(a)
            }
            fn backend(&self) -> &'static str {
                "damage-test-full"
            }
            fn capture(
                &self,
                scope: &str,
                opts: executor::EncodeOpts,
            ) -> anyhow::Result<executor::CapturedImage> {
                let (rgb, w, h) = self.0.capture_raw(scope, opts.max_long_edge)?;
                let mut out = Vec::new();
                {
                    let mut enc = png::Encoder::new(&mut out, w as u32, h as u32);
                    enc.set_color(png::ColorType::Rgb);
                    enc.set_depth(png::BitDepth::Eight);
                    let mut writer = enc.write_header()?;
                    writer.write_image_data(&rgb)?;
                    writer.finish()?;
                }
                Ok(executor::CapturedImage {
                    data: out,
                    format: executor::ImageFormat::Png,
                    w,
                    h,
                })
            }
            fn damage_cursor(&self) -> Option<u64> {
                self.0.damage_cursor()
            }
            fn damage_since(&self, cursor: u64) -> Option<(u64, executor::DamageSince)> {
                self.0.damage_since(cursor)
            }
        }

        let exec = Arc::new(FullExec(DamageExec::new(32, 16)));
        let (out, mut rx) = mpsc::channel::<WsMessage>(8);
        let permit = Arc::new(Semaphore::new(1)).try_acquire_owned().unwrap();
        let spec = ScreencastSpec {
            stream_id: "dmgf".into(),
            fps: 30.0,
            max_long_edge: None,
            format: executor::ImageFormat::Png,
            quality: executor::DEFAULT_JPEG_QUALITY,
            scope: "screen".into(),
            start_seq: 0,
            resume_stream: None,
            delta: false,
            binary: false,
        };
        let handle = spawn_screencast(
            exec.clone(),
            out,
            spec,
            permit,
            Arc::new(StreamRegistry::default()),
            0,
        );
        let first = tokio::time::timeout(Duration::from_secs(3), rx.recv())
            .await
            .expect("first frame within 3s")
            .expect("channel open");
        assert!(first.into_text().unwrap().contains("\"seq\":0"));
        let captures = exec
            .0
            .full_captures
            .load(std::sync::atomic::Ordering::SeqCst);
        assert!(
            tokio::time::timeout(Duration::from_millis(400), rx.recv())
                .await
                .is_err(),
            "idle ticks must emit nothing"
        );
        assert_eq!(
            exec.0
                .full_captures
                .load(std::sync::atomic::Ordering::SeqCst),
            captures,
            "idle ticks must capture nothing (damage says clean)"
        );
        // damage wakes the loop: a fresh full frame with seq 1
        exec.0.paint((1, 1, 4, 4), 77);
        let frame = tokio::time::timeout(Duration::from_secs(3), rx.recv())
            .await
            .expect("frame after damage")
            .expect("channel open");
        assert!(frame.into_text().unwrap().contains("\"seq\":1"));
        handle.abort();
    }

    /// End-to-end structured observation (M1b): a real WS client sends `observe`
    /// twice and an element click — ids stay stable across observes, the diff
    /// carries the change, and the element click is acked.
    #[tokio::test]
    async fn observe_and_element_click_over_websocket() {
        let addr = spawn_server().await;
        let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}"))
            .await
            .unwrap();
        ws.send(WsMessage::Text(HELLO.into())).await.unwrap();
        assert!(text(&mut ws)
            .await
            .contains("\"structured_observation\":true"));

        ws.send(WsMessage::Text(
            r#"{"type":"action","call_id":"o1","action":{"verb":"observe","structured":true,"settle_ms":10}}"#.into(),
        ))
        .await
        .unwrap();
        let v: serde_json::Value = serde_json::from_str(&text(&mut ws).await).unwrap();
        assert_eq!(v["type"], "observation");
        assert_eq!(v["tree"], "full");
        assert_eq!(v["revision"], 1);
        assert!(v["tree_text"].as_str().unwrap().contains("e2 push button"));
        assert_eq!(v["elements"][1]["ref"], "e2");

        // Second observe (diff requested, nothing changed): ids stable, explicit no-change.
        ws.send(WsMessage::Text(
            r#"{"type":"action","call_id":"o2","action":{"verb":"observe","diff":true}}"#.into(),
        ))
        .await
        .unwrap();
        let v: serde_json::Value = serde_json::from_str(&text(&mut ws).await).unwrap();
        assert_eq!(v["tree"], "diff");
        assert_eq!(v["elements"][1]["ref"], "e2", "ids stable across observes");
        assert!(v["tree_text"].as_str().unwrap().contains("no change"));

        // Element click by id: resolved guest-side, acked ok.
        ws.send(WsMessage::Text(
            r#"{"type":"action","call_id":"c1","action":{"verb":"click","target":{"kind":"element_ref","ref":"e2"}}}"#.into(),
        ))
        .await
        .unwrap();
        let ack = text(&mut ws).await;
        assert!(ack.contains("\"ok\":true"), "{ack}");
    }

    /// Read server-pushed frames until the next `observation`, skipping acks.
    async fn next_observation(
        ws: &mut (impl StreamExt<Item = tokio_tungstenite::tungstenite::Result<WsMessage>> + Unpin),
    ) -> serde_json::Value {
        loop {
            let v: serde_json::Value = serde_json::from_str(&text(ws).await).unwrap();
            if v["type"] == "observation" {
                return v;
            }
        }
    }

    /// End-to-end reconnect (#56): frames stream on one connection, the connection
    /// drops, and a `start_screencast` with `resume_stream` on a NEW connection
    /// continues the SAME logical stream — old `stream` id, `seq` strictly past the
    /// last frame seen, so the consumer can read the gap off the first resumed frame.
    #[tokio::test]
    async fn screencast_resume_continues_stream_id_and_seq() {
        let addr = spawn_server().await;

        let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}"))
            .await
            .unwrap();
        ws.send(WsMessage::Text(HELLO.into())).await.unwrap();
        assert!(text(&mut ws).await.contains("\"type\":\"welcome\""));
        ws.send(WsMessage::Text(
            r#"{"type":"action","call_id":"sc1","action":{"verb":"start_screencast","fps":30}}"#
                .into(),
        ))
        .await
        .unwrap();
        let mut last_seq = 0;
        for _ in 0..3 {
            let v = next_observation(&mut ws).await;
            assert_eq!(v["stream"], "sc1");
            last_seq = v["seq"].as_u64().unwrap();
        }
        drop(ws); // connection drop — no clean stop

        let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}"))
            .await
            .unwrap();
        ws.send(WsMessage::Text(HELLO.into())).await.unwrap();
        assert!(text(&mut ws).await.contains("\"type\":\"welcome\""));
        ws.send(WsMessage::Text(
            r#"{"type":"action","call_id":"sc2","action":{"verb":"start_screencast","fps":30,"resume_stream":"sc1"}}"#
                .into(),
        ))
        .await
        .unwrap();
        for _ in 0..2 {
            let v = next_observation(&mut ws).await;
            assert_eq!(v["stream"], "sc1", "resume keeps the logical stream id");
            let seq = v["seq"].as_u64().unwrap();
            assert!(
                seq > last_seq,
                "resumed seq {seq} must continue past {last_seq}"
            );
            last_seq = seq;
        }
    }

    /// An unknown (never-seen or expired) `resume_stream` starts a fresh stream:
    /// new id (the call_id), seq from 0 — the client learns continuity was lost.
    #[tokio::test]
    async fn screencast_unknown_resume_starts_fresh() {
        let addr = spawn_server().await;
        let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}"))
            .await
            .unwrap();
        ws.send(WsMessage::Text(HELLO.into())).await.unwrap();
        assert!(text(&mut ws).await.contains("\"type\":\"welcome\""));
        ws.send(WsMessage::Text(
            r#"{"type":"action","call_id":"scF","action":{"verb":"start_screencast","fps":30,"resume_stream":"ghost"}}"#
                .into(),
        ))
        .await
        .unwrap();
        let v = next_observation(&mut ws).await;
        assert_eq!(v["stream"], "scF");
        assert_eq!(v["seq"], 0);
    }

    /// When the screencast semaphore is exhausted, the saturation nack must address
    /// the REQUEST's call_id — not the resumed logical stream id — because the SDK
    /// correlates replies strictly by call_id (a nack addressed to the old stream id
    /// would never match the pending future and the resuming client would hang).
    #[tokio::test]
    async fn saturated_resume_nack_addresses_the_request_call_id() {
        let screencasts = Arc::new(Semaphore::new(0)); // exhausted
        let streams = Arc::new(StreamRegistry::default());
        streams.register("old-stream", 7); // a live logical stream to resume
        let addr = spawn_server_with(
            screencasts,
            streams,
            Arc::new(Semaphore::new(exec::MAX_EXECS)),
        )
        .await;

        let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}"))
            .await
            .unwrap();
        ws.send(WsMessage::Text(HELLO.into())).await.unwrap();
        assert!(text(&mut ws).await.contains("\"type\":\"welcome\""));
        ws.send(WsMessage::Text(
            r#"{"type":"action","call_id":"req-9","action":{"verb":"start_screencast","fps":5,"resume_stream":"old-stream"}}"#
                .into(),
        ))
        .await
        .unwrap();
        let v: serde_json::Value = serde_json::from_str(&text(&mut ws).await).unwrap();
        assert_eq!(v["type"], "ack");
        assert_eq!(v["ok"], false);
        assert_eq!(
            v["call_id"], "req-9",
            "the saturation nack must answer the request call_id, not the resumed stream id"
        );
    }

    #[test]
    fn stream_registry_resolves_and_expires_after_ttl() {
        let reg = StreamRegistry::default();
        let t0 = Instant::now();
        let generation = reg.register_at("a", 0, t0);
        reg.record_at("a", 7, generation, t0);
        assert_eq!(reg.resume_at("missing", t0), None);
        // still live just inside the TTL, pruned at/after it (no background task);
        // resume_at does not refresh last_seen — only record/touch by the owner do.
        assert_eq!(
            reg.resume_at("a", t0 + STREAM_RESUME_TTL - Duration::from_secs(1))
                .map(|(next_seq, _)| next_seq),
            Some(7)
        );
        assert_eq!(reg.resume_at("a", t0 + STREAM_RESUME_TTL), None);
    }

    /// Ownership (#56 review): resume hands the entry to a NEW generation; writes
    /// carrying the stale one (a zombie task on a half-open old connection) are
    /// ignored, so they can't corrupt seq/liveness under the new owner.
    #[test]
    fn stream_registry_stale_generation_record_is_a_noop() {
        let reg = StreamRegistry::default();
        let t0 = Instant::now();
        let old = reg.register_at("s", 0, t0);
        reg.record_at("s", 3, old, t0);
        let (next_seq, owner) = reg.resume_at("s", t0).unwrap();
        assert_eq!(next_seq, 3);
        assert_ne!(owner, old, "resume must bump the generation");
        reg.record_at("s", 4, owner, t0); // the owner advances seq...
        reg.record_at("s", 99, old, t0); // ...the zombie's write is ignored
        assert_eq!(
            reg.resume_at("s", t0).map(|(next_seq, _)| next_seq),
            Some(4)
        );
    }

    /// TTL liveness (#56 review): an idle-suppressed stream delivers no frames (never
    /// `record`s), but the task `touch`es every capture tick — the entry must stay
    /// resumable past the original TTL window. A stale-generation touch must not.
    #[test]
    fn stream_registry_touch_keeps_an_idle_stream_alive_past_ttl() {
        let reg = StreamRegistry::default();
        let t0 = Instant::now();
        let mid = t0 + STREAM_RESUME_TTL - Duration::from_secs(1);
        let past = t0 + STREAM_RESUME_TTL + Duration::from_secs(1);

        let generation = reg.register_at("s", 5, t0);
        reg.touch_at("s", generation, mid); // capture tick, no delivered frame
        assert_eq!(
            reg.resume_at("s", past).map(|(next_seq, _)| next_seq),
            Some(5),
            "a touched entry stays alive past the original TTL"
        );

        let old = reg.register_at("z", 0, t0);
        let _ = reg.resume_at("z", t0); // ownership moved to a new generation
        reg.touch_at("z", old, mid); // zombie keepalive — ignored
        assert_eq!(
            reg.resume_at("z", past),
            None,
            "a stale-generation touch must not extend liveness"
        );
    }

    #[test]
    fn stream_registry_is_bounded_evicting_the_stalest() {
        let reg = StreamRegistry::default();
        let t0 = Instant::now();
        for i in 0..STREAM_REGISTRY_MAX {
            reg.register_at(&format!("s{i}"), 1, t0 + Duration::from_millis(i as u64));
        }
        let now = t0 + Duration::from_secs(1);
        reg.register_at("overflow", 1, now); // one past the cap
        assert_eq!(reg.resume_at("s0", now), None); // the stalest entry is gone
        assert_eq!(
            reg.resume_at("s1", now).map(|(next_seq, _)| next_seq),
            Some(1)
        ); // newer survivors remain
        assert_eq!(
            reg.resume_at("overflow", now).map(|(next_seq, _)| next_seq),
            Some(1)
        );
    }
}
