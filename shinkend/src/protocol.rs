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
    /// Image codecs this runtime can encode (`png` / `jpeg`). A client must not request
    /// a `format` outside this list. Defaults to png-only when absent so a welcome from
    /// a pre-negotiation runtime still parses (those only ever encoded PNG).
    #[serde(default = "default_image_formats")]
    pub image_formats: Vec<String>,
    /// Whether this runtime can deliver image-bearing `observation`s as binary
    /// WebSocket messages (`u32 LE header_len | JSON header | raw codec payload`)
    /// when the client opts in via `hello.accept.binary_frames`. Defaults false when
    /// absent so a welcome from a pre-binary runtime still parses (those only ever
    /// sent base64-in-JSON text frames).
    #[serde(default)]
    pub binary_frames: bool,
}

fn default_image_formats() -> Vec<String> {
    vec!["png".to_string()]
}

/// An image carried in an `observation` (base64 image bytes + codec + dimensions).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ImageRef {
    #[serde(rename = "ref")]
    pub data: String,
    pub w: u16,
    pub h: u16,
    pub scope: String,
    /// Codec of `data`: `png` (default/back-compatible) or `jpeg`. A client that ignores
    /// this still gets a valid image (PNG remains the default when no `format` is sent).
    #[serde(default = "default_image_format")]
    pub format: String,
}

fn default_image_format() -> String {
    "png".to_string()
}

/// One changed tile in a dirty-tile delta frame (`tiles` on an `observation`).
/// Coordinates are in the delivered (post-downscale) resolution of the stream's last
/// full keyframe; `ref` is the tile's base64 image, encoded per the stream's
/// format/quality (the tile carries no codec field — the keyframe's `format` governs).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TileRef {
    pub x: u32,
    pub y: u32,
    pub w: u16,
    pub h: u16,
    #[serde(rename = "ref")]
    pub data: String,
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
        /// Dirty-tile delta frame (`start_screencast` with `delta`): only the tiles
        /// that changed since the previous delivered frame, INSTEAD of `image`.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        tiles: Option<Vec<TileRef>>,
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
        // Codec negotiation: advertise what encode_frame can actually produce, so a
        // client can reject an unsupported `format` before sending it.
        image_formats: ["png", "jpeg"].iter().map(|s| s.to_string()).collect(),
        // Binary media framing: opt-in per session via hello.accept.binary_frames.
        binary_frames: true,
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
        tiles: None,
    }
}

/// Build one server-pushed dirty-tile delta frame: only the changed tiles, no `image`.
pub fn stream_tiles_frame(stream_id: &str, seq: u64, tiles: Vec<TileRef>) -> Message {
    Message::Observation {
        obs_id: format!("{stream_id}-{seq}"),
        cause: None,
        stream: Some(stream_id.to_string()),
        seq: Some(seq),
        image: None,
        tiles: Some(tiles),
    }
}

// ---- binary media frames ----
//
// A session that negotiated binary framing (`hello.accept.binary_frames` accepted by a
// runtime advertising `capabilities.binary_frames`) receives every image-bearing
// `observation` — one-shot screenshots, screencast frames, dirty-tile delta frames —
// as ONE WebSocket **Binary** message instead of base64-in-JSON text:
//
//   u32 LE header_len | JSON header | payload area (raw codec bytes, concatenated)
//
// The header is the observation JSON with each image/tile `ref` (base64 string)
// replaced by `off`/`len` — byte offsets into the payload area (relative to its
// start, i.e. byte `4 + header_len` of the message). Everything else on the wire
// (hello/welcome, acks, results, queries, non-image observations) stays JSON text.
// This removes the ~33% base64 inflation and the megabyte-JSON-string parse from
// the hot media path. See `schema/aci.schema.json` `$defs.BinaryFrameHeader`.

/// Metadata for the single image of a binary `observation` frame.
pub struct BinaryImageMeta<'a> {
    pub w: u16,
    pub h: u16,
    pub scope: &'a str,
    pub format: &'a str,
}

/// One tile of a binary dirty-tile `observation` frame.
pub struct BinaryTileRef<'a> {
    pub x: u32,
    pub y: u32,
    pub w: u16,
    pub h: u16,
    pub data: &'a [u8],
}

