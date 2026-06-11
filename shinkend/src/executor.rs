//! Action execution — a backend-pluggable executor behind the typed ACI action.
//!
//! M1a implements **pointer** actions (move/click/double_click/right_click/scroll) on
//! `point_px`/`point_norm` targets via the X11 XTEST extension. `type_text`/`key`
//! (keysym mapping), `element_ref` resolution, and `screenshot` arrive in M1b. The
//! router prefers semantic actuation later (CDP/AT-SPI); X11/XTEST is the P0 baseline
//! for the Linux fork tier we control (see docs/11-aci-spec.md §3.1).

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use anyhow::{bail, ensure, Context, Result};
use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use serde::Deserialize;
use x11rb::connection::Connection;
use x11rb::protocol::xproto::{
    AtomEnum, ClientMessageEvent, ConfigureWindowAux, ConnectionExt as _, EventMask,
    ImageFormat as XImageFormat, InputFocus, MapState, StackMode, Window, BUTTON_PRESS_EVENT,
    BUTTON_RELEASE_EVENT, KEY_PRESS_EVENT, KEY_RELEASE_EVENT, MOTION_NOTIFY_EVENT,
};
use x11rb::protocol::xtest::ConnectionExt as _;

/// A spatial action target (mirrors `Target` in schema/aci.schema.json).
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
// `element_ref`/`source` are part of the ACI v0 wire contract; first read in M1b.
#[allow(dead_code)]
pub enum Target {
    PointPx {
        x: f64,
        y: f64,
    },
    PointNorm {
        x: f64,
        y: f64,
    },
    ElementRef {
        #[serde(rename = "ref")]
        element_ref: String,
        #[serde(default)]
        source: Option<String>,
    },
}

/// A typed action parsed from the `action` field of an ACI `action` message.
/// Unknown fields are rejected so wire drift fails loudly (#23).
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
// `dx`/`ms`/`scope` are part of the ACI v0 wire contract; read in later milestones.
#[allow(dead_code)]
pub struct ActionSpec {
    pub verb: String,
    #[serde(default)]
    pub target: Option<Target>,
    /// Drag destination: pointer down at `target`, interpolated moves, up at `to`.
    #[serde(default)]
    pub to: Option<Target>,
    /// Pointer button for `drag`/`mouse_down`/`mouse_up` (`left`/`middle`/`right`);
    /// omitted = left.
    #[serde(default)]
    pub button: Option<String>,
    /// Drag gesture duration (ms), spread across the interpolated path; omitted = 0
    /// (fastest). Clamped to [`MAX_DRAG_MS`].
    #[serde(default)]
    pub duration_ms: Option<u64>,
    /// Act-returns-observation (#observe-after-act): on a mutating verb, capture a
    /// fresh observation right after the action and push it with `cause` = the
    /// action's call_id. Consumed by the dispatcher (connection.rs), never here.
    #[serde(default)]
    pub observe: Option<ObserveSpec>,
    #[serde(default)]
    pub text: Option<String>,
    #[serde(default)]
    pub keys: Option<String>,
    #[serde(default)]
    pub dx: Option<f64>,
    #[serde(default)]
    pub dy: Option<f64>,
    #[serde(default)]
    pub ms: Option<u64>,
    #[serde(default)]
    pub scope: Option<String>,
    /// Target frame rate for `start_screencast` (frames/sec).
    #[serde(default)]
    pub fps: Option<f64>,
    /// Cap the captured frame's longer edge (px) — a screencast/screenshot bandwidth
    /// lever. Larger frames are downscaled; `None` keeps full resolution.
    #[serde(default)]
    pub max_long_edge: Option<u32>,
    /// Wire codec for `screenshot`/`start_screencast` frames: `png` (default, lossless)
    /// or `jpeg` (bandwidth lever). Unknown values are rejected.
    #[serde(default)]
    pub format: Option<String>,
    /// JPEG quality 1–100 (ignored for PNG); defaults to a balanced value when JPEG is
    /// requested without one.
    #[serde(default)]
    pub quality: Option<u8>,
    /// For `start_screencast`: ask to continue the named logical stream. If the
    /// runtime still holds that stream's state, frames keep the SAME `stream` id and
    /// `seq` continues where it left off; otherwise a fresh stream starts (#56).
    #[serde(default)]
    pub resume_stream: Option<String>,
    /// For `start_screencast`: dirty-tile delta mode (B2). Only changed 64px tiles
    /// are pushed (`tiles` on the observation) instead of full frames; a full
    /// keyframe is sent first, after a resume, and every [`KEYFRAME_INTERVAL`]th
    /// delivered frame. Omitted = full frames.
    #[serde(default)]
    pub delta: Option<bool>,
    /// For `screenshot`: content-negotiated observation. A `frame_hash` from a
    /// previous screenshot observation; when the freshly captured frame's raw-pixel
    /// hash equals it, the runtime answers with a compact `not_modified` observation
    /// (no payload) instead of re-encoding and re-sending identical pixels. Clients
    /// gate on `capabilities.frame_dedup` (this struct rejects unknown fields, so a
    /// pre-dedup runtime nacks an action carrying this).
    #[serde(default)]
    pub if_none_match: Option<String>,
    /// For `observe`: capture the structured (a11y) tree. Omitted defaults to true —
    /// `observe` IS the structured verb (pixels are `screenshot`); an explicit
    /// `false` is rejected rather than silently aliased.
    #[serde(default)]
    pub structured: Option<bool>,
    /// For `observe`: render the tree text as a diff (`~`/`+`/`-` lines) against this
    /// session's previous revision. Omitted = full tree.
    #[serde(default)]
    pub diff: Option<bool>,
    /// For `observe`: debounce a11y event notifications for this quiesce window (ms,
    /// clamped) before walking, so the tree is captured after the UI settles. The
    /// total settle wait is hard-capped runtime-side.
    #[serde(default)]
    pub settle_ms: Option<u64>,
    /// For `exec`: the program + arguments, executed directly (no shell) — the
    /// DEFAULT form. Exactly one of `argv`/`shell` is required (see `crate::exec`).
    #[serde(default)]
    pub argv: Option<Vec<String>>,
    /// For `exec`: a shell command line run via `/bin/sh -c` — the explicit opt-in
    /// alternative to `argv` (shell parsing is a choice, never a silent default).
    #[serde(default)]
    pub shell: Option<String>,
    /// For `exec`: the child's working directory (guest path).
    #[serde(default)]
    pub cwd: Option<String>,
    /// For `exec`: extra environment variables merged over the runtime's.
    #[serde(default)]
    pub env: Option<HashMap<String, String>>,
    /// For `exec`: kill-the-process-group deadline (ms, clamped runtime-side).
    #[serde(default)]
    pub timeout_ms: Option<u64>,
    /// For `exec`: text written to the child's stdin, then closed.
    #[serde(default)]
    pub stdin: Option<String>,
    /// For `exec`: streamed form (`exec_output` events + `exec_exit`) instead of
    /// the buffered `result`.
    #[serde(default)]
    pub stream: Option<bool>,
    /// For `exec`: RESERVED — PTY allocation is a designed follow-up; only `false`
    /// is accepted (the schema pins `const: false`, the runtime nacks `true`).
    #[serde(default)]
    pub pty: Option<bool>,
    /// For `launch_app`: the executable to start (PATH name or absolute path, spawned
    /// detached with the session environment — never through a shell). For
    /// `activate_window`: a window selector — the first window whose title contains
    /// this string (case-insensitive) is activated.
    #[serde(default)]
    pub app: Option<String>,
    /// For `launch_app`: argv tail passed verbatim to the executable.
    #[serde(default)]
    pub args: Option<Vec<String>>,
    /// For `activate_window`: the window to raise+focus — an `id` from the
    /// `list_windows` query (also usable as the `window:<id>` capture scope).
    #[serde(default)]
    pub window_id: Option<u32>,
}

/// Act-returns-observation parameters (schema `$defs.ObserveSpec`): the same capture
/// levers as a one-shot `screenshot`, attached to a mutating action. Unknown fields are
/// rejected so wire drift fails loudly, like [`ActionSpec`] itself.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ObserveSpec {
    #[serde(default)]
    pub scope: Option<String>,
    #[serde(default)]
    pub format: Option<String>,
    #[serde(default)]
    pub quality: Option<u8>,
    #[serde(default)]
    pub max_long_edge: Option<u32>,
}

/// One visible top-level window, answered by the `list_windows` query — the Linux
/// "enumerate apps" read primitive (EWMH `_NET_CLIENT_LIST` etc.; see
/// [`X11Executor::list_windows`]).
#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct WindowInfo {
    /// X11 window id — usable as the `window:<id>` capture scope.
    pub id: u32,
    /// Window title (`_NET_WM_NAME`, falling back to `WM_NAME`); empty if unset.
    pub title: String,
    /// Owning process id (`_NET_WM_PID`), when the window publishes one.
    pub pid: Option<u32>,
    /// Root-relative geometry (px).
    pub x: i32,
    pub y: i32,
    pub w: u32,
    pub h: u32,
    /// Whether this is the EWMH `_NET_ACTIVE_WINDOW`.
    pub focused: bool,
}

/// Wire image codec for a captured frame. PNG is the lossless default (back-compatible
/// with v0 clients that never sent a `format`); JPEG is the bandwidth lever — a 1080p
/// desktop screenshot is ~1.8 MB as PNG but ~80–150 KB as quality-80 JPEG, which is what
/// makes driving many sandboxes from one control process affordable (#bandwidth).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ImageFormat {
    Png,
    Jpeg,
}

impl ImageFormat {
    /// Wire/MIME-subtype string for this codec.
    pub fn as_str(self) -> &'static str {
        match self {
            ImageFormat::Png => "png",
            ImageFormat::Jpeg => "jpeg",
        }
    }

    /// Parse a wire `format` value; an absent field keeps the lossless PNG default.
    /// Accepts exactly the schema enum (`png` | `jpeg`) — no aliases, no trimming — so the
    /// runtime never accepts a value the published contract rejects. An unknown codec is
    /// an error (no silent fallback — the client asked for something the runtime cannot
    /// honor).
    pub fn parse(s: Option<&str>) -> Result<ImageFormat> {
        match s {
            None => Ok(ImageFormat::Png),
            Some("png") => Ok(ImageFormat::Png),
            Some("jpeg") => Ok(ImageFormat::Jpeg),
            Some(other) => bail!("unsupported image format: {other:?} (expected png or jpeg)"),
        }
    }
}

/// How to encode a captured frame: optional downscale, codec, and (JPEG) quality.
#[derive(Debug, Clone, Copy)]
pub struct EncodeOpts {
    pub max_long_edge: Option<u32>,
    pub format: ImageFormat,
    /// JPEG quality 1–100; ignored for PNG. Clamped into range at encode time.
    pub quality: u8,
}

/// Default JPEG quality — high enough that a VLM reads the screen cleanly, low enough
/// for the bandwidth win. Used when a client requests JPEG without a `quality`.
pub const DEFAULT_JPEG_QUALITY: u8 = 80;

impl Default for EncodeOpts {
    fn default() -> Self {
        EncodeOpts {
            max_long_edge: None,
            format: ImageFormat::Png,
            quality: DEFAULT_JPEG_QUALITY,
        }
    }
}

/// A captured frame: raw encoded image bytes, the codec they are in, and pixel dims.
/// Bytes stay raw end-to-end; the TEXT wire path base64-encodes at the protocol
/// boundary ([`CapturedImage::to_base64`]), the binary path sends them verbatim.
#[derive(Debug, Clone)]
pub struct CapturedImage {
    pub data: Vec<u8>,
    pub format: ImageFormat,
    pub w: u16,
    pub h: u16,
}

impl CapturedImage {
    /// Base64 of the encoded image bytes — only for the legacy base64-in-JSON path.
    pub fn to_base64(&self) -> String {
        B64.encode(&self.data)
    }
}

/// Guest-side readiness, answered by the `ready` query (schema `Query.q`): whether
/// the runtime can usefully serve observations RIGHT NOW. Computed in microseconds
/// (a few sampled root pixels via 1×1 GetImage), so a client can poll it at 10 ms
/// granularity during boot instead of pulling and decoding full screenshots.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Readiness {
    /// The one bit a readiness poll needs: observations of a live desktop will work.
    pub ready: bool,
    /// Whether the X11 display connection is established (false while the lazy
    /// backend is still retrying `$DISPLAY` during desktop boot).
    pub x11_up: bool,
    /// Whether sampled root-window pixels are non-black (the desktop has painted).
    /// `None` when the backend has no display to sample (virtual/pyautogui) or X11
    /// is not up yet.
    pub root_nonblack: Option<bool>,
    /// Whether the host still owes the runtime user-granted permissions before
    /// observations/input can work — macOS TCC (Screen Recording / Accessibility).
    /// `None` on backends with no such concept (X11/virtual): a display that is up
    /// is fully usable there.
    pub permissions_pending: Option<bool>,
}

/// Executes typed actions against a guest. Backends are swappable per OS.
pub trait Executor: Send + Sync {
    /// Execute one action; returns a short status string or an error.
    fn execute(&self, action: &ActionSpec) -> Result<String>;
    /// A human label for the active backend.
    fn backend(&self) -> &'static str;
    /// The display geometry `(width, height)` in px, for the `screen_size` query (#138).
    /// Default suits a headless/no-display backend; X11 reports the real root size.
    fn screen_size(&self) -> (u16, u16) {
        (1280, 800)
    }
    /// Capture the full screen as a PNG (M1b). Default: unsupported.
    fn screenshot(&self) -> Result<CapturedImage> {
        bail!("screenshot not supported by the {} backend", self.backend())
    }
    /// Capture a region (`scope`), optionally downscaled so the longer edge is at most
    /// `opts.max_long_edge` px and encoded in `opts.format` (a screencast bandwidth
    /// lever). `scope` is one of `screen`, `active_window`, or `window:<id>`. Default:
    /// full screen, no scaling, lossless PNG.
    fn capture(&self, _scope: &str, _opts: EncodeOpts) -> Result<CapturedImage> {
        self.screenshot()
    }
    /// Capture a region as raw RGB8 `(pixels, w, h)`, downscaled (if `max_long_edge`)
    /// but NOT encoded — the dirty-tile delta path (B2) needs pixels to diff before
    /// deciding what to encode. Downscale happens HERE, before tiling, so tile
    /// coordinates align with the delivered resolution. Default: unsupported.
    fn capture_raw(
        &self,
        _scope: &str,
        _max_long_edge: Option<u32>,
    ) -> Result<(Vec<u8>, u16, u16)> {
        bail!(
            "raw capture (delta screencast) not supported by the {} backend",
            self.backend()
        )
    }
    /// Whether [`Executor::capture_raw`] works on this backend — checked at
    /// `start_screencast` dispatch so a `delta` request on a backend without raw
    /// capture is nacked loudly instead of acked and then silently dying.
    fn supports_raw_capture(&self) -> bool {
        false
    }
    /// Begin damage-driven change tracking for FULL-SCREEN capture: returns the
    /// current damage cursor, or `None` when unavailable (backend without XDamage,
    /// extension missing, or `SHINKEND_DAMAGE=off`) — callers then poll-capture
    /// every tick as before.
    fn damage_cursor(&self) -> Option<u64> {
        None
    }
    /// What changed on the root window strictly after `cursor`: `(new cursor,
    /// verdict)`. `None` mirrors [`Executor::damage_cursor`]'s unavailability.
    fn damage_since(&self, _cursor: u64) -> Option<(u64, DamageSince)> {
        None
    }
    /// Capture a sub-rectangle of the FULL SCREEN as raw RGB8 (no downscale) — the
    /// damage-driven delta path fetches only the damaged region with this. Default:
    /// unsupported.
    fn capture_raw_region(&self, _rect: DamageRect) -> Result<Vec<u8>> {
        bail!(
            "region capture not supported by the {} backend",
            self.backend()
        )
    }

