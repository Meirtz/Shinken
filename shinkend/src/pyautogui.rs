//! Optional PyAutoGUI execution backend (#59).
//!
//! A synthetic GUI backend that drives input through PyAutoGUI, invoked as a subprocess.
//! Typed ACI verbs map to a **fixed** helper program with parameters passed as `argv`
//! (never interpolated into code), so the backend executes only the constrained verb set
//! — it never runs arbitrary Python from the wire. Selected via
//! `SHINKEND_EXECUTOR=pyautogui`; the Python interpreter is `$SHINKEND_PYTHON` (default
//! `python3`). It fails clearly when Python or PyAutoGUI is unavailable.

use std::process::Command;

use anyhow::{bail, Context, Result};

use crate::executor::{ActionSpec, Executor, ExecutorCapabilityProfile, Target};

/// The fixed helper: reads the verb + parameters from `argv` and calls the matching
/// PyAutoGUI function. There is no `eval`/`exec` — only this closed dispatch — so wire
/// input can never become executable code.
const HELPER: &str = r#"import sys, time, pyautogui
pyautogui.FAILSAFE = False
verb, args = sys.argv[1], sys.argv[2:]
if verb == "move":
    pyautogui.moveTo(float(args[0]), float(args[1]))
elif verb == "click":
    pyautogui.click(float(args[0]), float(args[1]))
elif verb == "double_click":
    pyautogui.doubleClick(float(args[0]), float(args[1]))
elif verb == "right_click":
    pyautogui.click(float(args[0]), float(args[1]), button="right")
elif verb == "scroll":
    # args: vertical wheel clicks, horizontal wheel clicks (already sign-corrected
    # for pyautogui by the Rust side, so this backend matches the X11 backend).
    v = int(args[0])
    if v:
        pyautogui.scroll(v)
    h = int(args[1]) if len(args) > 1 else 0
    if h and hasattr(pyautogui, "hscroll"):
        pyautogui.hscroll(h)
elif verb == "drag":
    # args: from_x, from_y, to_x, to_y, duration_secs, button
    pyautogui.moveTo(float(args[0]), float(args[1]))
    pyautogui.dragTo(float(args[2]), float(args[3]), duration=float(args[4]), button=args[5])
elif verb in ("mouse_down", "mouse_up"):
    # args: button, then either "-" (act at the current position) or x, y
    if args[1] != "-":
        pyautogui.moveTo(float(args[1]), float(args[2]))
    (pyautogui.mouseDown if verb == "mouse_down" else pyautogui.mouseUp)(button=args[0])
elif verb == "type_text":
    pyautogui.typewrite(args[0])
elif verb == "key":
    pyautogui.hotkey(*args[0].split("+"))
elif verb == "wait":
    time.sleep(float(args[0]))
else:
    sys.exit("unsupported verb: " + verb)
"#;

/// A PyAutoGUI-backed [`Executor`]. Holds the resolved Python interpreter.
pub struct PyAutoGuiExecutor {
    python: String,
}

const PYAUTOGUI_VERBS: &[&str] = &[
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
];

impl PyAutoGuiExecutor {
    /// Resolve the interpreter and verify PyAutoGUI imports, failing clearly otherwise.
    pub fn new() -> Result<Self> {
        let python = std::env::var("SHINKEND_PYTHON").unwrap_or_else(|_| "python3".to_string());
        let ok = Command::new(&python)
            .args(["-c", "import pyautogui"])
            .status()
            .map(|s| s.success())
            .unwrap_or(false);
        if !ok {
            bail!(
                "pyautogui backend unavailable: `{python} -c \"import pyautogui\"` failed \
                 (install Python 3 + pyautogui in the guest, or set SHINKEND_PYTHON)"
            );
        }
        Ok(Self { python })
    }
}

/// Pixel delta → signed, bounded wheel-click count (matches the X11 backend's
/// ~100 px/click step and 1..=20 clamp; 0 px → 0 clicks, i.e. no scroll on that axis).
fn scroll_clicks(px: f64) -> i32 {
    if px == 0.0 {
        return 0;
    }
    let steps = ((px.abs() / 100.0).ceil() as i32).clamp(1, 20);
    if px > 0.0 {
        steps
    } else {
        -steps
    }
}

fn point_px(action: &ActionSpec) -> Result<(f64, f64)> {
    match &action.target {
        Some(Target::PointPx { x, y }) => Ok((*x, *y)),
        Some(Target::PointNorm { .. }) => {
            bail!("pyautogui backend needs a point_px target (normalize point_norm first)")
        }
        _ => bail!("{} requires a point_px target", action.verb),
    }
}

