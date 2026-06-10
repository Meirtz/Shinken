//! Per-connection ACI session: the handshake state machine + dev-token auth.
//!
//! The first message on a connection MUST be `hello`; `hello` is accepted exactly
//! once. If a token is configured (`SHINKEND_TOKEN`), the `hello` must carry it.
//! Only after a successful handshake are actions/queries dispatched. This keeps an
//! unauthenticated client from driving the desktop or occupying RPC (#22).

use std::sync::Arc;

use crate::executor::{ActionSpec, EncodeOpts, Executor, ImageFormat, DEFAULT_JPEG_QUALITY};
use crate::protocol::{self, Message};

/// Constant-time byte-string equality for bearer-token comparison (#135), so token auth
/// doesn't leak a match prefix via early-exit timing. The length check can leak length,
/// which is acceptable for fixed-length dev tokens; the token *value* is the secret.
fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff: u8 = 0;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

/// A screencast the runtime should start streaming (see [`StreamCtl`]).
#[derive(Debug, Clone, PartialEq)]
pub struct ScreencastSpec {
    /// Stream id (the `start_screencast` call_id, or the resumed logical stream id);
    /// tags every pushed frame.
    pub stream_id: String,
    /// Target frame rate, clamped to a sane range at parse time.
    pub fps: f64,
    /// Cap each frame's longer edge (px) to save bandwidth; `None` = full resolution.
    pub max_long_edge: Option<u32>,
    /// Wire codec for each frame (`png` lossless / `jpeg` bandwidth lever).
    pub format: ImageFormat,
    /// JPEG quality 1–100 (ignored for PNG).
    pub quality: u8,
    /// Capture region: `screen`, `active_window`, or `window:<id>`.
    pub scope: String,
    /// First frame index; nonzero only when the serve loop resumed a logical stream.
    pub start_seq: u64,
    /// Client-requested logical stream to continue (#56). Resolved against the
    /// server-side stream registry by the serve loop (main.rs) — never here, so the
    /// [`Session`] stays a pure state machine.
    pub resume_stream: Option<String>,
    /// Dirty-tile delta mode (B2): push only changed 64px tiles plus periodic full
    /// keyframes instead of full frames.
    pub delta: bool,
}

/// A side effect on the connection's frame stream, returned alongside a reply.
/// The async serve loop owns the actual streaming task; the [`Session`] stays a
/// pure, synchronous state machine.
#[derive(Debug, Clone, PartialEq)]
pub enum StreamCtl {
    None,
    Start(ScreencastSpec),
    Stop,
}

/// What to do after handling one inbound message.
pub struct Step {
    pub reply: Option<String>,
    pub close: bool,
    pub stream: StreamCtl,
    /// Bounded async delay (ms) the serve loop sleeps before sending this reply — the
    /// `wait` verb's semantics, honored without blocking the runtime (#140).
    pub delay_ms: u64,
}

impl Step {
    fn reply(msg: &Message) -> Self {
        Step {
            reply: Some(encode(msg)),
            close: false,
            stream: StreamCtl::None,
            delay_ms: 0,
        }
    }
    fn text(text: String) -> Self {
        Step {
            reply: Some(text),
            close: false,
            stream: StreamCtl::None,
            delay_ms: 0,
        }
    }
    fn silent() -> Self {
        Step {
            reply: None,
            close: false,
            stream: StreamCtl::None,
            delay_ms: 0,
        }
    }
    fn close(text: String) -> Self {
        Step {
            reply: Some(text),
            close: true,
            stream: StreamCtl::None,
            delay_ms: 0,
        }
    }
}

/// One client connection's ACI state machine.
pub struct Session {
    authed: bool,
    token: Option<String>,
    exec: Arc<dyn Executor>,
}

impl Session {
    pub fn new(token: Option<String>, exec: Arc<dyn Executor>) -> Self {
        Self {
            authed: false,
            token,
            exec,
        }
    }

    pub fn is_authenticated(&self) -> bool {
        self.authed
    }