/// Assemble `u32 LE header_len | header JSON | payloads…` into one message body.
fn assemble_binary(header: &serde_json::Value, payloads: &[&[u8]]) -> Vec<u8> {
    let header = serde_json::to_vec(header).expect("json! header serialization is infallible");
    let total: usize = payloads.iter().map(|p| p.len()).sum();
    let mut out = Vec::with_capacity(4 + header.len() + total);
    out.extend_from_slice(&(header.len() as u32).to_le_bytes());
    out.extend_from_slice(&header);
    for p in payloads {
        out.extend_from_slice(p);
    }
    out
}

/// Build one binary `observation` carrying a full image: a one-shot screenshot when
/// `cause` is set, a screencast (key)frame when `stream`/`seq` are.
pub fn binary_image_frame(
    obs_id: &str,
    cause: Option<&str>,
    stream: Option<&str>,
    seq: Option<u64>,
    meta: BinaryImageMeta<'_>,
    data: &[u8],
) -> Vec<u8> {
    let mut header = serde_json::json!({
        "type": "observation",
        "obs_id": obs_id,
        "image": {
            "off": 0,
            "len": data.len(),
            "w": meta.w,
            "h": meta.h,
            "scope": meta.scope,
            "format": meta.format,
        },
    });
    if let Some(c) = cause {
        header["cause"] = c.into();
    }
    if let Some(s) = stream {
        header["stream"] = s.into();
    }
    if let Some(q) = seq {
        header["seq"] = q.into();
    }
    assemble_binary(&header, &[data])
}