    /// Cheap guest-side readiness (the `ready` query). Default suits a no-display
    /// backend: serving as soon as the listener is up, nothing to sample.
    fn readiness(&self) -> Readiness {
        Readiness {
            ready: true,
            x11_up: false,
            root_nonblack: None,
            permissions_pending: None,
        }
    }

    /// Enumerate visible top-level windows (the `list_windows` query). Default:
    /// unsupported — a backend without window enumeration answers the query with an
    /// error rather than fabricating an empty desktop.
    fn list_windows(&self) -> Result<Vec<WindowInfo>> {
        bail!(
            "list_windows not supported by the {} backend",
            self.backend()
        )
    }

    /// Read the desktop clipboard as text (the `clipboard_get` verb; G2). The write
    /// side (`clipboard_set`) rides [`Executor::execute`] like every other mutating
    /// verb — only the read needs its own channel for the returned `{text}`.
    /// Default: unsupported (typed error, never a fabricated empty string).
    fn clipboard_get(&self) -> Result<String> {
        bail!("clipboard not supported by the {} backend", self.backend())
    }
}

// ---- XDamage-driven change tracking ----

/// A damage bounding rectangle `(x, y, w, h)` in root-window pixels.
pub type DamageRect = (u32, u32, u32, u32);

/// Union of two damage rects (bounding box).
pub fn union_rect(a: DamageRect, b: DamageRect) -> DamageRect {
    let x0 = a.0.min(b.0);
    let y0 = a.1.min(b.1);
    let x1 = (a.0 + a.2).max(b.0 + b.2);
    let y1 = (a.1 + a.3).max(b.1 + b.3);
    (x0, y0, x1 - x0, y1 - y0)
}

/// Clamp a damage rect to `(w, h)`; `None` if nothing remains on-screen.
pub fn clamp_rect(r: DamageRect, w: u16, h: u16) -> Option<DamageRect> {
    let (w, h) = (w as u32, h as u32);
    let x0 = r.0.min(w);
    let y0 = r.1.min(h);
    let x1 = (r.0 + r.2).min(w);
    let y1 = (r.1 + r.3).min(h);
    if x1 > x0 && y1 > y0 {
        Some((x0, y0, x1 - x0, y1 - y0))
    } else {
        None
    }
}

/// What changed since a damage cursor.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum DamageSince {
    /// Nothing — the screen is byte-identical to the cursor's point in time, so a
    /// capture tick can skip the GetImage entirely (true idle-zero).
    Clean,
    /// Everything since the cursor fits this bounding region (root coords).
    Region(DamageRect),
    /// Unknown extent (cursor older than the retained log, or the tracker hit an
    /// error) — treat as full-frame damage. Always the SAFE answer.
    Full,
}

/// Bounded epoch log of damage regions — the pure bookkeeping behind the X-backed
/// tracker, factored out so the cursor semantics are unit-testable without X.
///
/// Each `record` is one batch of damage (epoch += 1, bounding rect retained); a
/// consumer holds a cursor (the last epoch it has fully captured) and asks
/// [`DamageLog::since`] what happened after it. Multiple consumers (one per
/// screencast) can hold independent cursors against the one log. The ring is
/// bounded; a cursor that falls off the retained window gets `Full` — correctness
/// degrades to a full capture, never to a missed change.
#[derive(Debug, Default)]
pub struct DamageLog {
    epoch: u64,
    /// Epochs at or below this have been dropped from the ring.
    base: u64,
    ring: std::collections::VecDeque<(u64, DamageRect)>,
}

/// Retained damage epochs. At 30 fps a consumer is at most ~1 tick behind, so 256
/// gives orders of magnitude of slack for MAX_SCREENCASTS consumers while bounding
/// memory at a few KB.
const DAMAGE_RING_MAX: usize = 256;

impl DamageLog {
    /// Record one batch of damage; returns the new epoch.
    pub fn record(&mut self, rect: DamageRect) -> u64 {
        self.epoch += 1;
        self.ring.push_back((self.epoch, rect));
        if self.ring.len() > DAMAGE_RING_MAX {
            if let Some((evicted, _)) = self.ring.pop_front() {
                self.base = evicted;
            }
        }
        self.epoch
    }

    /// The current epoch (a fresh consumer's starting cursor).
    pub fn epoch(&self) -> u64 {
        self.epoch
    }

    /// What changed strictly after `cursor`.
    pub fn since(&self, cursor: u64) -> DamageSince {
        if cursor >= self.epoch {
            return DamageSince::Clean;
        }
        if cursor < self.base {
            return DamageSince::Full;
        }
        let mut acc: Option<DamageRect> = None;
        for (e, r) in &self.ring {
            if *e > cursor {
                acc = Some(match acc {
                    Some(a) => union_rect(a, *r),
                    None => *r,
                });
            }
        }
        match acc {
            Some(r) => DamageSince::Region(r),
            None => DamageSince::Clean,
        }
    }
}

/// What region a capture should cover (parsed from the ACI `scope` string).
#[derive(Debug, Clone, PartialEq)]
pub enum Scope {
    Screen,
    ActiveWindow,
    Window(u32),
}

/// Parse a capture scope. `window:<id>` accepts decimal or `0x`-hex. Unknown scopes
/// fall back to the full screen so a typo degrades gracefully rather than erroring.
pub(crate) fn parse_scope(scope: &str) -> Scope {
    match scope {
        "screen" | "" => Scope::Screen,
        "active_window" | "active" => Scope::ActiveWindow,
        s => s
            .strip_prefix("window:")
            .and_then(|id| {
                id.strip_prefix("0x")
                    .and_then(|h| u32::from_str_radix(h, 16).ok())
                    .or_else(|| id.parse::<u32>().ok())
            })
            .map_or(Scope::Screen, Scope::Window),
    }
}

/// Map a normalized `point_norm` (x, y) to pixel coords, rejecting anything outside
/// `[0, 1]` — out-of-range would map off-screen, violating the ACI contract (#141).
fn norm_to_px(x: f64, y: f64, w: u16, h: u16) -> Result<(i16, i16)> {
    if !(0.0..=1.0).contains(&x) || !(0.0..=1.0).contains(&y) {
        bail!("point_norm out of range: ({x}, {y}) — must be within [0, 1]");
    }
    // Map onto [0, dim-1] so 1.0 lands on the last on-screen pixel, not one past it.
    let px = |n: f64, dim: u16| (n * (dim.saturating_sub(1)) as f64).round() as i16;
    Ok((px(x, w), px(y, h)))
}

/// [`norm_to_px`] for backends that keep float pixel coordinates (macOS posts
/// CGEvents in floating-point display points) — identical validation + mapping.
#[cfg(all(target_os = "macos", feature = "macos-native"))]
pub(crate) fn norm_to_px_f64(x: f64, y: f64, w: u16, h: u16) -> Result<(f64, f64)> {
    let (px, py) = norm_to_px(x, y, w, h)?;
    Ok((px as f64, py as f64))
}

/// Downscale an RGB8 buffer with nearest-neighbour sampling so its longer edge is at
/// most `max_long_edge` px. Returns the buffer unchanged if it already fits (or the
/// cap is 0). Cheap and dependency-free — good enough for a bandwidth preview.
pub(crate) fn downscale_rgb(rgb: &[u8], w: u16, h: u16, max_long_edge: u32) -> (Vec<u8>, u16, u16) {
    let (wu, hu) = (w as u32, h as u32);
    let long = wu.max(hu);
    if max_long_edge == 0 || long <= max_long_edge {
        return (rgb.to_vec(), w, h);
    }
    let nw = (wu * max_long_edge).div_ceil(long).max(1);
    let nh = (hu * max_long_edge).div_ceil(long).max(1);
    let mut out = Vec::with_capacity((nw * nh * 3) as usize);
    for y in 0..nh {
        let sy = (y as u64 * hu as u64 / nh as u64) as usize;
        for x in 0..nw {
            let sx = (x as u64 * wu as u64 / nw as u64) as usize;
            let idx = (sy * wu as usize + sx) * 3;
            out.extend_from_slice(&rgb[idx..idx + 3]);
        }
    }
    (out, nw as u16, nh as u16)
}

/// Encode raw RGB8 pixels as a PNG byte stream.
fn encode_png(rgb: &[u8], w: u32, h: u32) -> Result<Vec<u8>> {
    let mut out = Vec::new();
    {
        let mut enc = png::Encoder::new(&mut out, w, h);
        enc.set_color(png::ColorType::Rgb);
        enc.set_depth(png::BitDepth::Eight);
        let mut writer = enc.write_header()?;
        writer.write_image_data(rgb)?;
        writer.finish()?;
    }
    Ok(out)
}

/// Encode raw RGB8 pixels as a baseline JPEG. `quality` is clamped to 1–100. Pure-Rust
/// encoder (no C deps) so the static musl build that gets injected into a sandbox stays
/// portable.
fn encode_jpeg(rgb: &[u8], w: u16, h: u16, quality: u8) -> Result<Vec<u8>> {
    let q = quality.clamp(1, 100);
    let mut out = Vec::new();
    let enc = jpeg_encoder::Encoder::new(&mut out, q);
    enc.encode(rgb, w, h, jpeg_encoder::ColorType::Rgb)
        .map_err(|e| anyhow::anyhow!("jpeg encode failed: {e}"))?;
    Ok(out)
}

/// FNV-1a 64-bit offset basis / prime.
const FNV1A_BASIS: u64 = 0xcbf2_9ce4_8422_2325;
const FNV1A_PRIME: u64 = 0x0000_0100_0000_01b3;

/// Fold `bytes` into a running FNV-1a 64 state (start from [`FNV1A_BASIS`]).
fn fnv1a_fold(mut hash: u64, bytes: &[u8]) -> u64 {
    for &b in bytes {
        hash ^= b as u64;
        hash = hash.wrapping_mul(FNV1A_PRIME);
    }
    hash
}

/// FNV-1a 64-bit hash — the cheap content hash behind idle-frame suppression
/// (screencast, over encoded bytes) and screenshot dedup (over raw pixels).
pub fn fnv1a(bytes: &[u8]) -> u64 {
    fnv1a_fold(FNV1A_BASIS, bytes)
}

/// The wire `frame_hash` of a captured frame: FNV-1a 64 over the RAW RGB8 pixels
/// (dims folded in first, so a row-shift between geometries can't alias), as 16
/// lowercase hex chars.
///
/// The hash is deliberately computed over RAW pixels (post-scope/downscale,
/// PRE-encode), not the encoded payload, so it is **codec-independent**: the same
/// framebuffer hashes identically whether the client asked for png or jpeg at any
/// quality, and stays stable across encoder versions/settings. That is exactly the
/// identity fork fleets share — N replicas forked from one checkpoint have
/// byte-identical framebuffers, while their *encoded* bytes could legally differ —
/// so an `if_none_match` minted by one replica (or codec) matches any other.
pub fn frame_hash_hex(rgb: &[u8], w: u16, h: u16) -> String {
    let mut hash = fnv1a_fold(FNV1A_BASIS, &w.to_le_bytes());
    hash = fnv1a_fold(hash, &h.to_le_bytes());
    hash = fnv1a_fold(hash, rgb);
    format!("{hash:016x}")
}

/// Downscale (if requested) then encode an RGB8 frame per `opts`, returning the codec the
/// caller should advertise on the wire. Single chokepoint so every backend's `capture`
/// shares identical downscale+encode semantics.
pub fn encode_frame(rgb: &[u8], w: u16, h: u16, opts: EncodeOpts) -> Result<CapturedImage> {
    let (rgb, w, h) = match opts.max_long_edge {
        Some(m) => downscale_rgb(rgb, w, h, m),
        None => (rgb.to_vec(), w, h),
    };
    let bytes = match opts.format {
        ImageFormat::Png => encode_png(&rgb, w as u32, h as u32)?,
        ImageFormat::Jpeg => encode_jpeg(&rgb, w, h, opts.quality)?,
    };
    Ok(CapturedImage {
        data: bytes,
        format: opts.format,
        w,
        h,
    })
}

// ---- dirty-tile delta screencast (B2) ----

/// Tile edge (px) for the dirty-tile delta screencast. Tiles are TILE_SIZE×TILE_SIZE in
/// the DELIVERED (post-downscale) resolution; edge tiles are smaller when the frame is
/// not a multiple. 64px ≈ one toolbar button / one text line: small enough that a
/// keystroke dirties only a few tiles, big enough that per-tile codec overhead
/// (PNG/JPEG headers) doesn't dominate.
pub const TILE_SIZE: u16 = 64;

/// Every Nth DELIVERED delta frame is a full keyframe instead of tiles. A constant, not
/// negotiated: it bounds two error windows regardless of client behavior — (1) a client
/// that joined mid-stream or whose tile frame was dropped on a full writer queue is
/// self-healed within at most N-1 frames; (2) when `format=jpeg`, tiles are lossy
/// regions composited client-side onto a lossy keyframe, and N bounds the accumulated
/// compositing drift. 30 ≈ one keyframe per 6 s at the 5-fps default — frequent enough
/// to self-heal, rare enough that steady-state bandwidth stays tile-dominated.
pub const KEYFRAME_INTERVAL: u64 = 30;

/// One changed tile's rect `(x, y, w, h)` in the delivered resolution.
pub type TileRect = (u32, u32, u16, u16);

/// A changed tile, encoded for the wire (raw codec bytes; base64 only on the legacy
/// text path, via [`EncodedTile::to_base64`]).
#[derive(Debug, Clone)]
pub struct EncodedTile {
    pub x: u32,
    pub y: u32,
    pub w: u16,
    pub h: u16,
    pub data: Vec<u8>,
}

impl EncodedTile {
    /// Base64 of the tile's encoded bytes — only for the legacy base64-in-JSON path.
    pub fn to_base64(&self) -> String {
        B64.encode(&self.data)
    }
}

/// Compare two equal-sized RGB8 frames tile-by-tile and return the rects of the tiles
/// that changed. The comparison is per-row slice equality inside each tile (compiles to
/// memcmp) with early exit on the first differing row — cheap enough to run every
/// capture tick on a limited guest CPU.
pub fn diff_tiles(prev: &[u8], curr: &[u8], w: u16, h: u16) -> Vec<TileRect> {
    debug_assert_eq!(prev.len(), curr.len());
    let (wu, hu, ts) = (w as usize, h as usize, TILE_SIZE as usize);
    let mut out = Vec::new();
    let mut ty = 0usize;
    while ty < hu {
        let th = ts.min(hu - ty); // edge tiles are smaller at non-multiple-of-64 dims
        let mut tx = 0usize;
        while tx < wu {
            let tw = ts.min(wu - tx);
            let changed = (ty..ty + th).any(|row| {
                let start = (row * wu + tx) * 3;
                prev[start..start + tw * 3] != curr[start..start + tw * 3]
            });
            if changed {
                out.push((tx as u32, ty as u32, tw as u16, th as u16));
            }
            tx += ts;
        }
        ty += ts;
    }
    out
}

/// Encode raw RGB8 pixels as a PNG with the FAST compression preset + adaptive
/// filtering. Used for tile payloads only: a delta frame encodes many small images per
/// tick, so per-tile encode cost must stay well below the full-frame default path —
/// `Compression::Fast` trades a few percent of size for a large deflate-time win, and
/// adaptive filtering claws most of the size back. Keyframes keep the default-preset
/// [`encode_png`] (one image per ~30 frames; size matters more there).
fn encode_png_fast(rgb: &[u8], w: u32, h: u32) -> Result<Vec<u8>> {
    let mut out = Vec::new();
    {
        let mut enc = png::Encoder::new(&mut out, w, h);
        enc.set_color(png::ColorType::Rgb);
        enc.set_depth(png::BitDepth::Eight);
        enc.set_compression(png::Compression::Fast);
        enc.set_adaptive_filter(png::AdaptiveFilterType::Adaptive);
        let mut writer = enc.write_header()?;
        writer.write_image_data(rgb)?;
        writer.finish()?;
    }
    Ok(out)
}

