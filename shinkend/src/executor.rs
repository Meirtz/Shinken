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
    AtomEnum, ConnectionExt as _, ImageFormat as XImageFormat, Window, BUTTON_PRESS_EVENT,
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

/// A captured frame: base64-encoded image bytes, the codec they are in, and pixel dims.
#[derive(Debug, Clone)]
pub struct CapturedImage {
    pub data_base64: String,
    pub format: ImageFormat,
    pub w: u16,
    pub h: u16,
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
fn parse_scope(scope: &str) -> Scope {
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

/// Downscale an RGB8 buffer with nearest-neighbour sampling so its longer edge is at
/// most `max_long_edge` px. Returns the buffer unchanged if it already fits (or the
/// cap is 0). Cheap and dependency-free — good enough for a bandwidth preview.
fn downscale_rgb(rgb: &[u8], w: u16, h: u16, max_long_edge: u32) -> (Vec<u8>, u16, u16) {
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

/// Downscale (if requested) then encode an RGB8 frame per `opts`, returning the codec the
/// caller should advertise on the wire. Single chokepoint so every backend's `capture`
/// shares identical downscale+encode semantics.
fn encode_frame(rgb: &[u8], w: u16, h: u16, opts: EncodeOpts) -> Result<CapturedImage> {
    let (rgb, w, h) = match opts.max_long_edge {
        Some(m) => downscale_rgb(rgb, w, h, m),
        None => (rgb.to_vec(), w, h),
    };
    let bytes = match opts.format {
        ImageFormat::Png => encode_png(&rgb, w as u32, h as u32)?,
        ImageFormat::Jpeg => encode_jpeg(&rgb, w, h, opts.quality)?,
    };
    Ok(CapturedImage {
        data_base64: B64.encode(bytes),
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

/// A changed tile, encoded for the wire.
#[derive(Debug, Clone)]
pub struct EncodedTile {
    pub x: u32,
    pub y: u32,
    pub w: u16,
    pub h: u16,
    pub data_base64: String,
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
                data_base64: B64.encode(bytes),
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
}

const EXECUTOR_ENV: &str = "SHINKEND_EXECUTOR";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum BackendChoice {
    Auto,
    X11Xtest,
    Virtual,
    PyAutoGui,
}

impl BackendChoice {
    fn parse(value: &str) -> Result<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "" | "auto" => Ok(Self::Auto),
            "x11_xtest" | "x11/xtest" | "xtest" | "x11" => Ok(Self::X11Xtest),
            "virtual" => Ok(Self::Virtual),
            "pyautogui" | "pyautogui_subprocess" => Ok(Self::PyAutoGui),
            other => bail!(
                "unknown {EXECUTOR_ENV} value {other:?}; \
                 expected auto, x11_xtest, virtual, or pyautogui"
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
/// display is reachable, otherwise the virtual test backend.
pub fn default_executor() -> Result<Arc<dyn Executor>> {
    build_executor(BackendChoice::from_env()?)
}

fn build_executor(choice: BackendChoice) -> Result<Arc<dyn Executor>> {
    match choice {
        BackendChoice::Auto => match X11Executor::connect() {
            Ok(x) => {
                eprintln!(
                    "shinkend: action backend = x11/xtest ({}x{})",
                    x.width, x.height
                );
                Ok(Arc::new(x))
            }
            Err(e) => {
                eprintln!("shinkend: no X11 display ({e}); action backend = virtual (no-op)");
                Ok(Arc::new(VirtualExecutor::default()))
            }
        },
        BackendChoice::X11Xtest => {
            let x = X11Executor::connect().context("SHINKEND_EXECUTOR=x11_xtest")?;
            eprintln!(
                "shinkend: action backend = x11/xtest ({}x{})",
                x.width, x.height
            );
            Ok(Arc::new(x))
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
    }
}

// ---- pointer button numbers (X11) ----
const BTN_LEFT: u8 = 1;
const BTN_RIGHT: u8 = 3;
const BTN_SCROLL_UP: u8 = 4;
const BTN_SCROLL_DOWN: u8 = 5;
const BTN_SCROLL_LEFT: u8 = 6;
const BTN_SCROLL_RIGHT: u8 = 7;
/// Wire `dx`/`dy` are pixel-denominated (see docs/design/aci-spec.md); one wheel
/// click ≈ this many pixels. Adapters that speak in wheel clicks convert at their edge.
const SCROLL_PX_PER_STEP: f64 = 100.0;
const SCROLL_MAX_STEPS: u32 = 20;

/// Pixels → bounded wheel-click count (shared by both scroll axes).
fn scroll_steps(px: f64) -> u32 {
    ((px.abs() / SCROLL_PX_PER_STEP).ceil() as u32).clamp(1, SCROLL_MAX_STEPS)
}

/// X11 backend: synthetic pointer + keyboard input via the XTEST extension.
pub struct X11Executor {
    conn: Mutex<x11rb::rust_connection::RustConnection>,
    root: Window,
    width: u16,
    height: u16,
    /// keysym -> (keycode, needs_shift), built from the server's keyboard mapping.
    keymap: HashMap<u32, (u8, bool)>,
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
        Ok(Self {
            conn: Mutex::new(conn),
            root,
            width,
            height,
            keymap,
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

    /// The EWMH `_NET_ACTIVE_WINDOW`, if the window manager publishes one.
    fn active_window(&self) -> Result<Option<Window>> {
        let conn = self.conn.lock().expect("x11 conn lock");
        let atom = conn.intern_atom(true, b"_NET_ACTIVE_WINDOW")?.reply()?.atom;
        if atom == 0 {
            return Ok(None);
        }
        let prop = conn
            .get_property(false, self.root, atom, AtomEnum::WINDOW, 0, 1)?
            .reply()?;
        Ok(prop
            .value32()
            .and_then(|mut it| it.next())
            .filter(|w| *w != 0))
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
        let (wu, hu) = (w as usize, h as usize);
        ensure!(wu * hu > 0, "zero-sized capture region");
        let reply = {
            let conn = self.conn.lock().expect("x11 conn lock");
            let cookie = conn.get_image(XImageFormat::Z_PIXMAP, drawable, 0, 0, w, h, !0)?;
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
                bail!("element_ref resolution needs the observation engine (M1b, #4)")
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
        "printscreen" | "print" | "prtsc" => Some(0xff61),
        "menu" | "apps" => Some(0xff67),
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

/// Split `"ctrl+shift+s"` into (`["ctrl","shift"]`, `"s"`).
fn parse_combo(combo: &str) -> (Vec<&str>, &str) {
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

/// No-op backend that records executed verbs — used when no display is available
/// and in tests.
#[derive(Default)]
pub struct VirtualExecutor {
    pub log: Mutex<Vec<String>>,
    /// Frame counter so synthetic screenshots differ on every call.
    frame: std::sync::atomic::AtomicU64,
}

impl Executor for VirtualExecutor {
    fn backend(&self) -> &'static str {
        "virtual"
    }

    fn execute(&self, a: &ActionSpec) -> Result<String> {
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
            data_base64: B64.encode(png),
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
        // base64 inflates both equally; comparing encoded lengths is a fair size proxy.
        assert!(
            jpeg.data_base64.len() < png.data_base64.len(),
            "jpeg ({}) should be smaller than png ({})",
            jpeg.data_base64.len(),
            png.data_base64.len()
        );
        // JPEG bytes start with the SOI marker 0xFFD8.
        let bytes = B64.decode(&jpeg.data_base64).unwrap();
        assert_eq!(&bytes[..2], &[0xFF, 0xD8]);
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
        let bytes = B64.decode(&tiles[0].data_base64).unwrap();
        let decoder = png::Decoder::new(std::io::Cursor::new(bytes));
        let reader = decoder.read_info().unwrap();
        assert_eq!(reader.info().width, 36);
        assert_eq!(reader.info().height, 6);
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
        let jbytes = B64.decode(&jt[1].data_base64).unwrap();
        assert_eq!(&jbytes[..2], &[0xFF, 0xD8]);
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
            tiles[0].data_base64.len() < full.data_base64.len() / 2,
            "tile ({}) should be far smaller than the full frame ({})",
            tiles[0].data_base64.len(),
            full.data_base64.len()
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
