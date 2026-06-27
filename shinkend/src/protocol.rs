//! ACI v0 wire protocol — the typed messages exchanged with the client/Operator.
//!
//! Mirrors `schema/aci.schema.json`. Messages are JSON, discriminated by `type`.
//! M0 implements the handshake (`hello` → `welcome`), `ping`/`pong`, and
//! `query` (`platform`, `screen_size`). Action execution arrives in M1 (#4).

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
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
    /// Whether this runtime understands content-negotiated screenshots: it emits
    /// `frame_hash` on screenshot observations, honors `if_none_match` on the
    /// screenshot action, and answers a hash match with the compact `not_modified`
    /// observation. Defaults false when absent (pre-dedup welcome) — a client must
    /// not send `if_none_match` unless this is advertised, because pre-dedup
    /// runtimes reject unknown action fields.
    #[serde(default)]
    pub frame_dedup: bool,
    /// Whether this runtime honors the per-action `observe` argument
    /// (act-returns-observation): a mutating action's ack is followed by a fresh
    /// observation with `cause` = the action's call_id. Defaults false when absent so
    /// a welcome from an older runtime still parses — clients must not send `observe`
    /// to a runtime that doesn't advertise this.
    #[serde(default)]
    pub observe_after_act: bool,
    /// Whether this runtime ships the guest-side structured-observation engine
    /// (`observe` with `structured`, element-ref actions, `invoke_action`/`set_value`).
    /// A capability of the BINARY, like `image_formats`: AT-SPI availability is a
    /// runtime condition and failures are typed errors. Defaults false when absent so
    /// a welcome from a pre-engine runtime still parses.
    #[serde(default)]
    pub structured_observation: bool,
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
    /// One stdout/stderr chunk of a STREAMED exec (`exec` with `stream: true`) —
    /// the JSON-text form (`data_b64`); a binary-negotiated session ships the chunk
    /// bytes raw instead (see [`binary_exec_output`]).
    ExecOutput {
        /// The exec action's call_id.
        cause: String,
        /// Monotonic chunk index across BOTH channels of one exec.
        seq: u64,
        /// `stdout` or `stderr`.
        channel: String,
        /// base64 of the raw chunk bytes (output need not be UTF-8).
        data_b64: String,
    },
    /// The terminal event of a STREAMED exec — exactly one per `stream: true`
    /// action, after the last `exec_output`. Always JSON text.
    ExecExit {
        cause: String,
        /// The child's exit code; `None` when killed by a signal or never spawned.
        exit_code: Option<i32>,
        /// The killing signal, when one did (e.g. 9 after a timeout group-kill).
        #[serde(default, skip_serializing_if = "Option::is_none")]
        signal: Option<i32>,
        timed_out: bool,
        duration_ms: f64,
        /// Whether the stream's total output budget dropped later chunks.
        truncated: bool,
        /// Spawn/runtime failure — the run produced no process; exit_code is null.
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
        /// Mapping from delivered image pixels back into the runtime's global
        /// `point_px` action space. Optional only for compatibility with older or
        /// scoped legacy backends; the X11 screenshot path always emits it.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        display: Option<crate::executor::CoordinateSpace>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        image: Option<ImageRef>,
        /// Dirty-tile delta frame (`start_screencast` with `delta`): only the tiles
        /// that changed since the previous delivered frame, INSTEAD of `image`.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        tiles: Option<Vec<TileRef>>,
        /// Content hash of the captured frame's RAW pixels (codec-independent —
        /// `executor::frame_hash_hex`). Set on one-shot screenshot observations;
        /// the value a client echoes back as the screenshot action's `if_none_match`.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        frame_hash: Option<String>,
        /// Content-negotiated screenshot: the captured frame's hash equals the
        /// request's `if_none_match`, so the payload is omitted — always sent with
        /// `frame_hash` + `cause` and never `image`/`tiles` (and as a JSON text
        /// frame even on a binary session: there is no payload to carry).
        #[serde(default, skip_serializing_if = "Option::is_none")]
        not_modified: Option<bool>,
        /// Live pointer position in global `point_px` action coordinates `[x, y]` — metadata
        /// (captures stay cursor-free; frame-hash dedup depends on that). Set on
        /// one-shot screenshot/not-modified replies when the backend can report it;
        /// omitted on stream frames.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        pointer: Option<(i32, i32)>,
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
            "drag",
            "mouse_down",
            "mouse_up",
            "scroll",
            "type_text",
            "key",
            "screenshot",
            "start_screencast",
            "stop_screencast",
            "wait",
            // structured-observation family (M1b): the guest a11y engine
            "observe",
            "invoke_action",
            "set_value",
            // typed in-guest exec channel (G1): argv-default, shell opt-in,
            // buffered result or streamed exec_output/exec_exit
            "exec",
            // desktop task-parity verbs (G2+G3): clipboard + app launch/activate.
            // Advertised by every shinkend; a backend without an implementation
            // answers with a typed unsupported error at dispatch time.
            "clipboard_get",
            "clipboard_set",
            "launch_app",
            "activate_window",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect(),
        // element_ref targets resolve through the guest observation engine (M1b):
        // pointer verbs click the live element's bbox centre via XTEST.
        targets: ["point_px", "point_norm", "element_ref"]
            .iter()
            .map(|s| s.to_string())
            .collect(),
        observation_types: ["screenshot", "screencast", "a11y"]
            .iter()
            .map(|s| s.to_string())
            .collect(),
        max_long_edge: MAX_LONG_EDGE,
        // Codec negotiation: advertise what encode_frame can actually produce, so a
        // client can reject an unsupported `format` before sending it.
        image_formats: ["png", "jpeg"].iter().map(|s| s.to_string()).collect(),
        // Binary media framing: opt-in per session via hello.accept.binary_frames.
        binary_frames: true,
        // Content-negotiated screenshots: request-scoped (if_none_match), nothing to
        // negotiate per session beyond advertising it. The pyautogui fallback backend
        // has no raw capture, so it simply never emits a frame_hash (and thus never
        // matches) — a full frame is always the safe answer.
        frame_dedup: true,
        // Act-returns-observation: the per-action `observe` argument is honored.
        observe_after_act: true,
        // The guest structured-observation engine (observe.rs + atspi_source.rs) is
        // compiled into every shinkend; AT-SPI availability is checked at request time.
        structured_observation: true,
    }
}