/// Copy one tile's pixels out of a full RGB8 frame into a contiguous buffer.
fn tile_rgb(rgb: &[u8], frame_w: u16, rect: TileRect) -> Vec<u8> {
    let (x, y, tw, th) = (
        rect.0 as usize,
        rect.1 as usize,
        rect.2 as usize,
        rect.3 as usize,
    );
    let fw = frame_w as usize;
    let mut out = Vec::with_capacity(tw * th * 3);
    for row in y..y + th {
        let start = (row * fw + x) * 3;
        out.extend_from_slice(&rgb[start..start + tw * 3]);
    }
    out
}

/// Encode the changed tiles of `rgb` per `opts.format`/`opts.quality` (PNG tiles use the
/// fast preset; JPEG tiles use the existing encoder). `opts.max_long_edge` is ignored —
/// the delta path downscales BEFORE tiling (`capture_raw`), so tiles already sit in the
/// delivered resolution.
pub fn encode_tiles(
    rgb: &[u8],
    w: u16,
    _h: u16,
    rects: &[TileRect],
    opts: EncodeOpts,
) -> Result<Vec<EncodedTile>> {
    rects
        .iter()
        .map(|&rect| {
            let (tw, th) = (rect.2, rect.3);
            let pixels = tile_rgb(rgb, w, rect);
            let bytes = match opts.format {
                ImageFormat::Png => encode_png_fast(&pixels, tw as u32, th as u32)?,
                ImageFormat::Jpeg => encode_jpeg(&pixels, tw, th, opts.quality)?,
            };
            Ok(EncodedTile {
                x: rect.0,
                y: rect.1,
                w: tw,
                h: th,
                data: bytes,
            })
        })
        .collect()
}

/// What one delta-screencast tick decided to emit.
#[derive(Debug)]
pub enum DeltaFrame {
    /// Nothing changed — the same idle suppression as the full-frame path (no message).
    Unchanged,
    /// A full keyframe: the first frame, a post-resume restart, a dimension change
    /// (e.g. window resize), or the [`KEYFRAME_INTERVAL`] cadence.
    Key(CapturedImage),
    /// Only the changed tiles, to composite onto the last keyframe client-side.
    Tiles(Vec<EncodedTile>),
}

/// Dirty-tile delta screencast state (B2): the per-stream baseline + keyframe cadence.
///
/// Memory bound (deliberate, guest RAM is limited): ONE previous RGB frame per active
/// screencast — `w*h*3` bytes at the delivered (post-downscale) resolution, ≈6 MB at
/// 1920×1080 — no tile cache, no frame history. With `MAX_SCREENCASTS = 8` the worst
/// case stays under ~50 MB.
///
/// [`DeltaState::tick`] is pure compute over an already-captured frame; the baseline
/// advances only via [`DeltaState::commit`] AFTER the frame was actually delivered, so
/// a frame dropped on a full writer queue is re-diffed against the same baseline next
/// tick (mirroring the non-delta loop's commit-last_hash-on-send semantics).
#[derive(Default)]
pub struct DeltaState {
    prev: Option<(Vec<u8>, u16, u16)>,
    /// Delivered frames since the last keyframe.
    since_key: u64,
}

impl DeltaState {
    /// Decide what to emit for the captured `rgb` (already downscaled). Does NOT
    /// advance the baseline — call [`DeltaState::commit`] after a successful send.
    pub fn tick(&self, rgb: &[u8], w: u16, h: u16, opts: EncodeOpts) -> Result<DeltaFrame> {
        match &self.prev {
            Some((prev, pw, ph)) if (*pw, *ph) == (w, h) => {
                let rects = diff_tiles(prev, rgb, w, h);
                if rects.is_empty() {
                    return Ok(DeltaFrame::Unchanged);
                }
                if self.since_key >= KEYFRAME_INTERVAL - 1 {
                    Ok(DeltaFrame::Key(encode_frame(rgb, w, h, opts)?))
                } else {
                    Ok(DeltaFrame::Tiles(encode_tiles(rgb, w, h, &rects, opts)?))
                }
            }
            // No baseline (first frame / post-resume restart) or the dimensions
            // changed (resize invalidates every tile coordinate) → full keyframe.
            _ => Ok(DeltaFrame::Key(encode_frame(rgb, w, h, opts)?)),
        }
    }

    /// Commit a DELIVERED frame: adopt `rgb` as the new baseline and advance the
    /// keyframe cadence (`was_key` resets it).
    pub fn commit(&mut self, rgb: Vec<u8>, w: u16, h: u16, was_key: bool) {
        self.prev = Some((rgb, w, h));
        self.since_key = if was_key { 0 } else { self.since_key + 1 };
    }

    /// The committed baseline's dimensions, if any — the damage-driven loop region-
    /// captures only when the baseline matches the live screen geometry.
    pub fn baseline_dims(&self) -> Option<(u16, u16)> {
        self.prev.as_ref().map(|(_, w, h)| (*w, *h))
    }

    /// Compose the CURRENT full frame from the committed baseline plus freshly
    /// captured pixels for `rect` (XDamage said only `rect` changed): clone the
    /// baseline and patch the rectangle in. `pixels` is `rect.2 * rect.3 * 3` RGB8
    /// bytes. `None` when there is no baseline, the rect overflows it, or the pixel
    /// buffer doesn't match — callers then fall back to a full capture.
    pub fn compose_partial(&self, rect: DamageRect, pixels: &[u8]) -> Option<(Vec<u8>, u16, u16)> {
        let (base, w, h) = self.prev.as_ref()?;
        let (rx, ry, rw, rh) = (
            rect.0 as usize,
            rect.1 as usize,
            rect.2 as usize,
            rect.3 as usize,
        );
        let (wu, hu) = (*w as usize, *h as usize);
        if rx + rw > wu || ry + rh > hu || pixels.len() != rw * rh * 3 {
            return None;
        }
        let mut frame = base.clone();
        for row in 0..rh {
            let dst = ((ry + row) * wu + rx) * 3;
            let src = row * rw * 3;
            frame[dst..dst + rw * 3].copy_from_slice(&pixels[src..src + rw * 3]);
        }
        Some((frame, *w, *h))
    }
}

const EXECUTOR_ENV: &str = "SHINKEND_EXECUTOR";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum BackendChoice {
    Auto,
    X11Xtest,
    Virtual,
    PyAutoGui,
    /// Native macOS backend (CoreGraphics capture + CGEvent input). Parses on
    /// every platform; building it on a non-mac target fails loudly.
    MacOs,
}

impl BackendChoice {
    fn parse(value: &str) -> Result<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "" | "auto" => Ok(Self::Auto),
            "x11_xtest" | "x11/xtest" | "xtest" | "x11" => Ok(Self::X11Xtest),
            "virtual" => Ok(Self::Virtual),
            "pyautogui" | "pyautogui_subprocess" => Ok(Self::PyAutoGui),
            "macos" | "macos/coregraphics" | "quartz" | "coregraphics" => Ok(Self::MacOs),
            other => bail!(
                "unknown {EXECUTOR_ENV} value {other:?}; \
                 expected auto, x11_xtest, virtual, pyautogui, or macos"
            ),
        }
    }

    fn from_env() -> Result<Self> {
        match std::env::var(EXECUTOR_ENV) {
            Ok(value) => Self::parse(&value),
            Err(std::env::VarError::NotPresent) => Ok(Self::Auto),
            Err(e) => bail!("invalid {EXECUTOR_ENV}: {e}"),
        }
    }
}

/// Pick the configured executor. `auto` preserves the Phase-0 behavior: X11 if a
/// display is reachable, otherwise (on macOS) the native CoreGraphics backend,
/// otherwise the virtual test backend.
pub fn default_executor() -> Result<Arc<dyn Executor>> {
    build_executor(BackendChoice::from_env()?)
}

/// Pick the executor for an explicit backend name (the `--backend` flag), or the
/// `SHINKEND_EXECUTOR`/auto default when `name` is `None`.
pub fn executor_for(name: Option<&str>) -> Result<Arc<dyn Executor>> {
    match name {
        Some(value) => build_executor(BackendChoice::parse(value)?),
        None => default_executor(),
    }
}

/// Try the native macOS backend, logging the geometry on success. `None` when it
/// cannot be constructed (no display) — never a hard error on the auto path.
#[cfg(all(target_os = "macos", feature = "macos-native"))]
fn try_macos_backend() -> Option<Arc<dyn Executor>> {
    match crate::executor_macos::MacExecutor::new() {
        Ok(m) => {
            let (w, h) = Executor::screen_size(&m);
            eprintln!("shinkend: action backend = macos/coregraphics ({w}x{h})");
            Some(Arc::new(m))
        }
        Err(e) => {
            eprintln!("shinkend: macOS backend unavailable ({e})");
            None
        }
    }
}

fn build_executor(choice: BackendChoice) -> Result<Arc<dyn Executor>> {
    match choice {
        BackendChoice::Auto => {
            // On macOS with no $DISPLAY there is no X server to wait for — the
            // native backend is the auto default. With $DISPLAY set (XQuartz),
            // X11 keeps priority; the native backend is the fallback.
            #[cfg(all(target_os = "macos", feature = "macos-native"))]
            if std::env::var_os("DISPLAY").is_none() {
                if let Some(m) = try_macos_backend() {
                    return Ok(m);
                }
            }
            match X11Executor::connect() {
                Ok(x) => {
                    eprintln!(
                        "shinkend: action backend = x11/xtest ({}x{})",
                        x.width, x.height
                    );
                    Ok(Arc::new(x))
                }
                Err(e) => {
                    #[cfg(all(target_os = "macos", feature = "macos-native"))]
                    if let Some(m) = try_macos_backend() {
                        return Ok(m);
                    }
                    eprintln!("shinkend: no X11 display ({e}); action backend = virtual (no-op)");
                    Ok(Arc::new(VirtualExecutor::default()))
                }
            }
        }
        BackendChoice::X11Xtest => {
            // Explicit X11 is LAZY: shinkend must be exec'd before the desktop so the
            // ACI listener is accepting within milliseconds of container start; the
            // X11 connection is owned here (retry with short backoff) instead of a
            // shell poll loop gating the listener behind Xvfb (the boot-waterfall
            // finding, S8). The `ready` query reports x11_up=false until it lands.
            let lazy = Arc::new(LazyX11Executor::default());
            match lazy.try_connect() {
                Ok(x) => eprintln!(
                    "shinkend: action backend = x11/xtest ({}x{})",
                    x.width, x.height
                ),
                Err(e) => {
                    eprintln!(
                        "shinkend: action backend = x11/xtest (display not up yet: {e}; \
                         retrying in the background)"
                    );
                    let retry = lazy.clone();
                    std::thread::spawn(move || retry.retry_until_connected());
                }
            }
            Ok(lazy)
        }
        BackendChoice::Virtual => {
            eprintln!("shinkend: action backend = virtual (configured)");
            Ok(Arc::new(VirtualExecutor::default()))
        }
        BackendChoice::PyAutoGui => {
            let e = crate::pyautogui::PyAutoGuiExecutor::new()
                .context("SHINKEND_EXECUTOR=pyautogui")?;
            eprintln!("shinkend: action backend = pyautogui (subprocess)");
            Ok(Arc::new(e))
        }
        #[cfg(all(target_os = "macos", feature = "macos-native"))]
        BackendChoice::MacOs => {
            let m = crate::executor_macos::MacExecutor::new().context("backend=macos")?;
            let (w, h) = Executor::screen_size(&m);
            eprintln!("shinkend: action backend = macos/coregraphics ({w}x{h}, configured)");
            Ok(Arc::new(m))
        }
        #[cfg(not(all(target_os = "macos", feature = "macos-native")))]
        BackendChoice::MacOs => bail!(
            "the macos backend needs a macOS build with the `macos-native` feature \
             (this build: {})",
            crate::protocol::platform()
        ),
    }
}

// ---- pointer button numbers (X11) ----
const BTN_LEFT: u8 = 1;
const BTN_MIDDLE: u8 = 2;
const BTN_RIGHT: u8 = 3;
const BTN_SCROLL_UP: u8 = 4;
const BTN_SCROLL_DOWN: u8 = 5;
const BTN_SCROLL_LEFT: u8 = 6;
const BTN_SCROLL_RIGHT: u8 = 7;

/// Map a wire `button` name to its X11 button number. Absent = left (the schema
/// default); unknown names are rejected — the runtime must not guess a button.
fn parse_button(name: Option<&str>) -> Result<u8> {
    match name {
        None | Some("left") => Ok(BTN_LEFT),
        Some("middle") => Ok(BTN_MIDDLE),
        Some("right") => Ok(BTN_RIGHT),
        Some(other) => bail!("unknown pointer button: {other:?} (expected left, middle, or right)"),
    }
}

/// Hard cap on a `drag` gesture's duration (ms) — mirrors `MAX_WAIT_MS` so a client
/// can't park the action thread arbitrarily long with one action.
pub const MAX_DRAG_MS: u64 = 10_000;

/// Pixels of pointer travel per interpolated drag step. Small enough that toolkits
/// tracking motion (DnD thresholds, hover targets) see a continuous path, big enough
/// that a full-screen drag stays a handful of round trips.
const DRAG_PX_PER_STEP: f64 = 16.0;
const DRAG_MAX_STEPS: usize = 64;

/// The interpolated pointer path of a drag, INCLUDING both endpoints (the first entry
/// is `from`, the last is exactly `to`). Pure, so the gesture's shape is unit-testable
/// without a display.
fn drag_path(from: (i16, i16), to: (i16, i16)) -> Vec<(i16, i16)> {
    let (dx, dy) = (
        f64::from(to.0) - f64::from(from.0),
        f64::from(to.1) - f64::from(from.1),
    );
    let dist = dx.hypot(dy);
    let steps = ((dist / DRAG_PX_PER_STEP).ceil() as usize).clamp(1, DRAG_MAX_STEPS);
    let mut path = Vec::with_capacity(steps + 1);
    for i in 0..=steps {
        let t = i as f64 / steps as f64;
        path.push((
            (f64::from(from.0) + dx * t).round() as i16,
            (f64::from(from.1) + dy * t).round() as i16,
        ));
    }
    path
}
/// Wire `dx`/`dy` are pixel-denominated (see docs/design/aci-spec.md); one wheel
/// click ≈ this many pixels. Adapters that speak in wheel clicks convert at their edge.
const SCROLL_PX_PER_STEP: f64 = 100.0;
const SCROLL_MAX_STEPS: u32 = 20;

/// Pixels → bounded wheel-click count (shared by both scroll axes).
fn scroll_steps(px: f64) -> u32 {
    ((px.abs() / SCROLL_PX_PER_STEP).ceil() as u32).clamp(1, SCROLL_MAX_STEPS)
}

/// Env knob for XDamage-driven capture: `off`/`0`/`false` disables it (capture
/// loops then poll-capture every tick); anything else / absent keeps it on when
/// the display advertises DAMAGE.
const DAMAGE_ENV: &str = "SHINKEND_DAMAGE";

fn damage_disabled_by_env() -> bool {
    matches!(
        std::env::var(DAMAGE_ENV).as_deref().map(str::trim),
        Ok("off") | Ok("0") | Ok("false") | Ok("OFF")
    )
}

/// XDamage event accumulator: a DEDICATED X connection (so event reads never
/// contend with the action/capture connection's request/reply traffic) with one
/// Damage object on the root window at `DELTA_RECTANGLES` report level. Every
/// [`DamageTracker::poll`] drains the pending DamageNotify events, records their
/// union into the [`DamageLog`], and `damage_subtract`s the server-side region so
/// future drawing reports fresh rectangles. Damage to any window propagates to its
/// ancestors, so the root's region covers the whole visible desktop.
struct DamageTracker {
    conn: x11rb::rust_connection::RustConnection,
    damage: u32,
    log: DamageLog,
    /// A poll error (lost X connection) latches the tracker broken: every
    /// subsequent answer is `Full`, degrading to capture-every-tick, never to a
    /// missed change.
    broken: bool,
}