    /// Handle one inbound text frame.
    pub fn on_text(&mut self, text: &str) -> Step {
        let msg: Message = match serde_json::from_str(text) {
            Ok(m) => m,
            Err(e) => {
                return Step::close(protocol::error_result_text(
                    "?",
                    &format!("bad message: {e}"),
                ))
            }
        };
        if self.authed {
            self.on_authed(msg)
        } else {
            self.on_handshake(msg)
        }
    }

    fn on_handshake(&mut self, msg: Message) -> Step {
        match msg {
            Message::Hello { v, token, .. } => {
                if v != 0 {
                    return Step::close(protocol::error_result_text(
                        "?",
                        &format!("unsupported ACI version: {v} (this runtime speaks v0)"),
                    ));
                }
                if let Some(expected) = self.token.as_deref() {
                    if !ct_eq(
                        token.as_deref().unwrap_or("").as_bytes(),
                        expected.as_bytes(),
                    ) {
                        return Step::close(protocol::error_result_text(
                            "?",
                            "authentication required: missing or invalid token",
                        ));
                    }
                }
                self.authed = true;
                Step::reply(&protocol::welcome())
            }
            _ => Step::close(protocol::error_result_text(
                "?",
                "handshake required: the first message must be `hello`",
            )),
        }
    }

    fn on_authed(&mut self, msg: Message) -> Step {
        match msg {
            Message::Hello { .. } => {
                Step::text(protocol::error_result_text("?", "already authenticated"))
            }
            Message::Action { call_id, action } => {
                let (reply, stream, delay_ms) =
                    dispatch_action(&call_id, action, self.exec.as_ref());
                Step {
                    reply: Some(encode(&reply)),
                    close: false,
                    stream,
                    delay_ms,
                }
            }
            // screen_size must report the real executor geometry, not a stub (#138)
            Message::Query { call_id, q } if q == "screen_size" => {
                let (w, h) = self.exec.screen_size();
                Step::reply(&protocol::screen_size_result(&call_id, w, h))
            }
            other => match protocol::respond(other) {
                Some(r) => Step::reply(&r),
                None => Step::silent(),
            },
        }
    }
}

fn encode(msg: &Message) -> String {
    serde_json::to_string(msg).unwrap_or_else(|e| {
        protocol::error_result_text("?", &format!("failed to encode reply: {e}"))
    })
}

/// Lowest/highest screencast frame rate we will honor (frames/sec).
const FPS_MIN: f64 = 0.1;
const FPS_MAX: f64 = 30.0;
const FPS_DEFAULT: f64 = 5.0;

/// Whether a capture scope is well-formed per the ACI Scope contract: `screen`,
/// `active_window`, or `window:<id>` (id = decimal or `0x`-hex). Malformed scopes are
/// rejected rather than silently falling back to full-screen capture (#139).
fn valid_scope(s: &str) -> bool {
    if s == "screen" || s == "active_window" {
        return true;
    }
    // The id must parse as a real u32 window handle — not merely "all digits". An
    // out-of-u32-range id (window:4294967296) would otherwise pass here and then
    // silently fall back to full-screen capture in parse_scope, defeating #139.
    match s.strip_prefix("window:") {
        Some(id) => match id.strip_prefix("0x") {
            Some(hex) => u32::from_str_radix(hex, 16).is_ok(),
            None => id.parse::<u32>().is_ok(),
        },
        None => false,
    }
}

/// Clamp a requested `max_long_edge` to the negotiated maximum (#141), so a client can't
/// request a larger capture than the handshake advertised.
fn clamp_long_edge(m: Option<u32>) -> Option<u32> {
    m.map(|v| v.min(protocol::MAX_LONG_EDGE))
}

/// Maximum a `wait` action will sleep (ms) — bounds a client from parking a connection.
const MAX_WAIT_MS: u64 = 10_000;

