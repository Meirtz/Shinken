//! Action execution — a backend-pluggable executor behind the typed ACI action.
//!
//! M1a implements **pointer** actions (move/click/double_click/right_click/scroll) on
//! `point_px`/`point_norm` targets via the X11 XTEST extension. `type_text`/`key`
//! (keysym mapping), `element_ref` resolution, and `screenshot` arrive in M1b. The
//! router prefers semantic actuation later (CDP/AT-SPI); X11/XTEST is the P0 baseline
//! for the Linux fork tier we control (see docs/11-aci-spec.md §3.1).

use std::sync::Mutex;

use anyhow::{bail, Context, Result};
use serde::Deserialize;
use x11rb::connection::Connection;
use x11rb::protocol::xproto::{
    Window, BUTTON_PRESS_EVENT, BUTTON_RELEASE_EVENT, MOTION_NOTIFY_EVENT,
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
#[derive(Debug, Clone, Deserialize)]
// `text`/`keys`/`dx`/`ms` are part of the ACI v0 wire contract; first read in M1b.
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
}

/// Executes typed actions against a guest. Backends are swappable per OS.
pub trait Executor: Send + Sync {
    /// Execute one action; returns a short status string or an error.
    fn execute(&self, action: &ActionSpec) -> Result<String>;
    /// A human label for the active backend.
    fn backend(&self) -> &'static str;
}

/// Pick the best available executor: X11 if a display is reachable, else virtual.
pub fn default_executor() -> std::sync::Arc<dyn Executor> {
    match X11Executor::connect() {
        Ok(x) => {
            eprintln!(
                "shinkend: action backend = x11/xtest ({}x{})",
                x.width, x.height
            );
            std::sync::Arc::new(x)
        }
        Err(e) => {
            eprintln!("shinkend: no X11 display ({e}); action backend = virtual (no-op)");
            std::sync::Arc::new(VirtualExecutor::default())
        }
    }
}

// ---- pointer button numbers (X11) ----
const BTN_LEFT: u8 = 1;
const BTN_RIGHT: u8 = 3;
const BTN_SCROLL_UP: u8 = 4;
const BTN_SCROLL_DOWN: u8 = 5;

/// X11 backend: synthetic pointer input via the XTEST extension.
pub struct X11Executor {
    conn: Mutex<x11rb::rust_connection::RustConnection>,
    root: Window,
    width: u16,
    height: u16,
}

impl X11Executor {
    pub fn connect() -> Result<Self> {
        let (conn, screen_num) = x11rb::connect(None).context("connect to X display")?;
        let screen = &conn.setup().roots[screen_num];
        let root = screen.root;
        let (width, height) = (screen.width_in_pixels, screen.height_in_pixels);
        Ok(Self {
            conn: Mutex::new(conn),
            root,
            width,
            height,
        })
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
            Target::PointPx { x, y } => Ok((*x as i16, *y as i16)),
            Target::PointNorm { x, y } => Ok((
                (x * self.width as f64) as i16,
                (y * self.height as f64) as i16,
            )),
            Target::ElementRef { .. } => {
                bail!("element_ref resolution needs the observation engine (M1b, #4)")
            }
        }
    }
}

impl Executor for X11Executor {
    fn backend(&self) -> &'static str {
        "x11/xtest"
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
                let dy = a.dy.unwrap_or(0.0);
                let button = if dy >= 0.0 {
                    BTN_SCROLL_DOWN
                } else {
                    BTN_SCROLL_UP
                };
                let steps = ((dy.abs() / 100.0).ceil() as u32).clamp(1, 20);
                for _ in 0..steps {
                    self.click_button(button, x, y)?;
                }
                Ok(format!("scrolled {steps} step(s)"))
            }
            // M1b: keysym mapping + IME.
            "type_text" | "key" => bail!("{} lands in M1b (keysym mapping)", a.verb),
            // M1b: capture engine.
            "screenshot" => bail!("screenshot lands in M1b (observation engine)"),
            // We discourage fixed sleeps (prefer readiness probes, D7) — ack as a no-op.
            "wait" => Ok("wait acknowledged (prefer readiness probes over fixed sleeps)".into()),
            other => bail!("unknown verb: {other}"),
        }
    }
}

/// No-op backend that records executed verbs — used when no display is available
/// and in tests.
#[derive(Default)]
pub struct VirtualExecutor {
    pub log: Mutex<Vec<String>>,
}

impl Executor for VirtualExecutor {
    fn backend(&self) -> &'static str {
        "virtual"
    }

    fn execute(&self, a: &ActionSpec) -> Result<String> {
        self.log.lock().expect("log lock").push(a.verb.clone());
        Ok(format!("virtual: {}", a.verb))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec(json: &str) -> ActionSpec {
        serde_json::from_str(json).unwrap()
    }

    #[test]
    fn parses_point_px_target() {
        let a = spec(r#"{"verb":"click","target":{"kind":"point_px","x":10,"y":20}}"#);
        assert_eq!(a.verb, "click");
        assert!(matches!(a.target, Some(Target::PointPx { .. })));
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
}
