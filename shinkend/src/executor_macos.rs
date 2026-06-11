//! macOS native executor (v1): CoreGraphics capture + CGEvent input synthesis.
//!
//! The first cross-platform backend behind the same [`Executor`] trait the X11
//! backend implements — capture returns raw RGB8 and feeds the EXISTING shared
//! encode pipeline (PNG/JPEG/downscale), input maps the same wire verbs and the
//! same keysym-name model (`ctrl+s`, `super+a`, …; `super` = Command).
//!
//! **Binding choice (v1):** CoreGraphics — `CGDisplayCreateImage` (full screen)
//! and `CGWindowListCreateImage` (per-window). Both are deprecated since
//! macOS 14 in favor of ScreenCaptureKit but remain functional; the SCK upgrade
//! path (SCShareableContent/SCContentFilter/SCScreenshotManager via objc2) is
//! documented in docs/engineering/macos-engine.md. Input uses CGEvent synthesis
//! (`CGEventCreateMouseEvent`/`CGEventCreateKeyboardEvent`/
//! `CGEventCreateScrollWheelEvent`), posted at the HID tap.
//!
//! **Coordinate space:** the ACI speaks in CAPTURE pixels (what the agent sees in
//! the screenshot). On Retina displays CGEvent wants global display POINTS, so
//! pointer coordinates are scaled by the live points-per-pixel ratio read off the
//! current display mode — a click at screenshot pixel (x, y) lands on exactly that
//! pixel. `screen_size` reports capture pixels for the same reason.
//!
//! **Permissions (TCC):** Screen Recording gates real capture content
//! (`CGDisplayCreateImage` "succeeds" without it but returns a wallpaper-only
//! frame, so this backend preflights and refuses honestly); Accessibility gates
//! CGEvent posting (events are silently dropped without it). Both are surfaced
//! through [`Executor::readiness`] as `permissions_pending`, and a capture/input
//! call attempted without the grant fails with a typed `permission_pending: …`
//! error instead of lying or crashing. Grants attach to the RESPONSIBLE app —
//! the terminal that launched shinkend — see docs/engineering/macos-engine.md.
//!
//! **Keymap:** key combos resolve against a static US-ANSI virtual-keycode table
//! (`kVK_ANSI_*`); `type_text` is layout-independent (the text travels as a
//! unicode string on the keyboard event via `CGEventKeyboardSetUnicodeString`),
//! so only `key` combos assume the US physical layout. Layout-aware mapping
//! (`UCKeyTranslate`) is the documented follow-up.
//!
//! **AX tree:** out of v1 scope. The Linux a11y engine defines the observation
//! contract; the macOS implementation (AXUIElement) follows it later.

use std::sync::Once;

use anyhow::{anyhow, bail, Context, Result};
use core_foundation::base::{CFType, TCFType};
use core_foundation::number::CFNumber;
use core_foundation::string::CFString;
use core_graphics::base::kCGImageAlphaNoneSkipLast;
use core_graphics::color_space::CGColorSpace;
use core_graphics::context::CGContext;
use core_graphics::display::CGDisplay;
use core_graphics::event::{
    CGEvent, CGEventFlags, CGEventTapLocation, CGEventType, CGMouseButton, EventField,
    ScrollEventUnit,
};
use core_graphics::event_source::{CGEventSource, CGEventSourceStateID};
use core_graphics::geometry::{CGPoint, CGRect, CGSize};
use core_graphics::image::CGImage;
use core_graphics::window as cgwindow;

use crate::executor::{
    encode_frame, norm_to_px_f64, parse_combo, parse_scope, ActionSpec, CapturedImage, EncodeOpts,
    Executor, Readiness, Scope, Target,
};

// ---- TCC permission probes ----
// CoreGraphics is already linked by the core-graphics crate; ApplicationServices
// carries AXIsProcessTrusted. Both functions exist since macOS 10.15.
#[link(name = "ApplicationServices", kind = "framework")]
extern "C" {
    fn AXIsProcessTrusted() -> bool;
}
extern "C" {
    fn CGPreflightScreenCaptureAccess() -> bool;
    fn CGRequestScreenCaptureAccess() -> bool;
}

/// Whether Screen Recording is granted to this process's responsible app.
fn screen_capture_granted() -> bool {
    unsafe { CGPreflightScreenCaptureAccess() }
}

/// Whether Accessibility (input synthesis) is granted.
fn input_trusted() -> bool {
    unsafe { AXIsProcessTrusted() }
}

/// Whether the main display is ONLINE (connected). Deliberately not the "active"
/// list: CGGetActiveDisplayList answers empty for processes outside the console
/// user's graphical login session (and while displays sleep), while capture/input
/// still work there once TCC is granted — online is the honest existence bit.
fn display_present() -> bool {
    CGDisplay::main().is_online()
}

/// One-shot grant guidance, printed the first time a permission is found missing.
fn print_permission_guidance_once(screen: bool, input: bool) {
    static ONCE: Once = Once::new();
    ONCE.call_once(|| {
        eprintln!(
            "shinkend: macOS permissions pending — Screen Recording: {}, Accessibility: {}.\n         \
             Grant BOTH to the app that launched shinkend (your terminal) in\n         \
             System Settings → Privacy & Security → Screen & System Audio Recording / Accessibility,\n         \
             then restart that terminal. Until then captures/input report `permission_pending`\n         \
             and the `ready` query answers permissions_pending=true.",
            if screen { "granted" } else { "MISSING" },
            if input { "granted" } else { "MISSING" },
        );
    });
}

/// Live display geometry: capture-pixel dimensions plus the points-per-pixel
/// scale CGEvent coordinates need (Retina: 0.5; non-Retina: 1.0). Read fresh off
/// the current display mode (microseconds) so a resolution change mid-session
/// can't leave clicks landing on stale coordinates.
#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) struct Geometry {
    pub px_w: u16,
    pub px_h: u16,
    pub pt_per_px_x: f64,
    pub pt_per_px_y: f64,
}

fn live_geometry() -> Result<Geometry> {
    let display = CGDisplay::main();
    let mode = display
        .display_mode()
        .context("no display mode (no display attached?)")?;
    let (pw, ph) = (mode.pixel_width(), mode.pixel_height());
    let (w, h) = (mode.width(), mode.height());
    anyhow::ensure!(pw > 0 && ph > 0, "zero-sized display mode");
    anyhow::ensure!(
        pw <= u16::MAX as u64 && ph <= u16::MAX as u64,
        "display larger than the wire's u16 dimensions: {pw}x{ph}"
    );
    Ok(Geometry {
        px_w: pw as u16,
        px_h: ph as u16,
        pt_per_px_x: w as f64 / pw as f64,
        pt_per_px_y: h as f64 / ph as f64,
    })
}

