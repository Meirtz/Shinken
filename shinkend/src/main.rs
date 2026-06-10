//! shinkend — the Shinken Guest Runtime.
//!
//! A WebSocket server speaking the ACI: handshake (`hello`→`welcome`) + pointer/
//! keyboard action execution + screenshot capture, via a backend-pluggable
//! [`executor::Executor`]. Connections run a [`connection::Session`] state machine
//! (handshake-first, optional dev-token auth). Listens on `$SHINKEND_ADDR`
//! (default `127.0.0.1:8765`); a non-loopback bind requires `$SHINKEND_TOKEN`.
//! `$SHINKEND_EXECUTOR` selects the action backend (`auto`, `x11_xtest`, `virtual`,
//! `pyautogui`).

mod connection;
mod executor;
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
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;
use tokio_tungstenite::tungstenite::Message as WsMessage;

use connection::{ScreencastSpec, Session, StreamCtl};
use executor::Executor;

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

#[tokio::main]
async fn main() -> Result<()> {
    let addr = std::env::var("SHINKEND_ADDR").unwrap_or_else(|_| DEFAULT_ADDR.to_string());
    let token = std::env::var("SHINKEND_TOKEN")
        .ok()
        .filter(|t| !t.is_empty());

    if !is_loopback(&addr) && token.is_none() {
        eprintln!(
            "shinkend: refusing to bind non-loopback address {addr} without SHINKEND_TOKEN.\n         \
             Set SHINKEND_TOKEN to require a dev bearer token, or bind a loopback address (127.0.0.1)."
        );
        std::process::exit(1);
    }

    let listener = TcpListener::bind(&addr).await?;
    let exec = executor::default_executor()?;
    let connections = Arc::new(Semaphore::new(MAX_CONNECTIONS));
    let screencasts = Arc::new(Semaphore::new(MAX_SCREENCASTS));
    let streams = Arc::new(StreamRegistry::default());
    eprintln!(
        "shinkend v{} listening on ws://{addr} (platform: {}, backend: {}, auth: {})",
        env!("CARGO_PKG_VERSION"),
        protocol::platform(),
        exec.backend(),
        if token.is_some() {
            "token"
        } else {
            "none (loopback)"
        },
    );

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
        let exec = exec.clone();
        let token = token.clone();
        let screencasts = screencasts.clone();
        let streams = streams.clone();
        tokio::spawn(async move {
            let _conn_permit = conn_permit;
            if let Err(e) = serve(stream, exec, token, screencasts, streams).await {
                eprintln!("connection {peer} closed with error: {e}");
            }
        });
    }
}

/// True if `addr`'s host is a loopback address (no token required to bind).
fn is_loopback(addr: &str) -> bool {
    let host = addr.rsplit_once(':').map_or(addr, |(h, _)| h);
    let host = host.trim_start_matches('[').trim_end_matches(']');
    host == "::1" || host == "localhost" || host.starts_with("127.")
}