impl DamageTracker {
    fn connect() -> Result<Self> {
        use x11rb::connection::RequestConnection as _;
        use x11rb::protocol::damage::ConnectionExt as _;
        let (conn, screen_num) = x11rb::connect(None).context("connect damage display")?;
        conn.extension_information(x11rb::protocol::damage::X11_EXTENSION_NAME)?
            .context("X server does not advertise the DAMAGE extension")?;
        conn.damage_query_version(1, 1)?.reply()?;
        let root = conn.setup().roots[screen_num].root;
        let damage = conn.generate_id()?;
        conn.damage_create(
            damage,
            root,
            x11rb::protocol::damage::ReportLevel::DELTA_RECTANGLES,
        )?;
        conn.flush()?;
        Ok(Self {
            conn,
            damage,
            log: DamageLog::default(),
            broken: false,
        })
    }

    /// Drain pending damage events into the log; on any X error, latch broken.
    fn poll(&mut self) {
        use x11rb::protocol::damage::ConnectionExt as _;
        use x11rb::protocol::Event;
        if self.broken {
            return;
        }
        let mut acc: Option<DamageRect> = None;
        loop {
            match self.conn.poll_for_event() {
                Ok(Some(Event::DamageNotify(n))) => {
                    let r = (
                        n.area.x.max(0) as u32,
                        n.area.y.max(0) as u32,
                        n.area.width as u32,
                        n.area.height as u32,
                    );
                    acc = Some(match acc {
                        Some(a) => union_rect(a, r),
                        None => r,
                    });
                }
                Ok(Some(_)) => {}
                Ok(None) => break,
                Err(_) => {
                    self.broken = true;
                    return;
                }
            }
        }
        if let Some(r) = acc {
            // Empty the server-side region so the same pixels re-damaged later
            // produce fresh DELTA_RECTANGLES events. An event already in flight is
            // simply read next poll — a duplicate (over-)report, never a loss.
            let ok = self
                .conn
                .damage_subtract(self.damage, x11rb::NONE, x11rb::NONE)
                .is_ok()
                && self.conn.flush().is_ok();
            if !ok {
                self.broken = true;
            }
            self.log.record(r);
        }
    }

    fn cursor(&mut self) -> u64 {
        self.poll();
        self.log.epoch()
    }

    fn since(&mut self, cursor: u64) -> (u64, DamageSince) {
        self.poll();
        if self.broken {
            return (self.log.epoch(), DamageSince::Full);
        }
        (self.log.epoch(), self.log.since(cursor))
    }
}

/// X11 backend: synthetic pointer + keyboard input via the XTEST extension.
pub struct X11Executor {
    conn: Mutex<x11rb::rust_connection::RustConnection>,
    root: Window,
    width: u16,
    height: u16,
    /// keysym -> (keycode, needs_shift), built from the server's keyboard mapping.
    keymap: HashMap<u32, (u8, bool)>,
    /// XDamage change tracker (own X connection), when the display supports it and
    /// `SHINKEND_DAMAGE` doesn't disable it. `None` → capture loops poll every tick.
    damage: Option<Mutex<DamageTracker>>,
    /// CLIPBOARD selection worker (own X connection + owner window), spawned lazily
    /// on the first clipboard verb — see [`crate::clipboard`].
    clipboard: crate::clipboard::SharedClipboard,
}

impl X11Executor {
    pub fn connect() -> Result<Self> {
        let (conn, screen_num) = x11rb::connect(None).context("connect to X display")?;
        let (root, width, height, min_kc, max_kc) = {
            let setup = conn.setup();
            let screen = &setup.roots[screen_num];
            (
                screen.root,
                screen.width_in_pixels,
                screen.height_in_pixels,
                setup.min_keycode,
                setup.max_keycode,
            )
        };
        let mapping = conn
            .get_keyboard_mapping(min_kc, max_kc - min_kc + 1)?
            .reply()?;
        let per = mapping.keysyms_per_keycode as usize;
        let mut keymap: HashMap<u32, (u8, bool)> = HashMap::new();
        for (i, chunk) in mapping.keysyms.chunks(per).enumerate() {
            let kc = min_kc + i as u8;
            for (col, &ks) in chunk.iter().enumerate() {
                match (col, ks) {
                    (_, 0) => {}
                    (0, _) => {
                        keymap.insert(ks, (kc, false));
                    }
                    (1, _) => {
                        keymap.entry(ks).or_insert((kc, true));
                    }
                    _ => {}
                }
            }
        }
        // XDamage tracker (own connection). Failure is never fatal: the capture
        // loops just keep poll-capturing every tick.
        let damage = if damage_disabled_by_env() {
            eprintln!("shinkend: damage tracking = off ({DAMAGE_ENV})");
            None
        } else {
            match DamageTracker::connect() {
                Ok(t) => {
                    eprintln!("shinkend: damage tracking = on (X DAMAGE, root window)");
                    Some(Mutex::new(t))
                }
                Err(e) => {
                    eprintln!("shinkend: damage tracking = off ({e}); polling every tick");
                    None
                }
            }
        };
        Ok(Self {
            conn: Mutex::new(conn),
            root,
            width,
            height,
            keymap,
            damage,
            clipboard: crate::clipboard::SharedClipboard::default(),
        })
    }

    /// Whether the desktop has painted: sample a handful of root-window pixels with
    /// 1×1 GetImage round trips (microseconds over the local socket — never a full
    /// frame). Non-black anywhere ⇒ painted. The freshly-created Xvfb root is all
    /// black until the WM/`xsetroot` paints it, so this flips exactly when a
    /// screenshot would stop being useless.
    fn sample_root_nonblack(&self) -> Result<bool> {
        const POINTS: [(f64, f64); 5] = [
            (0.5, 0.5),
            (0.25, 0.25),
            (0.75, 0.25),
            (0.25, 0.75),
            (0.75, 0.75),
        ];
        let conn = self.conn.lock().expect("x11 conn lock");
        for (fx, fy) in POINTS {
            let x = ((self.width.saturating_sub(1)) as f64 * fx) as i16;
            let y = ((self.height.saturating_sub(1)) as f64 * fy) as i16;
            let reply = conn
                .get_image(XImageFormat::Z_PIXMAP, self.root, x, y, 1, 1, !0)?
                .reply()?;
            // One Z_PIXMAP pixel is BGR[X]; any non-zero color byte means painted.
            if reply.data.iter().take(3).any(|&b| b != 0) {
                return Ok(true);
            }
        }
        Ok(false)
    }

    /// Guest readiness with connection-level errors SURFACED: `Err` means the X
    /// connection itself failed (a root `GetImage` cannot otherwise fail — the
    /// server restarted), so a caller that owns the connection lifecycle
    /// ([`LazyX11Executor`]) can drop the dead connection and redial. This is the
    /// ONE readiness implementation for both X11 backends — the windows-mapped
    /// fallback must apply on the deployed lazy path too (it once lived only on
    /// this type's `Executor::readiness`, which the lazy wrapper never called, so
    /// the deployed `x11_xtest` backend silently lacked it).
    ///
    /// A black wallpaper is not an unusable desktop: if client windows are already
    /// mapped (xterm/WM up), observations are meaningful — some boots lose the
    /// root paint while the desktop is fine. Either signal flips ready;
    /// `root_nonblack` stays honestly what was sampled.
    fn readiness_checked(&self) -> Result<Readiness> {
        let nonblack = self.sample_root_nonblack()?;
        let windows_up = nonblack
            || self
                .list_windows_x11()
                .map(|w| !w.is_empty())
                .unwrap_or(false);
        Ok(Readiness {
            ready: nonblack || windows_up,
            x11_up: true,
            root_nonblack: Some(nonblack),
            permissions_pending: None,
        })
    }

    /// Resolve a [`Scope`] to captured RGB8 + dimensions.
    fn capture_scope(&self, scope: Scope) -> Result<(Vec<u8>, u16, u16)> {
        match scope {
            Scope::Screen => self.capture_drawable(self.root, self.width, self.height),
            Scope::ActiveWindow => match self.active_window()? {
                Some(win) => self.capture_window(win),
                // No focused window (e.g. a bare desktop) — fall back to full screen.
                None => self.capture_drawable(self.root, self.width, self.height),
            },
            Scope::Window(id) => self.capture_window(id),
        }
    }

    /// The active window: the EWMH `_NET_ACTIVE_WINDOW` when a window manager
    /// publishes one, else (WM-less display — bare Xvfb) the X input focus walked up
    /// to its top-level ancestor. The fallback keeps `active_window`-scoped capture,
    /// the `list_windows` focused flag, and `activate_window` verification meaningful
    /// without a WM, where the EWMH property never exists.
    fn active_window(&self) -> Result<Option<Window>> {
        let conn = self.conn.lock().expect("x11 conn lock");
        let atom = conn.intern_atom(true, b"_NET_ACTIVE_WINDOW")?.reply()?.atom;
        if atom != 0 {
            let prop = conn
                .get_property(false, self.root, atom, AtomEnum::WINDOW, 0, 1)?
                .reply()?;
            if let Some(w) = prop
                .value32()
                .and_then(|mut it| it.next())
                .filter(|w| *w != 0)
            {
                return Ok(Some(w));
            }
        }
        // Input-focus fallback. 0 = None, 1 = PointerRoot — neither names a window.
        let focus = conn.get_input_focus()?.reply()?.focus;
        if focus == 0 || focus == 1 || focus == self.root {
            return Ok(None);
        }
        // Apps often focus a subwindow; walk to the top-level (direct child of root),
        // bounded so a pathological tree can never spin this loop.
        let mut win = focus;
        for _ in 0..64 {
            let tree = conn.query_tree(win)?.reply()?;
            if tree.parent == self.root || tree.parent == 0 {
                break;
            }
            win = tree.parent;
        }
        Ok(Some(win))
    }

    /// Capture one window by id, sized to its current geometry.
    fn capture_window(&self, win: Window) -> Result<(Vec<u8>, u16, u16)> {
        let (w, h) = {
            let conn = self.conn.lock().expect("x11 conn lock");
            let geo = conn
                .get_geometry(win)?
                .reply()
                .context("window geometry (is the window id valid and mapped?)")?;
            (geo.width, geo.height)
        };
        self.capture_drawable(win, w, h)
    }

    /// Grab a drawable's `w`x`h` region as RGB8 (X delivers BGR[X]; reorder for PNG).
    fn capture_drawable(&self, drawable: Window, w: u16, h: u16) -> Result<(Vec<u8>, u16, u16)> {
        self.capture_drawable_at(drawable, 0, 0, w, h)
    }

    /// Grab a `w`x`h` region of a drawable at offset `(x, y)` as RGB8 — the
    /// damage-driven delta path fetches only the damaged rectangle this way.
    fn capture_drawable_at(
        &self,
        drawable: Window,
        x: i16,
        y: i16,
        w: u16,
        h: u16,
    ) -> Result<(Vec<u8>, u16, u16)> {
        let (wu, hu) = (w as usize, h as usize);
        ensure!(wu * hu > 0, "zero-sized capture region");
        let reply = {
            let conn = self.conn.lock().expect("x11 conn lock");
            let cookie = conn.get_image(XImageFormat::Z_PIXMAP, drawable, x, y, w, h, !0)?;
            cookie.reply()?
        };
        let bpp = reply.data.len() / (wu * hu);
        ensure!(bpp == 3 || bpp == 4, "unexpected bytes-per-pixel: {bpp}");
        let mut rgb = Vec::with_capacity(wu * hu * 3);
        for px in reply.data.chunks_exact(bpp) {
            rgb.push(px[2]);
            rgb.push(px[1]);
            rgb.push(px[0]);
        }
        Ok((rgb, w, h))
    }

    fn fake(&self, type_: u8, detail: u8, x: i16, y: i16) -> Result<()> {
        let conn = self.conn.lock().expect("x11 conn lock");
        conn.xtest_fake_input(type_, detail, 0, self.root, x, y, 0)?;
        conn.flush()?;
        Ok(())
    }

    fn motion(&self, x: i16, y: i16) -> Result<()> {
        self.fake(MOTION_NOTIFY_EVENT, 0, x, y)
    }

    fn click_button(&self, button: u8, x: i16, y: i16) -> Result<()> {
        self.motion(x, y)?;
        self.fake(BUTTON_PRESS_EVENT, button, x, y)?;
        self.fake(BUTTON_RELEASE_EVENT, button, x, y)
    }

    /// Press or release `button` at the pointer's CURRENT position (XTEST ignores the
    /// coordinates on button events; callers that were given a target move first).
    fn button_event(&self, button: u8, press: bool) -> Result<()> {
        let ty = if press {
            BUTTON_PRESS_EVENT
        } else {
            BUTTON_RELEASE_EVENT
        };
        self.fake(ty, button, 0, 0)
    }

    /// One full drag gesture: move to `from`, button down, interpolated motion along
    /// [`drag_path`] (`duration_ms` spread evenly across its segments), button up at
    /// `to`. The release is attempted even if a mid-path motion fails, so an error can
    /// never leave the synthetic button latched down.
    fn drag(&self, from: (i16, i16), to: (i16, i16), button: u8, duration_ms: u64) -> Result<()> {
        let path = drag_path(from, to);
        let segments = (path.len() - 1).max(1) as u64;
        let pause = std::time::Duration::from_millis(duration_ms / segments);
        self.motion(from.0, from.1)?;
        self.button_event(button, true)?;
        let walked = path[1..].iter().try_for_each(|&(x, y)| {
            if !pause.is_zero() {
                std::thread::sleep(pause);
            }
            self.motion(x, y)
        });
        let released = self.button_event(button, false);
        walked.and(released)
    }

    /// EWMH window enumeration (the `list_windows` query): `_NET_CLIENT_LIST` for the
    /// managed top-level windows, `_NET_WM_NAME`/`WM_NAME` for titles, `_NET_WM_PID`
    /// for owners, root-translated geometry, and `_NET_ACTIVE_WINDOW` for the focused
    /// flag. On a WM-less display (bare Xvfb — no EWMH properties) it falls back to
    /// the viewable, non-override-redirect children of the root, so the primitive
    /// still enumerates mapped apps in CI. A window that vanishes mid-enumeration is
    /// skipped, never an error.
    fn list_windows_x11(&self) -> Result<Vec<WindowInfo>> {
        let focused = self.active_window()?.unwrap_or(0);
        let conn = self.conn.lock().expect("x11 conn lock");
        let atom = |name: &[u8]| -> Result<u32> { Ok(conn.intern_atom(true, name)?.reply()?.atom) };
        let net_client_list = atom(b"_NET_CLIENT_LIST")?;
        let net_wm_name = atom(b"_NET_WM_NAME")?;
        let utf8_string = atom(b"UTF8_STRING")?;
        let net_wm_pid = atom(b"_NET_WM_PID")?;
        let mut windows: Vec<Window> = if net_client_list != 0 {
            conn.get_property(false, self.root, net_client_list, AtomEnum::WINDOW, 0, 4096)?
                .reply()?
                .value32()
                .map(Iterator::collect)
                .unwrap_or_default()
        } else {
            Vec::new()
        };
        if windows.is_empty() {
            // WM-less fallback: mapped, non-override-redirect direct children of root.
            for w in conn.query_tree(self.root)?.reply()?.children {
                if let Ok(attrs) = conn.get_window_attributes(w)?.reply() {
                    if attrs.map_state == MapState::VIEWABLE && !attrs.override_redirect {
                        windows.push(w);
                    }
                }
            }
        }
        let mut out = Vec::with_capacity(windows.len());
        for win in windows {
            // Geometry: w/h from the drawable, origin translated into root coords
            // (get_geometry's x/y are parent-relative). Skip windows that vanished.
            let Ok(geo) = conn
                .get_geometry(win)
                .map_err(anyhow::Error::from)
                .and_then(|c| Ok(c.reply()?))
            else {
                continue;
            };
            let Ok(pos) = conn
                .translate_coordinates(win, self.root, 0, 0)
                .map_err(anyhow::Error::from)
                .and_then(|c| Ok(c.reply()?))
            else {
                continue;
            };
            let mut title = String::new();
            if net_wm_name != 0 && utf8_string != 0 {
                if let Ok(prop) = conn
                    .get_property(false, win, net_wm_name, utf8_string, 0, 1024)
                    .map_err(anyhow::Error::from)
                    .and_then(|c| Ok(c.reply()?))
                {
                    title = String::from_utf8_lossy(&prop.value).into_owned();
                }
            }
            if title.is_empty() {
                if let Ok(prop) = conn
                    .get_property(false, win, AtomEnum::WM_NAME, AtomEnum::STRING, 0, 1024)
                    .map_err(anyhow::Error::from)
                    .and_then(|c| Ok(c.reply()?))
                {
                    title = String::from_utf8_lossy(&prop.value).into_owned();
                }
            }
            let pid = if net_wm_pid != 0 {
                conn.get_property(false, win, net_wm_pid, AtomEnum::CARDINAL, 0, 1)
                    .ok()
                    .and_then(|c| c.reply().ok())
                    .and_then(|p| p.value32().and_then(|mut it| it.next()))
            } else {
                None
            };
            out.push(WindowInfo {
                id: win,
                title,
                pid,
                x: i32::from(pos.dst_x),
                y: i32::from(pos.dst_y),
                w: u32::from(geo.width),
                h: u32::from(geo.height),
                focused: win == focused,
            });
        }
        Ok(out)
    }

