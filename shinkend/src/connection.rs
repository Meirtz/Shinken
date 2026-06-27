//! Per-connection ACI session: the handshake state machine + dev-token auth.
//!
//! The first message on a connection MUST be `hello`; `hello` is accepted exactly
//! once. The production TCP server always configures a token (`SHINKEND_TOKEN`), so
//! every `hello` must carry it; tokenless sessions exist only as an internal test seam.
//! Only after a successful handshake are actions/queries dispatched. This keeps an
//! unauthenticated client from driving the desktop or occupying RPC (#22).

use std::sync::Arc;

use crate::executor::{
    ActionSpec, EncodeOpts, Executor, ImageFormat, Target, DEFAULT_JPEG_QUALITY,
};
use crate::observe::{ObserveState, TreeSource};
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
    /// Negotiated binary media framing for this session (`hello.accept.binary_frames`):
    /// frames go out as one WS Binary message (`u32 LE header_len | JSON header | raw
    /// payload`) instead of base64-in-JSON text. See `protocol::binary_image_frame`.
    pub binary: bool,
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

/// One outbound reply: JSON text for control messages, or a binary media frame
/// (`u32 LE header_len | JSON header | raw payload`) on a binary-negotiated session.
#[derive(Debug, Clone, PartialEq)]
pub enum Reply {
    Text(String),
    Binary(Vec<u8>),
}

#[cfg(test)]
impl Reply {
    /// Unwrap the text reply (tests only; panics on a binary one).
    fn into_text(self) -> String {
        match self {
            Reply::Text(t) => t,
            Reply::Binary(_) => panic!("expected a text reply, got a binary frame"),
        }
    }
}

/// What to do after handling one inbound message.
pub struct Step {
    pub reply: Option<Reply>,
    pub close: bool,
    pub stream: StreamCtl,
    /// Bounded async delay (ms) the serve loop sleeps before sending this reply — the
    /// `wait` verb's semantics, honored without blocking the runtime (#140).
    pub delay_ms: u64,
    /// A second outbound message sent right after `reply` — the act-returns-observation
    /// follow-up (`observe` on a mutating action): the fresh observation (or its typed
    /// capture error) correlated to the action via `cause` = call_id.
    pub followup: Option<Reply>,
    /// A validated in-guest exec to run (G1). Like [`StreamCtl`], the side effect is
    /// owned by the async serve loop (bounded by its exec semaphore); the [`Session`]
    /// stays a pure state machine. Buffered form: `reply` is None and the exec task
    /// answers with the typed `result`; streamed form: `reply` is the ack and the
    /// task pushes `exec_output`/`exec_exit` events.
    pub exec: Option<crate::exec::ExecSpec>,
}

impl Default for Step {
    fn default() -> Self {
        Step {
            reply: None,
            close: false,
            stream: StreamCtl::None,
            delay_ms: 0,
            followup: None,
            exec: None,
        }
    }
}

impl Step {
    fn reply(msg: &Message) -> Self {
        Step::text(encode(msg))
    }
    fn text(text: String) -> Self {
        Step {
            reply: Some(Reply::Text(text)),
            ..Step::default()
        }
    }
    fn silent() -> Self {
        Step::default()
    }
    fn close(text: String) -> Self {
        Step {
            reply: Some(Reply::Text(text)),
            close: true,
            ..Step::default()
        }
    }
}

/// One client connection's ACI state machine.
pub struct Session {
    authed: bool,
    token: Option<String>,
    /// Privileged in-guest process execution is fail-closed in the production server.
    /// Unit tests and embedders retain the historical enabled default; `main` applies
    /// the operator's explicit `SHINKEND_ENABLE_EXEC` policy through the builder.
    exec_enabled: bool,
    exec: Arc<dyn Executor>,
    /// Binary media framing negotiated by this session's `hello.accept.binary_frames`.
    /// Off by default — an old client that never asked keeps base64-in-JSON frames.
    binary: bool,
    /// The structured-observation backend (the AT-SPI worker handle in production,
    /// a fake in tests); `None` nacks the observe family with a typed error.
    tree: Option<Arc<dyn TreeSource>>,
    /// Per-session structured-observation state: stable element ids, the diff
    /// baseline, and the live `element_ref` index (M1b).
    obs: ObserveState,
}

impl Session {
    pub fn new(token: Option<String>, exec: Arc<dyn Executor>) -> Self {
        Self {
            authed: false,
            token,
            exec_enabled: true,
            exec,
            binary: false,
            tree: None,
            obs: ObserveState::default(),
        }
    }

    /// Attach the structured-observation backend (builder-style, used by the serve
    /// loop; sessions without one keep nacking the observe family honestly).
    pub fn with_tree_source(mut self, tree: Arc<dyn TreeSource>) -> Self {
        self.tree = Some(tree);
        self
    }