/// Run one `action`. `screenshot` → one-shot `observation`; `start_screencast`/
/// `stop_screencast` → `ack` plus a [`StreamCtl`]; `wait` → `ack` plus a bounded delay
/// (ms) the serve loop sleeps before replying; every other verb → `ack`. The third tuple
/// element is that delay (0 for everything but `wait`).
fn dispatch_action(
    call_id: &str,
    action: serde_json::Value,
    exec: &dyn Executor,
) -> (Message, StreamCtl, u64) {
    let spec = match serde_json::from_value::<ActionSpec>(action) {
        Ok(s) => s,
        Err(e) => {
            return (
                ack(call_id, false, Some(format!("bad action: {e}"))),
                StreamCtl::None,
                0,
            )
        }
    };
    let scope = spec.scope.clone().unwrap_or_else(|| "screen".to_string());
    // Reject a provided-but-malformed capture scope instead of silently capturing the
    // full screen — a privacy/contract issue (#139). (An absent scope defaults to "screen".)
    if spec.scope.is_some()
        && matches!(spec.verb.as_str(), "screenshot" | "start_screencast")
        && !valid_scope(&scope)
    {
        return (
            ack(
                call_id,
                false,
                Some(format!("invalid capture scope: {scope}")),
            ),
            StreamCtl::None,
            0,
        );
    }
    // Resolve the wire codec + quality for the verbs that consume them (same verb gating
    // as the scope check above — an invalid `format` on e.g. `stop_screencast` must not
    // nack the stop). Quality is REJECTED outside the schema's 1–100, not silently
    // clamped: the runtime must not accept what the published contract rejects.
    let is_capture = matches!(spec.verb.as_str(), "screenshot" | "start_screencast");
    let (format, quality) = if is_capture {
        let format = match ImageFormat::parse(spec.format.as_deref()) {
            Ok(f) => f,
            Err(e) => return (ack(call_id, false, Some(e.to_string())), StreamCtl::None, 0),
        };
        let quality = match spec.quality {
            None => DEFAULT_JPEG_QUALITY,
            Some(q) if (1..=100).contains(&q) => q,
            Some(q) => {
                return (
                    ack(
                        call_id,
                        false,
                        Some(format!("quality out of range 1-100: {q}")),
                    ),
                    StreamCtl::None,
                    0,
                )
            }
        };
        (format, quality)
    } else {
        (ImageFormat::Png, DEFAULT_JPEG_QUALITY) // unused by non-capture verbs
    };
    match spec.verb.as_str() {
        "screenshot" => {
            let opts = EncodeOpts {
                max_long_edge: clamp_long_edge(spec.max_long_edge),
                format,
                quality,
            };
            let msg = match exec.capture(&scope, opts) {
                Ok(img) => Message::Observation {
                    obs_id: format!("obs-{call_id}"),
                    cause: Some(call_id.to_string()),
                    stream: None,
                    seq: None,
                    image: Some(protocol::ImageRef {
                        data: img.data_base64,
                        w: img.w,
                        h: img.h,
                        scope,
                        format: img.format.as_str().to_string(),
                    }),
                    tiles: None,
                },
                Err(e) => ack(call_id, false, Some(e.to_string())),
            };
            (msg, StreamCtl::None, 0)
        }
        "start_screencast" => {
            let fps = spec.fps.unwrap_or(FPS_DEFAULT).clamp(FPS_MIN, FPS_MAX);
            let delta = spec.delta.unwrap_or(false);
            // A delta stream needs raw (pre-encode) capture to diff tiles; nack a
            // backend that can't do it rather than ack and then die frameless.
            if delta && !exec.supports_raw_capture() {
                return (
                    ack(
                        call_id,
                        false,
                        Some(format!(
                            "delta screencast not supported by the {} backend",
                            exec.backend()
                        )),
                    ),
                    StreamCtl::None,
                    0,
                );
            }
            let cast = ScreencastSpec {
                stream_id: call_id.to_string(),
                fps,
                // Default an unspecified cap to the negotiated max so a busy full-res
                // desktop can't pin OUTBOUND_CAP × multi-MB frames in the writer queue.
                max_long_edge: clamp_long_edge(spec.max_long_edge)
                    .or(Some(protocol::MAX_LONG_EDGE)),
                format,
                quality,
                scope,
                start_seq: 0,
                resume_stream: spec.resume_stream,
                delta,
            };
            (ack(call_id, true, None), StreamCtl::Start(cast), 0)
        }
        "stop_screencast" => (ack(call_id, true, None), StreamCtl::Stop, 0),
        // wait: ack, but have the serve loop sleep first (bounded) so the ack lands after
        // the delay — real wait.ms semantics, async so it never blocks the runtime (#140)
        "wait" => (
            ack(call_id, true, None),
            StreamCtl::None,
            spec.ms.unwrap_or(0).min(MAX_WAIT_MS),
        ),
        _ => match exec.execute(&spec) {
            Ok(_) => (ack(call_id, true, None), StreamCtl::None, 0),
            Err(e) => (ack(call_id, false, Some(e.to_string())), StreamCtl::None, 0),
        },
    }
}