/// Handle one client connection through its [`Session`] state machine.
///
/// The socket's write half is owned by a single **writer task** fed over an mpsc
/// channel, so both request replies and unsolicited server-pushed frames (e.g. a
/// screencast) can be sent without interleaving. The read loop stays a thin driver
/// over the synchronous [`Session`]; streaming side effects arrive as [`StreamCtl`].
async fn serve(
    stream: TcpStream,
    exec: Arc<dyn Executor>,
    token: Option<String>,
    screencasts: Arc<Semaphore>,
    streams: Arc<StreamRegistry>,
) -> Result<()> {
    // Bound the WS HTTP upgrade itself by the pre-auth deadline. The connection permit
    // is already held by the caller, so an upgrade that never completes would squat a
    // slot forever — exactly the slot-exhaustion DoS #134's handshake deadline (which
    // only starts AFTER the upgrade) cannot otherwise see.
    let ws = tokio::time::timeout(
        HANDSHAKE_TIMEOUT,
        tokio_tungstenite::accept_async_with_config(stream, Some(ws_config())),
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

    let mut session = Session::new(token, exec.clone());
    let mut screencast: Option<JoinHandle<()>> = None;
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
                                    if out.send(WsMessage::Text(reply)).await.is_err() {
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
                            if out.send(WsMessage::Text(reply)).await.is_err() {
                                break Ok(());
                            }
                        }
                    }
                    StreamCtl::None => {
                        if let Some(reply) = step.reply {
                            if out.send(WsMessage::Text(reply)).await.is_err() {
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
        loop {
            tick.tick().await;
            // Capture off the runtime's worker threads — X11 GetImage is blocking. Awaiting
            // the blocking task here serializes captures: at most one is in flight per
            // screencast, and MissedTickBehavior::Skip drops ticks that arrive while busy,
            // so slow capture can't pile up blocking tasks/buffers behind the X11 mutex (#137).
            let exec = exec.clone();
            let scope = scope.clone();
            let img = match tokio::task::spawn_blocking(move || exec.capture(&scope, encode_opts))
                .await
            {
                Ok(Ok(img)) => img,
                _ => break, // capture failed or the blocking pool is gone
            };
            // Keep the resume entry alive on EVERY capture tick, before any
            // suppression/delivery decision: idle-suppressed and dropped-on-full
            // frames never `record`, and a healthy stream over a static screen must
            // not lose resumability after STREAM_RESUME_TTL.
            streams.touch(&spec.stream_id, generation);
            let hash = fnv1a(img.data_base64.as_bytes());
            if last_hash == Some(hash) {
                continue; // unchanged frame — skip
            }
            let frame = protocol::stream_frame(
                &spec.stream_id,
                seq,
                protocol::ImageRef {
                    data: img.data_base64,
                    w: img.w,
                    h: img.h,
                    scope: spec.scope.clone(),
                    format: img.format.as_str().to_string(),
                },
            );
            let Ok(text) = serde_json::to_string(&frame) else {
                break;
            };
            // Drop the frame if the client is behind (bounded memory); a live preview
            // wants recent frames, not a backlog. Only stop if the client is gone.
            // Commit last_hash/seq ONLY on a successful send: a frame dropped on Full
            // must be re-attempted next tick, else idle-suppression would treat the
            // never-delivered change as "already sent" and leave the view stale forever.
            match out.try_send(WsMessage::Text(text)) {
                Ok(()) => {
                    last_hash = Some(hash);
                    seq += 1;
                    streams.record(&spec.stream_id, seq, generation);
                }
                Err(mpsc::error::TrySendError::Full(_)) => continue,
                Err(mpsc::error::TrySendError::Closed(_)) => break,
            }
        }
    })
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
        loop {
            tick.tick().await;
            // Capture + diff + encode all run off the runtime's worker threads (X11
            // GetImage is blocking; tile diff/encode is real CPU work on a limited
            // guest). Awaiting serializes ticks like the full-frame path (#137). The
            // state moves through the closure and back, like serve() does its Session.
            let exec2 = exec.clone();
            let scope2 = scope.clone();
            let st = state;
            let (st, outcome) = match tokio::task::spawn_blocking(move || {
                let outcome = exec2
                    .capture_raw(&scope2, max_long_edge)
                    .and_then(|(rgb, w, h)| {
                        let frame = st.tick(&rgb, w, h, encode_opts)?;
                        Ok((frame, rgb, w, h))
                    });
                (st, outcome)
            })
            .await
            {
                Ok(pair) => pair,
                Err(_) => break, // the blocking pool is gone
            };
            state = st;
            let Ok((frame, rgb, w, h)) = outcome else {
                break; // capture or encode failed
            };
            // Keep the resume entry alive on EVERY capture tick (see the full-frame
            // loop): suppressed/dropped frames never `record`.
            streams.touch(&spec.stream_id, generation);
            let (msg, was_key) = match frame {
                DeltaFrame::Unchanged => continue, // idle suppression — no message
                DeltaFrame::Key(img) => (
                    protocol::stream_frame(
                        &spec.stream_id,
                        seq,
                        protocol::ImageRef {
                            data: img.data_base64,
                            w: img.w,
                            h: img.h,
                            scope: spec.scope.clone(),
                            format: img.format.as_str().to_string(),
                        },
                    ),
                    true,
                ),
                DeltaFrame::Tiles(tiles) => (
                    protocol::stream_tiles_frame(
                        &spec.stream_id,
                        seq,
                        tiles
                            .into_iter()
                            .map(|t| protocol::TileRef {
                                x: t.x,
                                y: t.y,
                                w: t.w,
                                h: t.h,
                                data: t.data_base64,
                            })
                            .collect(),
                    ),
                    false,
                ),
            };
            let Ok(text) = serde_json::to_string(&msg) else {
                break;
            };
            // Commit the baseline/cadence/seq ONLY on a successful send (mirrors the
            // full-frame loop's last_hash semantics): a frame dropped on Full must be
            // re-diffed against the SAME baseline next tick, or its tiles would be
            // treated as already delivered and the client's view would stay stale.
            match out.try_send(WsMessage::Text(text)) {
                Ok(()) => {
                    state.commit(rgb, w, h, was_key);
                    seq += 1;
                    streams.record(&spec.stream_id, seq, generation);
                }
                Err(mpsc::error::TrySendError::Full(_)) => continue,
                Err(mpsc::error::TrySendError::Closed(_)) => break,
            }
        }
    })
}