    /// Activate (raise + focus) a window (G3): by `window_id` (a `list_windows` id),
    /// or by `app` — the first window whose title contains it, case-insensitive.
    /// With a running EWMH window manager the request is the standard
    /// `_NET_ACTIVE_WINDOW` client message to the root (source = pager/user, so the
    /// WM honors it); on a WM-less display (bare Xvfb) it falls back to doing what
    /// the WM would have: raise the window and set the X input focus directly.
    fn activate_window_x11(&self, a: &ActionSpec) -> Result<Window> {
        let target = match (a.window_id, a.app.as_deref()) {
            (Some(id), _) => id,
            (None, Some(app)) => {
                let windows = self.list_windows_x11()?;
                find_window_by_app(&windows, app).with_context(|| {
                    format!(
                        "activate_window: no window title matches app {app:?} \
                         (of {} enumerated)",
                        windows.len()
                    )
                })?
            }
            (None, None) => bail!("activate_window requires `window_id` or `app`"),
        };
        let current = self.active_window()?.unwrap_or(0);
        let conn = self.conn.lock().expect("x11 conn lock");
        // The id must name a live window — refuse rather than message a ghost.
        conn.get_window_attributes(target)?
            .reply()
            .with_context(|| format!("activate_window: window {target:#x} not found"))?;
        let net_active = conn.intern_atom(true, b"_NET_ACTIVE_WINDOW")?.reply()?.atom;
        let wm_check = conn
            .intern_atom(true, b"_NET_SUPPORTING_WM_CHECK")?
            .reply()?
            .atom;
        let wm_present = wm_check != 0
            && conn
                .get_property(false, self.root, wm_check, AtomEnum::WINDOW, 0, 1)?
                .reply()?
                .value32()
                .and_then(|mut it| it.next())
                .filter(|w| *w != 0)
                .is_some();
        if wm_present && net_active != 0 {
            let ev = build_activate_message(net_active, target, current);
            conn.send_event(
                false,
                self.root,
                EventMask::SUBSTRUCTURE_REDIRECT | EventMask::SUBSTRUCTURE_NOTIFY,
                ev,
            )?;
        } else {
            conn.configure_window(
                target,
                &ConfigureWindowAux::new().stack_mode(StackMode::ABOVE),
            )?;
            conn.set_input_focus(InputFocus::PARENT, target, x11rb::CURRENT_TIME)?;
        }
        conn.flush()?;
        Ok(target)
    }

    fn resolve(&self, target: Option<&Target>) -> Result<(i16, i16)> {
        match target.context("action requires a target")? {
            // Validate point_px against the screen the same way point_norm is (#141):
            // NaN/out-of-range would otherwise silently saturate to a screen edge.
            Target::PointPx { x, y } => {
                if !x.is_finite() || !y.is_finite() {
                    bail!("point_px must be finite: ({x}, {y})");
                }
                if *x < 0.0 || *y < 0.0 || *x >= self.width as f64 || *y >= self.height as f64 {
                    bail!(
                        "point_px out of range: ({x}, {y}) — must be within [0, {}) x [0, {})",
                        self.width,
                        self.height
                    );
                }
                Ok((*x as i16, *y as i16))
            }
            Target::PointNorm { x, y } => norm_to_px(*x, *y, self.width, self.height),
            Target::ElementRef { .. } => {
                // The session resolves element_ref → point_px via its observation
                // engine BEFORE execute() (connection::Session::dispatch_action);
                // reaching here means a path skipped that resolution — refuse rather
                // than guess.
                bail!("element_ref must be resolved by the session's observation engine")
            }
        }
    }

    fn key_event(&self, keycode: u8, press: bool) -> Result<()> {
        let ty = if press {
            KEY_PRESS_EVENT
        } else {
            KEY_RELEASE_EVENT
        };
        self.fake(ty, keycode, 0, 0)
    }

    fn keycode_for(&self, keysym: u32) -> Result<(u8, bool)> {
        self.keymap
            .get(&keysym)
            .copied()
            .with_context(|| format!("no keycode for keysym {keysym:#x}"))
    }

    fn type_text(&self, text: &str) -> Result<usize> {
        // Resolve every char to (keycode, needs_shift) up front so a single untypable
        // char fails the whole action atomically — never leave a typed prefix behind.
        let plan: Vec<(u8, bool)> = text
            .chars()
            .map(|ch| {
                let ks = char_keysym(ch).with_context(|| format!("cannot type char {ch:?}"))?;
                self.keycode_for(ks)
            })
            .collect::<Result<_>>()?;
        for (kc, shift) in plan {
            let shift_kc = if shift {
                Some(self.keycode_for(KS_SHIFT_L)?.0)
            } else {
                None
            };
            if let Some(s) = shift_kc {
                self.key_event(s, true)?;
            }
            self.key_event(kc, true)?;
            self.key_event(kc, false)?;
            if let Some(s) = shift_kc {
                self.key_event(s, false)?;
            }
        }
        Ok(text.chars().count())
    }

    fn key_combo(&self, combo: &str) -> Result<()> {
        let (mods, key) = parse_combo(combo);
        ensure!(!key.is_empty(), "empty key combo");
        let mut mod_kcs = Vec::with_capacity(mods.len());
        for m in &mods {
            let ks = mod_keysym(m).with_context(|| format!("unknown modifier {m:?}"))?;
            mod_kcs.push(self.keycode_for(ks)?.0);
        }
        let key_ks = key_keysym(key).with_context(|| format!("unknown key {key:?}"))?;
        // Honor the keysym's shift level: a key like "A"/"!"/"%" lives in keyboard column
        // 1 and needs Shift held, or it synthesizes the unshifted glyph ('a'/'1'/'5').
        let (key_kc, needs_shift) = self.keycode_for(key_ks)?;
        let shift_already = mods
            .iter()
            .any(|m| matches!(m.to_ascii_lowercase().as_str(), "shift"));
        let implicit_shift_kc = if needs_shift && !shift_already {
            Some(self.keycode_for(KS_SHIFT_L)?.0)
        } else {
            None
        };
        for &m in &mod_kcs {
            self.key_event(m, true)?;
        }
        if let Some(s) = implicit_shift_kc {
            self.key_event(s, true)?;
        }
        self.key_event(key_kc, true)?;
        self.key_event(key_kc, false)?;
        if let Some(s) = implicit_shift_kc {
            self.key_event(s, false)?;
        }
        for &m in mod_kcs.iter().rev() {
            self.key_event(m, false)?;
        }
        Ok(())
    }
}

/// Shift_L keysym.
const KS_SHIFT_L: u32 = 0xffe1;

/// Map a character to an X keysym (Latin-1 maps 1:1; plus newline/tab).
fn char_keysym(ch: char) -> Option<u32> {
    let c = ch as u32;
    if (0x20..=0x7e).contains(&c) || (0xa0..=0xff).contains(&c) {
        Some(c)
    } else {
        match ch {
            '\n' => Some(0xff0d),
            '\t' => Some(0xff09),
            _ => None,
        }
    }
}

fn mod_keysym(name: &str) -> Option<u32> {
    match name.to_ascii_lowercase().as_str() {
        "ctrl" | "control" => Some(0xffe3),
        "shift" => Some(0xffe1),
        "alt" | "option" => Some(0xffe9),
        "super" | "meta" | "cmd" | "command" | "win" => Some(0xffeb),
        _ => None,
    }
}

fn key_keysym(key: &str) -> Option<u32> {
    let mut it = key.chars();
    if let (Some(c), None) = (it.next(), it.next()) {
        return char_keysym(c);
    }
    match key.to_ascii_lowercase().as_str() {
        "enter" | "return" => Some(0xff0d),
        "tab" => Some(0xff09),
        "escape" | "esc" => Some(0xff1b),
        "space" => Some(0x20),
        "backspace" => Some(0xff08),
        "delete" | "del" => Some(0xffff),
        "up" => Some(0xff52),
        "down" => Some(0xff54),
        "left" => Some(0xff51),
        "right" => Some(0xff53),
        "home" => Some(0xff50),
        "end" => Some(0xff57),
        "pageup" | "page_up" | "pgup" => Some(0xff55),
        "pagedown" | "page_down" | "pgdn" => Some(0xff56),
        "insert" | "ins" => Some(0xff63),
        "capslock" | "caps_lock" => Some(0xffe5),
        "numlock" | "num_lock" => Some(0xff7f),
        "scrolllock" | "scroll_lock" => Some(0xff14),
        "pause" | "break" => Some(0xff13),
        "printscreen" | "print" | "prtsc" => Some(0xff61),
        "menu" | "apps" => Some(0xff67),
        // Symbol keys that cannot ride the single-char path inside a '+'-separated
        // combo (xdotool-style names): "ctrl+plus" etc.
        "plus" => Some(0x2b),
        "minus" => Some(0x2d),
        "equal" | "equals" => Some(0x3d),
        "comma" => Some(0x2c),
        "period" | "dot" => Some(0x2e),
        "slash" => Some(0x2f),
        "backslash" => Some(0x5c),
        "semicolon" => Some(0x3b),
        "apostrophe" | "quote" => Some(0x27),
        "grave" | "backtick" => Some(0x60),
        "bracketleft" => Some(0x5b),
        "bracketright" => Some(0x5d),
        // Numpad (XK_KP_*): digits are contiguous from XK_KP_0 = 0xffb0.
        "kp_enter" => Some(0xff8d),
        "kp_add" | "kp_plus" => Some(0xffab),
        "kp_subtract" | "kp_minus" => Some(0xffad),
        "kp_multiply" => Some(0xffaa),
        "kp_divide" => Some(0xffaf),
        "kp_decimal" => Some(0xffae),
        kp if kp.starts_with("kp_")
            && kp[3..]
                .parse::<u32>()
                .map(|n| n <= 9 && kp.len() == 4)
                .unwrap_or(false) =>
        {
            let n: u32 = kp[3..].parse().unwrap();
            Some(0xffb0 + n)
        }
        // Function keys F1..F12 (XK_F1 = 0xffbe, contiguous).
        f if f.starts_with('f')
            && f[1..]
                .parse::<u32>()
                .map(|n| (1..=12).contains(&n))
                .unwrap_or(false) =>
        {
            let n: u32 = f[1..].parse().unwrap();
            Some(0xffbe + (n - 1))
        }
        _ => None,
    }
}

/// Build the EWMH `_NET_ACTIVE_WINDOW` client message (sent to the root with the
/// substructure-redirect mask): data = [source, timestamp, requestor's currently
/// active window, 0, 0]. Source 2 = "pager/direct user action", which window
/// managers honor without focus-stealing-prevention heuristics. Pure, so the
/// message shape is unit-testable without a display.
pub(crate) fn build_activate_message(
    net_active_window: u32,
    target: Window,
    current_active: Window,
) -> ClientMessageEvent {
    ClientMessageEvent::new(32, target, net_active_window, [2, 0, current_active, 0, 0])
}

/// Resolve an `app` selector against an enumeration: the first window whose title
/// contains the selector, case-insensitive. Deliberately simple for v1 — titles are
/// what `list_windows` already exposes; WM_CLASS matching can come later if needed.
pub(crate) fn find_window_by_app(windows: &[WindowInfo], app: &str) -> Option<u32> {
    let needle = app.to_lowercase();
    windows
        .iter()
        .find(|w| w.title.to_lowercase().contains(&needle))
        .map(|w| w.id)
}

/// Spawn `app` (a PATH name or absolute path; `args` passed verbatim — never through
/// a shell) detached from the runtime's stdio, inheriting the session environment
/// (`$DISPLAY` + the session D-Bus address shinkend itself runs under, so the app
/// lands on the sandbox desktop and its a11y tree reaches the session bus). A
/// background reaper `wait()`s the child so an exited app never zombifies. Returns
/// the child pid (correlatable with `list_windows`' `_NET_WM_PID`).
pub(crate) fn spawn_app(app: &str, args: &[String]) -> Result<u32> {
    ensure!(
        !app.trim().is_empty(),
        "launch_app requires a non-empty `app`"
    );
    let mut child = std::process::Command::new(app)
        .args(args)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .with_context(|| format!("launch_app: failed to spawn {app:?}"))?;
    let pid = child.id();
    std::thread::spawn(move || {
        let _ = child.wait();
    });
    Ok(pid)
}

/// Split `"ctrl+shift+s"` into (`["ctrl","shift"]`, `"s"`) — shared by every
/// keyboard-synthesizing backend (X11, macOS).
pub(crate) fn parse_combo(combo: &str) -> (Vec<&str>, &str) {
    let parts: Vec<&str> = combo
        .split('+')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .collect();
    match parts.split_last() {
        Some((key, mods)) => (mods.to_vec(), key),
        None => (Vec::new(), ""),
    }
}