// ---- US-ANSI virtual keycodes (kVK_*) ----

const KC_COMMAND: u16 = 0x37;
const KC_SHIFT: u16 = 0x38;
const KC_OPTION: u16 = 0x3A;
const KC_CONTROL: u16 = 0x3B;
const KC_RETURN: u16 = 0x24;
const KC_TAB: u16 = 0x30;
const KC_SPACE: u16 = 0x31;
const KC_BACKSPACE: u16 = 0x33; // kVK_Delete — the mac "delete" key = backspace
const KC_ESCAPE: u16 = 0x35;
const KC_CAPS_LOCK: u16 = 0x39;
const KC_FORWARD_DELETE: u16 = 0x75;
const KC_HOME: u16 = 0x73;
const KC_END: u16 = 0x77;
const KC_PAGE_UP: u16 = 0x74;
const KC_PAGE_DOWN: u16 = 0x79;
const KC_LEFT: u16 = 0x7B;
const KC_RIGHT: u16 = 0x7C;
const KC_DOWN: u16 = 0x7D;
const KC_UP: u16 = 0x7E;
/// F1..F12 (kVK_F1..kVK_F12 are NOT contiguous).
const KC_F: [u16; 12] = [
    0x7A, 0x78, 0x63, 0x76, 0x60, 0x61, 0x62, 0x64, 0x65, 0x6D, 0x67, 0x6F,
];

// CGEventFlags bits (match core-graphics' CGEventFlags constants).
pub(crate) const FLAG_SHIFT: u64 = 0x0002_0000;
pub(crate) const FLAG_CONTROL: u64 = 0x0004_0000;
pub(crate) const FLAG_ALTERNATE: u64 = 0x0008_0000;
pub(crate) const FLAG_COMMAND: u64 = 0x0010_0000;

/// Map a single character to `(US-ANSI keycode, needs_shift)`.
pub(crate) fn mac_char_keycode(ch: char) -> Option<(u16, bool)> {
    // Letters: lowercase unshifted, uppercase shifted, same physical key.
    if ch.is_ascii_alphabetic() {
        let kc = match ch.to_ascii_lowercase() {
            'a' => 0x00,
            'b' => 0x0B,
            'c' => 0x08,
            'd' => 0x02,
            'e' => 0x0E,
            'f' => 0x03,
            'g' => 0x05,
            'h' => 0x04,
            'i' => 0x22,
            'j' => 0x26,
            'k' => 0x28,
            'l' => 0x25,
            'm' => 0x2E,
            'n' => 0x2D,
            'o' => 0x1F,
            'p' => 0x23,
            'q' => 0x0C,
            'r' => 0x0F,
            's' => 0x01,
            't' => 0x11,
            'u' => 0x20,
            'v' => 0x09,
            'w' => 0x0D,
            'x' => 0x07,
            'y' => 0x10,
            'z' => 0x06,
            _ => unreachable!(),
        };
        return Some((kc, ch.is_ascii_uppercase()));
    }
    // Digits + US-ANSI punctuation, both shift levels.
    let (kc, shift) = match ch {
        '1' => (0x12, false),
        '2' => (0x13, false),
        '3' => (0x14, false),
        '4' => (0x15, false),
        '5' => (0x17, false),
        '6' => (0x16, false),
        '7' => (0x1A, false),
        '8' => (0x1C, false),
        '9' => (0x19, false),
        '0' => (0x1D, false),
        '!' => (0x12, true),
        '@' => (0x13, true),
        '#' => (0x14, true),
        '$' => (0x15, true),
        '%' => (0x17, true),
        '^' => (0x16, true),
        '&' => (0x1A, true),
        '*' => (0x1C, true),
        '(' => (0x19, true),
        ')' => (0x1D, true),
        '-' => (0x1B, false),
        '_' => (0x1B, true),
        '=' => (0x18, false),
        '+' => (0x18, true),
        '[' => (0x21, false),
        '{' => (0x21, true),
        ']' => (0x1E, false),
        '}' => (0x1E, true),
        '\\' => (0x2A, false),
        '|' => (0x2A, true),
        ';' => (0x29, false),
        ':' => (0x29, true),
        '\'' => (0x27, false),
        '"' => (0x27, true),
        ',' => (0x2B, false),
        '<' => (0x2B, true),
        '.' => (0x2F, false),
        '>' => (0x2F, true),
        '/' => (0x2C, false),
        '?' => (0x2C, true),
        '`' => (0x32, false),
        '~' => (0x32, true),
        ' ' => (KC_SPACE, false),
        '\n' => (KC_RETURN, false),
        '\t' => (KC_TAB, false),
        _ => return None,
    };
    Some((kc, shift))
}

/// Map a key NAME (the same xdotool-style names the X11 backend accepts) to
/// `(keycode, needs_shift)`. Names with no mac equivalent (insert, numlock,
/// printscreen, menu) return `None` — an honest error beats a wrong key.
pub(crate) fn mac_key_keycode(key: &str) -> Option<(u16, bool)> {
    let mut it = key.chars();
    if let (Some(c), None) = (it.next(), it.next()) {
        return mac_char_keycode(c);
    }
    let kc = match key.to_ascii_lowercase().as_str() {
        "enter" | "return" => KC_RETURN,
        "tab" => KC_TAB,
        "escape" | "esc" => KC_ESCAPE,
        "space" => KC_SPACE,
        "backspace" => KC_BACKSPACE,
        "delete" | "del" => KC_FORWARD_DELETE, // forward delete, like X11's XK_Delete
        "up" => KC_UP,
        "down" => KC_DOWN,
        "left" => KC_LEFT,
        "right" => KC_RIGHT,
        "home" => KC_HOME,
        "end" => KC_END,
        "pageup" | "page_up" | "pgup" => KC_PAGE_UP,
        "pagedown" | "page_down" | "pgdn" => KC_PAGE_DOWN,
        "capslock" | "caps_lock" => KC_CAPS_LOCK,
        f if f.starts_with('f')
            && f[1..]
                .parse::<u32>()
                .map(|n| (1..=12).contains(&n))
                .unwrap_or(false) =>
        {
            let n: usize = f[1..].parse().unwrap();
            KC_F[n - 1]
        }
        _ => return None,
    };
    Some((kc, false))
}