/// Build the `welcome` reply to a client `hello`.
#[cfg(test)]
pub fn welcome() -> Message {
    welcome_with_exec(true)
}

/// Build a policy-aware `welcome`. `exec` is a privileged server surface and is
/// omitted unless the operator explicitly enabled it; clients must never be told a
/// disabled action is available.
pub fn welcome_with_exec(exec_enabled: bool) -> Message {
    let mut capabilities = capabilities();
    if !exec_enabled {
        capabilities.verbs.retain(|verb| verb != "exec");
    }
    Message::Welcome {
        v: 0,
        server: ServerInfo {
            name: "shinkend".to_string(),
            version: env!("CARGO_PKG_VERSION").to_string(),
            platform: platform().to_string(),
        },
        capabilities,
    }
}

/// Build one server-pushed screencast frame for `stream_id` at index `seq`.
pub fn stream_frame(stream_id: &str, seq: u64, image: ImageRef) -> Message {
    Message::Observation {
        obs_id: format!("{stream_id}-{seq}"),
        cause: None,
        stream: Some(stream_id.to_string()),
        seq: Some(seq),
        display: None,
        image: Some(image),
        tiles: None,
        frame_hash: None,
        not_modified: None,
        pointer: None,
    }
}

/// Build one server-pushed dirty-tile delta frame: only the changed tiles, no `image`.
pub fn stream_tiles_frame(stream_id: &str, seq: u64, tiles: Vec<TileRef>) -> Message {
    Message::Observation {
        obs_id: format!("{stream_id}-{seq}"),
        cause: None,
        stream: Some(stream_id.to_string()),
        seq: Some(seq),
        display: None,
        image: None,
        tiles: Some(tiles),
        frame_hash: None,
        not_modified: None,
        pointer: None,
    }
}