/// Validate a wire `button` name into the pyautogui button string (absent = left).
fn button_name(name: Option<&str>) -> Result<&'static str> {
    match name {
        None | Some("left") => Ok("left"),
        Some("middle") => Ok("middle"),
        Some("right") => Ok("right"),
        Some(other) => bail!("unknown pointer button: {other:?} (expected left, middle, or right)"),
    }
}

/// Build the subprocess argv for an action — the command-construction unit, testable
/// without a live desktop. Parameters are discrete `argv` entries, so no value is ever
/// interpreted as code. Rejects verbs outside the supported synthetic-input subset.
pub fn build_argv(python: &str, action: &ActionSpec) -> Result<Vec<String>> {
    let mut argv = vec![
        python.to_string(),
        "-c".to_string(),
        HELPER.to_string(),
        action.verb.clone(),
    ];
    match action.verb.as_str() {
        "move" | "click" | "double_click" | "right_click" => {
            let (x, y) = point_px(action)?;
            argv.push(x.to_string());
            argv.push(y.to_string());
        }
        "scroll" => {
            let dx = action.dx.unwrap_or(0.0);
            let dy = action.dy.unwrap_or(0.0);
            if dx == 0.0 && dy == 0.0 {
                bail!("scroll requires a nonzero dx or dy");
            }
            // Wire dx/dy are pixels with +dy = down (ACI convention). pyautogui.scroll's
            // positive direction is UP, so negate the vertical clicks; hscroll's positive
            // is right, matching +dx. Magnitude tracks the X11 backend (~100 px / click).
            argv.push((-scroll_clicks(dy)).to_string());
            argv.push(scroll_clicks(dx).to_string());
        }
        "drag" => {
            let (x, y) = point_px(action)?;
            let (tx, ty) = match &action.to {
                Some(Target::PointPx { x, y }) => (*x, *y),
                Some(Target::PointNorm { .. }) => {
                    bail!("pyautogui backend needs a point_px `to` target")
                }
                _ => bail!("drag requires a point_px `to` target"),
            };
            let button = button_name(action.button.as_deref())?;
            let secs = action
                .duration_ms
                .unwrap_or(0)
                .min(crate::executor::MAX_DRAG_MS) as f64
                / 1000.0;
            argv.extend([
                x.to_string(),
                y.to_string(),
                tx.to_string(),
                ty.to_string(),
                secs.to_string(),
                button.to_string(),
            ]);
        }
        "mouse_down" | "mouse_up" => {
            argv.push(button_name(action.button.as_deref())?.to_string());
            // Optional target: "-" means act at the current pointer position.
            if action.target.is_some() {
                let (x, y) = point_px(action)?;
                argv.push(x.to_string());
                argv.push(y.to_string());
            } else {
                argv.push("-".to_string());
            }
        }
        "type_text" => {
            argv.push(action.text.clone().context("type_text requires text")?);
        }
        "key" => {
            argv.push(action.keys.clone().context("key requires keys")?);
        }
        "wait" => {
            let secs = action.ms.unwrap_or(0) as f64 / 1000.0;
            argv.push(secs.to_string());
        }
        other => bail!("pyautogui backend does not support verb {other:?}"),
    }
    Ok(argv)
}

impl Executor for PyAutoGuiExecutor {
    fn execute(&self, action: &ActionSpec) -> Result<String> {
        let argv = build_argv(&self.python, action)?;
        let status = Command::new(&argv[0])
            .args(&argv[1..])
            .status()
            .with_context(|| format!("spawning pyautogui helper via {}", self.python))?;
        if !status.success() {
            bail!(
                "pyautogui {} failed (exit {:?})",
                action.verb,
                status.code()
            );
        }
        Ok(format!("pyautogui:{}", action.verb))
    }