/// Map a modifier NAME to `(keycode, CGEventFlags bit)`. `super`/`meta`/`cmd`/
/// `command`/`win` all mean Command on macOS (the wire's `super+…` lands as ⌘).
pub(crate) fn mac_modifier(name: &str) -> Option<(u16, u64)> {
    match name.to_ascii_lowercase().as_str() {
        "ctrl" | "control" => Some((KC_CONTROL, FLAG_CONTROL)),
        "shift" => Some((KC_SHIFT, FLAG_SHIFT)),
        "alt" | "option" => Some((KC_OPTION, FLAG_ALTERNATE)),
        "super" | "meta" | "cmd" | "command" | "win" => Some((KC_COMMAND, FLAG_COMMAND)),
        _ => None,
    }
}

// ---- pure input planning (unit-testable without posting anything) ----

/// One synthesizable input event. Coordinates are CAPTURE PIXELS; the poster
/// converts to display points with the live [`Geometry`] right before posting.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum PlannedInput {
    Move {
        x: f64,
        y: f64,
    },
    /// Left/right press or release; `click_state` feeds kCGMouseEventClickState
    /// (2 marks the second press of a double-click).
    Button {
        right: bool,
        down: bool,
        x: f64,
        y: f64,
        click_state: i64,
    },
    /// Press/release at the pointer's CURRENT position (the target-less
    /// `mouse_down`/`mouse_up` halves) — resolved at post time from the live
    /// cursor location, already in display points.
    ButtonHere {
        right: bool,
        down: bool,
        click_state: i64,
    },
    /// Left-button drag motion (the pointer is down).
    Drag {
        x: f64,
        y: f64,
    },
    /// Pixel-denominated wheel deltas in CG sign convention (+y = up, +x = left).
    Scroll {
        wheel_dy: i32,
        wheel_dx: i32,
    },
    /// Keyboard key with the modifier flags active at press time.
    Key {
        keycode: u16,
        down: bool,
        flags: u64,
    },
    /// A `type_text` chunk (UTF-16, ≤[`TEXT_CHUNK_UTF16`] units, surrogate-safe),
    /// typed via CGEventKeyboardSetUnicodeString — layout-independent.
    Text {
        utf16: Vec<u16>,
    },
}

/// Max UTF-16 units per `type_text` keyboard event (Apple recommends ~20).
pub(crate) const TEXT_CHUNK_UTF16: usize = 20;

/// Steps interpolated between drag start and end so apps register the motion.
pub(crate) const DRAG_STEPS: u32 = 8;

/// Resolve a target to capture-pixel coords with the SAME validation the X11
/// backend applies (#141): finite, in `[0, dim)`; `point_norm` in `[0, 1]`.
fn resolve_px(target: Option<&Target>, w: u16, h: u16) -> Result<(f64, f64)> {
    match target.context("action requires a target")? {
        Target::PointPx { x, y } => {
            if !x.is_finite() || !y.is_finite() {
                bail!("point_px must be finite: ({x}, {y})");
            }
            if *x < 0.0 || *y < 0.0 || *x >= w as f64 || *y >= h as f64 {
                bail!("point_px out of range: ({x}, {y}) — must be within [0, {w}) x [0, {h})");
            }
            Ok((*x, *y))
        }
        Target::PointNorm { x, y } => norm_to_px_f64(*x, *y, w, h),
        Target::ElementRef { .. } => {
            bail!("element_ref resolution needs the observation engine (designed; not in macOS v1)")
        }
    }
}

/// Split text into UTF-16 chunks of at most [`TEXT_CHUNK_UTF16`] units without
/// splitting a surrogate pair (chunking is per-char, so a pair stays together).
pub(crate) fn plan_text_chunks(text: &str) -> Vec<Vec<u16>> {
    let mut chunks = Vec::new();
    let mut cur: Vec<u16> = Vec::with_capacity(TEXT_CHUNK_UTF16);
    let mut buf = [0u16; 2];
    for ch in text.chars() {
        let units = ch.encode_utf16(&mut buf);
        if cur.len() + units.len() > TEXT_CHUNK_UTF16 && !cur.is_empty() {
            chunks.push(std::mem::take(&mut cur));
        }
        cur.extend_from_slice(units);
    }
    if !cur.is_empty() {
        chunks.push(cur);
    }
    chunks
}

/// Plan a `key` combo: modifier downs (cumulative flags), implicit Shift when the
/// key's US-ANSI level needs it, the key press/release under the full flag set,
/// then releases in reverse order — mirroring the X11 backend's combo semantics.
pub(crate) fn plan_key_combo(combo: &str) -> Result<Vec<PlannedInput>> {
    let (mods, key) = parse_combo(combo);
    anyhow::ensure!(!key.is_empty(), "empty key combo");
    let mut mod_keys = Vec::with_capacity(mods.len() + 1);
    let mut flags = 0u64;
    for m in &mods {
        let (kc, bit) = mac_modifier(m).with_context(|| format!("unknown modifier {m:?}"))?;
        flags |= bit;
        mod_keys.push((kc, flags));
    }
    let (key_kc, needs_shift) =
        mac_key_keycode(key).with_context(|| format!("unknown key {key:?} (US-ANSI keymap)"))?;
    if needs_shift && flags & FLAG_SHIFT == 0 {
        flags |= FLAG_SHIFT;
        mod_keys.push((KC_SHIFT, flags));
    }
    let mut ev = Vec::with_capacity(mod_keys.len() * 2 + 2);
    for &(kc, f) in &mod_keys {
        ev.push(PlannedInput::Key {
            keycode: kc,
            down: true,
            flags: f,
        });
    }
    ev.push(PlannedInput::Key {
        keycode: key_kc,
        down: true,
        flags,
    });
    ev.push(PlannedInput::Key {
        keycode: key_kc,
        down: false,
        flags,
    });
    let mut remaining = flags;
    for &(kc, _) in mod_keys.iter().rev() {
        let bit = match kc {
            KC_SHIFT => FLAG_SHIFT,
            KC_CONTROL => FLAG_CONTROL,
            KC_OPTION => FLAG_ALTERNATE,
            _ => FLAG_COMMAND,
        };
        remaining &= !bit;
        ev.push(PlannedInput::Key {
            keycode: kc,
            down: false,
            flags: remaining,
        });
    }
    Ok(ev)
}