impl Executor for X11Executor {
    fn backend(&self) -> &'static str {
        "x11/xtest"
    }

    fn screen_size(&self) -> (u16, u16) {
        (self.width, self.height) // real X11 root geometry (#138)
    }

    fn screenshot(&self) -> Result<CapturedImage> {
        self.capture("screen", EncodeOpts::default())
    }

    fn capture(&self, scope: &str, opts: EncodeOpts) -> Result<CapturedImage> {
        let (rgb, w, h) = self.capture_scope(parse_scope(scope))?;
        encode_frame(&rgb, w, h, opts)
    }

    fn capture_raw(&self, scope: &str, max_long_edge: Option<u32>) -> Result<(Vec<u8>, u16, u16)> {
        let (rgb, w, h) = self.capture_scope(parse_scope(scope))?;
        Ok(match max_long_edge {
            Some(m) => downscale_rgb(&rgb, w, h, m),
            None => (rgb, w, h),
        })
    }

    fn supports_raw_capture(&self) -> bool {
        true
    }

    fn damage_cursor(&self) -> Option<u64> {
        self.damage
            .as_ref()
            .map(|t| t.lock().expect("damage lock").cursor())
    }

    fn damage_since(&self, cursor: u64) -> Option<(u64, DamageSince)> {
        self.damage
            .as_ref()
            .map(|t| t.lock().expect("damage lock").since(cursor))
    }

    fn capture_raw_region(&self, rect: DamageRect) -> Result<Vec<u8>> {
        let rect = clamp_rect(rect, self.width, self.height)
            .context("damage region entirely off-screen")?;
        let (rgb, _, _) = self.capture_drawable_at(
            self.root,
            rect.0 as i16,
            rect.1 as i16,
            rect.2 as u16,
            rect.3 as u16,
        )?;
        Ok(rgb)
    }

    fn readiness(&self) -> Readiness {
        // A connection-level failure means X is not usable RIGHT NOW — report it
        // honestly (a caller that owns the connection lifecycle uses
        // `readiness_checked` directly and redials instead).
        self.readiness_checked().unwrap_or(Readiness {
            ready: false,
            x11_up: false,
            root_nonblack: None,
            permissions_pending: None,
        })
    }

    fn list_windows(&self) -> Result<Vec<WindowInfo>> {
        self.list_windows_x11()
    }

    fn clipboard_get(&self) -> Result<String> {
        self.clipboard.get()
    }

    fn execute(&self, a: &ActionSpec) -> Result<String> {
        match a.verb.as_str() {
            "move" => {
                let (x, y) = self.resolve(a.target.as_ref())?;
                self.motion(x, y)?;
                Ok(format!("moved to {x},{y}"))
            }
            "click" => {
                let (x, y) = self.resolve(a.target.as_ref())?;
                self.click_button(BTN_LEFT, x, y)?;
                Ok(format!("clicked {x},{y}"))
            }
            "right_click" => {
                let (x, y) = self.resolve(a.target.as_ref())?;
                self.click_button(BTN_RIGHT, x, y)?;
                Ok(format!("right-clicked {x},{y}"))
            }
            "double_click" => {
                let (x, y) = self.resolve(a.target.as_ref())?;
                self.click_button(BTN_LEFT, x, y)?;
                self.click_button(BTN_LEFT, x, y)?;
                Ok(format!("double-clicked {x},{y}"))
            }
            "drag" => {
                let from = self.resolve(a.target.as_ref())?;
                let to_target = a.to.as_ref().context("drag requires a `to` target")?;
                let to = self.resolve(Some(to_target))?;
                let button = parse_button(a.button.as_deref())?;
                let ms = a.duration_ms.unwrap_or(0).min(MAX_DRAG_MS);
                self.drag(from, to, button, ms)?;
                Ok(format!(
                    "dragged {},{} -> {},{}",
                    from.0, from.1, to.0, to.1
                ))
            }
            "mouse_down" | "mouse_up" => {
                let press = a.verb == "mouse_down";
                let button = parse_button(a.button.as_deref())?;
                // Optional target: move first when given, else act at the pointer's
                // current position — the decomposed form free-form gestures need
                // (down → moves → up).
                if let Some(t) = a.target.as_ref() {
                    let (x, y) = self.resolve(Some(t))?;
                    self.motion(x, y)?;
                }
                self.button_event(button, press)?;
                Ok(format!(
                    "{} button {button}",
                    if press { "pressed" } else { "released" }
                ))
            }
            "scroll" => {
                let (x, y) = self.resolve(a.target.as_ref())?;
                let dx = a.dx.unwrap_or(0.0);
                let dy = a.dy.unwrap_or(0.0);
                if dx == 0.0 && dy == 0.0 {
                    bail!("scroll requires a nonzero dx or dy");
                }
                let mut total = 0u32;
                // Vertical: ACI convention is +dy = down (BTN 5), -dy = up (BTN 4).
                if dy != 0.0 {
                    let button = if dy > 0.0 {
                        BTN_SCROLL_DOWN
                    } else {
                        BTN_SCROLL_UP
                    };
                    let steps = scroll_steps(dy);
                    for _ in 0..steps {
                        self.click_button(button, x, y)?;
                    }
                    total += steps;
                }
                // Horizontal: +dx = right (BTN 7), -dx = left (BTN 6).
                if dx != 0.0 {
                    let button = if dx > 0.0 {
                        BTN_SCROLL_RIGHT
                    } else {
                        BTN_SCROLL_LEFT
                    };
                    let steps = scroll_steps(dx);
                    for _ in 0..steps {
                        self.click_button(button, x, y)?;
                    }
                    total += steps;
                }
                Ok(format!("scrolled {total} step(s)"))
            }
            "type_text" => {
                let text = a.text.as_deref().context("type_text requires `text`")?;
                let n = self.type_text(text)?;
                Ok(format!("typed {n} chars"))
            }
            "key" => {
                let keys = a.keys.as_deref().context("key requires `keys`")?;
                self.key_combo(keys)?;
                Ok(format!("key {keys}"))
            }
            // Desktop verbs (G2+G3). clipboard_get is NOT here — it returns data, so
            // the dispatcher routes it through Executor::clipboard_get to a `result`.
            "clipboard_set" => {
                let text = a.text.as_deref().context("clipboard_set requires `text`")?;
                self.clipboard.set(text)?;
                Ok(format!("clipboard set ({} bytes)", text.len()))
            }
            "launch_app" => {
                let app = a.app.as_deref().context("launch_app requires `app`")?;
                let args = a.args.as_deref().unwrap_or(&[]);
                let pid = spawn_app(app, args)?;
                Ok(format!("launched {app} (pid {pid})"))
            }
            "activate_window" => {
                let win = self.activate_window_x11(a)?;
                Ok(format!("activated window {win:#x}"))
            }
            // Dispatched via the capture path (screenshot()), not execute().
            "screenshot" => bail!("screenshot is handled via the capture path"),
            // `wait` is implemented by the serve loop's bounded async sleep
            // (connection::dispatch_action, #140); it never reaches execute() on the
            // live path. Bail rather than ack a no-op that misstates the real semantics.
            "wait" => bail!("wait is handled by the serve loop (connection::dispatch_action)"),
            other => bail!("unknown verb: {other}"),
        }
    }
}

/// X11 backend that owns its own display-readiness: connects to `$DISPLAY` lazily,
/// retrying with short backoff, so shinkend can be exec'd BEFORE the desktop and
/// have the ACI listener accepting within milliseconds of container start (the S8
/// boot-waterfall fix). Until the connection lands, `readiness()` honestly reports
/// `x11_up: false` and actions/captures fail with the connect error; the moment
/// Xvfb is up, the next attempt (background retry or any incoming call) promotes
/// the wrapped [`X11Executor`] and everything behaves as if connected at startup.
/// The held connection is also SELF-HEALING: a readiness sample that fails at the
/// connection level (root GetImage cannot otherwise fail) drops the dead connection
/// so the next call dials a fresh one — surviving an X server restart.
#[derive(Default)]
pub struct LazyX11Executor {
    inner: std::sync::RwLock<Option<Arc<X11Executor>>>,
}

/// First retry delay; doubles up to [`LAZY_X11_RETRY_MAX`]. Short enough that the
/// guest is observed ready within ~10 ms of the desktop painting.
const LAZY_X11_RETRY_START: std::time::Duration = std::time::Duration::from_millis(10);
const LAZY_X11_RETRY_MAX: std::time::Duration = std::time::Duration::from_millis(200);

impl LazyX11Executor {
    fn connected(&self) -> Option<Arc<X11Executor>> {
        self.inner
            .read()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }

    /// Drop a dead connection so the next call reconnects (X server restarted).
    fn disconnect(&self) {
        *self
            .inner
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = None;
    }

    /// Connect now if not already connected. Cheap to call repeatedly: a missing
    /// display socket fails in microseconds, and success persists until the
    /// connection is observed dead.
    pub fn try_connect(&self) -> Result<Arc<X11Executor>> {
        if let Some(x) = self.connected() {
            return Ok(x);
        }
        let x = Arc::new(X11Executor::connect()?);
        let mut slot = self
            .inner
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        // A concurrent connect may have won the race; either connection works.
        Ok(slot.get_or_insert_with(|| x).clone())
    }

    /// Block (on a dedicated thread) until the display connection lands, with
    /// exponential backoff from [`LAZY_X11_RETRY_START`]. Never gives up: the
    /// container's lifetime is bounded by its supervisor, and `readiness()` keeps
    /// reporting `x11_up: false` honestly the whole time.
    pub fn retry_until_connected(&self) {
        let mut delay = LAZY_X11_RETRY_START;
        loop {
            match self.try_connect() {
                Ok(x) => {
                    eprintln!("shinkend: X11 display connected ({}x{})", x.width, x.height);
                    return;
                }
                Err(_) => {
                    std::thread::sleep(delay);
                    delay = (delay * 2).min(LAZY_X11_RETRY_MAX);
                }
            }
        }
    }
}

impl Executor for LazyX11Executor {
    fn backend(&self) -> &'static str {
        "x11/xtest"
    }

    fn execute(&self, a: &ActionSpec) -> Result<String> {
        self.try_connect()
            .context("X11 display not available yet")?
            .execute(a)
    }

    fn screen_size(&self) -> (u16, u16) {
        // Before the display lands there is no real geometry; the trait default is
        // the documented fallback (clients should gate on the `ready` query first).
        self.connected()
            .map_or((1280, 800), |x| Executor::screen_size(x.as_ref()))
    }

    fn screenshot(&self) -> Result<CapturedImage> {
        self.try_connect()
            .context("X11 display not available yet")?
            .screenshot()
    }

    fn capture(&self, scope: &str, opts: EncodeOpts) -> Result<CapturedImage> {
        self.try_connect()
            .context("X11 display not available yet")?
            .capture(scope, opts)
    }

    fn capture_raw(&self, scope: &str, max_long_edge: Option<u32>) -> Result<(Vec<u8>, u16, u16)> {
        self.try_connect()
            .context("X11 display not available yet")?
            .capture_raw(scope, max_long_edge)
    }

    fn supports_raw_capture(&self) -> bool {
        true // the eventual X11 backend supports raw capture
    }

    fn damage_cursor(&self) -> Option<u64> {
        // MUST forward: the trait default is None, which silently degrades every
        // screencast on this (deployed) backend to full poll-capture per tick —
        // the exact dead-code hole the readiness fallback fell into.
        self.try_connect().ok()?.damage_cursor()
    }

    fn damage_since(&self, cursor: u64) -> Option<(u64, DamageSince)> {
        self.try_connect().ok()?.damage_since(cursor)
    }

    fn capture_raw_region(&self, rect: DamageRect) -> Result<Vec<u8>> {
        self.try_connect()?.capture_raw_region(rect)
    }

    fn list_windows(&self) -> Result<Vec<WindowInfo>> {
        self.try_connect()
            .context("X11 display not available yet")?
            .list_windows()
    }

    fn clipboard_get(&self) -> Result<String> {
        self.try_connect()
            .context("X11 display not available yet")?
            .clipboard_get()
    }

    fn readiness(&self) -> Readiness {
        let not_up = Readiness {
            ready: false,
            x11_up: false,
            root_nonblack: None,
            permissions_pending: None,
        };
        let Ok(x) = self.try_connect() else {
            return not_up;
        };
        // Delegate to the ONE X11 readiness implementation (painted root OR mapped
        // client windows), keeping the lazy wrapper's self-healing: a
        // connection-level error means the held connection is dead (X server
        // restarted) — drop it so the next call reconnects, and report honestly
        // that X is not up RIGHT NOW.
        match x.readiness_checked() {
            Ok(r) => r,
            Err(_) => {
                self.disconnect();
                not_up
            }
        }
    }
}

/// No-op backend that records executed verbs — used when no display is available
/// and in tests.
#[derive(Default)]
pub struct VirtualExecutor {
    pub log: Mutex<Vec<String>>,
    /// Frame counter so synthetic screenshots differ on every call.
    frame: std::sync::atomic::AtomicU64,
    /// In-memory clipboard so the set→get contract is exercisable without X.
    /// `None` until the first `clipboard_set` — a get then errors honestly.
    clipboard: Mutex<Option<String>>,
}