/// Build the compact content-negotiated screenshot answer: the freshly captured
/// frame's raw-pixel hash equals the request's `if_none_match`, so no payload is
/// re-sent — only the hash the client already holds the bytes for. Travels as JSON
/// text even on a binary-negotiated session (there is no payload to carry).
pub fn not_modified_observation(
    call_id: &str,
    frame_hash: &str,
    pointer: Option<(i32, i32)>,
    display: Option<crate::executor::CoordinateSpace>,
) -> Message {
    Message::Observation {
        obs_id: format!("obs-{call_id}"),
        cause: Some(call_id.to_string()),
        stream: None,
        seq: None,
        display,
        image: None,
        tiles: None,
        frame_hash: Some(frame_hash.to_string()),
        not_modified: Some(true),
        pointer,
    }
}

// ---- exec channel (G1) ----

/// Build the `result` answering a BUFFERED exec: `ok: true` (the ACTION ran — a
/// nonzero exit code is the COMMAND failing, reported in the typed value, not an
/// action error) with `value` = the schema's `$defs.ExecResult`. stdout/stderr are
/// UTF-8 with lossy replacement; byte-exact output rides the streamed binary form.
pub fn exec_result(call_id: &str, o: &crate::exec::ExecOutcome) -> Message {
    ok_result(
        call_id,
        serde_json::json!({
            "exit_code": o.exit_code,
            "signal": o.signal,
            "timed_out": o.timed_out,
            "stdout": String::from_utf8_lossy(&o.stdout),
            "stderr": String::from_utf8_lossy(&o.stderr),
            "stdout_truncated": o.stdout_truncated,
            "stderr_truncated": o.stderr_truncated,
            "duration_ms": o.duration_ms,
        }),
    )
}

/// Serialize one JSON-text `exec_output` event (base64 chunk). `None` only on a
/// serialization failure, which the streamed runner skips rather than wedging.
pub fn exec_output_text(cause: &str, seq: u64, channel: &str, data: &[u8]) -> Option<String> {
    serde_json::to_string(&Message::ExecOutput {
        cause: cause.to_string(),
        seq,
        channel: channel.to_string(),
        data_b64: B64.encode(data),
    })
    .ok()
}

/// Build one BINARY `exec_output` frame — the same `u32 LE header_len | JSON header |
/// payload` layout as media frames, with the header's `type` as the kind
/// discriminator and the chunk bytes located by `data.off`/`data.len`
/// (schema `$defs.BinaryExecOutputHeader`).
pub fn binary_exec_output(cause: &str, seq: u64, channel: &str, data: &[u8]) -> Vec<u8> {
    let header = serde_json::json!({
        "type": "exec_output",
        "cause": cause,
        "seq": seq,
        "channel": channel,
        "data": { "off": 0, "len": data.len() },
    });
    assemble_binary(&header, &[data])
}

/// Serialize the terminal `exec_exit` of a streamed run.
pub fn exec_exit_text(
    cause: &str,
    exit_code: Option<i32>,
    signal: Option<i32>,
    timed_out: bool,
    duration_ms: f64,
    truncated: bool,
) -> String {
    serde_json::to_string(&Message::ExecExit {
        cause: cause.to_string(),
        exit_code,
        signal,
        timed_out,
        duration_ms,
        truncated,
        error: None,
    })
    .unwrap_or_else(|_| error_result_text(cause, "failed to encode exec_exit"))
}

