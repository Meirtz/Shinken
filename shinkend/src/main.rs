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

use std::sync::Arc;
use std::time::Duration;

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
        let (stream, peer) = listener.accept().await?;
        let Ok(conn_permit) = connections.clone().try_acquire_owned() else {
            eprintln!("connection {peer} rejected: max connections reached ({MAX_CONNECTIONS})");
            continue;
        };
        let exec = exec.clone();
        let token = token.clone();
        let screencasts = screencasts.clone();
        tokio::spawn(async move {
            let _conn_permit = conn_permit;
            if let Err(e) = serve(stream, exec, token, screencasts).await {
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
) -> Result<()> {
    let ws = tokio_tungstenite::accept_async_with_config(stream, Some(ws_config())).await?;
    let (mut tx, mut rx) = ws.split();

    // Single writer behind a BOUNDED channel: replies are sent reliably (awaited),
    // but screencast frames are dropped when the client falls behind (see
    // spawn_screencast) so a slow/stalled reader cannot grow memory without bound.
    let (out, mut out_rx) = mpsc::channel::<WsMessage>(OUTBOUND_CAP);
    let writer: JoinHandle<()> = tokio::spawn(async move {
        while let Some(msg) = out_rx.recv().await {
            if tx.send(msg).await.is_err() {
                break;
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
                let step = session.on_text(&text);
                // wait.ms (#140): sleep (async, bounded by MAX_WAIT_MS) so the ack lands
                // after the delay; yields to other connections, never blocks the runtime.
                if step.delay_ms > 0 {
                    tokio::time::sleep(Duration::from_millis(step.delay_ms)).await;
                }
                match step.stream {
                    StreamCtl::Start(spec) => {
                        if let Some(h) = screencast.take() {
                            h.abort();
                        }
                        match screencasts.clone().try_acquire_owned() {
                            Ok(permit) => {
                                screencast =
                                    Some(spawn_screencast(exec.clone(), out.clone(), spec, permit));
                                if let Some(reply) = step.reply {
                                    if out.send(WsMessage::Text(reply)).await.is_err() {
                                        break Ok(());
                                    }
                                }
                            }
                            Err(_) => {
                                let _ = out
                                    .send(WsMessage::Text(format!(
                                        "{{\"type\":\"ack\",\"call_id\":\"{}\",\"ok\":false,\"error\":\"max concurrent screencasts reached ({MAX_SCREENCASTS})\"}}",
                                        spec.stream_id
                                    )))
                                    .await;
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
    let _ = writer.await;
    outcome
}

/// Stream screencast frames for `spec` into the connection's writer channel until
/// the task is aborted (on `stop_screencast`, a new stream, or disconnect). Frames
/// identical to the previous one are dropped (idle-frame suppression) — the first
/// bandwidth lever; resolution/codec controls follow.
fn spawn_screencast(
    exec: Arc<dyn Executor>,
    out: mpsc::Sender<WsMessage>,
    spec: ScreencastSpec,
    _permit: tokio::sync::OwnedSemaphorePermit,
) -> JoinHandle<()> {
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(Duration::from_secs_f64(1.0 / spec.fps));
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let max_long_edge = spec.max_long_edge;
        let scope = spec.scope.clone();
        let mut seq: u64 = 0;
        let mut last_hash: Option<u64> = None;
        loop {
            tick.tick().await;
            // Capture off the runtime's worker threads — X11 GetImage is blocking. Awaiting
            // the blocking task here serializes captures: at most one is in flight per
            // screencast, and MissedTickBehavior::Skip drops ticks that arrive while busy,
            // so slow capture can't pile up blocking tasks/buffers behind the X11 mutex (#137).
            let exec = exec.clone();
            let scope = scope.clone();
            let img = match tokio::task::spawn_blocking(move || exec.capture(&scope, max_long_edge))
                .await
            {
                Ok(Ok(img)) => img,
                _ => break, // capture failed or the blocking pool is gone
            };
            let hash = fnv1a(img.png_base64.as_bytes());
            if last_hash == Some(hash) {
                continue; // unchanged frame — skip
            }
            last_hash = Some(hash);
            let frame = protocol::stream_frame(
                &spec.stream_id,
                seq,
                protocol::ImageRef {
                    data: img.png_base64,
                    w: img.w,
                    h: img.h,
                    scope: spec.scope.clone(),
                },
            );
            seq += 1;
            let Ok(text) = serde_json::to_string(&frame) else {
                break;
            };
            // Drop the frame if the client is behind (bounded memory); a live preview
            // wants recent frames, not a backlog. Only stop if the client is gone.
            match out.try_send(WsMessage::Text(text)) {
                Ok(()) => {}
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
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let exec: Arc<dyn Executor> = Arc::new(executor::VirtualExecutor::default());
        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let _ = serve(
                stream,
                exec,
                None,
                Arc::new(Semaphore::new(MAX_SCREENCASTS)),
            )
            .await;
        });

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

        use executor::{ActionSpec, CapturedImage};

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
            fn capture(&self, _: &str, _: Option<u32>) -> anyhow::Result<CapturedImage> {
                let n = self.inflight.fetch_add(1, Ordering::SeqCst) + 1;
                self.max.fetch_max(n, Ordering::SeqCst);
                std::thread::sleep(Duration::from_millis(20)); // slow capture
                self.inflight.fetch_sub(1, Ordering::SeqCst);
                Ok(CapturedImage {
                    png_base64: format!("f{n}"),
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
            scope: "screen".into(),
        };
        let handle = spawn_screencast(exec.clone(), out, spec, permit);
        tokio::time::sleep(Duration::from_millis(120)).await; // several ticks
        handle.abort();
        // the loop awaits each blocking capture before the next tick → never concurrent (#137)
        assert_eq!(exec.max.load(Ordering::SeqCst), 1);
    }
}