/// Translate one ACI action into the input events to post plus the ack status
/// string. Pure — the verb dispatch is unit-tested against this with no display.
pub(crate) fn plan_action(a: &ActionSpec, w: u16, h: u16) -> Result<(Vec<PlannedInput>, String)> {
    match a.verb.as_str() {
        "move" => {
            let (x, y) = resolve_px(a.target.as_ref(), w, h)?;
            Ok((
                vec![PlannedInput::Move { x, y }],
                format!("moved to {},{}", x as i64, y as i64),
            ))
        }
        "click" | "right_click" => {
            let right = a.verb == "right_click";
            let (x, y) = resolve_px(a.target.as_ref(), w, h)?;
            let ev = vec![
                PlannedInput::Move { x, y },
                PlannedInput::Button {
                    right,
                    down: true,
                    x,
                    y,
                    click_state: 1,
                },
                PlannedInput::Button {
                    right,
                    down: false,
                    x,
                    y,
                    click_state: 1,
                },
            ];
            let label = if right { "right-clicked" } else { "clicked" };
            Ok((ev, format!("{label} {},{}", x as i64, y as i64)))
        }
        "double_click" => {
            let (x, y) = resolve_px(a.target.as_ref(), w, h)?;
            let mut ev = vec![PlannedInput::Move { x, y }];
            for click_state in [1i64, 2] {
                ev.push(PlannedInput::Button {
                    right: false,
                    down: true,
                    x,
                    y,
                    click_state,
                });
                ev.push(PlannedInput::Button {
                    right: false,
                    down: false,
                    x,
                    y,
                    click_state,
                });
            }
            Ok((ev, format!("double-clicked {},{}", x as i64, y as i64)))
        }
        "scroll" => {
            let (x, y) = resolve_px(a.target.as_ref(), w, h)?;
            let dx = a.dx.unwrap_or(0.0);
            let dy = a.dy.unwrap_or(0.0);
            if dx == 0.0 && dy == 0.0 {
                bail!("scroll requires a nonzero dx or dy");
            }
            // ACI: +dy = down, +dx = right (pixel-denominated). CG wheel axes:
            // +wheel1 = up, +wheel2 = left — negate both.
            let clamp = |v: f64| (-v).clamp(i32::MIN as f64, i32::MAX as f64) as i32;
            let ev = vec![
                PlannedInput::Move { x, y },
                PlannedInput::Scroll {
                    wheel_dy: clamp(dy),
                    wheel_dx: clamp(dx),
                },
            ];
            Ok((ev, format!("scrolled {},{} px", dx as i64, dy as i64)))
        }
        "drag" => {
            // Schema shape (the 17-verb ACI `drag`): down at `target`, interpolated
            // drag motion, up at the `to` target. `duration_ms` is accepted but the
            // pacing is the planner's fixed EVENT_SETTLE per step in v1; CG only has
            // a LeftMouseDragged event type, so a non-left `button` is an honest nack.
            let (x0, y0) = resolve_px(a.target.as_ref(), w, h)?;
            let to = a.to.as_ref().context("drag requires a `to` target")?;
            let (x1, y1) = resolve_px(Some(to), w, h)?;
            if let Some(b) = a.button.as_deref() {
                if b != "left" {
                    bail!("drag button {b:?} not supported by the macos backend (v1: left only)");
                }
            }
            if x0 == x1 && y0 == y1 {
                bail!("drag requires distinct start and end points");
            }
            let (dx, dy) = (x1 - x0, y1 - y0);
            let mut ev = vec![
                PlannedInput::Move { x: x0, y: y0 },
                PlannedInput::Button {
                    right: false,
                    down: true,
                    x: x0,
                    y: y0,
                    click_state: 1,
                },
            ];
            for i in 1..=DRAG_STEPS {
                let t = i as f64 / DRAG_STEPS as f64;
                ev.push(PlannedInput::Drag {
                    x: x0 + dx * t,
                    y: y0 + dy * t,
                });
            }
            ev.push(PlannedInput::Button {
                right: false,
                down: false,
                x: x1,
                y: y1,
                click_state: 1,
            });
            Ok((
                ev,
                format!(
                    "dragged {},{} -> {},{}",
                    x0 as i64, y0 as i64, x1 as i64, y1 as i64
                ),
            ))
        }
        "mouse_down" | "mouse_up" => {
            // Decomposed button halves (down → free moves → up). Optional target:
            // move first when given, else act at the pointer's current position
            // (resolved at post time — the planner stays pure).
            let down = a.verb == "mouse_down";
            let right = match a.button.as_deref() {
                None | Some("left") => false,
                Some("right") => true,
                Some(other) => bail!(
                    "pointer button {other:?} not supported by the macos backend (v1: left, right)"
                ),
            };
            let status = format!(
                "{} button {}",
                if down { "pressed" } else { "released" },
                if right { "right" } else { "left" }
            );
            let ev = match a.target.as_ref() {
                Some(t) => {
                    let (x, y) = resolve_px(Some(t), w, h)?;
                    vec![
                        PlannedInput::Move { x, y },
                        PlannedInput::Button {
                            right,
                            down,
                            x,
                            y,
                            click_state: 1,
                        },
                    ]
                }
                None => vec![PlannedInput::ButtonHere {
                    right,
                    down,
                    click_state: 1,
                }],
            };
            Ok((ev, status))
        }
        "type_text" => {
            let text = a.text.as_deref().context("type_text requires `text`")?;
            let ev = plan_text_chunks(text)
                .into_iter()
                .map(|utf16| PlannedInput::Text { utf16 })
                .collect();
            Ok((ev, format!("typed {} chars", text.chars().count())))
        }
        "key" => {
            let keys = a.keys.as_deref().context("key requires `keys`")?;
            Ok((plan_key_combo(keys)?, format!("key {keys}")))
        }
        // Dispatched via the capture path (screenshot()), not execute().
        "screenshot" => bail!("screenshot is handled via the capture path"),
        // `wait` is the serve loop's bounded async sleep (connection::dispatch_action).
        "wait" => bail!("wait is handled by the serve loop (connection::dispatch_action)"),
        other => bail!("unknown verb: {other}"),
    }
}

// ---- the executor ----

/// Settle delay after each posted CGEvent — event delivery to apps is
/// asynchronous, and back-to-back press/release with zero spacing is dropped or
/// coalesced by some apps. 8 ms keeps a 5-event click well under human latency.
const EVENT_SETTLE: std::time::Duration = std::time::Duration::from_millis(8);

/// The native macOS backend. Stateless between calls: geometry is re-read per
/// action/capture (microseconds) so resolution changes never go stale, and TCC
/// permission state is re-probed so a mid-session grant starts working
/// immediately (no restart needed for the probe itself; macOS may still require
/// the responsible app to relaunch for the grant to take effect).
pub struct MacExecutor;

impl MacExecutor {
    /// Construct, requiring only that a display exists. Missing TCC grants do
    /// NOT fail construction — readiness reports them honestly and per-call
    /// errors carry the `permission_pending` state — but guidance is printed
    /// once, and the Screen Recording request is registered with TCC so the
    /// process appears in the System Settings pane.
    pub fn new() -> Result<Self> {
        anyhow::ensure!(display_present(), "no online display (headless session?)");
        let (screen, input) = (screen_capture_granted(), input_trusted());
        if !screen || !input {
            print_permission_guidance_once(screen, input);
            if !screen {
                // Registers shinkend's responsible app in the Screen Recording
                // pane and triggers the system prompt (once per app per TCC reset).
                unsafe { CGRequestScreenCaptureAccess() };
            }
        }
        Ok(MacExecutor)
    }