/// Serialize an error-terminal `exec_exit` (spawn/wait failure: no process ran).
pub fn exec_exit_error(cause: &str, error: &str, duration_ms: f64) -> String {
    serde_json::to_string(&Message::ExecExit {
        cause: cause.to_string(),
        exit_code: None,
        signal: None,
        timed_out: false,
        duration_ms,
        truncated: false,
        error: Some(error.to_string()),
    })
    .unwrap_or_else(|_| error_result_text(cause, error))
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

/// Envelope metadata for a binary image observation.  Grouping it separately from
/// codec/dimensions keeps the coordinate descriptor on the observation (not inside
/// the encoded image object) and avoids growing the historical helper's argument
/// list every time observation metadata evolves.
pub struct BinaryObservationMeta<'a> {
    pub obs_id: &'a str,
    pub cause: Option<&'a str>,
    pub stream: Option<&'a str>,
    pub seq: Option<u64>,
    pub frame_hash: Option<&'a str>,
    pub pointer: Option<(i32, i32)>,
    pub display: Option<&'a crate::executor::CoordinateSpace>,
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
/// `cause` is set, a screencast (key)frame when `stream`/`seq` are. `frame_hash` is
/// the raw-pixel content hash (screenshot dedup) — set on one-shot screenshots from
/// a raw-capture backend, absent on stream frames.
#[allow(clippy::too_many_arguments)]
pub fn binary_image_frame(
    obs_id: &str,
    cause: Option<&str>,
    stream: Option<&str>,
    seq: Option<u64>,
    frame_hash: Option<&str>,
    pointer: Option<(i32, i32)>,
    meta: BinaryImageMeta<'_>,
    data: &[u8],
) -> Vec<u8> {
    binary_image_frame_with_display(
        BinaryObservationMeta {
            obs_id,
            cause,
            stream,
            seq,
            frame_hash,
            pointer,
            display: None,
        },
        meta,
        data,
    )
}

/// Coordinate-aware binary image observation.  The legacy
/// [`binary_image_frame`] remains as a compatibility wrapper for screencast call
/// sites that do not yet attach frame coordinates.
pub fn binary_image_frame_with_display(
    observation: BinaryObservationMeta<'_>,
    meta: BinaryImageMeta<'_>,
    data: &[u8],
) -> Vec<u8> {
    let mut header = serde_json::json!({
        "type": "observation",
        "obs_id": observation.obs_id,
        "image": {
            "off": 0,
            "len": data.len(),
            "w": meta.w,
            "h": meta.h,
            "scope": meta.scope,
            "format": meta.format,
        },
    });
    if let Some(c) = observation.cause {
        header["cause"] = c.into();
    }
    if let Some(s) = observation.stream {
        header["stream"] = s.into();
    }
    if let Some(q) = observation.seq {
        header["seq"] = q.into();
    }
    if let Some(fh) = observation.frame_hash {
        header["frame_hash"] = fh.into();
    }
    if let Some((px, py)) = observation.pointer {
        header["pointer"] = serde_json::json!([px, py]);
    }
    if let Some(display) = observation.display {
        header["display"] =
            serde_json::to_value(display).expect("CoordinateSpace serialization is infallible");
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
            // Cross-platform alias for x11_up: "the display connection is up"
            // (X11 connected on Linux; a live display on macOS).
            "display_up": r.x11_up,
            "root_nonblack": r.root_nonblack,
            // macOS TCC: whether user grants (Screen Recording/Accessibility) are
            // still owed. null on backends with no permission concept (X11/virtual).
            "permissions_pending": r.permissions_pending,
        }),
    )
}

/// Build a `list_windows` query result: the executor's EWMH window enumeration as a
/// JSON array of `{id, title, pid, x, y, w, h, focused}` objects.
pub fn list_windows_result(call_id: &str, windows: &[crate::executor::WindowInfo]) -> Message {
    ok_result(call_id, serde_json::json!(windows))
}

