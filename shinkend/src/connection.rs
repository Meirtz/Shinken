//! Per-connection ACI session: the handshake state machine + dev-token auth.
//!
//! The first message on a connection MUST be `hello`; `hello` is accepted exactly
//! once. If a token is configured (`SHINKEND_TOKEN`), the `hello` must carry it.
//! Only after a successful handshake are actions/queries dispatched. This keeps an
//! unauthenticated client from driving the desktop or occupying RPC (#22).

use std::sync::Arc;

use crate::executor::{ActionSpec, Executor};
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
    /// Stream id (the `start_screencast` call_id); tags every pushed frame.
    pub stream_id: String,
    /// Target frame rate, clamped to a sane range at parse time.
    pub fps: f64,
    /// Cap each frame's longer edge (px) to save bandwidth; `None` = full resolution.
    pub max_long_edge: Option<u32>,
    /// Capture region: `screen`, `active_window`, or `window:<id>`.
    pub scope: String,
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
}

impl Step {
    fn reply(msg: &Message) -> Self {
        Step {
            reply: Some(encode(msg)),
            close: false,
            stream: StreamCtl::None,
        }
    }
    fn text(text: String) -> Self {
        Step {
            reply: Some(text),
            close: false,
            stream: StreamCtl::None,
        }
    }
    fn silent() -> Self {
        Step {
            reply: None,
            close: false,
            stream: StreamCtl::None,
        }
    }
    fn close(text: String) -> Self {
        Step {
            reply: Some(text),
            close: true,
            stream: StreamCtl::None,
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
                let (reply, stream) = dispatch_action(&call_id, action, self.exec.as_ref());
                Step {
                    reply: Some(encode(&reply)),
                    close: false,
                    stream,
                }
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

/// Run one `action`. `screenshot` → one-shot `observation`; `start_screencast`/
/// `stop_screencast` → `ack` plus a [`StreamCtl`] for the serve loop; every other
/// verb → `ack`.
fn dispatch_action(
    call_id: &str,
    action: serde_json::Value,
    exec: &dyn Executor,
) -> (Message, StreamCtl) {
    let spec = match serde_json::from_value::<ActionSpec>(action) {
        Ok(s) => s,
        Err(e) => {
            return (
                ack(call_id, false, Some(format!("bad action: {e}"))),
                StreamCtl::None,
            )
        }
    };
    let scope = spec.scope.clone().unwrap_or_else(|| "screen".to_string());
    match spec.verb.as_str() {
        "screenshot" => {
            let msg = match exec.capture(&scope, spec.max_long_edge) {
                Ok(img) => Message::Observation {
                    obs_id: format!("obs-{call_id}"),
                    cause: Some(call_id.to_string()),
                    stream: None,
                    seq: None,
                    image: Some(protocol::ImageRef {
                        data: img.png_base64,
                        w: img.w,
                        h: img.h,
                        scope,
                    }),
                },
                Err(e) => ack(call_id, false, Some(e.to_string())),
            };
            (msg, StreamCtl::None)
        }
        "start_screencast" => {
            let fps = spec.fps.unwrap_or(FPS_DEFAULT).clamp(FPS_MIN, FPS_MAX);
            let cast = ScreencastSpec {
                stream_id: call_id.to_string(),
                fps,
                max_long_edge: spec.max_long_edge,
                scope,
            };
            (ack(call_id, true, None), StreamCtl::Start(cast))
        }
        "stop_screencast" => (ack(call_id, true, None), StreamCtl::Stop),
        _ => match exec.execute(&spec) {
            Ok(_) => (ack(call_id, true, None), StreamCtl::None),
            Err(e) => (ack(call_id, false, Some(e.to_string())), StreamCtl::None),
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

    const HELLO: &str = r#"{"type":"hello","v":0,"client":{"name":"t","version":"0"}}"#;

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
            }
            other => panic!("expected Start, got {other:?}"),
        }
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