    fn geometry(&self) -> Result<Geometry> {
        live_geometry()
    }

    /// Resolve a [`Scope`] to captured RGB8 + dimensions (capture pixels).
    fn capture_scope(&self, scope: Scope) -> Result<(Vec<u8>, u16, u16)> {
        if !screen_capture_granted() {
            print_permission_guidance_once(false, input_trusted());
            bail!(
                "permission_pending: Screen Recording not granted to the app that launched \
                 shinkend — grant it in System Settings → Privacy & Security → Screen & System \
                 Audio Recording, then restart that app"
            );
        }
        match scope {
            Scope::Screen => self.capture_screen(),
            Scope::ActiveWindow => match frontmost_window_id() {
                Some(id) => self.capture_window(id),
                // No frontmost layer-0 window (bare desktop) — full screen, like X11.
                None => self.capture_screen(),
            },
            Scope::Window(id) => self.capture_window(id),
        }
    }

    fn capture_screen(&self) -> Result<(Vec<u8>, u16, u16)> {
        let image = CGDisplay::main()
            .image()
            .context("CGDisplayCreateImage returned NULL (display asleep or detached?)")?;
        cgimage_to_rgb(&image)
    }

    fn capture_window(&self, id: u32) -> Result<(Vec<u8>, u16, u16)> {
        // CGRectNull = "the window's own bounds".
        let null_rect = CGRect::new(
            &CGPoint::new(f64::INFINITY, f64::INFINITY),
            &CGSize::new(0.0, 0.0),
        );
        let image = cgwindow::create_image(
            null_rect,
            cgwindow::kCGWindowListOptionIncludingWindow,
            id,
            cgwindow::kCGWindowImageBoundsIgnoreFraming | cgwindow::kCGWindowImageBestResolution,
        )
        .with_context(|| {
            format!(
                "CGWindowListCreateImage returned NULL (is window id {id} valid and on-screen?)"
            )
        })?;
        cgimage_to_rgb(&image)
    }

    /// Post one planned event, converting capture-pixel coords to display points.
    fn post(&self, ev: &PlannedInput, geom: Geometry) -> Result<()> {
        let src = CGEventSource::new(CGEventSourceStateID::HIDSystemState)
            .map_err(|_| anyhow!("CGEventSource creation failed"))?;
        let pt = |x: f64, y: f64| CGPoint::new(x * geom.pt_per_px_x, y * geom.pt_per_px_y);
        let event = match *ev {
            PlannedInput::Move { x, y } => CGEvent::new_mouse_event(
                src,
                CGEventType::MouseMoved,
                pt(x, y),
                CGMouseButton::Left,
            ),
            PlannedInput::Button {
                right,
                down,
                x,
                y,
                click_state,
            } => {
                let ty = match (right, down) {
                    (false, true) => CGEventType::LeftMouseDown,
                    (false, false) => CGEventType::LeftMouseUp,
                    (true, true) => CGEventType::RightMouseDown,
                    (true, false) => CGEventType::RightMouseUp,
                };
                let button = if right {
                    CGMouseButton::Right
                } else {
                    CGMouseButton::Left
                };
                let e = CGEvent::new_mouse_event(src, ty, pt(x, y), button);
                if let Ok(e) = &e {
                    e.set_integer_value_field(EventField::MOUSE_EVENT_CLICK_STATE, click_state);
                }
                e
            }
            PlannedInput::ButtonHere {
                right,
                down,
                click_state,
            } => {
                // The live cursor location is already in display points.
                let loc = CGEvent::new(src.clone())
                    .map_err(|_| anyhow!("CGEvent creation failed"))?
                    .location();
                let ty = match (right, down) {
                    (false, true) => CGEventType::LeftMouseDown,
                    (false, false) => CGEventType::LeftMouseUp,
                    (true, true) => CGEventType::RightMouseDown,
                    (true, false) => CGEventType::RightMouseUp,
                };
                let button = if right {
                    CGMouseButton::Right
                } else {
                    CGMouseButton::Left
                };
                let e = CGEvent::new_mouse_event(src, ty, loc, button);
                if let Ok(e) = &e {
                    e.set_integer_value_field(EventField::MOUSE_EVENT_CLICK_STATE, click_state);
                }
                e
            }
            PlannedInput::Drag { x, y } => CGEvent::new_mouse_event(
                src,
                CGEventType::LeftMouseDragged,
                pt(x, y),
                CGMouseButton::Left,
            ),
            PlannedInput::Scroll { wheel_dy, wheel_dx } => {
                CGEvent::new_scroll_event(src, ScrollEventUnit::PIXEL, 2, wheel_dy, wheel_dx, 0)
            }
            PlannedInput::Key {
                keycode,
                down,
                flags,
            } => {
                let e = CGEvent::new_keyboard_event(src, keycode, down);
                if let Ok(e) = &e {
                    e.set_flags(CGEventFlags::from_bits_truncate(flags));
                }
                e
            }
            PlannedInput::Text { ref utf16 } => {
                // Layout-independent typing: a key event carrying the unicode
                // string (CGEventKeyboardSetUnicodeString), press + release.
                let down = CGEvent::new_keyboard_event(src.clone(), 0, true);
                let up = CGEvent::new_keyboard_event(src, 0, false);
                if let (Ok(d), Ok(u)) = (&down, &up) {
                    d.set_string_from_utf16_unchecked(utf16);
                    u.set_string_from_utf16_unchecked(utf16);
                    d.post(CGEventTapLocation::HID);
                    std::thread::sleep(EVENT_SETTLE);
                }
                up
            }
        }
        .map_err(|_| anyhow!("CGEvent creation failed"))?;
        event.post(CGEventTapLocation::HID);
        std::thread::sleep(EVENT_SETTLE);
        Ok(())
    }
}

