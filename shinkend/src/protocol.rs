//! ACI v0 wire protocol — the typed messages exchanged with the client/Operator.
//!
//! Mirrors `schema/aci.schema.json`. Messages are JSON, discriminated by `type`.
//! M0 implements the handshake (`hello` → `welcome`), `ping`/`pong`, and
//! `query` (`platform`, `screen_size`). Action execution arrives in M1 (#4).

use serde::{Deserialize, Serialize};

/// ACI schema version this runtime speaks.
pub const SCHEMA_VERSION: u8 = 0;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Client {
    pub name: String,
    pub version: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServerInfo {
    pub name: String,
    pub version: String,
    pub platform: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Capabilities {
    pub schema_version: u8,
    pub verbs: Vec<String>,
    pub targets: Vec<String>,
    pub observation_types: Vec<String>,
    pub max_long_edge: u32,
}

/// An image carried in an `observation` (base64 PNG + dimensions).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ImageRef {
    #[serde(rename = "ref")]
    pub data: String,
    pub w: u16,
    pub h: u16,
    pub scope: String,
}

/// One ACI message. `#[serde(tag = "type")]` gives the `{"type": "..."}` discriminator.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Message {
    Hello {
        v: u8,
        client: Client,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        accept: Option<serde_json::Value>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        token: Option<String>,
    },
    Welcome {
        v: u8,
        server: ServerInfo,
        capabilities: Capabilities,
    },
    Ping {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        t: Option<f64>,
    },
    Pong {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        t: Option<f64>,
    },
    Query {
        call_id: String,
        q: String,
    },
    Result {
        call_id: String,
        ok: bool,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        value: Option<serde_json::Value>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        error: Option<String>,
    },
    Action {
        call_id: String,
        action: serde_json::Value,
    },
    Ack {
        call_id: String,
        ok: bool,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        error: Option<String>,
    },
    Observation {
        obs_id: String,
        /// The `call_id` this observation answers (set for one-shot `screenshot`),
        /// or `None` for an unsolicited server-pushed frame.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        cause: Option<String>,
        /// The screencast stream id this frame belongs to (server-push only).
        #[serde(default, skip_serializing_if = "Option::is_none")]
        stream: Option<String>,
        /// Monotonic frame index within `stream` (server-push only).
        #[serde(default, skip_serializing_if = "Option::is_none")]
        seq: Option<u64>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        image: Option<ImageRef>,
    },
}

/// The host platform this runtime is executing on.
pub fn platform() -> &'static str {
    if cfg!(target_os = "windows") {
        "windows"
    } else if cfg!(target_os = "macos") {
        "macos"
    } else {
        "linux"
    }
}

/// Negotiated maximum for `max_long_edge` (px) — advertised in the handshake and
/// enforced (clamped) on capture requests so a client can't bypass the limit (#141).
pub const MAX_LONG_EDGE: u32 = 2576;