/// Build the `clipboard_get` reply: a typed `result` whose value is `{text}`. The
/// verb is a READ — its data rides the result channel like a query answer (an `ack`
/// carries no payload). v1 is text-only; binary clipboard formats are future work.
pub fn clipboard_text_result(call_id: &str, text: &str) -> Message {
    ok_result(call_id, serde_json::json!({ "text": text }))
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
            // And `list_windows`: only the live session (holding the executor) can
            // enumerate real windows — never fabricated here.
            "list_windows" => err_result(
                &call_id,
                "list_windows is answered by the session from the live executor",
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

    /// The advertised frame-dedup capability must exist in the published schema
    /// (Welcome.capabilities.frame_dedup, Action.if_none_match, the observation's
    /// frame_hash/not_modified), so negotiation never promises a contract shape the
    /// schema doesn't know.
    #[test]
    fn advertised_frame_dedup_is_in_schema() {
        assert!(capabilities().frame_dedup);
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../schema/aci.schema.json");
        let raw = std::fs::read_to_string(path).expect("read schema/aci.schema.json");
        let schema: serde_json::Value = serde_json::from_str(&raw).expect("parse aci schema");
        assert_eq!(
            schema["$defs"]["Welcome"]["properties"]["capabilities"]["properties"]["frame_dedup"]
                ["type"],
            "boolean",
            "schema Welcome.capabilities must define frame_dedup"
        );
        assert_eq!(
            schema["$defs"]["Action"]["properties"]["if_none_match"]["type"], "string",
            "schema Action must define if_none_match"
        );
        assert_eq!(
            schema["$defs"]["ObservationMsg"]["properties"]["frame_hash"]["type"], "string",
            "schema ObservationMsg must define frame_hash"
        );
        assert_eq!(
            schema["$defs"]["ObservationMsg"]["properties"]["not_modified"]["const"], true,
            "schema ObservationMsg must define not_modified (const true)"
        );
        assert_eq!(
            schema["$defs"]["BinaryFrameHeader"]["properties"]["frame_hash"]["type"], "string",
            "schema BinaryFrameHeader must define frame_hash"
        );
    }

    // Back-compat: a welcome from a pre-dedup runtime (no frame_dedup field) must
    // parse as false — clients must not send if_none_match against those (they
    // reject unknown action fields).
    #[test]
    fn welcome_without_frame_dedup_parses_as_false() {
        let old = r#"{"type":"welcome","v":0,
            "server":{"name":"shinkend","version":"0.0.0","platform":"linux"},
            "capabilities":{"schema_version":0,"verbs":[],"targets":[],
                            "observation_types":[],"max_long_edge":2576}}"#;
        let msg: Message = serde_json::from_str(old).unwrap();
        match msg {
            Message::Welcome { capabilities, .. } => assert!(!capabilities.frame_dedup),
            other => panic!("expected welcome, got {other:?}"),
        }
    }

    /// The structured-observation capability must exist in the published schema and
    /// default false on welcomes from pre-engine runtimes (back-compat).
    #[test]
    fn structured_observation_capability_in_schema_and_back_compatible() {
        assert!(capabilities().structured_observation);
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../schema/aci.schema.json");
        let raw = std::fs::read_to_string(path).expect("read schema/aci.schema.json");
        let schema: serde_json::Value = serde_json::from_str(&raw).expect("parse aci schema");
        assert_eq!(
            schema["$defs"]["Welcome"]["properties"]["capabilities"]["properties"]
                ["structured_observation"]["type"],
            "boolean",
            "schema Welcome.capabilities must define structured_observation"
        );
        let old = r#"{"type":"welcome","v":0,
            "server":{"name":"shinkend","version":"0.0.0","platform":"linux"},
            "capabilities":{"schema_version":0,"verbs":[],"targets":[],
                            "observation_types":[],"max_long_edge":2576}}"#;
        match serde_json::from_str::<Message>(old).unwrap() {
            Message::Welcome { capabilities, .. } => {
                assert!(!capabilities.structured_observation);
            }
            other => panic!("expected welcome, got {other:?}"),
        }
    }

    /// The compact not_modified observation: cause + frame_hash + not_modified=true,
    /// and NO image/tiles — the shape the schema's ObservationMsg rule pins.
    #[test]
    fn not_modified_observation_is_compact_and_correlated() {
        let msg = not_modified_observation("c7", "deadbeefdeadbeef", Some((640, 360)), None);
        let text = serde_json::to_string(&msg).unwrap();
        let v: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(v["type"], "observation");
        assert_eq!(v["cause"], "c7");
        assert_eq!(v["not_modified"], true);
        assert_eq!(v["frame_hash"], "deadbeefdeadbeef");
        assert!(v.get("image").is_none() && v.get("tiles").is_none());
        assert!(v.get("stream").is_none() && v.get("seq").is_none());
        // and it round-trips through the typed Message
        let back: Message = serde_json::from_str(&text).unwrap();
        assert!(matches!(back, Message::Observation { .. }));
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

    /// The advertised act-returns-observation capability must exist in the published
    /// schema, and a welcome from an older runtime (no field) must parse as false —
    /// a client must never send `observe` to a runtime that didn't advertise it.
    #[test]
    fn observe_after_act_is_advertised_in_schema_and_back_compatible() {
        assert!(capabilities().observe_after_act);
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../schema/aci.schema.json");
        let raw = std::fs::read_to_string(path).expect("read schema/aci.schema.json");
        let schema: serde_json::Value = serde_json::from_str(&raw).expect("parse aci schema");
        assert_eq!(
            schema["$defs"]["Welcome"]["properties"]["capabilities"]["properties"]
                ["observe_after_act"]["type"],
            "boolean",
            "schema Welcome.capabilities must define observe_after_act"
        );
        // and the Action carries the observe argument per $defs.ObserveSpec
        assert_eq!(
            schema["$defs"]["Action"]["properties"]["observe"]["$ref"],
            "#/$defs/ObserveSpec"
        );
        let old = r#"{"type":"welcome","v":0,
            "server":{"name":"shinkend","version":"0.0.0","platform":"linux"},
            "capabilities":{"schema_version":0,"verbs":[],"targets":[],
                            "observation_types":[],"max_long_edge":2576}}"#;
        let msg: Message = serde_json::from_str(old).unwrap();
        match msg {
            Message::Welcome { capabilities, .. } => assert!(!capabilities.observe_after_act),
            other => panic!("expected welcome, got {other:?}"),
        }
    }

    /// list_windows results serialize the executor's WindowInfo shape verbatim.
    #[test]
    fn list_windows_result_carries_the_window_array() {
        let msg = list_windows_result(
            "c1",
            &[crate::executor::WindowInfo {
                id: 7,
                title: "xterm".into(),
                pid: Some(123),
                x: 0,
                y: 0,
                w: 200,
                h: 100,
                focused: false,
            }],
        );
        let v: serde_json::Value =
            serde_json::from_str(&serde_json::to_string(&msg).unwrap()).unwrap();
        assert_eq!(v["ok"], true);
        assert_eq!(v["value"][0]["id"], 7);
        assert_eq!(v["value"][0]["title"], "xterm");
        assert_eq!(v["value"][0]["pid"], 123);
        assert_eq!(v["value"][0]["focused"], false);
    }

    /// clipboard_get's answer is a typed result carrying {text} — the read verb's
    /// payload channel (acks carry no value).
    #[test]
    fn clipboard_text_result_carries_text_value() {
        let msg = clipboard_text_result("c3", "hello ✂️");
        let v: serde_json::Value =
            serde_json::from_str(&serde_json::to_string(&msg).unwrap()).unwrap();
        assert_eq!(v["type"], "result");
        assert_eq!(v["call_id"], "c3");
        assert_eq!(v["ok"], true);
        assert_eq!(v["value"]["text"], "hello ✂️");
    }

    /// respond() must not fabricate a window list — only the live session can.
    #[test]
    fn respond_refuses_list_windows_like_ready() {
        let msg: Message =
            serde_json::from_str(r#"{"type":"query","call_id":"c1","q":"list_windows"}"#).unwrap();
        match respond(msg) {
            Some(Message::Result { ok, error, .. }) => {
                assert!(!ok);
                assert!(error
                    .unwrap()
                    .contains("list_windows is answered by the session"));
            }
            other => panic!("expected result, got {other:?}"),
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
            None,
            None,
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
            Some("00ff00ff00ff00ff"),
            Some((12, 34)),
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
        // the one-shot screenshot header carries the raw-pixel content hash
        assert_eq!(header["frame_hash"], "00ff00ff00ff00ff");
    }

    #[test]
    fn binary_image_frame_preserves_coordinate_space() {
        let display = crate::executor::CoordinateSpace::new(
            (1920, 1080),
            crate::executor::FrameRect {
                x: 320,
                y: 180,
                w: 800,
                h: 600,
            },
            (400, 300),
            1.0,
        );
        let frame = binary_image_frame_with_display(
            BinaryObservationMeta {
                obs_id: "obs-coord",
                cause: Some("coord"),
                stream: None,
                seq: None,
                frame_hash: None,
                pointer: None,
                display: Some(&display),
            },
            BinaryImageMeta {
                w: 400,
                h: 300,
                scope: "window:42",
                format: "jpeg",
            },
            b"jpeg",
        );
        let hlen = u32::from_le_bytes(frame[..4].try_into().unwrap()) as usize;
        let header: serde_json::Value = serde_json::from_slice(&frame[4..4 + hlen]).unwrap();
        assert_eq!(header["display"]["w"], 1920);
        assert_eq!(header["display"]["source_rect"]["x"], 320);
        assert_eq!(header["display"]["delivered"]["w"], 400);
        assert_eq!(header["image"]["scope"], "window:42");
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

    // ---- exec channel wire shapes (G1) ----

    /// The buffered exec result is ok=true with the typed ExecResult value — a
    /// nonzero exit code is the COMMAND's outcome, not an action error.
    #[test]
    fn exec_result_is_ok_true_with_typed_value() {
        let o = crate::exec::ExecOutcome {
            exit_code: Some(3),
            signal: None,
            timed_out: false,
            stdout: b"out\n".to_vec(),
            stderr: b"err\n".to_vec(),
            stdout_truncated: false,
            stderr_truncated: true,
            duration_ms: 12.5,
        };
        let v: serde_json::Value =
            serde_json::from_str(&serde_json::to_string(&exec_result("c1", &o)).unwrap()).unwrap();
        assert_eq!(v["type"], "result");
        assert_eq!(v["call_id"], "c1");
        assert_eq!(v["ok"], true);
        assert_eq!(v["value"]["exit_code"], 3);
        assert_eq!(v["value"]["stdout"], "out\n");
        assert_eq!(v["value"]["stderr"], "err\n");
        assert_eq!(v["value"]["stderr_truncated"], true);
        assert_eq!(v["value"]["timed_out"], false);
        assert!(v["value"]["signal"].is_null());
    }

    /// exec_output text events carry cause/seq/channel/data_b64 and round-trip
    /// through the typed Message; exec_exit pins the terminal shape (exit_code is
    /// REQUIRED-nullable, so a signal kill serializes `null`, never omits the key).
    #[test]
    fn exec_output_and_exit_serialize_the_schema_shapes() {
        let text = exec_output_text("c2", 4, "stderr", b"\xFFraw").unwrap();
        let v: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(v["type"], "exec_output");
        assert_eq!(v["cause"], "c2");
        assert_eq!(v["seq"], 4);
        assert_eq!(v["channel"], "stderr");
        assert_eq!(v["data_b64"], B64.encode(b"\xFFraw"));
        let back: Message = serde_json::from_str(&text).unwrap();
        assert!(matches!(back, Message::ExecOutput { .. }));

        let exit = exec_exit_text("c2", None, Some(9), true, 200.0, false);
        let v: serde_json::Value = serde_json::from_str(&exit).unwrap();
        assert_eq!(v["type"], "exec_exit");
        assert!(
            v["exit_code"].is_null(),
            "killed → exit_code must be null, present"
        );
        assert_eq!(v["signal"], 9);
        assert_eq!(v["timed_out"], true);
        assert_eq!(v["truncated"], false);
        assert!(v.get("error").is_none(), "no error key on a clean kill");

        let err = exec_exit_error("c3", "exec_spawn_failed: nope", 1.0);
        let v: serde_json::Value = serde_json::from_str(&err).unwrap();
        assert_eq!(v["error"], "exec_spawn_failed: nope");
        assert!(v["exit_code"].is_null());
    }

    /// Binary exec_output frames reuse the media-frame layout with the header's
    /// `type` as the kind discriminator and `data.off/len` locating the raw bytes.
    #[test]
    fn binary_exec_output_frame_layout() {
        let frame = binary_exec_output("c1", 2, "stdout", b"chunk-bytes");
        let hlen = u32::from_le_bytes(frame[..4].try_into().unwrap()) as usize;
        let header: serde_json::Value = serde_json::from_slice(&frame[4..4 + hlen]).unwrap();
        assert_eq!(header["type"], "exec_output");
        assert_eq!(header["cause"], "c1");
        assert_eq!(header["seq"], 2);
        assert_eq!(header["channel"], "stdout");
        assert_eq!(header["data"]["off"], 0);
        assert_eq!(header["data"]["len"], b"chunk-bytes".len());
        assert!(header.get("data_b64").is_none(), "no base64 in binary");
        assert_eq!(&frame[4 + hlen..], b"chunk-bytes");
    }

    /// The exec messages must exist in the published schema's oneOf vocabulary.
    #[test]
    fn exec_messages_are_in_schema() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../schema/aci.schema.json");
        let raw = std::fs::read_to_string(path).expect("read schema/aci.schema.json");
        let schema: serde_json::Value = serde_json::from_str(&raw).expect("parse aci schema");
        assert_eq!(
            schema["$defs"]["ExecOutputMsg"]["properties"]["type"]["const"],
            "exec_output"
        );
        assert_eq!(
            schema["$defs"]["ExecExitMsg"]["properties"]["type"]["const"],
            "exec_exit"
        );
        assert_eq!(
            schema["$defs"]["BinaryExecOutputHeader"]["properties"]["type"]["const"],
            "exec_output"
        );
        // and the Action carries the exec argument family
        for field in [
            "argv",
            "shell",
            "cwd",
            "env",
            "timeout_ms",
            "stdin",
            "stream",
            "pty",
        ] {
            assert!(
                !schema["$defs"]["Action"]["properties"][field].is_null(),
                "schema Action must define {field}"
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
    fn policy_aware_welcome_omits_disabled_exec() {
        match welcome_with_exec(false) {
            Message::Welcome { capabilities, .. } => {
                assert!(!capabilities.verbs.iter().any(|verb| verb == "exec"));
                assert!(capabilities.verbs.iter().any(|verb| verb == "click"));
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
        assert_eq!(q, vec!["list_windows", "platform", "ready", "screen_size"]);
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
    fn ready_result_serializes_the_readiness_fields() {
        let msg = ready_result(
            "c9",
            crate::executor::Readiness {
                ready: false,
                x11_up: false,
                root_nonblack: None,
                permissions_pending: None,
            },
        );
        let v: serde_json::Value =
            serde_json::from_str(&serde_json::to_string(&msg).unwrap()).unwrap();
        assert_eq!(v["call_id"], "c9");
        assert_eq!(v["ok"], true); // the QUERY succeeded; readiness is in the value
        assert_eq!(v["value"]["ready"], false);
        assert_eq!(v["value"]["x11_up"], false);
        assert_eq!(v["value"]["display_up"], false); // cross-platform alias of x11_up
        assert!(v["value"]["root_nonblack"].is_null());
        // No permission concept on this backend → null, never a fabricated bool.
        assert!(v["value"]["permissions_pending"].is_null());

        // A macOS-shaped readiness round-trips the pending bit.
        let msg = ready_result(
            "c10",
            crate::executor::Readiness {
                ready: false,
                x11_up: true,
                root_nonblack: None,
                permissions_pending: Some(true),
            },
        );
        let v: serde_json::Value =
            serde_json::from_str(&serde_json::to_string(&msg).unwrap()).unwrap();
        assert_eq!(v["value"]["display_up"], true);
        assert_eq!(v["value"]["permissions_pending"], true);
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