/// Convert a `CGImage` to tightly-packed RGB8 by drawing it into an owned RGBX
/// bitmap context — robust to whatever pixel format/stride the capture returned,
/// at the cost of one blit. Feeds the shared downscale/encode pipeline.
fn cgimage_to_rgb(image: &CGImage) -> Result<(Vec<u8>, u16, u16)> {
    let (w, h) = (image.width(), image.height());
    anyhow::ensure!(w > 0 && h > 0, "zero-sized capture");
    anyhow::ensure!(
        w <= u16::MAX as usize && h <= u16::MAX as usize,
        "capture larger than the wire's u16 dimensions: {w}x{h}"
    );
    let mut rgbx = vec![0u8; w * h * 4];
    {
        let cs = CGColorSpace::create_device_rgb();
        let ctx = CGContext::create_bitmap_context(
            Some(rgbx.as_mut_ptr() as *mut _),
            w,
            h,
            8,
            w * 4,
            &cs,
            kCGImageAlphaNoneSkipLast, // memory layout: R,G,B,X per pixel
        );
        ctx.draw_image(
            CGRect::new(&CGPoint::new(0.0, 0.0), &CGSize::new(w as f64, h as f64)),
            image,
        );
    }
    let mut rgb = Vec::with_capacity(w * h * 3);
    for px in rgbx.chunks_exact(4) {
        rgb.extend_from_slice(&px[..3]);
    }
    Ok((rgb, w as u16, h as u16))
}

/// The frontmost normal (layer-0) window's id, if any. CGWindowListCopyWindowInfo
/// returns on-screen windows front-to-back; the first layer-0 entry is the active
/// app's focused window — a permissionless heuristic (no AX needed) that matches
/// the EWMH `_NET_ACTIVE_WINDOW` semantics closely enough for v1 capture.
fn frontmost_window_id() -> Option<u32> {
    let info = cgwindow::copy_window_info(
        cgwindow::kCGWindowListOptionOnScreenOnly | cgwindow::kCGWindowListExcludeDesktopElements,
        cgwindow::kCGNullWindowID,
    )?;
    let layer_key = CFString::from_static_string("kCGWindowLayer");
    let number_key = CFString::from_static_string("kCGWindowNumber");
    for item in info.iter() {
        let dict = unsafe {
            core_foundation::dictionary::CFDictionary::<CFString, CFType>::wrap_under_get_rule(
                *item as *const _,
            )
        };
        let layer = dict
            .find(&layer_key)
            .and_then(|v| v.downcast::<CFNumber>())
            .and_then(|n| n.to_i64());
        if layer != Some(0) {
            continue;
        }
        if let Some(id) = dict
            .find(&number_key)
            .and_then(|v| v.downcast::<CFNumber>())
            .and_then(|n| n.to_i64())
        {
            return u32::try_from(id).ok();
        }
    }
    None
}