    fn backend(&self) -> &'static str {
        "pyautogui"
    }

    fn capability_profile(&self) -> ExecutorCapabilityProfile {
        ExecutorCapabilityProfile {
            verbs: PYAUTOGUI_VERBS,
            targets: &["point_px"],
            observation_types: &[],
            image_formats: &[],
            observe_after_act: false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn spec(value: serde_json::Value) -> ActionSpec {
        serde_json::from_value(value).expect("valid ActionSpec")
    }

    #[test]
    fn capability_profile_does_not_claim_capture_or_structured_surfaces() {
        let exec = PyAutoGuiExecutor {
            python: "python3".to_string(),
        };
        let profile = exec.capability_profile();
        assert!(profile.verbs.contains(&"click"));
        assert!(!profile.verbs.contains(&"screenshot"));
        assert_eq!(profile.targets, &["point_px"]);
        assert!(profile.observation_types.is_empty());
        assert!(profile.image_formats.is_empty());
        assert!(!profile.observe_after_act);
    }

    #[test]
    fn builds_pointer_argv_from_point_px() {
        let argv = build_argv(
            "python3",
            &spec(json!({"verb": "click", "target": {"kind": "point_px", "x": 100, "y": 200}})),
        )
        .unwrap();
        assert_eq!(argv[0], "python3");
        assert_eq!(argv[1], "-c");
        assert_eq!(argv[3], "click");
        assert_eq!(&argv[4..], &["100".to_string(), "200".to_string()]);
        // params are discrete argv entries — the helper program is fixed, never the input
        assert!(argv[2].contains("pyautogui.click"));
    }

    #[test]
    fn builds_type_key_scroll_wait_argv() {
        let t = build_argv(
            "py",
            &spec(json!({"verb": "type_text", "text": "hi there"})),
        )
        .unwrap();
        assert_eq!(&t[3..], &["type_text".to_string(), "hi there".to_string()]);

        let k = build_argv("py", &spec(json!({"verb": "key", "keys": "ctrl+s"}))).unwrap();
        assert_eq!(&k[3..], &["key".to_string(), "ctrl+s".to_string()]);

        // dy=-300 (up, 3 clicks). pyautogui.scroll is +=up, so the vertical arg is +3;
        // horizontal arg is 0. This matches the X11 backend's direction + magnitude.
        let s = build_argv("py", &spec(json!({"verb": "scroll", "dy": -300}))).unwrap();
        assert_eq!(
            &s[3..],
            &["scroll".to_string(), "3".to_string(), "0".to_string()]
        );
        // +dy = down → negative pyautogui clicks; +dx = right → positive horizontal arg.
        let sd = build_argv("py", &spec(json!({"verb": "scroll", "dy": 100, "dx": 200}))).unwrap();
        assert_eq!(
            &sd[3..],
            &["scroll".to_string(), "-1".to_string(), "2".to_string()]
        );

        let w = build_argv("py", &spec(json!({"verb": "wait", "ms": 500}))).unwrap();
        assert_eq!(&w[3..], &["wait".to_string(), "0.5".to_string()]);
    }

    #[test]
    fn builds_drag_and_mouse_button_argv() {
        let d = build_argv(
            "py",
            &spec(json!({
                "verb": "drag",
                "target": {"kind": "point_px", "x": 10, "y": 20},
                "to": {"kind": "point_px", "x": 300, "y": 200},
                "duration_ms": 250,
                "button": "left"
            })),
        )
        .unwrap();
        assert_eq!(
            &d[3..],
            &[
                "drag".to_string(),
                "10".to_string(),
                "20".to_string(),
                "300".to_string(),
                "200".to_string(),
                "0.25".to_string(), // duration_ms → seconds for pyautogui.dragTo
                "left".to_string(),
            ]
        );
        assert!(d[2].contains("pyautogui.dragTo"));

        // mouse_down with a target moves first; mouse_up without one acts in place ("-")
        let down = build_argv(
            "py",
            &spec(json!({
                "verb": "mouse_down",
                "target": {"kind": "point_px", "x": 5, "y": 6},
                "button": "middle"
            })),
        )
        .unwrap();
        assert_eq!(
            &down[3..],
            &[
                "mouse_down".to_string(),
                "middle".to_string(),
                "5".to_string(),
                "6".to_string(),
            ]
        );
        let up = build_argv("py", &spec(json!({"verb": "mouse_up"}))).unwrap();
        assert_eq!(
            &up[3..],
            &["mouse_up".to_string(), "left".to_string(), "-".to_string()]
        );

        // missing `to` / unknown button are rejected before any subprocess spawns
        assert!(build_argv(
            "py",
            &spec(json!({"verb": "drag", "target": {"kind": "point_px", "x": 1, "y": 2}}))
        )
        .is_err());
        assert!(build_argv(
            "py",
            &spec(json!({"verb": "mouse_down", "button": "wheel"}))
        )
        .is_err());
    }

    #[test]
    fn rejects_unsupported_verb_and_missing_params() {
        assert!(build_argv("py", &spec(json!({"verb": "teleport"}))).is_err());
        assert!(build_argv("py", &spec(json!({"verb": "type_text"}))).is_err()); // no text
        assert!(build_argv("py", &spec(json!({"verb": "click"}))).is_err()); // no target
                                                                             // point_norm is rejected with a clear message (must be normalized to px first)
        assert!(build_argv(
            "py",
            &spec(json!({"verb": "click", "target": {"kind": "point_norm", "x": 0.5, "y": 0.5}}))
        )
        .is_err());
    }
}