/// Build one binary dirty-tile `observation`: tile payloads are concatenated in tile
/// order; each tile's `off`/`len` locate its bytes within the payload area.
pub fn binary_tiles_frame(stream: &str, seq: u64, tiles: &[BinaryTileRef<'_>]) -> Vec<u8> {
    let mut off = 0usize;
    let tile_meta: Vec<serde_json::Value> = tiles
        .iter()
        .map(|t| {
            let m = serde_json::json!({
                "x": t.x, "y": t.y, "w": t.w, "h": t.h,
                "off": off, "len": t.data.len(),
            });
            off += t.data.len();
            m
        })
        .collect();
    let header = serde_json::json!({
        "type": "observation",
        "obs_id": format!("{stream}-{seq}"),
        "stream": stream,
        "seq": seq,
        "tiles": tile_meta,
    });
    let payloads: Vec<&[u8]> = tiles.iter().map(|t| t.data).collect();
    assemble_binary(&header, &payloads)
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

/// Build a `ready` query result from the guest-side readiness probe (S8): the cheap
/// boot-time signal a client polls instead of pulling full screenshots. A runtime
/// predating this query answers `unknown query: ready` (ok=false), so old servers
/// and old clients are both unaffected — new clients fall back on that error.
pub fn ready_result(call_id: &str, r: crate::executor::Readiness) -> Message {
    ok_result(
        call_id,
        serde_json::json!({
            "ready": r.ready,
            "x11_up": r.x11_up,
            "root_nonblack": r.root_nonblack,
        }),
    )
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
            // Same shape: `ready` is answered by the session from the live executor's
            // readiness probe — never fabricated here.
            "ready" => err_result(
                &call_id,
                "ready is answered by the session from the live executor",
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

    // Contract test: the advertised codecs must equal the schema's ImageFormat enum, so
    // negotiation can never promise a codec the published contract doesn't know.
    #[test]
    fn advertised_image_formats_match_schema() {
        let mut adv = capabilities().image_formats;
        adv.sort();
        assert_eq!(
            adv,
            schema_enum("ImageFormat"),
            "advertised image_formats must equal the schema ImageFormat enum"
        );
    }

    // Back-compat: a welcome from a pre-negotiation runtime (no image_formats field)
    // must parse as png-only — those runtimes only ever encoded PNG.
    #[test]
    fn welcome_without_image_formats_parses_as_png_only() {
        let old = r#"{"type":"welcome","v":0,
            "server":{"name":"shinkend","version":"0.0.0","platform":"linux"},
            "capabilities":{"schema_version":0,"verbs":[],"targets":[],
                            "observation_types":[],"max_long_edge":2576}}"#;
        let msg: Message = serde_json::from_str(old).unwrap();
        match msg {
            Message::Welcome { capabilities, .. } => {
                assert_eq!(capabilities.image_formats, vec!["png".to_string()]);
            }
            other => panic!("expected welcome, got {other:?}"),
        }
    }

    #[test]
    fn tiles_frame_serializes_tiles_without_image() {
        let msg = stream_tiles_frame(
            "s1",
            7,
            vec![TileRef {
                x: 64,
                y: 0,
                w: 64,
                h: 36,
                data: "abc".to_string(),
            }],
        );
        let text = serde_json::to_string(&msg).unwrap();
        let v: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(v["stream"], "s1");
        assert_eq!(v["seq"], 7);
        assert!(
            v.get("image").is_none(),
            "a tiles frame must carry no image"
        );
        assert_eq!(v["tiles"][0]["ref"], "abc");
        assert_eq!(v["tiles"][0]["x"], 64);
        assert_eq!(v["tiles"][0]["h"], 36);
    }

    /// The advertised binary-framing capability must exist in the published schema
    /// (Welcome.capabilities.binary_frames), so negotiation never promises a wire
    /// format the contract doesn't know.
    #[test]
    fn advertised_binary_frames_is_in_schema() {
        assert!(capabilities().binary_frames);
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../schema/aci.schema.json");
        let raw = std::fs::read_to_string(path).expect("read schema/aci.schema.json");
        let schema: serde_json::Value = serde_json::from_str(&raw).expect("parse aci schema");
        assert_eq!(
            schema["$defs"]["Welcome"]["properties"]["capabilities"]["properties"]["binary_frames"]
                ["type"],
            "boolean",
            "schema Welcome.capabilities must define binary_frames"
        );
        assert_eq!(
            schema["$defs"]["Hello"]["properties"]["accept"]["properties"]["binary_frames"]["type"],
            "boolean",
            "schema Hello.accept must define binary_frames"
        );
    }

    // Back-compat: a welcome from a pre-binary runtime (no binary_frames field) must
    // parse as false — those runtimes only ever sent base64-in-JSON text frames.
    #[test]
    fn welcome_without_binary_frames_parses_as_false() {
        let old = r#"{"type":"welcome","v":0,
            "server":{"name":"shinkend","version":"0.0.0","platform":"linux"},
            "capabilities":{"schema_version":0,"verbs":[],"targets":[],
                            "observation_types":[],"max_long_edge":2576}}"#;
        let msg: Message = serde_json::from_str(old).unwrap();
        match msg {
            Message::Welcome { capabilities, .. } => assert!(!capabilities.binary_frames),
            other => panic!("expected welcome, got {other:?}"),
        }
    }

    /// Binary image frame layout: u32 LE header_len | JSON header | raw payload, with
    /// the header carrying off/len instead of a base64 ref.
    #[test]
    fn binary_image_frame_layout_roundtrips() {
        let payload = b"\xFF\xD8raw-jpeg-bytes";
        let frame = binary_image_frame(
            "s1-7",
            None,
            Some("s1"),
            Some(7),
            BinaryImageMeta {
                w: 640,
                h: 400,
                scope: "screen",
                format: "jpeg",
            },
            payload,
        );
        let hlen = u32::from_le_bytes(frame[..4].try_into().unwrap()) as usize;
        let header: serde_json::Value = serde_json::from_slice(&frame[4..4 + hlen]).unwrap();
        assert_eq!(header["type"], "observation");
        assert_eq!(header["stream"], "s1");
        assert_eq!(header["seq"], 7);
        assert_eq!(header["image"]["off"], 0);
        assert_eq!(header["image"]["len"], payload.len());
        assert_eq!(header["image"]["w"], 640);
        assert_eq!(header["image"]["format"], "jpeg");
        assert!(header["image"].get("ref").is_none(), "no base64 in binary");
        assert_eq!(&frame[4 + hlen..], payload, "payload bytes verbatim");
        // total size = 4 + header + payload, nothing else
        assert_eq!(frame.len(), 4 + hlen + payload.len());
    }

    /// A one-shot screenshot frame carries `cause` (no stream/seq).
    #[test]
    fn binary_image_frame_cause_only() {
        let frame = binary_image_frame(
            "obs-c1",
            Some("c1"),
            None,
            None,
            BinaryImageMeta {
                w: 2,
                h: 2,
                scope: "screen",
                format: "png",
            },
            b"png",
        );
        let hlen = u32::from_le_bytes(frame[..4].try_into().unwrap()) as usize;
        let header: serde_json::Value = serde_json::from_slice(&frame[4..4 + hlen]).unwrap();
        assert_eq!(header["cause"], "c1");
        assert!(header.get("stream").is_none());
        assert!(header.get("seq").is_none());
    }

    /// Binary tiles frame: payloads concatenated in tile order, offsets contiguous.
    #[test]
    fn binary_tiles_frame_concatenates_payloads_with_offsets() {
        let frame = binary_tiles_frame(
            "s2",
            3,
            &[
                BinaryTileRef {
                    x: 0,
                    y: 0,
                    w: 64,
                    h: 64,
                    data: b"AAAA",
                },
                BinaryTileRef {
                    x: 64,
                    y: 0,
                    w: 36,
                    h: 64,
                    data: b"BB",
                },
            ],
        );
        let hlen = u32::from_le_bytes(frame[..4].try_into().unwrap()) as usize;
        let header: serde_json::Value = serde_json::from_slice(&frame[4..4 + hlen]).unwrap();
        assert_eq!(header["stream"], "s2");
        assert_eq!(header["seq"], 3);
        let tiles = header["tiles"].as_array().unwrap();
        assert_eq!(tiles.len(), 2);
        assert_eq!(
            (tiles[0]["off"].as_u64(), tiles[0]["len"].as_u64()),
            (Some(0), Some(4))
        );
        assert_eq!(
            (tiles[1]["off"].as_u64(), tiles[1]["len"].as_u64()),
            (Some(4), Some(2))
        );
        assert_eq!(tiles[1]["x"], 64);
        let payload = &frame[4 + hlen..];
        assert_eq!(payload, b"AAAABB");
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

    // The Rust runtime's query vocabulary must not drift from the schema's Query.q enum:
    // every q the session answers ("platform" via respond(), "screen_size"/"ready" via
    // the session) must be in the schema, and vice versa.
    #[test]
    fn query_vocabulary_matches_schema() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../schema/aci.schema.json");
        let raw = std::fs::read_to_string(path).expect("read schema/aci.schema.json");
        let schema: serde_json::Value = serde_json::from_str(&raw).expect("parse aci schema");
        let mut q: Vec<String> = schema["$defs"]["Query"]["properties"]["q"]["enum"]
            .as_array()
            .expect("Query.q enum")
            .iter()
            .map(|x| x.as_str().unwrap().to_string())
            .collect();
        q.sort();
        assert_eq!(q, vec!["platform", "ready", "screen_size"]);
    }

    /// respond() must not fabricate a `ready` answer — only the live session (which
    /// holds the executor) can; same shape as the screen_size refusal.
    #[test]
    fn respond_refuses_ready_like_screen_size() {
        let msg: Message =
            serde_json::from_str(r#"{"type":"query","call_id":"c1","q":"ready"}"#).unwrap();
        match respond(msg) {
            Some(Message::Result { ok, error, .. }) => {
                assert!(!ok);
                assert!(error.unwrap().contains("ready is answered by the session"));
            }
            other => panic!("expected result, got {other:?}"),
        }
    }

    #[test]
    fn ready_result_serializes_the_three_fields() {
        let msg = ready_result(
            "c9",
            crate::executor::Readiness {
                ready: false,
                x11_up: false,
                root_nonblack: None,
            },
        );
        let v: serde_json::Value =
            serde_json::from_str(&serde_json::to_string(&msg).unwrap()).unwrap();
        assert_eq!(v["call_id"], "c9");
        assert_eq!(v["ok"], true); // the QUERY succeeded; readiness is in the value
        assert_eq!(v["value"]["ready"], false);
        assert_eq!(v["value"]["x11_up"], false);
        assert!(v["value"]["root_nonblack"].is_null());
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