/// FNV-1a 64-bit hash — used to detect unchanged frames cheaply.
fn fnv1a(bytes: &[u8]) -> u64 {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in bytes {
        hash ^= b as u64;
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash
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
    fn loopback_detection() {
        assert!(is_loopback("127.0.0.1:8765"));
        assert!(is_loopback("localhost:8765"));
        assert!(is_loopback("[::1]:8765"));
        assert!(!is_loopback("0.0.0.0:8765"));
        assert!(!is_loopback("10.0.0.5:8765"));
    }

    #[test]
    fn fnv1a_distinguishes_and_repeats() {
        assert_eq!(fnv1a(b"frame-a"), fnv1a(b"frame-a"));
        assert_ne!(fnv1a(b"frame-a"), fnv1a(b"frame-b"));
    }

    const HELLO: &str = r#"{"type":"hello","v":0,"client":{"name":"t","version":"0"}}"#;

    /// A test server that keeps accepting connections — every `serve` shares one
    /// executor and the GIVEN screencast semaphore / resume registry, like the real
    /// main loop — so reconnect/saturation tests can shape the shared state.
    async fn spawn_server_with(
        screencasts: Arc<Semaphore>,
        streams: Arc<StreamRegistry>,
    ) -> std::net::SocketAddr {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let exec: Arc<dyn Executor> = Arc::new(executor::VirtualExecutor::default());
        tokio::spawn(async move {
            loop {
                let (stream, _) = listener.accept().await.unwrap();
                let exec = exec.clone();
                let screencasts = screencasts.clone();
                let streams = streams.clone();
                tokio::spawn(async move {
                    let _ = serve(stream, exec, None, screencasts, streams).await;
                });
            }
        });
        addr
    }

    async fn spawn_server() -> std::net::SocketAddr {
        spawn_server_with(
            Arc::new(Semaphore::new(MAX_SCREENCASTS)),
            Arc::new(StreamRegistry::default()),
        )
        .await
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
            r#"{"type":"action","call_id":"sc1","action":{"verb":"start_screencast","fps":50}}"#
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
                    data_base64: format!("f{n}"),
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
            r#"{"type":"action","call_id":"sc1","action":{"verb":"start_screencast","fps":50}}"#
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
            r#"{"type":"action","call_id":"sc2","action":{"verb":"start_screencast","fps":50,"resume_stream":"sc1"}}"#
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
            r#"{"type":"action","call_id":"scF","action":{"verb":"start_screencast","fps":50,"resume_stream":"ghost"}}"#
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
        let addr = spawn_server_with(screencasts, streams).await;

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