fn ack(call_id: &str, ok: bool, error: Option<String>) -> Message {
    Message::Ack {
        call_id: call_id.to_string(),
        ok,
        error,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::executor::VirtualExecutor;

    fn session(token: Option<&str>) -> Session {
        Session::new(
            token.map(str::to_string),
            Arc::new(VirtualExecutor::default()),
        )
    }

    /// A minimal executor with a fixed geometry, to prove screen_size routes through it.
    struct SizedExec(u16, u16);
    impl Executor for SizedExec {
        fn execute(&self, _action: &ActionSpec) -> anyhow::Result<String> {
            Ok("ok".into())
        }
        fn backend(&self) -> &'static str {
            "sized-test"
        }
        fn screen_size(&self) -> (u16, u16) {
            (self.0, self.1)
        }
    }

    const HELLO: &str = r#"{"type":"hello","v":0,"client":{"name":"t","version":"0"}}"#;

    #[test]
    fn screen_size_reports_executor_geometry() {
        let mut s = Session::new(None, Arc::new(SizedExec(640, 480)));
        s.on_text(HELLO);
        let reply = s
            .on_text(r#"{"type":"query","call_id":"c1","q":"screen_size"}"#)
            .reply
            .unwrap();
        // reflects the executor's geometry, not the 1280x800 stub (#138)
        assert!(reply.contains("\"w\":640") && reply.contains("\"h\":480"));
    }

    #[test]
    fn first_message_must_be_hello() {
        let mut s = session(None);
        let step = s.on_text(r#"{"type":"ping"}"#);
        assert!(step.close);
        assert!(!s.is_authenticated());
    }

    #[test]
    fn hello_authenticates_and_welcomes() {
        let mut s = session(None);
        let step = s.on_text(HELLO);
        assert!(!step.close && s.is_authenticated());
        assert!(step.reply.unwrap().contains("\"type\":\"welcome\""));
    }

    #[test]
    fn token_required_when_configured() {
        let mut s = session(Some("secret"));
        assert!(s.on_text(HELLO).close && !s.is_authenticated());

        let mut s = session(Some("secret"));
        let ok = s.on_text(
            r#"{"type":"hello","v":0,"client":{"name":"t","version":"0"},"token":"secret"}"#,
        );
        assert!(!ok.close && s.is_authenticated());
    }

    #[test]
    fn wrong_token_is_rejected() {
        let mut s = session(Some("secret"));
        let step = s.on_text(
            r#"{"type":"hello","v":0,"client":{"name":"t","version":"0"},"token":"wrong"}"#,
        );
        assert!(step.close && !s.is_authenticated());
    }

    #[test]
    fn ct_eq_matches_only_equal_byte_strings() {
        assert!(ct_eq(b"abc123", b"abc123"));
        assert!(!ct_eq(b"abc123", b"abc124")); // same length, one byte differs
        assert!(!ct_eq(b"abc", b"abcdef")); // different length
        assert!(!ct_eq(b"", b"x"));
        assert!(ct_eq(b"", b"")); // empty == empty
    }

    #[test]
    fn valid_scope_accepts_known_forms_only() {
        for s in ["screen", "active_window", "window:42", "window:0x1a2b"] {
            assert!(valid_scope(s), "{s} should be valid");
        }
        for s in [
            "bogus",
            "window:",
            "window:xyz",
            "window:0x",
            "",
            "Screen",
            // out-of-u32-range ids must be rejected, not silently full-screened (#139)
            "window:4294967296",
            "window:0x1ffffffff",
        ] {
            assert!(!valid_scope(s), "{s} should be invalid");
        }
    }

    #[test]
    fn clamp_long_edge_caps_at_negotiated_max() {
        assert_eq!(clamp_long_edge(Some(99999)), Some(protocol::MAX_LONG_EDGE));
        assert_eq!(clamp_long_edge(Some(100)), Some(100)); // under the cap, unchanged
        assert_eq!(clamp_long_edge(None), None);
    }

    #[test]
    fn invalid_capture_scope_is_rejected() {
        let mut s = session(None);
        s.on_text(HELLO); // authenticate first
        let step = s.on_text(
            r#"{"type":"action","call_id":"c1","action":{"verb":"screenshot","scope":"bogus"}}"#,
        );
        let reply = step.reply.unwrap();
        assert!(reply.contains("\"ok\":false") && reply.contains("invalid capture scope"));
    }

    #[test]
    fn format_and_quality_are_gated_to_capture_verbs() {
        let mut s = session(None);
        s.on_text(HELLO);
        // Invalid codec on a capture verb → nack before any capture.
        let step = s.on_text(
            r#"{"type":"action","call_id":"c1","action":{"verb":"screenshot","format":"webp"}}"#,
        );
        assert!(step.reply.unwrap().contains("unsupported image format"));
        // Out-of-range quality is REJECTED (schema: 1–100), not clamped.
        let step = s.on_text(
            r#"{"type":"action","call_id":"c2","action":{"verb":"screenshot","format":"jpeg","quality":0}}"#,
        );
        assert!(step.reply.unwrap().contains("quality out of range"));
        // An invalid format on a NON-capture verb must not nack it: the stop (and its
        // StreamCtl) still happens — same gating as the scope check.
        let step = s.on_text(
            r#"{"type":"action","call_id":"c3","action":{"verb":"stop_screencast","format":"webp"}}"#,
        );
        assert!(step.reply.unwrap().contains("\"ok\":true"));
        assert!(matches!(step.stream, StreamCtl::Stop));
    }

    #[test]
    fn wait_action_acks_with_a_bounded_delay() {
        let mut s = session(None);
        s.on_text(HELLO);
        let step =
            s.on_text(r#"{"type":"action","call_id":"c1","action":{"verb":"wait","ms":500}}"#);
        assert_eq!(step.delay_ms, 500); // serve loop sleeps this before the ack (#140)
        assert!(step.reply.unwrap().contains("\"ok\":true"));
        // an absurd wait is clamped to MAX_WAIT_MS, not honored verbatim
        let big =
            s.on_text(r#"{"type":"action","call_id":"c2","action":{"verb":"wait","ms":99999999}}"#);
        assert_eq!(big.delay_ms, MAX_WAIT_MS);
    }

    #[test]
    fn valid_window_scope_is_not_rejected_as_invalid() {
        let mut s = session(None);
        s.on_text(HELLO);
        // a well-formed window:<id> must not hit the invalid-scope rejection
        let step = s.on_text(
            r#"{"type":"action","call_id":"c2","action":{"verb":"screenshot","scope":"window:0x1a"}}"#,
        );
        assert!(!step.reply.unwrap().contains("invalid capture scope"));
    }

    #[test]
    fn non_v0_hello_is_rejected() {
        let mut s = session(None);
        let step = s.on_text(r#"{"type":"hello","v":1,"client":{"name":"t","version":"0"}}"#);
        assert!(step.close && !s.is_authenticated());
        assert!(step.reply.unwrap().contains("unsupported ACI version"));
    }

    #[test]
    fn action_dispatches_only_after_auth() {
        let mut s = session(None);
        s.on_text(HELLO);
        let step = s.on_text(
            r#"{"type":"action","call_id":"c1","action":{"verb":"click","target":{"kind":"point_px","x":1,"y":2}}}"#,
        );
        assert!(step.reply.unwrap().contains("\"type\":\"ack\""));
    }

    #[test]
    fn second_hello_is_rejected_not_reauth() {
        let mut s = session(None);
        s.on_text(HELLO);
        let step = s.on_text(HELLO);
        assert!(!step.close);
        assert!(step.reply.unwrap().contains("already authenticated"));
    }

    #[test]
    fn start_screencast_acks_and_requests_a_stream() {
        let mut s = session(None);
        s.on_text(HELLO);
        let step = s.on_text(
            r#"{"type":"action","call_id":"sc1","action":{"verb":"start_screencast","fps":12,"max_long_edge":640,"scope":"active_window"}}"#,
        );
        assert!(step.reply.unwrap().contains("\"type\":\"ack\""));
        match step.stream {
            StreamCtl::Start(spec) => {
                assert_eq!(spec.stream_id, "sc1");
                assert_eq!(spec.fps, 12.0);
                assert_eq!(spec.max_long_edge, Some(640));
                assert_eq!(spec.scope, "active_window");
                assert_eq!(spec.start_seq, 0); // fresh stream until main.rs resumes one
                assert_eq!(spec.resume_stream, None);
            }
            other => panic!("expected Start, got {other:?}"),
        }
    }

    #[test]
    fn start_screencast_passes_resume_stream_through() {
        let mut s = session(None);
        s.on_text(HELLO);
        // The Session must NOT resolve the resume itself (it has no registry) — it
        // forwards the requested id on the spec for the serve loop to resolve.
        let step = s.on_text(
            r#"{"type":"action","call_id":"sc9","action":{"verb":"start_screencast","resume_stream":"sc1"}}"#,
        );
        match step.stream {
            StreamCtl::Start(spec) => {
                assert_eq!(spec.stream_id, "sc9");
                assert_eq!(spec.start_seq, 0);
                assert_eq!(spec.resume_stream.as_deref(), Some("sc1"));
            }
            other => panic!("expected Start, got {other:?}"),
        }
    }

    #[test]
    fn start_screencast_delta_flag_reaches_the_spec() {
        let mut s = session(None);
        s.on_text(HELLO);
        let step = s.on_text(
            r#"{"type":"action","call_id":"sc1","action":{"verb":"start_screencast","delta":true}}"#,
        );
        assert!(step.reply.unwrap().contains("\"ok\":true"));
        match step.stream {
            StreamCtl::Start(spec) => assert!(spec.delta),
            other => panic!("expected Start, got {other:?}"),
        }
        // and an omitted delta stays full-frame mode
        let step =
            s.on_text(r#"{"type":"action","call_id":"sc2","action":{"verb":"start_screencast"}}"#);
        match step.stream {
            StreamCtl::Start(spec) => assert!(!spec.delta),
            other => panic!("expected Start, got {other:?}"),
        }
    }

    #[test]
    fn delta_screencast_is_nacked_on_a_backend_without_raw_capture() {
        // SizedExec keeps the trait defaults: no capture_raw → supports_raw_capture()
        // is false → the delta request must be nacked, not acked-then-frameless.
        let mut s = Session::new(None, Arc::new(SizedExec(640, 480)));
        s.on_text(HELLO);
        let step = s.on_text(
            r#"{"type":"action","call_id":"sc1","action":{"verb":"start_screencast","delta":true}}"#,
        );
        let reply = step.reply.unwrap();
        assert!(reply.contains("\"ok\":false") && reply.contains("delta screencast not supported"));
        assert_eq!(step.stream, StreamCtl::None);
    }

    #[test]
    fn screencast_fps_is_clamped() {
        let mut s = session(None);
        s.on_text(HELLO);
        let step = s.on_text(
            r#"{"type":"action","call_id":"sc1","action":{"verb":"start_screencast","fps":9000}}"#,
        );
        match step.stream {
            StreamCtl::Start(spec) => assert_eq!(spec.fps, 30.0),
            other => panic!("expected Start, got {other:?}"),
        }
    }

    #[test]
    fn stop_screencast_acks_and_requests_stop() {
        let mut s = session(None);
        s.on_text(HELLO);
        let step =
            s.on_text(r#"{"type":"action","call_id":"sc2","action":{"verb":"stop_screencast"}}"#);
        assert!(step.reply.unwrap().contains("\"ok\":true"));
        assert_eq!(step.stream, StreamCtl::Stop);
    }
}