impl Executor for MacExecutor {
    fn backend(&self) -> &'static str {
        "macos/coregraphics"
    }

    fn screen_size(&self) -> (u16, u16) {
        // Capture-pixel dimensions (the ACI coordinate space); the trait default
        // is the documented fallback if the display vanished mid-session.
        self.geometry()
            .map(|g| (g.px_w, g.px_h))
            .unwrap_or((1280, 800))
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
            Some(m) => crate::executor::downscale_rgb(&rgb, w, h, m),
            None => (rgb, w, h),
        })
    }

    fn supports_raw_capture(&self) -> bool {
        true // delta screencast works; damage_cursor() is None → poll-diff engages
    }

    fn readiness(&self) -> Readiness {
        let display_up = display_present();
        let (screen, input) = (screen_capture_granted(), input_trusted());
        Readiness {
            // Observations work iff a display exists AND capture shows real
            // content (without the Screen Recording grant CGDisplayCreateImage
            // returns a wallpaper-only frame — not a usable observation).
            ready: display_up && screen,
            // The cross-platform "display connection is up" bit (X11's x11_up).
            x11_up: display_up,
            root_nonblack: None, // not sampled on macOS
            permissions_pending: Some(!(screen && input)),
        }
    }

    fn execute(&self, a: &ActionSpec) -> Result<String> {
        if !input_trusted() {
            print_permission_guidance_once(screen_capture_granted(), false);
            bail!(
                "permission_pending: Accessibility not granted to the app that launched \
                 shinkend (CGEvent input would be silently dropped) — grant it in System \
                 Settings → Privacy & Security → Accessibility, then restart that app"
            );
        }
        let geom = self.geometry()?;
        let (events, status) = plan_action(a, geom.px_w, geom.px_h)?;
        for ev in &events {
            self.post(ev, geom)?;
        }
        Ok(status)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec(json: &str) -> ActionSpec {
        serde_json::from_str(json).unwrap()
    }

    // ---- keycode mapping ----

    #[test]
    fn char_keycodes_cover_letters_digits_and_shift_levels() {
        assert_eq!(mac_char_keycode('a'), Some((0x00, false)));
        assert_eq!(mac_char_keycode('A'), Some((0x00, true)));
        assert_eq!(mac_char_keycode('z'), Some((0x06, false)));
        assert_eq!(mac_char_keycode('1'), Some((0x12, false)));
        assert_eq!(mac_char_keycode('!'), Some((0x12, true)));
        assert_eq!(mac_char_keycode('/'), Some((0x2C, false)));
        assert_eq!(mac_char_keycode('?'), Some((0x2C, true)));
        assert_eq!(mac_char_keycode(' '), Some((KC_SPACE, false)));
        assert_eq!(mac_char_keycode('\n'), Some((KC_RETURN, false)));
        assert_eq!(mac_char_keycode('\t'), Some((KC_TAB, false)));
        assert_eq!(mac_char_keycode('€'), None); // beyond the combo keymap (type_text handles it)
                                                 // Every printable ASCII char must map (the agent's typical combo space).
        for c in 0x20u8..=0x7e {
            assert!(
                mac_char_keycode(c as char).is_some(),
                "unmapped ASCII char {:?}",
                c as char
            );
        }
    }

    #[test]
    fn named_keys_match_the_x11_name_set() {
        assert_eq!(mac_key_keycode("enter"), Some((KC_RETURN, false)));
        assert_eq!(mac_key_keycode("Return"), Some((KC_RETURN, false)));
        assert_eq!(mac_key_keycode("escape"), Some((KC_ESCAPE, false)));
        assert_eq!(mac_key_keycode("backspace"), Some((KC_BACKSPACE, false)));
        assert_eq!(mac_key_keycode("delete"), Some((KC_FORWARD_DELETE, false)));
        assert_eq!(mac_key_keycode("pageup"), Some((KC_PAGE_UP, false)));
        assert_eq!(mac_key_keycode("f1"), Some((0x7A, false)));
        assert_eq!(mac_key_keycode("F12"), Some((0x6F, false)));
        assert_eq!(mac_key_keycode("f13"), None); // out of F1..F12
        assert_eq!(mac_key_keycode("s"), Some((0x01, false)));
        // No mac equivalent → honest None, never a wrong key.
        for missing in ["insert", "numlock", "printscreen", "menu", "nope"] {
            assert_eq!(mac_key_keycode(missing), None, "{missing:?}");
        }
    }

    #[test]
    fn super_and_friends_mean_command() {
        for name in ["super", "meta", "cmd", "command", "win"] {
            assert_eq!(mac_modifier(name), Some((KC_COMMAND, FLAG_COMMAND)));
        }
        assert_eq!(mac_modifier("ctrl"), Some((KC_CONTROL, FLAG_CONTROL)));
        assert_eq!(mac_modifier("shift"), Some((KC_SHIFT, FLAG_SHIFT)));
        assert_eq!(mac_modifier("alt"), Some((KC_OPTION, FLAG_ALTERNATE)));
        assert_eq!(mac_modifier("hyper"), None);
    }

    // ---- verb dispatch (pure planner = the mock) ----

    #[test]
    fn click_plans_move_then_press_release() {
        let (ev, status) = plan_action(
            &spec(r#"{"verb":"click","target":{"kind":"point_px","x":10,"y":20}}"#),
            100,
            100,
        )
        .unwrap();
        assert_eq!(status, "clicked 10,20");
        assert_eq!(
            ev,
            vec![
                PlannedInput::Move { x: 10.0, y: 20.0 },
                PlannedInput::Button {
                    right: false,
                    down: true,
                    x: 10.0,
                    y: 20.0,
                    click_state: 1
                },
                PlannedInput::Button {
                    right: false,
                    down: false,
                    x: 10.0,
                    y: 20.0,
                    click_state: 1
                },
            ]
        );
    }

    #[test]
    fn double_click_marks_the_second_pair_with_click_state_2() {
        let (ev, _) = plan_action(
            &spec(r#"{"verb":"double_click","target":{"kind":"point_norm","x":0.5,"y":0.5}}"#),
            101,
            101,
        )
        .unwrap();
        assert_eq!(ev.len(), 5); // move + 2x(press+release)
        let states: Vec<i64> = ev
            .iter()
            .filter_map(|e| match e {
                PlannedInput::Button { click_state, .. } => Some(*click_state),
                _ => None,
            })
            .collect();
        assert_eq!(states, vec![1, 1, 2, 2]);
        // point_norm 0.5 on a 101px screen lands on pixel 50 (the X11 mapping).
        assert!(matches!(ev[0], PlannedInput::Move { x, y } if x == 50.0 && y == 50.0));
    }

    #[test]
    fn right_click_uses_the_right_button() {
        let (ev, status) = plan_action(
            &spec(r#"{"verb":"right_click","target":{"kind":"point_px","x":1,"y":1}}"#),
            10,
            10,
        )
        .unwrap();
        assert!(status.starts_with("right-clicked"));
        assert!(ev
            .iter()
            .all(|e| !matches!(e, PlannedInput::Button { right: false, .. })));
    }

    #[test]
    fn scroll_negates_into_cg_wheel_axes() {
        // ACI +dy = down → CG wheel1 negative; ACI -dx = left → CG wheel2 positive.
        let (ev, _) = plan_action(
            &spec(
                r#"{"verb":"scroll","target":{"kind":"point_px","x":5,"y":5},"dy":120,"dx":-30}"#,
            ),
            10,
            10,
        )
        .unwrap();
        assert_eq!(
            ev[1],
            PlannedInput::Scroll {
                wheel_dy: -120,
                wheel_dx: 30
            }
        );
        // zero scroll is rejected, like X11
        assert!(plan_action(
            &spec(r#"{"verb":"scroll","target":{"kind":"point_px","x":5,"y":5}}"#),
            10,
            10
        )
        .is_err());
    }

    #[test]
    fn drag_is_down_interpolated_motion_up() {
        // The schema drag shape: `target` (start) → `to` (drop point).
        let (ev, status) = plan_action(
            &spec(
                r#"{"verb":"drag","target":{"kind":"point_px","x":10,"y":10},
                    "to":{"kind":"point_px","x":50,"y":10},"duration_ms":250,"button":"left"}"#,
            ),
            100,
            100,
        )
        .unwrap();
        assert_eq!(status, "dragged 10,10 -> 50,10");
        assert_eq!(ev.len(), 2 + DRAG_STEPS as usize + 1);
        assert!(matches!(
            ev[1],
            PlannedInput::Button {
                down: true,
                x,
                ..
            } if x == 10.0
        ));
        // motion is monotone toward the drop point and ends there
        assert!(matches!(ev[ev.len() - 2], PlannedInput::Drag { x, y } if x == 50.0 && y == 10.0));
        assert!(matches!(
            ev[ev.len() - 1],
            PlannedInput::Button {
                down: false,
                x,
                ..
            } if x == 50.0
        ));
        // a missing `to`, an off-screen endpoint, and a non-left button are rejected
        assert!(plan_action(
            &spec(r#"{"verb":"drag","target":{"kind":"point_px","x":90,"y":10}}"#),
            100,
            100
        )
        .is_err());
        assert!(plan_action(
            &spec(
                r#"{"verb":"drag","target":{"kind":"point_px","x":90,"y":10},
                    "to":{"kind":"point_px","x":130,"y":10}}"#,
            ),
            100,
            100
        )
        .is_err());
        assert!(plan_action(
            &spec(
                r#"{"verb":"drag","target":{"kind":"point_px","x":10,"y":10},
                    "to":{"kind":"point_px","x":50,"y":10},"button":"middle"}"#,
            ),
            100,
            100
        )
        .is_err());
    }

    #[test]
    fn mouse_down_up_halves_plan_button_events() {
        // Targeted half: move + button at the point.
        let (ev, status) = plan_action(
            &spec(
                r#"{"verb":"mouse_down","target":{"kind":"point_px","x":5,"y":6},"button":"right"}"#,
            ),
            100,
            100,
        )
        .unwrap();
        assert_eq!(status, "pressed button right");
        assert!(matches!(ev[0], PlannedInput::Move { x, y } if x == 5.0 && y == 6.0));
        assert!(matches!(
            ev[1],
            PlannedInput::Button {
                right: true,
                down: true,
                ..
            }
        ));
        // Target-less half: a ButtonHere resolved at post time (live cursor).
        let (ev, status) = plan_action(&spec(r#"{"verb":"mouse_up"}"#), 100, 100).unwrap();
        assert_eq!(status, "released button left");
        assert_eq!(
            ev,
            vec![PlannedInput::ButtonHere {
                right: false,
                down: false,
                click_state: 1
            }]
        );
        // middle is honestly unsupported in v1
        assert!(plan_action(
            &spec(r#"{"verb":"mouse_down","button":"middle"}"#),
            100,
            100
        )
        .is_err());
    }

    #[test]
    fn type_text_chunks_utf16_without_splitting_pairs() {
        let chunks = plan_text_chunks(&"a".repeat(45));
        assert_eq!(
            chunks.iter().map(Vec::len).collect::<Vec<_>>(),
            vec![20, 20, 5]
        );
        // 🦀 is one surrogate pair (2 units); 10 pairs + 1 ascii = 21 units → the
        // pair is never split across a chunk boundary.
        let s = format!("{}x", "🦀".repeat(10));
        let chunks = plan_text_chunks(&s);
        assert_eq!(chunks.iter().map(Vec::len).collect::<Vec<_>>(), vec![20, 1]);
        for c in &chunks {
            assert!(
                !matches!(c.last(), Some(u) if (0xD800..0xDC00).contains(u)),
                "chunk ends on a high surrogate"
            );
        }
        let (ev, status) =
            plan_action(&spec(r#"{"verb":"type_text","text":"héllo 🦀"}"#), 10, 10).unwrap();
        assert_eq!(status, "typed 7 chars");
        assert_eq!(ev.len(), 1);
    }

    #[test]
    fn key_combo_orders_modifiers_and_honors_implicit_shift() {
        // super+s → command down, s down/up under ⌘, command up.
        let ev = plan_key_combo("super+s").unwrap();
        assert_eq!(
            ev,
            vec![
                PlannedInput::Key {
                    keycode: KC_COMMAND,
                    down: true,
                    flags: FLAG_COMMAND
                },
                PlannedInput::Key {
                    keycode: 0x01,
                    down: true,
                    flags: FLAG_COMMAND
                },
                PlannedInput::Key {
                    keycode: 0x01,
                    down: false,
                    flags: FLAG_COMMAND
                },
                PlannedInput::Key {
                    keycode: KC_COMMAND,
                    down: false,
                    flags: 0
                },
            ]
        );
        // ctrl+A: the uppercase key needs Shift — an implicit Shift press is added.
        let ev = plan_key_combo("ctrl+A").unwrap();
        let downs: Vec<u16> = ev
            .iter()
            .filter_map(|e| match e {
                PlannedInput::Key {
                    keycode,
                    down: true,
                    ..
                } => Some(*keycode),
                _ => None,
            })
            .collect();
        assert_eq!(downs, vec![KC_CONTROL, KC_SHIFT, 0x00]);
        // explicit shift is not doubled
        let ev = plan_key_combo("shift+a").unwrap();
        assert_eq!(
            ev.iter()
                .filter(|e| matches!(
                    e,
                    PlannedInput::Key {
                        keycode: KC_SHIFT,
                        down: true,
                        ..
                    }
                ))
                .count(),
            1
        );
        assert!(plan_key_combo("").is_err());
        assert!(plan_key_combo("hyper+s").is_err());
        assert!(plan_key_combo("ctrl+insert").is_err()); // unmappable key
    }

    #[test]
    fn targets_validate_like_the_x11_backend() {
        // out-of-range point_px
        assert!(plan_action(
            &spec(r#"{"verb":"move","target":{"kind":"point_px","x":100,"y":5}}"#),
            100,
            100
        )
        .is_err());
        // point_norm out of [0,1]
        assert!(plan_action(
            &spec(r#"{"verb":"move","target":{"kind":"point_norm","x":1.5,"y":0.5}}"#),
            100,
            100
        )
        .is_err());
        // element_ref needs the (unbuilt) observation engine
        assert!(plan_action(
            &spec(r#"{"verb":"click","target":{"kind":"element_ref","ref":"e1"}}"#),
            100,
            100
        )
        .is_err());
        // screenshot/wait never reach the planner on the live path
        assert!(plan_action(&spec(r#"{"verb":"screenshot"}"#), 100, 100).is_err());
        assert!(plan_action(&spec(r#"{"verb":"wait","ms":5}"#), 100, 100).is_err());
        assert!(plan_action(&spec(r#"{"verb":"bogus"}"#), 100, 100).is_err());
    }

    // ---- capture conversion (TCC-free: synthesized CGImage, no screen read) ----

    #[test]
    fn cgimage_to_rgb_preserves_colors_and_row_order() {
        // Draw a 1x2 image: TOP pixel red, BOTTOM pixel blue. CG drawing coords
        // are bottom-left-origin while bitmap rows are stored top-down — this
        // asserts the blit lands top-row-first RGB, the exact contract the shared
        // encode pipeline expects from capture_raw.
        let cs = CGColorSpace::create_device_rgb();
        let ctx =
            CGContext::create_bitmap_context(None, 1, 2, 8, 0, &cs, kCGImageAlphaNoneSkipLast);
        ctx.set_rgb_fill_color(1.0, 0.0, 0.0, 1.0); // red → upper half (CG y ∈ [1,2))
        ctx.fill_rect(CGRect::new(&CGPoint::new(0.0, 1.0), &CGSize::new(1.0, 1.0)));
        ctx.set_rgb_fill_color(0.0, 0.0, 1.0, 1.0); // blue → lower half
        ctx.fill_rect(CGRect::new(&CGPoint::new(0.0, 0.0), &CGSize::new(1.0, 1.0)));
        let image = ctx.create_image().expect("bitmap context yields an image");
        let (rgb, w, h) = cgimage_to_rgb(&image).unwrap();
        assert_eq!((w, h), (1, 2));
        assert_eq!(
            rgb,
            vec![255, 0, 0, 0, 0, 255],
            "top row red, bottom row blue"
        );
    }

    // ---- live-host probes (no TCC prompt; pure reads) ----

    #[test]
    fn live_geometry_is_plausible_when_a_display_exists() {
        if let Ok(g) = live_geometry() {
            assert!(g.px_w >= 640 && g.px_h >= 480, "{g:?}");
            assert!(g.pt_per_px_x > 0.0 && g.pt_per_px_x <= 1.0, "{g:?}");
            assert!(g.pt_per_px_y > 0.0 && g.pt_per_px_y <= 1.0, "{g:?}");
        } // headless build hosts have no display — nothing to assert
    }

    #[test]
    fn readiness_reports_permissions_pending_honestly() {
        if let Ok(ex) = MacExecutor::new() {
            let r = ex.readiness();
            assert!(r.x11_up, "a constructed MacExecutor implies a display");
            assert_eq!(r.root_nonblack, None);
            let pending = r.permissions_pending.expect("macOS always reports it");
            // ready ⇔ display + screen-recording grant; pending covers input too.
            if r.ready {
                assert!(screen_capture_granted(), "ready requires the screen grant");
            } else {
                assert!(pending, "not ready on a live display ⇒ pending grants");
            }
        }
    }
}