    pub fn with_exec_enabled(mut self, enabled: bool) -> Self {
        self.exec_enabled = enabled;
        self
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
            Message::Hello {
                v, token, accept, ..
            } => {
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
                // Binary media framing is strictly opt-in (#binary-frames): only a
                // hello whose `accept.binary_frames` is exactly `true` switches this
                // session's image observations to binary WS messages. Anything else
                // (absent accept, other types) keeps the base64-in-JSON text frames,
                // so a pre-binary client is never handed bytes it can't parse.
                self.binary = accept
                    .as_ref()
                    .and_then(|a| a.get("binary_frames"))
                    .and_then(serde_json::Value::as_bool)
                    .unwrap_or(false);
                self.authed = true;
                Step::reply(&protocol::welcome_with_exec(self.exec_enabled))
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
            Message::Action { call_id, action } => self.dispatch_action(&call_id, action),
            // screen_size must report the real executor geometry, not a stub (#138)
            Message::Query { call_id, q } if q == "screen_size" => {
                let (w, h) = self.exec.screen_size();
                Step::reply(&protocol::screen_size_result(&call_id, w, h))
            }
            // Guest-side readiness (S8): microseconds (sampled root pixels), so boot
            // polls don't pull/decode full screenshots. Runs on the serve loop's
            // blocking thread like every other executor call.
            Message::Query { call_id, q } if q == "ready" => {
                Step::reply(&protocol::ready_result(&call_id, self.exec.readiness()))
            }
            // EWMH window enumeration — the Linux "enumerate apps" read primitive.
            // Answered from the live executor (several X round trips, on the serve
            // loop's blocking thread like every other executor call).
            Message::Query { call_id, q } if q == "list_windows" => {
                match self.exec.list_windows() {
                    Ok(ws) => Step::reply(&protocol::list_windows_result(&call_id, &ws)),
                    Err(e) => Step::text(protocol::error_result_text(&call_id, &e.to_string())),
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

/// The verbs that mutate guest state and therefore admit the per-action `observe`
/// argument (act-returns-observation). Mirrors the schema's `$defs.MutatingVerb` —
/// the coordinate tier, the element verbs (their AX-path writes mutate guest state
/// exactly like a click does), and the desktop verbs (G2+G3: a clipboard write, an
/// app launch, a window activation all change what the next observation shows).
const MUTATING_VERBS: [&str; 15] = [
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
    "invoke_action",
    "set_value",
    "clipboard_set",
    "launch_app",
    "activate_window",
];

/// Capture one observation and shape it for the wire — a binary frame on a
/// binary-negotiated session, base64-in-JSON text otherwise — answering `call_id` via
/// `cause`. Shared by the one-shot `screenshot` and the act-returns-observation
/// follow-up so both produce byte-identical observation shapes. `Err` carries the
/// capture error message; the caller decides its wire form (nack vs error result).
fn observation_reply(
    call_id: &str,
    scope: &str,
    opts: EncodeOpts,
    exec: &dyn Executor,
    binary: bool,
) -> Result<Reply, String> {
    let frame = exec.capture_frame(scope, opts).map_err(|e| e.to_string())?;
    let pointer = exec.pointer_position();
    Ok(image_reply(
        call_id,
        scope,
        &frame.image,
        None,
        binary,
        pointer,
        frame.display.as_ref(),
    ))
}

/// Validate an `observe` spec's capture parameters (same rules as `screenshot`:
/// well-formed scope, schema codec, 1–100 quality, clamped long edge) into the
/// `(scope, EncodeOpts)` a capture takes. `Err` is the nack message — validation runs
/// BEFORE the action executes, so a malformed observe never half-applies an action.
fn resolve_observe(obs: &crate::executor::ObserveSpec) -> Result<(String, EncodeOpts), String> {
    let scope = obs.scope.clone().unwrap_or_else(|| "screen".to_string());
    if obs.scope.is_some() && !valid_scope(&scope) {
        return Err(format!("invalid observe scope: {scope}"));
    }
    let format = ImageFormat::parse(obs.format.as_deref()).map_err(|e| e.to_string())?;
    let quality = match obs.quality {
        None => DEFAULT_JPEG_QUALITY,
        Some(q) if (1..=100).contains(&q) => q,
        Some(q) => return Err(format!("observe quality out of range 1-100: {q}")),
    };
    Ok((
        scope,
        EncodeOpts {
            max_long_edge: clamp_long_edge(obs.max_long_edge),
            format,
            quality,
        },
    ))
}

impl Session {
    /// Run one `action`. `screenshot` → one-shot `observation` (a binary frame on a
    /// binary-negotiated session, content-negotiated via `if_none_match`); `observe`
    /// → one-shot structured `observation` (always JSON text);
    /// `start_screencast`/`stop_screencast` → `ack` plus a [`StreamCtl`]; `wait` →
    /// `ack` plus a bounded delay (ms) the serve loop sleeps before replying; `exec`
    /// → a validated [`crate::exec::ExecSpec`] side effect the serve loop runs (the
    /// streamed form acks here, the buffered form's only reply is the exec task's
    /// typed `result`); every other verb → `ack` (with `element_ref` targets resolved
    /// to bbox-centre points via this session's observation engine first).
    /// `Step::delay_ms` is the `wait` delay (0 otherwise); `Step::followup` is the
    /// act-returns-observation follow-up (the fresh observation — or its typed
    /// capture error — sent right after the ack when a mutating action carried
    /// `observe`).
    fn dispatch_action(&mut self, call_id: &str, action: serde_json::Value) -> Step {
        let exec = self.exec.clone();
        let exec = exec.as_ref();
        let binary = self.binary;
        let nack = |error: String| Step {
            reply: Some(ack(call_id, false, Some(error))),
            ..Step::default()
        };
        let spec = match serde_json::from_value::<ActionSpec>(action) {
            Ok(s) => s,
            Err(e) => return nack(format!("bad action: {e}")),
        };
        let scope = spec.scope.clone().unwrap_or_else(|| "screen".to_string());
        // Reject a provided-but-malformed capture scope instead of silently capturing the
        // full screen — a privacy/contract issue (#139). (An absent scope defaults to "screen".)
        if spec.scope.is_some()
            && matches!(spec.verb.as_str(), "screenshot" | "start_screencast")
            && !valid_scope(&scope)
        {
            return nack(format!("invalid capture scope: {scope}"));
        }
        // Resolve the wire codec + quality for the verbs that consume them (same verb gating
        // as the scope check above — an invalid `format` on e.g. `stop_screencast` must not
        // nack the stop). Quality is REJECTED outside the schema's 1–100, not silently
        // clamped: the runtime must not accept what the published contract rejects.
        let is_capture = matches!(spec.verb.as_str(), "screenshot" | "start_screencast");
        let (format, quality) = if is_capture {
            let format = match ImageFormat::parse(spec.format.as_deref()) {
                Ok(f) => f,
                Err(e) => return nack(e.to_string()),
            };
            let quality = match spec.quality {
                None => DEFAULT_JPEG_QUALITY,
                Some(q) if (1..=100).contains(&q) => q,
                Some(q) => return nack(format!("quality out of range 1-100: {q}")),
            };
            (format, quality)
        } else {
            (ImageFormat::Png, DEFAULT_JPEG_QUALITY) // unused by non-capture verbs
        };
        // Act-returns-observation (`observe`): only mutating verbs admit it (the schema's
        // MutatingVerb gate), and its capture parameters are validated BEFORE the action
        // executes so a malformed observe nacks cleanly instead of half-applying.
        let observe = match spec.observe.as_ref() {
            Some(obs) => {
                if !MUTATING_VERBS.contains(&spec.verb.as_str()) {
                    return nack(format!("observe is not supported on verb {:?}", spec.verb));
                }
                match resolve_observe(obs) {
                    Ok(resolved) => Some(resolved),
                    Err(e) => return nack(e),
                }
            }
            None => None,
        };
        // The fresh observation (or its typed capture error) following a successful
        // mutating action that carried `observe` — shared by the XTEST and AX paths.
        let observe_followup = |observe: Option<(String, EncodeOpts)>| {
            observe.map(|(obs_scope, opts)| {
                observation_reply(call_id, &obs_scope, opts, exec, binary).unwrap_or_else(|e| {
                    Reply::Text(protocol::error_result_text(
                        call_id,
                        &format!("observe failed: {e}"),
                    ))
                })
            })
        };
        match spec.verb.as_str() {
            "screenshot" => {
                let opts = EncodeOpts {
                    max_long_edge: clamp_long_edge(spec.max_long_edge),
                    format,
                    quality,
                };
                // Content-negotiated (if_none_match / frame_hash / not_modified) on a
                // raw-capture backend; a binary-negotiated session gets the image as one
                // WS Binary message (raw codec bytes after the JSON header — no base64).
                let reply = screenshot_reply(
                    call_id,
                    &scope,
                    opts,
                    spec.if_none_match.as_deref(),
                    exec,
                    binary,
                );
                Step {
                    reply: Some(reply),
                    ..Step::default()
                }
            }
            // Typed in-guest exec (G1): validate here (the nack path), run in the
            // serve loop's bounded exec task. The child is a process of shinkend —
            // no Executor backend involved, so exec works on EVERY backend.
            "exec" if !self.exec_enabled => nack("exec is disabled by server policy".to_string()),
            "exec" => match crate::exec::ExecSpec::from_action(call_id, &spec, binary) {
                Ok(espec) => Step {
                    // Streamed form: ack now, events follow. Buffered form: silence
                    // until the exec task answers with the typed `result`.
                    reply: espec.stream.then(|| ack(call_id, true, None)),
                    exec: Some(espec),
                    ..Step::default()
                },
                Err(e) => nack(e),
            },
            "start_screencast" => {
                let fps = spec.fps.unwrap_or(FPS_DEFAULT).clamp(FPS_MIN, FPS_MAX);
                let delta = spec.delta.unwrap_or(false);
                // A delta stream needs raw (pre-encode) capture to diff tiles; nack a
                // backend that can't do it rather than ack and then die frameless.
                if delta && !exec.supports_raw_capture() {
                    return nack(format!(
                        "delta screencast not supported by the {} backend",
                        exec.backend()
                    ));
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
                    binary,
                };
                Step {
                    reply: Some(ack(call_id, true, None)),
                    stream: StreamCtl::Start(cast),
                    ..Step::default()
                }
            }
            "stop_screencast" => Step {
                reply: Some(ack(call_id, true, None)),
                stream: StreamCtl::Stop,
                ..Step::default()
            },
            // wait: ack, but have the serve loop sleep first (bounded) so the ack lands after
            // the delay — real wait.ms semantics, async so it never blocks the runtime (#140)
            "wait" => Step {
                reply: Some(ack(call_id, true, None)),
                delay_ms: spec.ms.unwrap_or(0).min(MAX_WAIT_MS),
                ..Step::default()
            },
            // Structured observation (M1b): settle → snapshot → stable ids → render
            // (full or diff). Runs on the serve loop's blocking thread like every other
            // executor call; the AT-SPI worker bounds its own reply deadline, so a hung
            // bus answers a typed error here instead of wedging the runtime.
            "observe" => Step {
                reply: Some(self.observe_action(call_id, &spec)),
                ..Step::default()
            },
            // Clipboard read (G2): the answer is a typed `result` carrying `{text}` —
            // the read's data channel (an ack has no payload), like a query answer.
            // Backends without clipboard support answer a typed nack.
            "clipboard_get" => match exec.clipboard_get() {
                Ok(text) => Step {
                    reply: Some(Reply::Text(encode(&protocol::clipboard_text_result(
                        call_id, &text,
                    )))),
                    ..Step::default()
                },
                Err(e) => nack(e.to_string()),
            },
            // Element verbs (AX path): resolve the ref against the LAST observation,
            // then drive the node's AT-SPI Action / Value / EditableText interface. A
            // successful element write honors `observe` exactly like a pointer verb.
            "invoke_action" | "set_value" => {
                let (reply, ok) = self.element_action(call_id, &spec);
                let followup = if ok { observe_followup(observe) } else { None };
                Step {
                    reply: Some(reply),
                    followup,
                    ..Step::default()
                }
            }
            _ => {
                // Physical-event preference: an element_ref target on a pointer verb
                // resolves to the element's bbox centre and the click goes out as a
                // real XTEST event there (invoke_action is the AX-path alternative).
                let mut spec = spec;
                if let Some(Target::ElementRef { element_ref, .. }) = &spec.target {
                    match self.obs.resolve_point(element_ref) {
                        Ok((x, y)) => {
                            spec.target = Some(Target::PointPx {
                                x: f64::from(x),
                                y: f64::from(y),
                            })
                        }
                        Err(e) => return nack(e),
                    }
                }
                match exec.execute(&spec) {
                    Ok(_) => {
                        // The action landed; when `observe` was requested, follow the ack
                        // with a fresh observation (cause = call_id) — or, if the capture
                        // itself fails, a typed error result so the client's wait resolves
                        // instead of timing out. The ack stays honest either way: the
                        // ACTION succeeded.
                        Step {
                            reply: Some(ack(call_id, true, None)),
                            followup: observe_followup(observe),
                            ..Step::default()
                        }
                    }
                    Err(e) => nack(e.to_string()),
                }
            }
        }
    }

    /// Handle the `observe` verb: capture + render one structured observation.
    fn observe_action(&mut self, call_id: &str, spec: &ActionSpec) -> Reply {
        if spec.structured == Some(false) {
            return ack(
                call_id,
                false,
                Some("observe structured:false is the pixel path — use `screenshot`".to_string()),
            );
        }
        let Some(tree) = self.tree.clone() else {
            return ack(
                call_id,
                false,
                Some(
                    "structured_observation_unavailable: this runtime has no tree source"
                        .to_string(),
                ),
            );
        };
        let t0 = std::time::Instant::now();
        match tree.snapshot(spec.settle_ms) {
            Ok(snapshot) => {
                let rendered = self.obs.observe(&snapshot, spec.diff.unwrap_or(false));
                let capture_ms = (t0.elapsed().as_secs_f64() * 100_000.0).round() / 100.0; // 0.01 ms
                Reply::Text(rendered.to_observation_text(call_id, capture_ms))
            }
            Err(e) => ack(
                call_id,
                false,
                Some(format!("structured observation failed: {e:#}")),
            ),
        }
    }

    /// Handle `invoke_action` / `set_value`: element_ref → backend node → AT-SPI.
    /// Returns the wire reply plus whether the element write SUCCEEDED — the success
    /// flag drives the act-returns-observation follow-up in the dispatcher.
    fn element_action(&mut self, call_id: &str, spec: &ActionSpec) -> (Reply, bool) {
        let Some(tree) = self.tree.clone() else {
            return (
                ack(
                    call_id,
                    false,
                    Some(
                        "structured_observation_unavailable: this runtime has no tree source"
                            .to_string(),
                    ),
                ),
                false,
            );
        };
        let element_ref = match &spec.target {
            Some(Target::ElementRef { element_ref, .. }) => element_ref.clone(),
            _ => {
                return (
                    ack(
                        call_id,
                        false,
                        Some(format!("{} requires an element_ref target", spec.verb)),
                    ),
                    false,
                )
            }
        };
        let record = match self.obs.resolve(&element_ref) {
            Ok(r) => r.clone(),
            Err(e) => return (ack(call_id, false, Some(e)), false),
        };
        let result = match spec.verb.as_str() {
            // `text` carries the action name (None → the node's first action).
            "invoke_action" => tree.invoke(&record.backend_id, spec.text.as_deref()),
            _ => match &spec.text {
                Some(value) => tree.set_value(&record.backend_id, value),
                None => {
                    return (
                        ack(
                            call_id,
                            false,
                            Some("set_value requires `text` (the new value)".to_string()),
                        ),
                        false,
                    )
                }
            },
        };
        match result {
            Ok(_) => (ack(call_id, true, None), true),
            Err(e) => (
                ack(
                    call_id,
                    false,
                    Some(format!("{} {element_ref} failed: {e:#}", spec.verb)),
                ),
                false,
            ),
        }
    }
}

/// Answer one `screenshot` action, with content negotiation when the backend can
/// capture raw pixels: capture RAW once, hash it (`executor::frame_hash_hex` — the
/// hash is over raw pixels, NOT the encoded payload, so it is codec-independent;
/// see its docs for why that is the right identity for fork fleets), and
///
/// - on an `if_none_match` hit, skip the encode entirely and answer with the
///   compact `not_modified` observation (no payload — the client already holds
///   these pixels, keyed by `frame_hash`);
/// - otherwise encode the SAME raw buffer (downscale already applied by
///   `capture_raw`) and attach `frame_hash` to the observation, so the client can
///   offer it on its next request.
///
/// A backend without raw capture (e.g. the pyautogui fallback) keeps the plain
/// encoded-capture path: full frame, no `frame_hash` — `if_none_match` then simply
/// never matches, which is always the safe answer.
fn screenshot_reply(
    call_id: &str,
    scope: &str,
    opts: EncodeOpts,
    if_none_match: Option<&str>,
    exec: &dyn Executor,
    binary: bool,
) -> Reply {
    let pointer = exec.pointer_position();
    if exec.supports_raw_capture() {
        return match exec.capture_raw_frame(scope, opts.max_long_edge) {
            Ok(frame) => {
                let crate::executor::RawCapturedFrame { rgb, w, h, display } = frame;
                let hash = crate::executor::frame_hash_hex(&rgb, w, h);
                if if_none_match == Some(hash.as_str()) {
                    // Not modified: no encode, no payload — text even on a binary
                    // session (there are no payload bytes to frame). The pointer
                    // and coordinate map ride along: pixels may be unchanged even
                    // though the captured window moved on the global screen.
                    return Reply::Text(encode(&protocol::not_modified_observation(
                        call_id, &hash, pointer, display,
                    )));
                }
                // capture_raw already downscaled; encoding must not downscale again.
                let encode_opts = EncodeOpts {
                    max_long_edge: None,
                    ..opts
                };
                match crate::executor::encode_frame(&rgb, w, h, encode_opts) {
                    Ok(img) => image_reply(
                        call_id,
                        scope,
                        &img,
                        Some(&hash),
                        binary,
                        pointer,
                        display.as_ref(),
                    ),
                    Err(e) => ack(call_id, false, Some(e.to_string())),
                }
            }
            Err(e) => ack(call_id, false, Some(e.to_string())),
        };
    }
    match exec.capture_frame(scope, opts) {
        Ok(frame) => image_reply(
            call_id,
            scope,
            &frame.image,
            None,
            binary,
            pointer,
            frame.display.as_ref(),
        ),
        Err(e) => ack(call_id, false, Some(e.to_string())),
    }
}

/// Build the full-image screenshot observation in the session's negotiated wire
/// form: a binary WS message (raw codec bytes after the JSON header) on a binary
/// session, the base64-in-JSON text observation otherwise. `frame_hash` (raw-pixel
/// content hash) rides along on both forms when the capture path computed one.
fn image_reply(
    call_id: &str,
    scope: &str,
    img: &crate::executor::CapturedImage,
    frame_hash: Option<&str>,
    binary: bool,
    pointer: Option<(i32, i32)>,
    display: Option<&crate::executor::CoordinateSpace>,
) -> Reply {
    if binary {
        let obs_id = format!("obs-{call_id}");
        return Reply::Binary(protocol::binary_image_frame_with_display(
            protocol::BinaryObservationMeta {
                obs_id: &obs_id,
                cause: Some(call_id),
                stream: None,
                seq: None,
                frame_hash,
                pointer,
                display,
            },
            protocol::BinaryImageMeta {
                w: img.w,
                h: img.h,
                scope,
                format: img.format.as_str(),
            },
            &img.data,
        ));
    }
    Reply::Text(encode(&Message::Observation {
        obs_id: format!("obs-{call_id}"),
        cause: Some(call_id.to_string()),
        stream: None,
        seq: None,
        display: display.cloned(),
        image: Some(protocol::ImageRef {
            data: img.to_base64(),
            w: img.w,
            h: img.h,
            scope: scope.to_string(),
            format: img.format.as_str().to_string(),
        }),
        tiles: None,
        frame_hash: frame_hash.map(str::to_string),
        not_modified: None,
        pointer,
    }))
}

fn ack(call_id: &str, ok: bool, error: Option<String>) -> Reply {
    Reply::Text(encode(&Message::Ack {
        call_id: call_id.to_string(),
        ok,
        error,
    }))
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
            .unwrap()
            .into_text();
        // reflects the executor's geometry, not the 1280x800 stub (#138)
        assert!(reply.contains("\"w\":640") && reply.contains("\"h\":480"));
    }

    /// The `ready` query (S8) answers from the live executor's readiness probe. The
    /// virtual backend has no display: ready immediately, x11_up=false, nothing sampled.
    #[test]
    fn ready_query_reports_guest_readiness() {
        let mut s = session(None);
        s.on_text(HELLO);
        let reply = s
            .on_text(r#"{"type":"query","call_id":"r1","q":"ready"}"#)
            .reply
            .unwrap()
            .into_text();
        let v: serde_json::Value = serde_json::from_str(&reply).unwrap();
        assert_eq!(v["ok"], true);
        assert_eq!(v["value"]["ready"], true);
        assert_eq!(v["value"]["x11_up"], false);
        assert!(v["value"]["root_nonblack"].is_null());
    }

    /// `ready`, like every query, must not be answerable before the handshake.
    #[test]
    fn ready_query_requires_auth() {
        let mut s = session(Some("secret"));
        let step = s.on_text(r#"{"type":"query","call_id":"r1","q":"ready"}"#);
        assert!(step.close && !s.is_authenticated());
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
        assert!(step
            .reply
            .unwrap()
            .into_text()
            .contains("\"type\":\"welcome\""));
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
        let reply = step.reply.unwrap().into_text();
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
        assert!(step
            .reply
            .unwrap()
            .into_text()
            .contains("unsupported image format"));
        // Out-of-range quality is REJECTED (schema: 1–100), not clamped.
        let step = s.on_text(
            r#"{"type":"action","call_id":"c2","action":{"verb":"screenshot","format":"jpeg","quality":0}}"#,
        );
        assert!(step
            .reply
            .unwrap()
            .into_text()
            .contains("quality out of range"));
        // An invalid format on a NON-capture verb must not nack it: the stop (and its
        // StreamCtl) still happens — same gating as the scope check.
        let step = s.on_text(
            r#"{"type":"action","call_id":"c3","action":{"verb":"stop_screencast","format":"webp"}}"#,
        );
        assert!(step.reply.unwrap().into_text().contains("\"ok\":true"));
        assert!(matches!(step.stream, StreamCtl::Stop));
    }

    #[test]
    fn wait_action_acks_with_a_bounded_delay() {
        let mut s = session(None);
        s.on_text(HELLO);
        let step =
            s.on_text(r#"{"type":"action","call_id":"c1","action":{"verb":"wait","ms":500}}"#);
        assert_eq!(step.delay_ms, 500); // serve loop sleeps this before the ack (#140)
        assert!(step.reply.unwrap().into_text().contains("\"ok\":true"));
        // an absurd wait is clamped to MAX_WAIT_MS, not honored verbatim
        let big =
            s.on_text(r#"{"type":"action","call_id":"c2","action":{"verb":"wait","ms":99999999}}"#);
        assert_eq!(big.delay_ms, MAX_WAIT_MS);
    }

    /// A backend whose raw frame never changes — the dedup hit case.
    struct StaticRawExec;
    impl Executor for StaticRawExec {
        fn execute(&self, _: &ActionSpec) -> anyhow::Result<String> {
            Ok(String::new())
        }
        fn backend(&self) -> &'static str {
            "static-raw"
        }
        fn capture_raw(&self, _: &str, _: Option<u32>) -> anyhow::Result<(Vec<u8>, u16, u16)> {
            Ok((vec![7u8; 2 * 2 * 3], 2, 2))
        }
        fn supports_raw_capture(&self) -> bool {
            true
        }
        fn pointer_position(&self) -> Option<(i32, i32)> {
            Some((11, 22))
        }
    }

    /// A backend with ONLY encoded capture (no raw pixels) — like the pyautogui
    /// fallback: screenshots work, but no frame_hash is ever computed.
    struct EncodedOnlyExec;
    impl Executor for EncodedOnlyExec {
        fn execute(&self, _: &ActionSpec) -> anyhow::Result<String> {
            Ok(String::new())
        }
        fn backend(&self) -> &'static str {
            "encoded-only"
        }
        fn capture(
            &self,
            _: &str,
            _: EncodeOpts,
        ) -> anyhow::Result<crate::executor::CapturedImage> {
            Ok(crate::executor::CapturedImage {
                data: b"png-bytes".to_vec(),
                format: ImageFormat::Png,
                w: 1,
                h: 1,
            })
        }
    }

    /// Scoped/downscaled executor used to prove the wire carries enough metadata
    /// to invert delivered image coordinates back into global point_px actions.
    struct ScopedCoordinateExec;
    impl Executor for ScopedCoordinateExec {
        fn execute(&self, _: &ActionSpec) -> anyhow::Result<String> {
            Ok(String::new())
        }
        fn backend(&self) -> &'static str {
            "scoped-coordinate"
        }
        fn screen_size(&self) -> (u16, u16) {
            (20, 10)
        }
        fn capture_raw_frame(
            &self,
            scope: &str,
            max_long_edge: Option<u32>,
        ) -> anyhow::Result<crate::executor::RawCapturedFrame> {
            assert_eq!(scope, "window:42");
            assert_eq!(max_long_edge, Some(2));
            Ok(crate::executor::RawCapturedFrame {
                rgb: vec![7; 6],
                w: 2,
                h: 1,
                display: Some(crate::executor::CoordinateSpace::new(
                    (20, 10),
                    crate::executor::FrameRect {
                        x: 5,
                        y: 3,
                        w: 4,
                        h: 2,
                    },
                    (2, 1),
                    1.0,
                )),
            })
        }
        fn capture_frame(
            &self,
            scope: &str,
            opts: EncodeOpts,
        ) -> anyhow::Result<crate::executor::CapturedFrame> {
            assert_eq!(scope, "window:42");
            assert_eq!(opts.max_long_edge, Some(2));
            let image = crate::executor::encode_frame(&[7; 6], 2, 1, opts)?;
            Ok(crate::executor::CapturedFrame {
                image,
                display: Some(crate::executor::CoordinateSpace::new(
                    (20, 10),
                    crate::executor::FrameRect {
                        x: 5,
                        y: 3,
                        w: 4,
                        h: 2,
                    },
                    (2, 1),
                    1.0,
                )),
            })
        }
        fn supports_raw_capture(&self) -> bool {
            true
        }
    }

    fn obs_json(step: Step) -> serde_json::Value {
        serde_json::from_str(&step.reply.unwrap().into_text()).unwrap()
    }

    #[test]
    fn scoped_downscaled_screenshot_carries_frame_to_global_mapping() {
        let opts = EncodeOpts {
            max_long_edge: Some(2),
            ..EncodeOpts::default()
        };
        let reply = screenshot_reply(
            "coord",
            "window:42",
            opts,
            None,
            &ScopedCoordinateExec,
            false,
        );
        let Reply::Text(text) = reply else {
            panic!("expected text observation")
        };
        let value: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(value["image"]["w"], 2);
        assert_eq!(value["image"]["h"], 1);
        assert_eq!(value["display"]["w"], 20);
        assert_eq!(value["display"]["h"], 10);
        assert_eq!(value["display"]["dpr"], 1.0);
        assert_eq!(
            value["display"]["source_rect"],
            serde_json::json!({"x": 5, "y": 3, "w": 4, "h": 2})
        );
        assert_eq!(
            value["display"]["delivered"],
            serde_json::json!({"w": 2, "h": 1})
        );
    }

    #[test]
    fn observe_after_act_uses_the_same_coordinate_mapping() {
        let opts = EncodeOpts {
            max_long_edge: Some(2),
            ..EncodeOpts::default()
        };
        let reply = observation_reply(
            "observe-coord",
            "window:42",
            opts,
            &ScopedCoordinateExec,
            false,
        )
        .unwrap();
        let Reply::Text(text) = reply else {
            panic!("expected text observation")
        };
        let value: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(value["cause"], "observe-coord");
        assert_eq!(value["display"]["source_rect"]["x"], 5);
        assert_eq!(value["display"]["delivered"]["w"], 2);
    }

    /// Content-negotiated screenshot (#frame-dedup): the first screenshot carries a
    /// frame_hash; echoing it back as if_none_match over an unchanged screen yields
    /// the compact not_modified observation (no image); a stale hash yields the
    /// full frame again.
    #[test]
    fn screenshot_if_none_match_answers_not_modified_on_hash_hit() {
        let mut s = Session::new(None, Arc::new(StaticRawExec));
        s.on_text(HELLO);
        let v = obs_json(
            s.on_text(r#"{"type":"action","call_id":"c1","action":{"verb":"screenshot"}}"#),
        );
        assert_eq!(v["type"], "observation");
        assert!(v["image"]["ref"].is_string());
        // Pointer metadata rides the one-shot screenshot (capture pixels, [x, y]).
        assert_eq!(v["pointer"], serde_json::json!([11, 22]));
        let hash = v["frame_hash"]
            .as_str()
            .expect("frame_hash on screenshot")
            .to_string();
        assert!(v.get("not_modified").is_none());

        // Hash hit → not_modified, no payload, same hash, correlated by cause —
        // and the pointer still rides along (fresh metadata over unchanged pixels).
        let v = obs_json(s.on_text(&format!(
            r#"{{"type":"action","call_id":"c2","action":{{"verb":"screenshot","if_none_match":"{hash}"}}}}"#,
        )));
        assert_eq!(v["type"], "observation");
        assert_eq!(v["not_modified"], true);
        assert_eq!(v["frame_hash"], hash.as_str());
        assert_eq!(v["cause"], "c2");
        assert_eq!(v["pointer"], serde_json::json!([11, 22]));
        assert!(v.get("image").is_none() && v.get("tiles").is_none());

        // Stale hash → full frame again (with the current hash attached).
        let v = obs_json(s.on_text(
            r#"{"type":"action","call_id":"c3","action":{"verb":"screenshot","if_none_match":"00000000000000000000000000000000"}}"#,
        ));
        assert!(v["image"]["ref"].is_string());
        assert_eq!(v["frame_hash"], hash.as_str());
        assert!(v.get("not_modified").is_none());
    }

    /// Mixed-version safety: a client holding a frame_hash minted by a pre-xxh3
    /// runtime (16-char fnv1a-64 hex) against THIS runtime (32-char xxh3-128 hex).
    /// The wire value is opaque and matched by string equality, so the stale
    /// format can never collide with a current hash — the answer degrades to a
    /// full frame (never a wrong `not_modified`), and the observation carries the
    /// current-format hash for the client to re-key on.
    #[test]
    fn if_none_match_from_an_old_hash_format_safely_misses() {
        let mut s = Session::new(None, Arc::new(StaticRawExec));
        s.on_text(HELLO);
        let v = obs_json(s.on_text(
            r#"{"type":"action","call_id":"c1","action":{"verb":"screenshot","if_none_match":"cbf29ce484222325"}}"#,
        ));
        assert!(v["image"]["ref"].is_string(), "must serve the full frame");
        assert!(v.get("not_modified").is_none());
        let fresh = v["frame_hash"].as_str().unwrap();
        assert_eq!(fresh.len(), 32, "current wire hash is xxh3-128 hex");
    }

    /// The dedup identity is codec-independent: a hash minted under PNG matches a
    /// JPEG request over the same pixels (the hash is over RAW pixels, not payload).
    #[test]
    fn if_none_match_matches_across_codecs() {
        let mut s = Session::new(None, Arc::new(StaticRawExec));
        s.on_text(HELLO);
        let v = obs_json(s.on_text(
            r#"{"type":"action","call_id":"c1","action":{"verb":"screenshot","format":"png"}}"#,
        ));
        let hash = v["frame_hash"].as_str().unwrap().to_string();
        let v = obs_json(s.on_text(&format!(
            r#"{{"type":"action","call_id":"c2","action":{{"verb":"screenshot","format":"jpeg","if_none_match":"{hash}"}}}}"#,
        )));
        assert_eq!(v["not_modified"], true);
    }

    /// A changing screen never matches: the VirtualExecutor's frame advances every
    /// capture, so an if_none_match echo still gets a full frame (a NEW hash).
    #[test]
    fn if_none_match_misses_when_the_screen_changed() {
        let mut s = session(None);
        s.on_text(HELLO);
        let v = obs_json(
            s.on_text(r#"{"type":"action","call_id":"c1","action":{"verb":"screenshot"}}"#),
        );
        let hash = v["frame_hash"].as_str().unwrap().to_string();
        let v = obs_json(s.on_text(&format!(
            r#"{{"type":"action","call_id":"c2","action":{{"verb":"screenshot","if_none_match":"{hash}"}}}}"#,
        )));
        assert!(v["image"]["ref"].is_string());
        assert!(v.get("not_modified").is_none());
        assert_ne!(v["frame_hash"].as_str().unwrap(), hash);
    }

    /// A backend without raw capture keeps the plain path: full frame, NO frame_hash
    /// — if_none_match simply never matches (always the safe answer).
    #[test]
    fn backend_without_raw_capture_serves_full_frames_without_hash() {
        let mut s = Session::new(None, Arc::new(EncodedOnlyExec));
        s.on_text(HELLO);
        let v = obs_json(s.on_text(
            r#"{"type":"action","call_id":"c1","action":{"verb":"screenshot","if_none_match":"0000000000000000"}}"#,
        ));
        assert_eq!(v["type"], "observation");
        assert!(v["image"]["ref"].is_string());
        assert!(v.get("frame_hash").is_none());
        assert!(v.get("not_modified").is_none());
    }

    /// On a binary-negotiated session the full screenshot is a binary frame whose
    /// header carries frame_hash, while a not_modified answer is TEXT (no payload
    /// to carry).
    #[test]
    fn binary_session_gets_frame_hash_header_and_text_not_modified() {
        const HELLO_BINARY: &str = r#"{"type":"hello","v":0,"client":{"name":"t","version":"0"},"accept":{"binary_frames":true}}"#;
        let mut s = Session::new(None, Arc::new(StaticRawExec));
        s.on_text(HELLO_BINARY);
        let step = s.on_text(r#"{"type":"action","call_id":"c1","action":{"verb":"screenshot"}}"#);
        let Some(Reply::Binary(frame)) = step.reply else {
            panic!("expected a binary screenshot frame");
        };
        let hlen = u32::from_le_bytes(frame[..4].try_into().unwrap()) as usize;
        let header: serde_json::Value = serde_json::from_slice(&frame[4..4 + hlen]).unwrap();
        let hash = header["frame_hash"]
            .as_str()
            .expect("hash in binary header")
            .to_string();
        // Hash hit → a TEXT not_modified observation even on the binary session.
        let v = obs_json(s.on_text(&format!(
            r#"{{"type":"action","call_id":"c2","action":{{"verb":"screenshot","if_none_match":"{hash}"}}}}"#,
        )));
        assert_eq!(v["not_modified"], true);
        assert_eq!(v["frame_hash"], hash.as_str());
    }

    #[test]
    fn valid_window_scope_is_not_rejected_as_invalid() {
        let mut s = session(None);
        s.on_text(HELLO);
        // a well-formed window:<id> must not hit the invalid-scope rejection
        let step = s.on_text(
            r#"{"type":"action","call_id":"c2","action":{"verb":"screenshot","scope":"window:0x1a"}}"#,
        );
        assert!(!step
            .reply
            .unwrap()
            .into_text()
            .contains("invalid capture scope"));
    }

    #[test]
    fn non_v0_hello_is_rejected() {
        let mut s = session(None);
        let step = s.on_text(r#"{"type":"hello","v":1,"client":{"name":"t","version":"0"}}"#);
        assert!(step.close && !s.is_authenticated());
        assert!(step
            .reply
            .unwrap()
            .into_text()
            .contains("unsupported ACI version"));
    }

    #[test]
    fn action_dispatches_only_after_auth() {
        let mut s = session(None);
        s.on_text(HELLO);
        let step = s.on_text(
            r#"{"type":"action","call_id":"c1","action":{"verb":"click","target":{"kind":"point_px","x":1,"y":2}}}"#,
        );
        assert!(step.reply.unwrap().into_text().contains("\"type\":\"ack\""));
    }

    #[test]
    fn second_hello_is_rejected_not_reauth() {
        let mut s = session(None);
        s.on_text(HELLO);
        let step = s.on_text(HELLO);
        assert!(!step.close);
        assert!(step
            .reply
            .unwrap()
            .into_text()
            .contains("already authenticated"));
    }

    #[test]
    fn start_screencast_acks_and_requests_a_stream() {
        let mut s = session(None);
        s.on_text(HELLO);
        let step = s.on_text(
            r#"{"type":"action","call_id":"sc1","action":{"verb":"start_screencast","fps":12,"max_long_edge":640,"scope":"active_window"}}"#,
        );
        assert!(step.reply.unwrap().into_text().contains("\"type\":\"ack\""));
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
        assert!(step.reply.unwrap().into_text().contains("\"ok\":true"));
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
        let reply = step.reply.unwrap().into_text();
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
        assert!(step.reply.unwrap().into_text().contains("\"ok\":true"));
        assert_eq!(step.stream, StreamCtl::Stop);
    }

    // ---- act-returns-observation (`observe`) ----

    #[test]
    fn observe_on_mutating_action_acks_then_follows_up_with_observation() {
        let mut s = session(None);
        s.on_text(HELLO);
        let step = s.on_text(
            r#"{"type":"action","call_id":"c1","action":{"verb":"click",
                "target":{"kind":"point_px","x":1,"y":2},
                "observe":{"format":"jpeg","quality":70}}}"#,
        );
        let ack = step.reply.unwrap().into_text();
        assert!(ack.contains("\"type\":\"ack\"") && ack.contains("\"ok\":true"));
        let obs = step.followup.expect("observe must produce a follow-up");
        let v: serde_json::Value = serde_json::from_str(&obs.into_text()).unwrap();
        assert_eq!(v["type"], "observation");
        assert_eq!(
            v["cause"], "c1",
            "the follow-up correlates via cause=call_id"
        );
        assert_eq!(v["obs_id"], "obs-c1");
        assert_eq!(
            v["image"]["format"], "jpeg",
            "observe params reach the capture"
        );
        assert!(v["image"]["ref"].is_string());
    }

    #[test]
    fn observe_is_rejected_on_non_mutating_verbs() {
        let mut s = session(None);
        s.on_text(HELLO);
        for action in [
            r#"{"verb":"wait","ms":1,"observe":{}}"#,
            r#"{"verb":"screenshot","observe":{}}"#,
            r#"{"verb":"start_screencast","observe":{}}"#,
        ] {
            let step = s.on_text(&format!(
                r#"{{"type":"action","call_id":"c1","action":{action}}}"#
            ));
            let reply = step.reply.unwrap().into_text();
            assert!(
                reply.contains("\"ok\":false") && reply.contains("observe is not supported"),
                "observe on {action} must be nacked, got {reply}"
            );
            assert!(step.followup.is_none());
            // and the stream side effect must not fire either
            assert_eq!(step.stream, StreamCtl::None);
        }
    }

    // ---- desktop verbs (G2+G3): clipboard / launch_app / activate_window ----

    /// clipboard_set acks; clipboard_get answers a typed `result` whose value is
    /// `{text}` — the read's payload channel (an ack carries no data).
    #[test]
    fn clipboard_set_then_get_roundtrips_through_dispatch() {
        let mut s = session(None);
        s.on_text(HELLO);
        let step = s.on_text(
            r#"{"type":"action","call_id":"cb1","action":{"verb":"clipboard_set","text":"copy me"}}"#,
        );
        let reply = step.reply.unwrap().into_text();
        assert!(reply.contains("\"type\":\"ack\"") && reply.contains("\"ok\":true"));
        let step =
            s.on_text(r#"{"type":"action","call_id":"cb2","action":{"verb":"clipboard_get"}}"#);
        let v: serde_json::Value = serde_json::from_str(&step.reply.unwrap().into_text()).unwrap();
        assert_eq!(v["type"], "result");
        assert_eq!(v["call_id"], "cb2");
        assert_eq!(v["ok"], true);
        assert_eq!(v["value"]["text"], "copy me");
    }

    /// A backend without clipboard support nacks the read with the trait's typed
    /// error; an empty clipboard answers the backend's typed empty error.
    #[test]
    fn clipboard_get_nacks_unsupported_backend_and_empty_clipboard() {
        let mut s = Session::new(None, Arc::new(SizedExec(640, 480)));
        s.on_text(HELLO);
        let step =
            s.on_text(r#"{"type":"action","call_id":"cb1","action":{"verb":"clipboard_get"}}"#);
        let reply = step.reply.unwrap().into_text();
        assert!(
            reply.contains("\"ok\":false") && reply.contains("clipboard not supported"),
            "got {reply}"
        );
        let mut s = session(None); // virtual backend, nothing set yet
        s.on_text(HELLO);
        let step =
            s.on_text(r#"{"type":"action","call_id":"cb0","action":{"verb":"clipboard_get"}}"#);
        let reply = step.reply.unwrap().into_text();
        assert!(
            reply.contains("\"ok\":false") && reply.contains("clipboard is empty"),
            "got {reply}"
        );
    }

    /// The desktop WRITES are mutating (observe-after-act follows the ack); the
    /// clipboard READ is not (observe is rejected before dispatch).
    #[test]
    fn desktop_write_verbs_admit_observe_clipboard_get_does_not() {
        let mut s = session(None);
        s.on_text(HELLO);
        for (cid, action) in [
            ("cb1", r#"{"verb":"clipboard_set","text":"x","observe":{}}"#),
            (
                "la1",
                r#"{"verb":"launch_app","app":"xclock","observe":{}}"#,
            ),
            (
                "aw1",
                r#"{"verb":"activate_window","window_id":42,"observe":{}}"#,
            ),
        ] {
            let step = s.on_text(&format!(
                r#"{{"type":"action","call_id":"{cid}","action":{action}}}"#
            ));
            let reply = step.reply.unwrap().into_text();
            assert!(reply.contains("\"ok\":true"), "{action} → {reply}");
            let obs = step
                .followup
                .unwrap_or_else(|| panic!("{action} with observe must produce a follow-up"));
            let v: serde_json::Value = serde_json::from_str(&obs.into_text()).unwrap();
            assert_eq!(v["cause"], *cid, "follow-up correlates via cause");
        }
        let step = s.on_text(
            r#"{"type":"action","call_id":"cb2","action":{"verb":"clipboard_get","observe":{}}}"#,
        );
        let reply = step.reply.unwrap().into_text();
        assert!(
            reply.contains("\"ok\":false") && reply.contains("observe is not supported"),
            "got {reply}"
        );
    }

    /// MUTATING_VERBS must not drift from the schema's `$defs.MutatingVerb` enum.
    #[test]
    fn mutating_verbs_match_schema() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../schema/aci.schema.json");
        let raw = std::fs::read_to_string(path).expect("read schema/aci.schema.json");
        let schema: serde_json::Value = serde_json::from_str(&raw).expect("parse aci schema");
        let mut from_schema: Vec<String> = schema["$defs"]["MutatingVerb"]["enum"]
            .as_array()
            .expect("MutatingVerb enum")
            .iter()
            .map(|x| x.as_str().unwrap().to_string())
            .collect();
        from_schema.sort();
        let mut ours: Vec<String> = MUTATING_VERBS.iter().map(|s| s.to_string()).collect();
        ours.sort();
        assert_eq!(
            ours, from_schema,
            "runtime MUTATING_VERBS drifted from schema"
        );
    }

    #[test]
    fn invalid_observe_params_nack_before_the_action_executes() {
        let exec = Arc::new(VirtualExecutor::default());
        let mut s = Session::new(None, exec.clone());
        s.on_text(HELLO);
        for (action, want) in [
            (
                r#"{"verb":"click","target":{"kind":"point_px","x":1,"y":2},"observe":{"format":"webp"}}"#,
                "unsupported image format",
            ),
            (
                r#"{"verb":"click","target":{"kind":"point_px","x":1,"y":2},"observe":{"quality":0}}"#,
                "observe quality out of range",
            ),
            (
                r#"{"verb":"click","target":{"kind":"point_px","x":1,"y":2},"observe":{"scope":"bogus"}}"#,
                "invalid observe scope",
            ),
        ] {
            let step = s.on_text(&format!(
                r#"{{"type":"action","call_id":"c1","action":{action}}}"#
            ));
            let reply = step.reply.unwrap().into_text();
            assert!(
                reply.contains("\"ok\":false") && reply.contains(want),
                "expected {want:?} in {reply}"
            );
        }
        // validation happens BEFORE execution: nothing was half-applied
        assert!(exec.log.lock().unwrap().is_empty());
    }

    #[test]
    fn observe_followup_is_binary_on_a_binary_session() {
        let mut s = session(None);
        s.on_text(
            r#"{"type":"hello","v":0,"client":{"name":"t","version":"0"},"accept":{"binary_frames":true}}"#,
        );
        let step = s.on_text(
            r#"{"type":"action","call_id":"c2","action":{"verb":"type_text","text":"x","observe":{}}}"#,
        );
        assert!(step.reply.unwrap().into_text().contains("\"ok\":true"));
        match step.followup {
            Some(Reply::Binary(bytes)) => {
                let hlen = u32::from_le_bytes(bytes[..4].try_into().unwrap()) as usize;
                let header: serde_json::Value =
                    serde_json::from_slice(&bytes[4..4 + hlen]).unwrap();
                assert_eq!(header["cause"], "c2");
                assert!(header["image"]["len"].is_number());
            }
            other => panic!("expected a binary follow-up, got {other:?}"),
        }
    }

    #[test]
    fn observe_capture_failure_yields_typed_error_followup_after_honest_ack() {
        // SizedExec executes actions fine but cannot capture (trait default bails):
        // the ack must stay ok=true (the ACTION succeeded) and the follow-up must be
        // a typed error result so the client's observation wait resolves.
        let mut s = Session::new(None, Arc::new(SizedExec(640, 480)));
        s.on_text(HELLO);
        let step = s.on_text(
            r#"{"type":"action","call_id":"c3","action":{"verb":"key","keys":"a","observe":{}}}"#,
        );
        assert!(step.reply.unwrap().into_text().contains("\"ok\":true"));
        let v: serde_json::Value =
            serde_json::from_str(&step.followup.unwrap().into_text()).unwrap();
        assert_eq!(v["type"], "result");
        assert_eq!(v["call_id"], "c3");
        assert_eq!(v["ok"], false);
        assert!(v["error"].as_str().unwrap().contains("observe failed"));
    }

    #[test]
    fn failed_action_with_observe_nacks_without_a_followup() {
        // FailingExec: every action errors — observe must not capture anything.
        struct FailingExec;
        impl Executor for FailingExec {
            fn execute(&self, _: &ActionSpec) -> anyhow::Result<String> {
                anyhow::bail!("boom")
            }
            fn backend(&self) -> &'static str {
                "failing-test"
            }
        }
        let mut s = Session::new(None, Arc::new(FailingExec));
        s.on_text(HELLO);
        let step = s.on_text(
            r#"{"type":"action","call_id":"c4","action":{"verb":"key","keys":"a","observe":{}}}"#,
        );
        assert!(step.reply.unwrap().into_text().contains("\"ok\":false"));
        assert!(step.followup.is_none());
    }

    // ---- typed exec dispatch (G1) ----

    #[test]
    fn exec_dispatch_validates_and_returns_the_spec_side_effect() {
        use crate::exec::ExecCommand;
        let mut s = session(None);
        s.on_text(HELLO);
        // Buffered form: NO reply yet (the serve loop's exec task answers with the
        // typed result), the validated spec rides the Step.
        let step = s.on_text(
            r#"{"type":"action","call_id":"x1","action":{"verb":"exec","argv":["echo","hi"],"cwd":"/tmp"}}"#,
        );
        assert!(step.reply.is_none(), "buffered exec must not ack");
        let espec = step.exec.expect("exec side effect");
        assert_eq!(espec.call_id, "x1");
        assert_eq!(
            espec.command,
            ExecCommand::Argv(vec!["echo".into(), "hi".into()])
        );
        assert_eq!(espec.cwd.as_deref(), Some("/tmp"));
        assert!(!espec.stream && !espec.binary);
        // Streamed form: ack ok=true plus the spec.
        let step = s.on_text(
            r#"{"type":"action","call_id":"x2","action":{"verb":"exec","shell":"ls | wc -l","stream":true}}"#,
        );
        let reply = step.reply.unwrap().into_text();
        assert!(reply.contains("\"type\":\"ack\"") && reply.contains("\"ok\":true"));
        let espec = step.exec.expect("exec side effect");
        assert!(espec.stream);
        assert_eq!(espec.command, ExecCommand::Shell("ls | wc -l".into()));
    }

    #[test]
    fn exec_disabled_by_server_policy_is_not_advertised_or_dispatched() {
        let mut s = session(None).with_exec_enabled(false);
        let welcome = s.on_text(HELLO).reply.unwrap().into_text();
        let welcome: serde_json::Value = serde_json::from_str(&welcome).unwrap();
        let verbs = welcome["capabilities"]["verbs"].as_array().unwrap();
        assert!(!verbs.iter().any(|verb| verb == "exec"));

        let step = s.on_text(
            r#"{"type":"action","call_id":"x1","action":{"verb":"exec","argv":["true"]}}"#,
        );
        assert!(
            step.exec.is_none(),
            "disabled exec must never escape as a side effect"
        );
        let reply = step.reply.unwrap().into_text();
        assert!(reply.contains("\"ok\":false"));
        assert!(reply.contains("disabled by server policy"));
    }

    #[test]
    fn exec_binary_negotiation_reaches_the_spec() {
        let mut s = session(None);
        s.on_text(
            r#"{"type":"hello","v":0,"client":{"name":"t","version":"0"},"accept":{"binary_frames":true}}"#,
        );
        let step = s.on_text(
            r#"{"type":"action","call_id":"x1","action":{"verb":"exec","argv":["true"],"stream":true}}"#,
        );
        assert!(
            step.exec.unwrap().binary,
            "streamed chunks ride binary frames"
        );
    }

    #[test]
    fn exec_validation_failures_nack_without_a_side_effect() {
        let mut s = session(None);
        s.on_text(HELLO);
        for (action, want) in [
            (r#"{"verb":"exec"}"#, "requires"),
            (
                r#"{"verb":"exec","argv":["ls"],"shell":"ls"}"#,
                "exactly one",
            ),
            (r#"{"verb":"exec","argv":[]}"#, "empty"),
            (
                r#"{"verb":"exec","argv":["ls"],"pty":true}"#,
                "pty is reserved",
            ),
            // observe is not admitted on exec (not a MutatingVerb)
            (
                r#"{"verb":"exec","argv":["ls"],"observe":{}}"#,
                "observe is not supported",
            ),
        ] {
            let step = s.on_text(&format!(
                r#"{{"type":"action","call_id":"x1","action":{action}}}"#
            ));
            assert!(step.exec.is_none(), "no exec must escape: {action}");
            let reply = step.reply.unwrap().into_text();
            assert!(
                reply.contains("\"ok\":false") && reply.contains(want),
                "expected {want:?} for {action}, got {reply}"
            );
        }
    }

    // ---- drag / mouse_down / mouse_up dispatch + list_windows query ----

    #[test]
    fn gesture_verbs_dispatch_to_the_executor() {
        let exec = Arc::new(VirtualExecutor::default());
        let mut s = Session::new(None, exec.clone());
        s.on_text(HELLO);
        for action in [
            r#"{"verb":"drag","target":{"kind":"point_px","x":1,"y":2},"to":{"kind":"point_px","x":3,"y":4},"duration_ms":10,"button":"left"}"#,
            r#"{"verb":"mouse_down","target":{"kind":"point_px","x":1,"y":2}}"#,
            r#"{"verb":"mouse_up"}"#,
        ] {
            let step = s.on_text(&format!(
                r#"{{"type":"action","call_id":"c1","action":{action}}}"#
            ));
            assert!(step.reply.unwrap().into_text().contains("\"ok\":true"));
        }
        assert_eq!(
            exec.log.lock().unwrap().as_slice(),
            ["drag", "mouse_down", "mouse_up"]
        );
    }

    #[test]
    fn list_windows_query_answers_from_the_executor() {
        let mut s = session(None); // virtual backend: honest empty enumeration
        s.on_text(HELLO);
        let reply = s
            .on_text(r#"{"type":"query","call_id":"w1","q":"list_windows"}"#)
            .reply
            .unwrap()
            .into_text();
        let v: serde_json::Value = serde_json::from_str(&reply).unwrap();
        assert_eq!(v["ok"], true);
        assert_eq!(v["value"], serde_json::json!([]));
        // a backend without enumeration answers with a typed error, not a fabrication
        let mut s = Session::new(None, Arc::new(SizedExec(640, 480)));
        s.on_text(HELLO);
        let reply = s
            .on_text(r#"{"type":"query","call_id":"w2","q":"list_windows"}"#)
            .reply
            .unwrap()
            .into_text();
        let v: serde_json::Value = serde_json::from_str(&reply).unwrap();
        assert_eq!(v["ok"], false);
        assert!(v["error"]
            .as_str()
            .unwrap()
            .contains("list_windows not supported"));
    }

    // ---- structured observation + element_ref actions (M1b) ----

    use crate::observe::tests::{sample_tree, FakeSource};
    use crate::observe::STALE_ELEMENT_PREFIX;

    /// `(verb, resolved point)` as seen by the backend.
    type RecordedTarget = (String, Option<(f64, f64)>);

    /// An executor that records the RESOLVED target of every pointer action, to
    /// prove element_ref → bbox-centre point_px rewriting reaches the backend.
    #[derive(Default)]
    struct TargetRecorder {
        targets: std::sync::Mutex<Vec<RecordedTarget>>,
    }
    impl Executor for TargetRecorder {
        fn execute(&self, a: &ActionSpec) -> anyhow::Result<String> {
            let point = match &a.target {
                Some(Target::PointPx { x, y }) => Some((*x, *y)),
                _ => None,
            };
            self.targets.lock().unwrap().push((a.verb.clone(), point));
            Ok("ok".into())
        }
        fn backend(&self) -> &'static str {
            "target-recorder"
        }
    }

    fn observing_session(src: Arc<FakeSource>) -> (Session, Arc<TargetRecorder>) {
        let exec = Arc::new(TargetRecorder::default());
        let mut s = Session::new(None, exec.clone()).with_tree_source(src);
        s.on_text(HELLO);
        (s, exec)
    }

    const OBSERVE: &str =
        r#"{"type":"action","call_id":"o1","action":{"verb":"observe","structured":true}}"#;

    #[test]
    fn observe_returns_structured_observation_with_stable_ids() {
        let src = Arc::new(FakeSource::new(sample_tree()));
        let (mut s, _) = observing_session(src);
        let v = obs_json(s.on_text(OBSERVE));
        assert_eq!(v["type"], "observation");
        assert_eq!(v["cause"], "o1");
        assert_eq!(v["tree"], "full");
        assert_eq!(v["revision"], 1);
        assert_eq!(v["node_count"], 3);
        assert_eq!(v["focus"], "e3");
        assert!(v["capture_ms"].is_number());
        let text = v["tree_text"].as_str().unwrap();
        assert!(text.starts_with("app: zenity"), "{text}");
        assert!(text.contains("e2 push button"));
        assert!(text.ends_with("focus: e3"));
        assert_eq!(v["elements"].as_array().unwrap().len(), 3);
        // Re-observe: SAME ids, bumped revision.
        let v2 =
            obs_json(s.on_text(r#"{"type":"action","call_id":"o2","action":{"verb":"observe"}}"#));
        assert_eq!(v2["revision"], 2);
        assert_eq!(v2["elements"][1]["ref"], "e2", "ids stable across observes");
    }

    #[test]
    fn observe_diff_renders_change_against_previous_revision() {
        let src = Arc::new(FakeSource::new(sample_tree()));
        let (mut s, _) = observing_session(src.clone());
        s.on_text(OBSERVE);
        src.tree.lock().unwrap().nodes[2].value = Some("typed!".to_string());
        let v = obs_json(s.on_text(
            r#"{"type":"action","call_id":"o2","action":{"verb":"observe","diff":true}}"#,
        ));
        assert_eq!(v["tree"], "diff");
        assert_eq!(v["diff_of"], 1);
        assert!(
            v["tree_text"].as_str().unwrap().contains("~   e3 entry"),
            "{}",
            v["tree_text"]
        );
        // The raw structured array still carries the FULL element list.
        assert_eq!(v["elements"].as_array().unwrap().len(), 3);
    }

    #[test]
    fn observe_without_a_tree_source_is_a_typed_nack() {
        let mut s = session(None); // no tree source attached
        s.on_text(HELLO);
        let v = obs_json(s.on_text(OBSERVE));
        assert_eq!(v["ok"], false);
        assert!(
            v["error"]
                .as_str()
                .unwrap()
                .contains("structured_observation_unavailable"),
            "{v}"
        );
        // and explicit structured:false is rejected, not aliased to pixels
        let src = Arc::new(FakeSource::new(sample_tree()));
        let (mut s, _) = observing_session(src);
        let v = obs_json(s.on_text(
            r#"{"type":"action","call_id":"oX","action":{"verb":"observe","structured":false}}"#,
        ));
        assert_eq!(v["ok"], false);
        assert!(v["error"].as_str().unwrap().contains("screenshot"));
    }

    #[test]
    fn element_ref_click_resolves_to_bbox_centre_point() {
        let src = Arc::new(FakeSource::new(sample_tree()));
        let (mut s, exec) = observing_session(src);
        s.on_text(OBSERVE);
        let v = obs_json(s.on_text(
            r#"{"type":"action","call_id":"c1","action":{"verb":"click","target":{"kind":"element_ref","ref":"e2"}}}"#,
        ));
        assert_eq!(v["ok"], true, "{v}");
        let recorded = exec.targets.lock().unwrap().clone();
        // sample_tree's button bbox is [10, 20, 80, 30] → centre (50, 35)
        assert_eq!(recorded, vec![("click".to_string(), Some((50.0, 35.0)))]);
    }

    #[test]
    fn stale_or_unknown_element_ref_is_a_machine_readable_nack() {
        let src = Arc::new(FakeSource::new(sample_tree()));
        let (mut s, exec) = observing_session(src.clone());
        // Before ANY observation: stale-typed.
        let v = obs_json(s.on_text(
            r#"{"type":"action","call_id":"c0","action":{"verb":"click","target":{"kind":"element_ref","ref":"e2"}}}"#,
        ));
        assert_eq!(v["ok"], false);
        assert!(v["error"]
            .as_str()
            .unwrap()
            .starts_with(STALE_ELEMENT_PREFIX));
        // Observe, then make the button disappear and re-observe: its id is evicted.
        s.on_text(OBSERVE);
        src.tree.lock().unwrap().nodes.remove(1);
        s.on_text(r#"{"type":"action","call_id":"o2","action":{"verb":"observe"}}"#);
        let v = obs_json(s.on_text(
            r#"{"type":"action","call_id":"c1","action":{"verb":"click","target":{"kind":"element_ref","ref":"e2"}}}"#,
        ));
        assert_eq!(v["ok"], false);
        let err = v["error"].as_str().unwrap();
        assert!(
            err.starts_with(STALE_ELEMENT_PREFIX) && err.contains("re-observe"),
            "{err}"
        );
        assert!(
            exec.targets.lock().unwrap().is_empty(),
            "no click must reach the backend"
        );
    }

    #[test]
    fn invoke_action_routes_to_the_ax_backend_by_name() {
        let src = Arc::new(FakeSource::new(sample_tree()));
        let (mut s, _) = observing_session(src.clone());
        s.on_text(OBSERVE);
        let v = obs_json(s.on_text(
            r#"{"type":"action","call_id":"i1","action":{"verb":"invoke_action","target":{"kind":"element_ref","ref":"e2"},"text":"click"}}"#,
        ));
        assert_eq!(v["ok"], true, "{v}");
        assert_eq!(
            src.invoked.lock().unwrap().clone(),
            vec![(":1.9/b".to_string(), Some("click".to_string()))]
        );
        // A missing target is rejected with the verb named.
        let v = obs_json(
            s.on_text(r#"{"type":"action","call_id":"i2","action":{"verb":"invoke_action"}}"#),
        );
        assert_eq!(v["ok"], false);
        assert!(v["error"].as_str().unwrap().contains("element_ref target"));
    }

    #[test]
    fn set_value_requires_text_and_routes_the_value() {
        let src = Arc::new(FakeSource::new(sample_tree()));
        let (mut s, _) = observing_session(src.clone());
        s.on_text(OBSERVE);
        let v = obs_json(s.on_text(
            r#"{"type":"action","call_id":"s1","action":{"verb":"set_value","target":{"kind":"element_ref","ref":"e3"}}}"#,
        ));
        assert_eq!(v["ok"], false);
        assert!(v["error"].as_str().unwrap().contains("requires `text`"));
        let v = obs_json(s.on_text(
            r#"{"type":"action","call_id":"s2","action":{"verb":"set_value","target":{"kind":"element_ref","ref":"e3"},"text":"hello"}}"#,
        ));
        assert_eq!(v["ok"], true, "{v}");
        assert_eq!(
            src.set.lock().unwrap().clone(),
            vec![(":1.9/e".to_string(), "hello".to_string())]
        );
    }
}
