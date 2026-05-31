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

use crate::executor::{ActionSpec, Executor, Target};

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
    pyautogui.scroll(int(float(args[0])))
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

fn point_px(action: &ActionSpec) -> Result<(f64, f64)> {
    match &action.target {
        Some(Target::PointPx { x, y }) => Ok((*x, *y)),
        Some(Target::PointNorm { .. }) => {
            bail!("pyautogui backend needs a point_px target (normalize point_norm first)")
        }
        _ => bail!("{} requires a point_px target", action.verb),
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
            let dy = action.dy.context("scroll requires dy")?;
            argv.push(dy.to_string());
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
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn spec(value: serde_json::Value) -> ActionSpec {
        serde_json::from_value(value).expect("valid ActionSpec")
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

        let s = build_argv("py", &spec(json!({"verb": "scroll", "dy": -3}))).unwrap();
        assert_eq!(&s[3..], &["scroll".to_string(), "-3".to_string()]);

        let w = build_argv("py", &spec(json!({"verb": "wait", "ms": 500}))).unwrap();
        assert_eq!(&w[3..], &["wait".to_string(), "0.5".to_string()]);
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