impl Executor for VirtualExecutor {
    fn backend(&self) -> &'static str {
        "virtual"
    }

    fn execute(&self, a: &ActionSpec) -> Result<String> {
        if a.verb == "clipboard_set" {
            let text = a.text.as_deref().context("clipboard_set requires `text`")?;
            *self.clipboard.lock().expect("clipboard lock") = Some(text.to_string());
        }
        self.log.lock().expect("log lock").push(a.verb.clone());
        Ok(format!("virtual: {}", a.verb))
    }

    /// A deterministic 2×2 frame whose content changes every call, so streaming
    /// consumers (and the screencast test) observe distinct, advancing frames.
    fn screenshot(&self) -> Result<CapturedImage> {
        let n = self
            .frame
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed) as u8;
        let rgb: Vec<u8> = (0..2u8 * 2 * 3).map(|i| n.wrapping_add(i)).collect();
        let png = encode_png(&rgb, 2, 2)?;
        Ok(CapturedImage {
            data: png,
            format: ImageFormat::Png,
            w: 2,
            h: 2,
        })
    }

    /// Honor the requested codec/scale even on the synthetic backend, so a client that
    /// asked for JPEG never receives PNG bytes labeled by a frame the trait default
    /// produced (the reported `format` must always match the actual encoding).
    fn capture(&self, _scope: &str, opts: EncodeOpts) -> Result<CapturedImage> {
        let n = self
            .frame
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed) as u8;
        let rgb: Vec<u8> = (0..2u8 * 2 * 3).map(|i| n.wrapping_add(i)).collect();
        encode_frame(&rgb, 2, 2, opts)
    }

    /// The same advancing synthetic frame as raw RGB, so the delta screencast path is
    /// integration-testable without a display (every capture changes every pixel).
    fn capture_raw(&self, _scope: &str, max_long_edge: Option<u32>) -> Result<(Vec<u8>, u16, u16)> {
        let n = self
            .frame
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed) as u8;
        let rgb: Vec<u8> = (0..2u8 * 2 * 3).map(|i| n.wrapping_add(i)).collect();
        Ok(match max_long_edge {
            Some(m) => downscale_rgb(&rgb, 2, 2, m),
            None => (rgb, 2, 2),
        })
    }

    fn supports_raw_capture(&self) -> bool {
        true
    }

    /// A virtual display has no windows — an empty enumeration is the honest answer
    /// (unlike a backend that cannot enumerate at all, which keeps the bailing default).
    fn list_windows(&self) -> Result<Vec<WindowInfo>> {
        Ok(Vec::new())
    }

    /// The in-memory clipboard (set via `clipboard_set` through `execute`). An unset
    /// clipboard is a typed error, mirroring the X11 backend's empty-selection answer.
    fn clipboard_get(&self) -> Result<String> {
        self.clipboard
            .lock()
            .expect("clipboard lock")
            .clone()
            .context("clipboard is empty (nothing set this session)")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec(json: &str) -> ActionSpec {
        serde_json::from_str(json).unwrap()
    }

    #[test]
    fn norm_to_px_maps_in_range_and_rejects_out_of_range() {
        assert_eq!(norm_to_px(0.0, 0.0, 800, 600).unwrap(), (0, 0));
        // 1.0 maps to the LAST on-screen pixel (dim-1), not one past the edge.
        assert_eq!(norm_to_px(1.0, 1.0, 800, 600).unwrap(), (799, 599));
        assert_eq!(norm_to_px(0.5, 0.5, 800, 600).unwrap(), (400, 300));
        assert!(norm_to_px(1.5, 0.5, 800, 600).is_err()); // x > 1
        assert!(norm_to_px(0.5, -0.1, 800, 600).is_err()); // y < 0
    }

    /// A no-display backend is ready as soon as the listener is (nothing to sample).
    #[test]
    fn virtual_readiness_is_immediately_ready_with_no_display() {
        let r = VirtualExecutor::default().readiness();
        assert!(r.ready);
        assert!(!r.x11_up);
        assert_eq!(r.root_nonblack, None);
    }

    /// The lazy X11 backend must be HONEST either way: not yet connected ⇒ not ready,
    /// nothing sampled; connected ⇒ x11_up with a real sampled answer. (Whether a
    /// display exists depends on the test host, so assert the invariant, not one side.)
    #[test]
    fn lazy_x11_readiness_invariants() {
        let lazy = LazyX11Executor::default();
        let r = lazy.readiness();
        if r.x11_up {
            assert!(r.root_nonblack.is_some());
            assert_eq!(r.ready, r.root_nonblack == Some(true));
        } else {
            assert!(!r.ready);
            assert_eq!(r.root_nonblack, None);
        }
        // And the wrapper still advertises raw capture for the eventual backend.
        assert!(lazy.supports_raw_capture());
    }

    #[test]
    fn key_keysym_resolves_function_and_named_keys() {
        assert_eq!(key_keysym("f1"), Some(0xffbe));
        assert_eq!(key_keysym("F12"), Some(0xffc9));
        assert_eq!(key_keysym("f13"), None); // out of F1..F12
        assert_eq!(key_keysym("insert"), Some(0xff63));
        assert_eq!(key_keysym("printscreen"), Some(0xff61));
    }

    #[test]
    fn scroll_steps_is_pixel_denominated_and_bounded() {
        assert_eq!(scroll_steps(0.0), 1); // minimum one click
        assert_eq!(scroll_steps(100.0), 1);
        assert_eq!(scroll_steps(150.0), 2);
        assert_eq!(scroll_steps(-300.0), 3);
        assert_eq!(scroll_steps(1_000_000.0), SCROLL_MAX_STEPS); // clamped
    }

    #[test]
    fn virtual_executor_reports_default_screen_size() {
        assert_eq!(VirtualExecutor::default().screen_size(), (1280, 800));
    }

    #[test]
    fn backend_choice_parses_stable_ids() {
        assert_eq!(BackendChoice::parse("").unwrap(), BackendChoice::Auto);
        assert_eq!(BackendChoice::parse("auto").unwrap(), BackendChoice::Auto);
        assert_eq!(
            BackendChoice::parse("x11_xtest").unwrap(),
            BackendChoice::X11Xtest
        );
        assert_eq!(
            BackendChoice::parse("x11/xtest").unwrap(),
            BackendChoice::X11Xtest
        );
        assert_eq!(
            BackendChoice::parse("virtual").unwrap(),
            BackendChoice::Virtual
        );
        // The macOS backend id parses on every platform (building it elsewhere fails).
        assert_eq!(BackendChoice::parse("macos").unwrap(), BackendChoice::MacOs);
        assert_eq!(
            BackendChoice::parse("quartz").unwrap(),
            BackendChoice::MacOs
        );
        assert!(BackendChoice::parse("python").is_err());
    }

    #[test]
    fn explicit_virtual_backend_builds_without_display() {
        let ex = build_executor(BackendChoice::Virtual).unwrap();
        assert_eq!(ex.backend(), "virtual");
    }

    #[test]
    fn parses_point_px_target() {
        let a = spec(r#"{"verb":"click","target":{"kind":"point_px","x":10,"y":20}}"#);
        assert_eq!(a.verb, "click");
        assert!(matches!(a.target, Some(Target::PointPx { .. })));
    }

    #[test]
    fn rejects_unknown_action_fields() {
        let r = serde_json::from_str::<ActionSpec>(r#"{"verb":"click","bogus":1}"#);
        assert!(r.is_err(), "unknown fields must be rejected");
    }

    #[test]
    fn screenshot_scope_is_accepted() {
        let a = spec(r#"{"verb":"screenshot","scope":"screen"}"#);
        assert_eq!(a.scope.as_deref(), Some("screen"));
    }

    #[test]
    fn parses_element_ref_with_renamed_field() {
        let a = spec(r#"{"verb":"click","target":{"kind":"element_ref","ref":"e1"}}"#);
        match a.target {
            Some(Target::ElementRef { element_ref, .. }) => assert_eq!(element_ref, "e1"),
            other => panic!("expected element_ref, got {other:?}"),
        }
    }

    #[test]
    fn virtual_executor_records_and_acks() {
        let ex = VirtualExecutor::default();
        let out = ex.execute(&spec(r#"{"verb":"scroll","dy":-300}"#)).unwrap();
        assert!(out.contains("scroll"));
        assert_eq!(ex.log.lock().unwrap().as_slice(), ["scroll"]);
        assert_eq!(ex.backend(), "virtual");
    }

    #[test]
    fn image_format_matches_the_schema_enum_exactly() {
        assert_eq!(ImageFormat::parse(None).unwrap(), ImageFormat::Png);
        assert_eq!(ImageFormat::parse(Some("png")).unwrap(), ImageFormat::Png);
        assert_eq!(ImageFormat::parse(Some("jpeg")).unwrap(), ImageFormat::Jpeg);
        // The schema enum is exactly ["png", "jpeg"]: no aliases, no trimming, no empty
        // string — the runtime must not accept what the published contract rejects.
        for bad in ["webp", "jpg", "jpeg ", " png", ""] {
            assert!(
                ImageFormat::parse(Some(bad)).is_err(),
                "{bad:?} must be rejected"
            );
        }
    }

    #[test]
    fn jpeg_encodes_smaller_than_png_and_carries_format() {
        // A 64x64 photographic-ish gradient: PNG keeps it lossless+large, JPEG compresses it.
        let (w, h) = (64u16, 64u16);
        let rgb: Vec<u8> = (0..(w as usize * h as usize))
            .flat_map(|i| {
                let x = (i % w as usize) as u8;
                let y = (i / w as usize) as u8;
                [x.wrapping_mul(3), y.wrapping_mul(5), x ^ y]
            })
            .collect();
        let png = encode_frame(
            &rgb,
            w,
            h,
            EncodeOpts {
                max_long_edge: None,
                format: ImageFormat::Png,
                quality: 80,
            },
        )
        .unwrap();
        let jpeg = encode_frame(
            &rgb,
            w,
            h,
            EncodeOpts {
                max_long_edge: None,
                format: ImageFormat::Jpeg,
                quality: 80,
            },
        )
        .unwrap();
        assert_eq!(png.format, ImageFormat::Png);
        assert_eq!(jpeg.format, ImageFormat::Jpeg);
        assert_eq!((jpeg.w, jpeg.h), (w, h));
        assert!(
            jpeg.data.len() < png.data.len(),
            "jpeg ({}) should be smaller than png ({})",
            jpeg.data.len(),
            png.data.len()
        );
        // JPEG bytes start with the SOI marker 0xFFD8.
        assert_eq!(&jpeg.data[..2], &[0xFF, 0xD8]);
        // The text-path base64 round-trips to the same raw bytes.
        assert_eq!(B64.decode(jpeg.to_base64()).unwrap(), jpeg.data);
    }

    #[test]
    fn encode_frame_downscales_before_encoding() {
        let (w, h) = (100u16, 50u16);
        let rgb = vec![128u8; w as usize * h as usize * 3];
        let img = encode_frame(
            &rgb,
            w,
            h,
            EncodeOpts {
                max_long_edge: Some(20),
                format: ImageFormat::Jpeg,
                quality: 80,
            },
        )
        .unwrap();
        assert_eq!(img.w, 20); // longer edge capped
        assert_eq!(img.h, 10); // aspect preserved
    }

    #[test]
    fn fnv1a_distinguishes_and_repeats() {
        assert_eq!(fnv1a(b"frame-a"), fnv1a(b"frame-a"));
        assert_ne!(fnv1a(b"frame-a"), fnv1a(b"frame-b"));
    }

    /// frame_hash stability: same raw pixels+dims → same hex; any pixel or dimension
    /// change → different hex. The hex form is 16 lowercase chars (fnv1a-64).
    #[test]
    fn frame_hash_is_stable_and_dimension_sensitive() {
        let rgb = vec![9u8; 4 * 2 * 3];
        let h1 = frame_hash_hex(&rgb, 4, 2);
        assert_eq!(h1, frame_hash_hex(&rgb, 4, 2));
        assert_eq!(h1.len(), 16);
        assert!(h1
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
        // same bytes, transposed dims → different hash (dims are folded in)
        assert_ne!(h1, frame_hash_hex(&rgb, 2, 4));
        let mut other = rgb.clone();
        other[5] ^= 1;
        assert_ne!(h1, frame_hash_hex(&other, 4, 2));
    }

    /// The dedup identity is the RAW frame: encoding the same pixels as PNG and JPEG
    /// yields different payload bytes but the SAME frame_hash — codec-independence,
    /// the property that lets a hash minted under one codec match under another.
    #[test]
    fn frame_hash_is_codec_independent() {
        let (w, h) = (8u16, 8u16);
        let rgb = gradient(w, h);
        let png = encode_frame(&rgb, w, h, EncodeOpts::default()).unwrap();
        let jpeg = encode_frame(
            &rgb,
            w,
            h,
            EncodeOpts {
                max_long_edge: None,
                format: ImageFormat::Jpeg,
                quality: 60,
            },
        )
        .unwrap();
        assert_ne!(png.data, jpeg.data);
        assert_eq!(frame_hash_hex(&rgb, w, h), frame_hash_hex(&rgb, w, h));
        // and hashing the ENCODED payloads would NOT have matched:
        assert_ne!(fnv1a(&png.data), fnv1a(&jpeg.data));
    }

    #[test]
    fn parse_scope_handles_screen_window_and_active() {
        assert_eq!(parse_scope("screen"), Scope::Screen);
        assert_eq!(parse_scope(""), Scope::Screen);
        assert_eq!(parse_scope("active_window"), Scope::ActiveWindow);
        assert_eq!(parse_scope("window:42"), Scope::Window(42));
        assert_eq!(parse_scope("window:0x1a"), Scope::Window(26));
        // garbage / unknown → graceful full-screen fallback
        assert_eq!(parse_scope("window:nope"), Scope::Screen);
        assert_eq!(parse_scope("bogus"), Scope::Screen);
    }

    #[test]
    fn downscale_caps_the_long_edge() {
        // 4x2 RGB → cap long edge at 2 → 2x1.
        let rgb = vec![0u8; 4 * 2 * 3];
        let (out, w, h) = downscale_rgb(&rgb, 4, 2, 2);
        assert_eq!((w, h), (2, 1));
        assert_eq!(out.len(), (w as usize) * (h as usize) * 3);
    }

    // ---- desktop verbs (G2+G3): EWMH activate message, app resolution, launch ----

    /// The _NET_ACTIVE_WINDOW client message per EWMH: format 32 on the TARGET
    /// window, data = [source=2 (pager/user — honored without focus-stealing
    /// heuristics), timestamp 0 (CurrentTime), requestor's current active, 0, 0].
    #[test]
    fn activate_message_has_the_ewmh_shape() {
        let ev = build_activate_message(77, 0x2c, 0x1a);
        assert_eq!(ev.format, 32);
        assert_eq!(ev.window, 0x2c);
        assert_eq!(ev.type_, 77);
        assert_eq!(ev.data.as_data32(), [2, 0, 0x1a, 0, 0]);
        assert_eq!(
            ev.response_type,
            x11rb::protocol::xproto::CLIENT_MESSAGE_EVENT
        );
    }

    #[test]
    fn find_window_by_app_matches_title_case_insensitively() {
        let win = |id: u32, title: &str| WindowInfo {
            id,
            title: title.into(),
            pid: None,
            x: 0,
            y: 0,
            w: 10,
            h: 10,
            focused: false,
        };
        let ws = vec![win(1, "xterm"), win(2, "xclock — Clock"), win(3, "Files")];
        assert_eq!(find_window_by_app(&ws, "XCLOCK"), Some(2));
        assert_eq!(find_window_by_app(&ws, "clock"), Some(2)); // substring
        assert_eq!(find_window_by_app(&ws, "files"), Some(3));
        assert_eq!(find_window_by_app(&ws, "gimp"), None);
    }

    #[test]
    fn spawn_app_returns_a_pid_and_reaps() {
        // `sh -c true` exists on every CI/dev host; the reaper thread wait()s it.
        let pid = spawn_app("sh", &["-c".into(), "true".into()]).unwrap();
        assert!(pid > 0);
    }

    #[test]
    fn spawn_app_rejects_missing_binary_and_empty_app() {
        let err = spawn_app("/nonexistent/shinken-no-such-binary", &[])
            .unwrap_err()
            .to_string();
        assert!(err.contains("failed to spawn"), "unexpected error: {err}");
        let err = spawn_app("  ", &[]).unwrap_err().to_string();
        assert!(err.contains("non-empty"), "unexpected error: {err}");
    }

    /// The virtual backend's in-memory clipboard honors the set→get contract and
    /// answers a typed error before anything was set.
    #[test]
    fn virtual_clipboard_set_get_roundtrip() {
        let v = VirtualExecutor::default();
        let err = v.clipboard_get().unwrap_err().to_string();
        assert!(err.contains("clipboard is empty"), "unexpected: {err}");
        let a = spec(r#"{"verb":"clipboard_set","text":"hello ✂️"}"#);
        v.execute(&a).unwrap();
        assert_eq!(v.clipboard_get().unwrap(), "hello ✂️");
    }

    #[test]
    fn downscale_is_noop_when_within_cap() {
        let rgb = vec![7u8; 3 * 3 * 3];
        let (out, w, h) = downscale_rgb(&rgb, 3, 3, 10);
        assert_eq!((w, h), (3, 3));
        assert_eq!(out, rgb);
        // a cap of 0 means "no limit" — also a no-op
        let (_, w0, h0) = downscale_rgb(&rgb, 3, 3, 0);
        assert_eq!((w0, h0), (3, 3));
    }

    // ---- dirty-tile delta (B2) ----

    /// A photographic-ish gradient frame (PNG-compressible but not trivial).
    fn gradient(w: u16, h: u16) -> Vec<u8> {
        (0..(w as usize * h as usize))
            .flat_map(|i| {
                let x = (i % w as usize) as u8;
                let y = (i / w as usize) as u8;
                [x.wrapping_mul(3), y.wrapping_mul(5), x ^ y]
            })
            .collect()
    }

    /// Set one pixel in an RGB8 frame.
    fn poke(rgb: &mut [u8], w: u16, x: usize, y: usize) {
        let i = (y * w as usize + x) * 3;
        rgb[i] ^= 0xff;
    }

    #[test]
    fn diff_tiles_finds_only_changed_tiles_including_edge_tiles() {
        // 100x70: tile grid is (0,0,64,64) (64,0,36,64) (0,64,64,6) (64,64,36,6) —
        // the right/bottom tiles are edge tiles smaller than 64.
        let (w, h) = (100u16, 70u16);
        let prev = gradient(w, h);
        // identical frames → no tiles
        assert!(diff_tiles(&prev, &prev, w, h).is_empty());
        // one pixel inside the top-left tile → exactly that tile
        let mut curr = prev.clone();
        poke(&mut curr, w, 10, 10);
        assert_eq!(diff_tiles(&prev, &curr, w, h), vec![(0, 0, 64, 64)]);
        // the very last pixel (bottom-right edge tile, 36x6) → exactly that tile
        let mut curr = prev.clone();
        poke(&mut curr, w, 99, 69);
        assert_eq!(diff_tiles(&prev, &curr, w, h), vec![(64, 64, 36, 6)]);
        // pixels in two tiles → both, in row-major order
        let mut curr = prev.clone();
        poke(&mut curr, w, 70, 0); // (64,0,36,64)
        poke(&mut curr, w, 0, 69); // (0,64,64,6)
        assert_eq!(
            diff_tiles(&prev, &curr, w, h),
            vec![(64, 0, 36, 64), (0, 64, 64, 6)]
        );
    }

    #[test]
    fn encode_tiles_produces_decodable_payloads_in_the_stream_codec() {
        let (w, h) = (100u16, 70u16);
        let rgb = gradient(w, h);
        let rects = vec![(64, 64, 36, 6), (0, 0, 64, 64)];
        // PNG tiles: decodable, with the tile's own dimensions.
        let tiles = encode_tiles(&rgb, w, h, &rects, EncodeOpts::default()).unwrap();
        assert_eq!(tiles.len(), 2);
        assert_eq!(
            (tiles[0].x, tiles[0].y, tiles[0].w, tiles[0].h),
            (64, 64, 36, 6)
        );
        let decoder = png::Decoder::new(std::io::Cursor::new(tiles[0].data.clone()));
        let reader = decoder.read_info().unwrap();
        assert_eq!(reader.info().width, 36);
        assert_eq!(reader.info().height, 6);
        // The text-path base64 round-trips to the same raw bytes.
        assert_eq!(B64.decode(tiles[0].to_base64()).unwrap(), tiles[0].data);
        // JPEG tiles use the existing encoder (SOI marker).
        let jt = encode_tiles(
            &rgb,
            w,
            h,
            &rects,
            EncodeOpts {
                max_long_edge: None,
                format: ImageFormat::Jpeg,
                quality: 80,
            },
        )
        .unwrap();
        assert_eq!(&jt[1].data[..2], &[0xFF, 0xD8]);
    }

    #[test]
    fn one_dirty_tile_costs_less_than_the_full_frame() {
        // The lossless bandwidth lever: a single changed 64px tile must encode to
        // (much) fewer bytes than re-encoding the whole frame.
        let (w, h) = (256u16, 192u16);
        let rgb = gradient(w, h);
        let full = encode_frame(&rgb, w, h, EncodeOpts::default()).unwrap();
        let tiles = encode_tiles(&rgb, w, h, &[(0, 0, 64, 64)], EncodeOpts::default()).unwrap();
        assert!(
            tiles[0].data.len() < full.data.len() / 2,
            "tile ({}) should be far smaller than the full frame ({})",
            tiles[0].data.len(),
            full.data.len()
        );
    }

    #[test]
    fn delta_state_keyframe_tiles_cadence_and_idle() {
        let (w, h) = (100u16, 70u16);
        let opts = EncodeOpts::default();
        let mut state = DeltaState::default();
        let mut frame = gradient(w, h);

        // 1) no baseline → keyframe
        match state.tick(&frame, w, h, opts).unwrap() {
            DeltaFrame::Key(img) => assert_eq!((img.w, img.h), (w, h)),
            other => panic!("expected Key, got {other:?}"),
        }
        state.commit(frame.clone(), w, h, true);

        // 2) unchanged → idle suppression, and suppression does NOT advance cadence
        assert!(matches!(
            state.tick(&frame, w, h, opts).unwrap(),
            DeltaFrame::Unchanged
        ));

        // 3) KEYFRAME_INTERVAL-1 changed frames → tiles; the next one → keyframe
        for i in 1..KEYFRAME_INTERVAL {
            poke(&mut frame, w, (i % 60) as usize, 5);
            match state.tick(&frame, w, h, opts).unwrap() {
                DeltaFrame::Tiles(t) => assert_eq!(t.len(), 1, "one dirty tile expected"),
                other => panic!("expected Tiles at delivered frame {i}, got {other:?}"),
            }
            state.commit(frame.clone(), w, h, false);
        }
        poke(&mut frame, w, 3, 3);
        assert!(
            matches!(state.tick(&frame, w, h, opts).unwrap(), DeltaFrame::Key(_)),
            "every {KEYFRAME_INTERVAL}th delivered frame must be a keyframe"
        );
        state.commit(frame.clone(), w, h, true);

        // 4) a dimension change (resize) invalidates the baseline → keyframe
        let small = gradient(50, 40);
        assert!(matches!(
            state.tick(&small, 50, 40, opts).unwrap(),
            DeltaFrame::Key(_)
        ));
    }

    #[test]
    fn delta_state_uncommitted_tick_rediffs_the_same_baseline() {
        // Commit-on-send semantics: when a frame is dropped (try_send Full), the
        // baseline must not advance — the next tick re-diffs against the SAME prev,
        // so the change is re-attempted instead of lost.
        let (w, h) = (100u16, 70u16);
        let opts = EncodeOpts::default();
        let mut state = DeltaState::default();
        let base = gradient(w, h);
        state.commit(base.clone(), w, h, true);

        let mut changed = base.clone();
        poke(&mut changed, w, 10, 10);
        // tick once (frame "dropped": no commit), then tick again — both must see the change
        assert!(matches!(
            state.tick(&changed, w, h, opts).unwrap(),
            DeltaFrame::Tiles(_)
        ));
        assert!(matches!(
            state.tick(&changed, w, h, opts).unwrap(),
            DeltaFrame::Tiles(_)
        ));
    }

    // ---- XDamage-driven capture (change-proportional pipeline, half B) ----

    #[test]
    fn rect_union_and_clamp() {
        assert_eq!(union_rect((0, 0, 10, 10), (5, 5, 10, 10)), (0, 0, 15, 15));
        assert_eq!(union_rect((20, 30, 4, 4), (0, 0, 2, 2)), (0, 0, 24, 34));
        // clamp keeps the on-screen part, drops fully off-screen rects
        assert_eq!(
            clamp_rect((10, 10, 100, 100), 50, 40),
            Some((10, 10, 40, 30))
        );
        assert_eq!(clamp_rect((0, 0, 10, 10), 50, 40), Some((0, 0, 10, 10)));
        assert_eq!(clamp_rect((60, 0, 10, 10), 50, 40), None);
        assert_eq!(clamp_rect((0, 40, 10, 10), 50, 40), None);
    }

    #[test]
    fn damage_log_cursor_semantics() {
        let mut log = DamageLog::default();
        let c0 = log.epoch();
        assert_eq!(log.since(c0), DamageSince::Clean, "fresh log is clean");

        let c1 = log.record((10, 10, 5, 5));
        assert_eq!(log.since(c0), DamageSince::Region((10, 10, 5, 5)));
        assert_eq!(log.since(c1), DamageSince::Clean, "cursor at head is clean");

        let c2 = log.record((30, 2, 4, 4));
        // an older cursor sees the union of everything after it
        assert_eq!(log.since(c0), DamageSince::Region((10, 2, 24, 13)));
        assert_eq!(log.since(c1), DamageSince::Region((30, 2, 4, 4)));
        assert_eq!(log.since(c2), DamageSince::Clean);
        // a future/garbage cursor (>= epoch) reads clean, never panics
        assert_eq!(log.since(c2 + 100), DamageSince::Clean);
    }

    #[test]
    fn damage_log_overflow_degrades_to_full_never_misses() {
        let mut log = DamageLog::default();
        let stale = log.epoch();
        for i in 0..(DAMAGE_RING_MAX as u32 + 10) {
            log.record((i, 0, 1, 1));
        }
        // the stale cursor fell off the ring: the answer must be Full (capture
        // everything), never Clean (silently miss a change)
        assert_eq!(log.since(stale), DamageSince::Full);
        // a recent cursor still gets a precise region
        let recent = log.epoch();
        log.record((7, 7, 3, 3));
        assert_eq!(log.since(recent), DamageSince::Region((7, 7, 3, 3)));
    }

    #[test]
    fn compose_partial_patches_the_baseline() {
        let (w, h) = (8u16, 6u16);
        let base = vec![1u8; w as usize * h as usize * 3];
        let mut st = DeltaState::default();
        assert!(
            st.compose_partial((0, 0, 2, 2), &[9u8; 12]).is_none(),
            "no baseline yet"
        );
        assert_eq!(st.baseline_dims(), None);
        st.commit(base.clone(), w, h, true);
        assert_eq!(st.baseline_dims(), Some((w, h)));

        // patch a 2x2 region at (3, 1)
        let pixels = vec![9u8; 2 * 2 * 3];
        let (frame, fw, fh) = st.compose_partial((3, 1, 2, 2), &pixels).unwrap();
        assert_eq!((fw, fh), (w, h));
        let mut expect = base.clone();
        for row in 1..3usize {
            for col in 3..5usize {
                let i = (row * w as usize + col) * 3;
                expect[i..i + 3].copy_from_slice(&[9, 9, 9]);
            }
        }
        assert_eq!(frame, expect);

        // overflowing rect or wrong buffer size → None (caller falls back to full)
        assert!(st.compose_partial((7, 5, 2, 2), &[0u8; 12]).is_none());
        assert!(st.compose_partial((0, 0, 2, 2), &[0u8; 11]).is_none());
    }

    #[test]
    fn damage_env_parsing() {
        // (reads the real env var; only assert the default-off spellings)
        for v in ["off", "0", "false", "OFF"] {
            std::env::set_var(DAMAGE_ENV, v);
            assert!(damage_disabled_by_env(), "{v:?} must disable damage");
        }
        std::env::set_var(DAMAGE_ENV, "on");
        assert!(!damage_disabled_by_env());
        std::env::remove_var(DAMAGE_ENV);
        assert!(!damage_disabled_by_env(), "absent env keeps damage on");
    }

    #[test]
    fn virtual_executor_capture_raw_advances_and_downscales() {
        let ex = VirtualExecutor::default();
        let (a, w, h) = ex.capture_raw("screen", None).unwrap();
        assert_eq!((w, h), (2, 2));
        assert_eq!(a.len(), 2 * 2 * 3);
        let (b, _, _) = ex.capture_raw("screen", None).unwrap();
        assert_ne!(a, b, "synthetic raw frames must advance per capture");
        let (c, w1, h1) = ex.capture_raw("screen", Some(1)).unwrap();
        assert_eq!((w1, h1), (1, 1));
        assert_eq!(c.len(), 3);
        assert!(ex.supports_raw_capture());
    }

    #[test]
    fn parse_combo_splits_modifiers_and_key() {
        assert_eq!(parse_combo("ctrl+s"), (vec!["ctrl"], "s"));
        assert_eq!(
            parse_combo("ctrl + shift + a"),
            (vec!["ctrl", "shift"], "a")
        );
        assert_eq!(parse_combo("Return"), (Vec::<&str>::new(), "Return"));
        // xdotool-style chords resolve end to end at the keysym level: every
        // modifier and the key itself have a keysym.
        let (mods, key) = parse_combo("super+shift+t");
        assert_eq!(mods, vec!["super", "shift"]);
        assert!(mods.iter().all(|m| mod_keysym(m).is_some()));
        assert_eq!(key_keysym(key), Some(0x74));
    }

    #[test]
    fn key_keysym_covers_numpad_and_named_symbols() {
        // numpad digits are contiguous from XK_KP_0
        assert_eq!(key_keysym("kp_0"), Some(0xffb0));
        assert_eq!(key_keysym("kp_9"), Some(0xffb9));
        assert_eq!(key_keysym("kp_enter"), Some(0xff8d));
        assert_eq!(key_keysym("kp_add"), Some(0xffab));
        assert_eq!(key_keysym("kp_decimal"), Some(0xffae));
        assert_eq!(key_keysym("kp_10"), None); // not a single numpad digit
        assert_eq!(key_keysym("kp_x"), None);
        // named symbols (a literal '+' can't survive the combo splitter)
        assert_eq!(key_keysym("plus"), Some(0x2b));
        assert_eq!(key_keysym("minus"), Some(0x2d));
        assert_eq!(key_keysym("equal"), Some(0x3d));
        assert_eq!(key_keysym("period"), Some(0x2e));
        assert_eq!(key_keysym("scroll_lock"), Some(0xff14));
        assert_eq!(key_keysym("pause"), Some(0xff13));
        // and a chord using them parses cleanly
        assert_eq!(parse_combo("ctrl+plus"), (vec!["ctrl"], "plus"));
    }

    // ---- drag / mouse_down / mouse_up (coordinate-tier gesture verbs) ----

    #[test]
    fn parse_button_maps_names_and_rejects_unknown() {
        assert_eq!(parse_button(None).unwrap(), BTN_LEFT); // schema default
        assert_eq!(parse_button(Some("left")).unwrap(), BTN_LEFT);
        assert_eq!(parse_button(Some("middle")).unwrap(), BTN_MIDDLE);
        assert_eq!(parse_button(Some("right")).unwrap(), BTN_RIGHT);
        for bad in ["Left", "wheel", "back", ""] {
            assert!(parse_button(Some(bad)).is_err(), "{bad:?} must be rejected");
        }
    }

    #[test]
    fn drag_path_hits_both_endpoints_and_is_bounded() {
        // endpoints are exact, including a same-point "drag"
        let p = drag_path((10, 20), (10, 20));
        assert_eq!(p.first(), Some(&(10, 20)));
        assert_eq!(p.last(), Some(&(10, 20)));
        // a short drag still interpolates at least one segment
        let p = drag_path((0, 0), (4, 0));
        assert_eq!(p, vec![(0, 0), (4, 0)]);
        // a long drag is bounded at DRAG_MAX_STEPS segments and ends exactly at `to`
        let p = drag_path((0, 0), (10_000, 5_000));
        assert_eq!(p.len(), DRAG_MAX_STEPS + 1);
        assert_eq!(p[0], (0, 0));
        assert_eq!(*p.last().unwrap(), (10_000, 5_000));
        // the path is monotonic toward the target on both axes
        for w in p.windows(2) {
            assert!(w[1].0 >= w[0].0 && w[1].1 >= w[0].1);
        }
        // a medium drag steps roughly every DRAG_PX_PER_STEP pixels
        let p = drag_path((0, 0), (160, 0));
        assert_eq!(p.len(), 11); // 160 / 16 = 10 segments + the origin
    }

    #[test]
    fn parses_drag_with_to_button_duration_and_observe() {
        let a = spec(
            r#"{"verb":"drag",
                "target":{"kind":"point_px","x":10,"y":20},
                "to":{"kind":"point_px","x":300,"y":200},
                "button":"left","duration_ms":250,
                "observe":{"format":"jpeg","quality":70,"max_long_edge":640,"scope":"screen"}}"#,
        );
        assert_eq!(a.verb, "drag");
        assert!(matches!(a.to, Some(Target::PointPx { .. })));
        assert_eq!(a.button.as_deref(), Some("left"));
        assert_eq!(a.duration_ms, Some(250));
        let obs = a.observe.unwrap();
        assert_eq!(obs.format.as_deref(), Some("jpeg"));
        assert_eq!(obs.quality, Some(70));
        assert_eq!(obs.max_long_edge, Some(640));
        assert_eq!(obs.scope.as_deref(), Some("screen"));
    }

    #[test]
    fn observe_spec_rejects_unknown_fields() {
        let r = serde_json::from_str::<ActionSpec>(
            r#"{"verb":"click","target":{"kind":"point_px","x":1,"y":2},"observe":{"fps":30}}"#,
        );
        assert!(r.is_err(), "unknown observe fields must be rejected");
    }

    #[test]
    fn virtual_executor_lists_no_windows_and_logs_gesture_verbs() {
        let ex = VirtualExecutor::default();
        assert_eq!(ex.list_windows().unwrap(), Vec::<WindowInfo>::new());
        ex.execute(&spec(
            r#"{"verb":"mouse_down","target":{"kind":"point_px","x":1,"y":2}}"#,
        ))
        .unwrap();
        ex.execute(&spec(r#"{"verb":"mouse_up"}"#)).unwrap();
        assert_eq!(
            ex.log.lock().unwrap().as_slice(),
            ["mouse_down", "mouse_up"]
        );
    }

    #[test]
    fn window_info_serializes_the_query_value_shape() {
        let w = WindowInfo {
            id: 0x1a,
            title: "xterm".into(),
            pid: Some(42),
            x: 4,
            y: 8,
            w: 200,
            h: 100,
            focused: true,
        };
        let v: serde_json::Value =
            serde_json::from_str(&serde_json::to_string(&w).unwrap()).unwrap();
        assert_eq!(v["id"], 26);
        assert_eq!(v["title"], "xterm");
        assert_eq!(v["pid"], 42);
        assert_eq!(v["x"], 4);
        assert_eq!(v["y"], 8);
        assert_eq!(v["w"], 200);
        assert_eq!(v["h"], 100);
        assert_eq!(v["focused"], true);
    }

    #[test]
    fn keysym_lookups() {
        assert_eq!(char_keysym('a'), Some(0x61));
        assert_eq!(char_keysym('A'), Some(0x41));
        assert_eq!(char_keysym(' '), Some(0x20));
        assert_eq!(char_keysym('€'), None);
        assert_eq!(mod_keysym("CTRL"), Some(0xffe3));
        assert_eq!(key_keysym("enter"), Some(0xff0d));
        assert_eq!(key_keysym("s"), Some(0x73));
        assert_eq!(key_keysym("nope"), None);
    }
}