/// Capabilities advertised in the handshake. M0 advertises the v0 verb set even
/// though execution lands in M1, so clients can negotiate up front.
pub fn capabilities() -> Capabilities {
    Capabilities {
        schema_version: SCHEMA_VERSION,
        verbs: [
            "click",
            "double_click",
            "right_click",
            "move",
            "scroll",
            "type_text",
            "key",
            "screenshot",
            "start_screencast",
            "stop_screencast",
            "wait",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect(),
        // Advertise only what is implemented today. element_ref targets need the
        // a11y engine (M1b); a11y/som observation land later. Honest negotiation.
        targets: ["point_px", "point_norm"]
            .iter()
            .map(|s| s.to_string())
            .collect(),
        observation_types: ["screenshot", "screencast"]
            .iter()
            .map(|s| s.to_string())
            .collect(),
        max_long_edge: MAX_LONG_EDGE,
    }
}

/// Build the `welcome` reply to a client `hello`.
pub fn welcome() -> Message {
    Message::Welcome {
        v: 0,
        server: ServerInfo {
            name: "shinkend".to_string(),
            version: env!("CARGO_PKG_VERSION").to_string(),
            platform: platform().to_string(),
        },
        capabilities: capabilities(),
    }
}

/// Build one server-pushed screencast frame for `stream_id` at index `seq`.
pub fn stream_frame(stream_id: &str, seq: u64, image: ImageRef) -> Message {
    Message::Observation {
        obs_id: format!("{stream_id}-{seq}"),
        cause: None,
        stream: Some(stream_id.to_string()),
        seq: Some(seq),
        image: Some(image),
    }
}

fn ok_result(call_id: &str, value: serde_json::Value) -> Message {
    Message::Result {
        call_id: call_id.to_string(),
        ok: true,
        value: Some(value),
        error: None,
    }
}

/// Build a `screen_size` query result from real executor geometry (#138).
pub fn screen_size_result(call_id: &str, w: u16, h: u16) -> Message {
    ok_result(call_id, serde_json::json!({ "w": w, "h": h }))
}

fn err_result(call_id: &str, error: &str) -> Message {
    Message::Result {
        call_id: call_id.to_string(),
        ok: false,
        value: None,
        error: Some(error.to_string()),
    }
}

/// Serialize an error `result` to JSON text (used when a message fails to parse).
pub fn error_result_text(call_id: &str, error: &str) -> String {
    serde_json::to_string(&err_result(call_id, error)).unwrap_or_else(|_| {
        format!("{{\"type\":\"result\",\"call_id\":\"{call_id}\",\"ok\":false}}")
    })
}

/// Reply to a post-handshake control message (queries, pings). `hello` is deliberately
/// NOT handled here: authentication and the version/token check live in
/// [`crate::connection::Session::on_handshake`], so `respond()` never grants a
/// tokenless welcome even if a future caller wires it onto a connection path.
pub fn respond(msg: Message) -> Option<Message> {
    match msg {
        Message::Ping { t } => Some(Message::Pong { t }),
        Message::Query { call_id, q } => Some(match q.as_str() {
            "platform" => ok_result(&call_id, serde_json::json!(platform())),
            // The live session answers screen_size from real executor geometry
            // (connection::on_authed → screen_size_result, #138); refuse here rather
            // than fabricate a 1280x800 stub that would diverge from the real display.
            "screen_size" => err_result(
                &call_id,
                "screen_size is answered by the session from real executor geometry",
            ),
            other => err_result(&call_id, &format!("unknown query: {other}")),
        }),
        // `hello` → handshake (connection.rs); `action` → Executor (main.rs).
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Read $defs.<name>.enum from the source-of-truth ACI schema as a sorted Vec.
    fn schema_enum(name: &str) -> Vec<String> {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../schema/aci.schema.json");
        let raw = std::fs::read_to_string(path).expect("read schema/aci.schema.json");
        let schema: serde_json::Value = serde_json::from_str(&raw).expect("parse aci schema");
        let mut v: Vec<String> = schema["$defs"][name]["enum"]
            .as_array()
            .unwrap_or_else(|| panic!("$defs.{name}.enum missing"))
            .iter()
            .map(|x| x.as_str().unwrap().to_string())
            .collect();
        v.sort();
        v
    }

    // Contract test (#156): the runtime's advertised capabilities must not drift from the
    // source-of-truth schema/aci.schema.json (the Python side is gated by #89).
    #[test]
    fn advertised_verbs_match_schema() {
        let mut adv = capabilities().verbs;
        adv.sort();
        assert_eq!(
            adv,
            schema_enum("Verb"),
            "advertised verbs must equal the schema Verb enum"
        );
    }

    #[test]
    fn advertised_targets_and_observations_are_in_schema() {
        let target_kinds = schema_enum("TargetKind");
        for t in capabilities().targets {
            assert!(
                target_kinds.contains(&t),
                "advertised target {t} not in schema TargetKind"
            );
        }
        let obs = schema_enum("ObservationType");
        for o in capabilities().observation_types {
            assert!(
                obs.contains(&o),
                "advertised observation {o} not in schema ObservationType"
            );
        }
    }

    #[test]
    fn respond_does_not_welcome_hello() {
        // hello must go through Session::on_handshake (auth + version check), never
        // respond() — which would grant a tokenless, unchecked welcome.
        let hello = r#"{"type":"hello","v":0,"client":{"name":"shinken-py","version":"0.0.0"}}"#;
        let msg: Message = serde_json::from_str(hello).unwrap();
        assert!(respond(msg).is_none(), "respond() must not answer hello");
    }

    #[test]
    fn welcome_advertises_schema_and_verbs() {
        match welcome() {
            Message::Welcome {
                v, capabilities, ..
            } => {
                assert_eq!(v, 0);
                assert_eq!(capabilities.schema_version, SCHEMA_VERSION);
                assert!(capabilities.verbs.iter().any(|s| s == "click"));
            }
            other => panic!("expected welcome, got {other:?}"),
        }
    }

    #[test]
    fn welcome_roundtrips_with_type_tag() {
        let text = serde_json::to_string(&welcome()).unwrap();
        assert!(text.contains("\"type\":\"welcome\""));
        let back: Message = serde_json::from_str(&text).unwrap();
        assert!(matches!(back, Message::Welcome { .. }));
    }

    #[test]
    fn ping_echoes_timestamp_as_pong() {
        let msg: Message = serde_json::from_str(r#"{"type":"ping","t":42.0}"#).unwrap();
        match respond(msg) {
            Some(Message::Pong { t }) => assert_eq!(t, Some(42.0)),
            other => panic!("expected pong, got {other:?}"),
        }
    }

    #[test]
    fn query_platform_is_ok() {
        let msg: Message =
            serde_json::from_str(r#"{"type":"query","call_id":"c1","q":"platform"}"#).unwrap();
        match respond(msg) {
            Some(Message::Result { ok, value, .. }) => {
                assert!(ok);
                assert_eq!(value, Some(serde_json::json!(platform())));
            }
            other => panic!("expected result, got {other:?}"),
        }
    }
}
