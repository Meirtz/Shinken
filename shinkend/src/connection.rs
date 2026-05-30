//! Per-connection ACI session: the handshake state machine + dev-token auth.
//!
//! The first message on a connection MUST be `hello`; `hello` is accepted exactly
//! once. If a token is configured (`SHINKEND_TOKEN`), the `hello` must carry it.
//! Only after a successful handshake are actions/queries dispatched. This keeps an
//! unauthenticated client from driving the desktop or occupying RPC (#22).

use std::sync::Arc;

use crate::executor::{ActionSpec, Executor};
use crate::protocol::{self, Message};

/// What to do after handling one inbound message.
pub struct Step {
    pub reply: Option<String>,
    pub close: bool,
}

impl Step {
    fn reply(msg: &Message) -> Self {
        Step {
            reply: Some(encode(msg)),
            close: false,
        }
    }
    fn text(text: String) -> Self {
        Step {
            reply: Some(text),
            close: false,
        }
    }
    fn silent() -> Self {
        Step {
            reply: None,
            close: false,
        }
    }
    fn close(text: String) -> Self {
        Step {
            reply: Some(text),
            close: true,
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
            Message::Hello { token, .. } => {
                if let Some(expected) = self.token.as_deref() {
                    if token.as_deref() != Some(expected) {
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
                Step::reply(&dispatch_action(&call_id, action, self.exec.as_ref()))
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

/// Run one `action`; `screenshot` → `observation`, every other verb → `ack`.
fn dispatch_action(call_id: &str, action: serde_json::Value, exec: &dyn Executor) -> Message {
    let spec = match serde_json::from_value::<ActionSpec>(action) {
        Ok(s) => s,
        Err(e) => return ack(call_id, false, Some(format!("bad action: {e}"))),
    };
    if spec.verb == "screenshot" {
        return match exec.screenshot() {
            Ok(img) => Message::Observation {
                obs_id: format!("obs-{call_id}"),
                cause: Some(call_id.to_string()),
                image: Some(protocol::ImageRef {
                    data: img.png_base64,
                    w: img.w,
                    h: img.h,
                    scope: "screen".to_string(),
                }),
            },
            Err(e) => ack(call_id, false, Some(e.to_string())),
        };
    }
    match exec.execute(&spec) {
        Ok(_) => ack(call_id, true, None),
        Err(e) => ack(call_id, false, Some(e.to_string())),
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
}
