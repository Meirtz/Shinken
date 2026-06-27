//! Typed in-guest exec channel — the `exec` ACI verb (G1).
//!
//! Runs a command **inside the Sandbox** as a child of `shinkend`, so setup/verify
//! work that today leaks through out-of-band substrate channels (`docker exec`, the
//! inject transport) flows over the SAME WebSocket as every other action — and
//! therefore works on any substrate a `shinkend` runs on. Two wire forms:
//!
//! - **buffered** (default): the action is answered with one `result` whose value is
//!   the typed `$defs.ExecResult` — exit code/signal, capped stdout/stderr (honest
//!   truncation flags), `timed_out`, `duration_ms`.
//! - **streamed** (`stream: true`): ack first, then incremental `exec_output` events
//!   (monotonic `seq` across both channels; raw-byte binary frames on a
//!   binary-negotiated session) and one terminal `exec_exit`.
//!
//! Execution discipline: `argv` is the default form (no shell interpretation);
//! `shell` is the explicit opt-in (`/bin/sh -c`). The child gets its own process
//! group, and the timeout kills the **group**, so a `sh -c 'sleep 100 & wait'` can't
//! orphan a runaway grandchild. `kill_on_drop` reaps the child even if the task is
//! aborted mid-run (connection drop). Concurrency is bounded by a small semaphore in
//! the serve loop ([`MAX_EXECS`]). PTY allocation is a designed follow-up (the wire
//! field `pty` is reserved and rejected); v1 children run pipe-connected.

use std::process::Stdio;
use std::time::{Duration, Instant};

use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};
use tokio::process::{Child, Command};
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message as WsMessage;

use crate::executor::ActionSpec;
use crate::protocol;

/// Maximum concurrently running execs per runtime (serve-loop semaphore).
pub const MAX_EXECS: usize = 4;
/// Default deadline when the action carries no `timeout_ms`.
pub const DEFAULT_TIMEOUT_MS: u64 = 60_000;
/// Hard cap a valid requested `timeout_ms` is clamped to.
pub const MAX_TIMEOUT_MS: u64 = 600_000;
/// Per-channel capture budget of the buffered form; beyond it the channel keeps
/// draining (no pipe deadlock) but stops storing, and the truncation flag is set.
pub const BUFFERED_CAP: usize = 256 * 1024;
/// Per-`exec_output` chunk ceiling of the streamed form.
pub const STREAM_CHUNK: usize = 64 * 1024;
/// Total forwarded-bytes budget of one streamed exec (both channels); beyond it
/// chunks are dropped (drained, not forwarded) and `exec_exit.truncated` is set.
pub const STREAM_BUDGET: usize = 8 * 1024 * 1024;
/// How long after reaping the child the runtime keeps reading its pipes. The group
/// kill closes them in practice; this bounds the pathological case (a `setsid`
/// daemon that escaped the group and still holds the write end).
const PIPE_DRAIN_GRACE: Duration = Duration::from_millis(500);

/// The command form: argv (default, no shell) or an explicit shell line.
#[derive(Debug, Clone, PartialEq)]
pub enum ExecCommand {
    Argv(Vec<String>),
    Shell(String),
}

/// A validated exec request — what the [`crate::connection::Session`] hands the
/// serve loop to run (the Session stays a pure state machine; the child runs in an
/// async task feeding the connection's writer channel).
#[derive(Debug, Clone, PartialEq)]
pub struct ExecSpec {
    /// The action's call_id: correlates the result / `exec_output` events (`cause`).
    pub call_id: String,
    pub command: ExecCommand,
    pub cwd: Option<String>,
    pub env: Vec<(String, String)>,
    /// Kill-the-process-group deadline (clamped to [`MAX_TIMEOUT_MS`]).
    pub timeout: Duration,
    /// Text written to the child's stdin (then closed); None = stdin from /dev/null.
    pub stdin: Option<String>,
    /// Streamed form (`exec_output` events + `exec_exit`) vs buffered `result`.
    pub stream: bool,
    /// Session-negotiated binary framing: streamed chunks ride binary WS frames.
    pub binary: bool,
}

impl ExecSpec {
    /// Validate an `exec` action into a runnable spec. `Err` is the nack message —
    /// validation runs at dispatch, BEFORE anything spawns.
    pub fn from_action(call_id: &str, spec: &ActionSpec, binary: bool) -> Result<Self, String> {
        if spec.pty == Some(true) {
            return Err(
                "pty is reserved (not supported in v0): exec runs pipe-connected; \
                 PTY allocation is the designed follow-up"
                    .to_string(),
            );
        }
        let command = match (&spec.argv, &spec.shell) {
            (Some(argv), None) => {
                if argv.is_empty() {
                    return Err("exec argv must not be empty".to_string());
                }
                ExecCommand::Argv(argv.clone())
            }
            (None, Some(shell)) => ExecCommand::Shell(shell.clone()),
            (Some(_), Some(_)) => {
                return Err(
                    "exec takes exactly one of `argv` or `shell`, not both (argv is the \
                     default form; shell is the explicit opt-in)"
                        .to_string(),
                )
            }
            (None, None) => {
                return Err(
                    "exec requires `argv` (default form) or `shell` (explicit opt-in)".to_string(),
                )
            }
        };
        if spec.timeout_ms == Some(0) {
            return Err("exec timeout_ms must be at least 1".to_string());
        }
        let timeout_ms = spec
            .timeout_ms
            .unwrap_or(DEFAULT_TIMEOUT_MS)
            .min(MAX_TIMEOUT_MS);
        let env = spec
            .env
            .as_ref()
            .map(|m| m.iter().map(|(k, v)| (k.clone(), v.clone())).collect())
            .unwrap_or_default();
        Ok(ExecSpec {
            call_id: call_id.to_string(),
            command,
            cwd: spec.cwd.clone(),
            env,
            timeout: Duration::from_millis(timeout_ms),
            stdin: spec.stdin.clone(),
            stream: spec.stream.unwrap_or(false),
            binary,
        })
    }
}

/// The reaped run, shaped for the wire (`$defs.ExecResult` / `exec_exit`).
#[derive(Debug, Clone, Default)]
pub struct ExecOutcome {
    pub exit_code: Option<i32>,
    pub signal: Option<i32>,
    pub timed_out: bool,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    pub stdout_truncated: bool,
    pub stderr_truncated: bool,
    pub duration_ms: f64,
}

/// The direct child's reaped status plus the terminal decision made at the deadline.
///
/// A process can exit naturally in the narrow window after the deadline fires but
/// before SIGKILL reaches its process group. Once the timeout branch has won, exposing
/// that later `0` as a successful exit is contradictory (`timed_out=true, exit_code=0`)
/// and lets callers mistake an over-deadline run for success. Keep the raw status for
/// the actual signal, but suppress exit codes for every timed-out terminal outcome.
struct WaitOutcome {
    status: std::process::ExitStatus,
    timed_out: bool,
}

impl WaitOutcome {
    fn exit_code(&self) -> Option<i32> {
        (!self.timed_out).then(|| self.status.code()).flatten()
    }

    fn signal(&self) -> Option<i32> {
        status_signal(&self.status)
    }
}

fn build_command(spec: &ExecSpec) -> Command {
    let mut cmd = match &spec.command {
        ExecCommand::Argv(argv) => {
            let mut c = Command::new(&argv[0]);
            c.args(&argv[1..]);
            c
        }
        ExecCommand::Shell(line) => {
            let mut c = Command::new("/bin/sh");
            c.arg("-c").arg(line);
            c
        }
    };
    if let Some(cwd) = &spec.cwd {
        cmd.current_dir(cwd);
    }
    for (k, v) in &spec.env {
        cmd.env(k, v);
    }
    cmd.stdin(if spec.stdin.is_some() {
        Stdio::piped()
    } else {
        Stdio::null()
    });
    cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
    // Reap even when the owning task is aborted (connection drop): tokio kills the
    // child on Child drop and reaps it off the runtime.
    cmd.kill_on_drop(true);
    // Own process group, so the timeout kill reaches the whole tree (`sh -c` children,
    // grandchildren) — not just the immediate child.
    #[cfg(unix)]
    cmd.process_group(0);
    cmd
}

/// SIGKILL the child's whole process group (it IS the group leader: spawned with
/// `process_group(0)`). `process_group` is captured immediately after spawn: by the
/// time a timeout/client disconnect is observed the direct child may already have
/// been reaped while a background descendant still owns the group's pipes. Falls
/// back to killing the immediate child where group kill is unavailable or gone.
fn kill_tree(child: &mut Child, process_group: Option<u32>) {
    #[cfg(unix)]
    if let Some(pgid) = process_group {
        // Safety: plain libc call; an ESRCH/EPERM error is handled by the fallback.
        unsafe {
            libc::killpg(pgid as libc::pid_t, libc::SIGKILL);
        }
    }
    #[cfg(not(unix))]
    let _ = process_group;
    let _ = child.start_kill();
}

#[cfg(unix)]
fn status_signal(status: &std::process::ExitStatus) -> Option<i32> {
    use std::os::unix::process::ExitStatusExt;
    status.signal()
}

#[cfg(not(unix))]
fn status_signal(_status: &std::process::ExitStatus) -> Option<i32> {
    None
}

/// Write `text` to the child's stdin and close it — as a task, so a child that never
/// reads can't park the exec flow on a full pipe.
fn feed_stdin(child: &mut Child, text: Option<String>) {
    if let (Some(text), Some(mut sin)) = (text, child.stdin.take()) {
        tokio::spawn(async move {
            let _ = sin.write_all(text.as_bytes()).await;
            // dropping `sin` closes the pipe → the child sees EOF
        });
    }
}

/// Await the child within `timeout`; on expiry kill the process group and reap.
///
/// The timeout branch gets one final non-blocking reap before it commits: if the
/// status is already observable, natural completion won the race. Otherwise timeout
/// wins, the whole originally-captured process group is killed, and a later natural
/// direct-child status never gets exposed as a successful exit code.
async fn wait_with_deadline(
    child: &mut Child,
    timeout: Duration,
    process_group: Option<u32>,
) -> Result<WaitOutcome, String> {
    match tokio::time::timeout(timeout, child.wait()).await {
        Ok(res) => Ok(WaitOutcome {
            status: res.map_err(|e| format!("exec_wait_failed: {e}"))?,
            timed_out: false,
        }),
        Err(_) => {
            if let Some(status) = child
                .try_wait()
                .map_err(|e| format!("exec_wait_failed: {e}"))?
            {
                return Ok(WaitOutcome {
                    status,
                    timed_out: false,
                });
            }
            kill_tree(child, process_group);
            let status = child
                .wait()
                .await
                .map_err(|e| format!("exec_wait_failed: {e}"))?;
            Ok(WaitOutcome {
                status,
                timed_out: true,
            })
        }
    }
}

/// Read a pipe to EOF, keeping only the first `cap` bytes (the rest is drained and
/// counted as truncation — the child must never block on a full pipe).
async fn read_capped(pipe: Option<impl AsyncRead + Unpin>, cap: usize) -> (Vec<u8>, bool) {
    let Some(mut pipe) = pipe else {
        return (Vec::new(), false);
    };
    let mut kept = Vec::new();
    let mut truncated = false;
    let mut buf = vec![0u8; STREAM_CHUNK];
    loop {
        match pipe.read(&mut buf).await {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                if kept.len() < cap {
                    let take = n.min(cap - kept.len());
                    kept.extend_from_slice(&buf[..take]);
                    if take < n {
                        truncated = true;
                    }
                } else {
                    truncated = true;
                }
            }
        }
    }
    (kept, truncated)
}

/// Join a capture task within the pipe-drain grace; an overrun (a daemonized
/// grandchild holding the pipe open) abandons the read and reports truncation —
/// honest loss, never a wedged connection.
async fn drain_capture(task: tokio::task::JoinHandle<(Vec<u8>, bool)>) -> (Vec<u8>, bool, bool) {
    let mut task = task;
    match tokio::time::timeout(PIPE_DRAIN_GRACE, &mut task).await {
        Ok(Ok((bytes, truncated))) => (bytes, truncated, false),
        Ok(Err(_)) => (Vec::new(), true, false),
        Err(_) => {
            task.abort();
            (Vec::new(), true, true)
        }
    }
}

/// Run one BUFFERED exec to completion. `Err` carries the typed failure message
/// (spawn/wait), which the caller answers as an error `result`.
pub async fn run_buffered(spec: ExecSpec) -> Result<ExecOutcome, String> {
    let started = Instant::now();
    let mut child = build_command(&spec)
        .spawn()
        .map_err(|e| format!("exec_spawn_failed: {e}"))?;
    let process_group = child.id();
    feed_stdin(&mut child, spec.stdin.clone());
    let out_task = tokio::spawn(read_capped(child.stdout.take(), BUFFERED_CAP));
    let err_task = tokio::spawn(read_capped(child.stderr.take(), BUFFERED_CAP));
    let waited = wait_with_deadline(&mut child, spec.timeout, process_group).await?;
    let (out_capture, err_capture) = tokio::join!(drain_capture(out_task), drain_capture(err_task));
    let (stdout, stdout_truncated, stdout_stalled) = out_capture;
    let (stderr, stderr_truncated, stderr_stalled) = err_capture;
    if stdout_stalled || stderr_stalled {
        // The direct child can exit after launching a descendant that still owns its
        // stdout/stderr. Never leave that process behind merely because the leader was
        // already reaped; the captured PGID still identifies the original exec tree.
        kill_tree(&mut child, process_group);
    }
    Ok(ExecOutcome {
        exit_code: waited.exit_code(),
        signal: waited.signal(),
        timed_out: waited.timed_out,
        stdout,
        stderr,
        stdout_truncated,
        stderr_truncated,
        duration_ms: started.elapsed().as_secs_f64() * 1000.0,
    })
}

/// Pump one pipe into the funnel as ≤[`STREAM_CHUNK`] chunks tagged with their channel.
async fn pump(
    pipe: Option<impl AsyncRead + Unpin>,
    channel: &'static str,
    tx: mpsc::Sender<(&'static str, Vec<u8>)>,
) {
    let Some(mut pipe) = pipe else { return };
    let mut buf = vec![0u8; STREAM_CHUNK];
    loop {
        match pipe.read(&mut buf).await {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                if tx.send((channel, buf[..n].to_vec())).await.is_err() {
                    break; // forwarder gone — stop reading
                }
            }
        }
    }
}

/// Run one STREAMED exec: ack already sent by the dispatcher; this pushes
/// `exec_output` events (seq-ordered across both channels, binary frames on a
/// binary session) and exactly one terminal `exec_exit` into the connection's
/// writer channel. A dead client (writer closed) kills the process group.
pub async fn run_streamed(spec: ExecSpec, out: mpsc::Sender<WsMessage>) {
    let started = Instant::now();
    let call_id = spec.call_id.clone();
    let mut child = match build_command(&spec).spawn() {
        Ok(c) => c,
        Err(e) => {
            // The ack accepted the ACTION; the spawn failure is the run's terminal
            // event, so the client's stream resolves instead of waiting forever.
            let exit = protocol::exec_exit_error(
                &call_id,
                &format!("exec_spawn_failed: {e}"),
                started.elapsed().as_secs_f64() * 1000.0,
            );
            let _ = out.send(WsMessage::Text(exit)).await;
            return;
        }
    };
    let process_group = child.id();
    feed_stdin(&mut child, spec.stdin.clone());
    let (tx, mut rx) = mpsc::channel::<(&'static str, Vec<u8>)>(8);
    let out_pump = tokio::spawn(pump(child.stdout.take(), "stdout", tx.clone()));
    let err_pump = tokio::spawn(pump(child.stderr.take(), "stderr", tx));
    drop(out_pump);
    drop(err_pump);
    // Forwarder: assigns the cross-channel seq and enforces the total budget. Ends
    // when both pumps close the funnel (pipes EOF — the group kill guarantees it).
    let fwd_out = out.clone();
    let binary = spec.binary;
    let fwd_call_id = call_id.clone();
    let forwarder = tokio::spawn(async move {
        let mut seq: u64 = 0;
        let mut sent: usize = 0;
        let mut truncated = false;
        let mut client_gone = false;
        while let Some((channel, data)) = rx.recv().await {
            if truncated || client_gone {
                continue; // keep draining so the child never blocks on a full pipe
            }
            if sent + data.len() > STREAM_BUDGET {
                truncated = true;
                continue;
            }
            sent += data.len();
            let msg = if binary {
                WsMessage::Binary(protocol::binary_exec_output(
                    &fwd_call_id,
                    seq,
                    channel,
                    &data,
                ))
            } else {
                match protocol::exec_output_text(&fwd_call_id, seq, channel, &data) {
                    Some(text) => WsMessage::Text(text),
                    None => continue,
                }
            };
            // Awaited send: exec output is RELIABLE (unlike droppable screencast
            // frames); the bounded writer channel is the backpressure.
            if fwd_out.send(msg).await.is_err() {
                client_gone = true;
                continue;
            }
            seq += 1;
        }
        (truncated, client_gone)
    });
    let waited = wait_with_deadline(&mut child, spec.timeout, process_group).await;
    let mut forwarder = forwarder;
    let (truncated, client_gone, drain_stalled) =
        match tokio::time::timeout(PIPE_DRAIN_GRACE, &mut forwarder).await {
            Ok(Ok((truncated, client_gone))) => (truncated, client_gone, false),
            Ok(Err(_)) => (true, false, false),
            Err(_) => {
                forwarder.abort();
                (true, false, true)
            }
        };
    if client_gone || drain_stalled {
        kill_tree(&mut child, process_group);
        let _ = child.wait().await;
    }
    if client_gone {
        return;
    }
    let duration_ms = started.elapsed().as_secs_f64() * 1000.0;
    let exit = match waited {
        Ok(waited) => protocol::exec_exit_text(
            &call_id,
            waited.exit_code(),
            waited.signal(),
            waited.timed_out,
            duration_ms,
            truncated,
        ),
        Err(e) => protocol::exec_exit_error(&call_id, &e, duration_ms),
    };
    let _ = out.send(WsMessage::Text(exit)).await;
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    fn spec(command: ExecCommand) -> ExecSpec {
        ExecSpec {
            call_id: "x1".into(),
            command,
            cwd: None,
            env: Vec::new(),
            timeout: Duration::from_secs(10),
            stdin: None,
            stream: false,
            binary: false,
        }
    }

    fn action(json: &str) -> ActionSpec {
        serde_json::from_str(json).unwrap()
    }

    // ---- validation (ExecSpec::from_action) ----

    #[test]
    fn from_action_requires_exactly_one_command_form() {
        let both = action(r#"{"verb":"exec","argv":["ls"],"shell":"ls"}"#);
        assert!(ExecSpec::from_action("c", &both, false)
            .unwrap_err()
            .contains("exactly one"));
        let neither = action(r#"{"verb":"exec"}"#);
        assert!(ExecSpec::from_action("c", &neither, false)
            .unwrap_err()
            .contains("requires"));
        let empty = action(r#"{"verb":"exec","argv":[]}"#);
        assert!(ExecSpec::from_action("c", &empty, false)
            .unwrap_err()
            .contains("empty"));
    }

    #[test]
    fn from_action_rejects_pty_and_clamps_timeout() {
        let pty = action(r#"{"verb":"exec","argv":["ls"],"pty":true}"#);
        assert!(ExecSpec::from_action("c", &pty, false)
            .unwrap_err()
            .contains("pty is reserved"));
        // pty:false (the reserved field's only valid value) is accepted
        let ok = action(r#"{"verb":"exec","argv":["ls"],"pty":false}"#);
        assert!(ExecSpec::from_action("c", &ok, false).is_ok());
        let huge = action(r#"{"verb":"exec","argv":["ls"],"timeout_ms":99999999}"#);
        let s = ExecSpec::from_action("c", &huge, false).unwrap();
        assert_eq!(s.timeout, Duration::from_millis(MAX_TIMEOUT_MS));
        let zero = action(r#"{"verb":"exec","argv":["ls"],"timeout_ms":0}"#);
        assert!(ExecSpec::from_action("c", &zero, false)
            .unwrap_err()
            .contains("at least 1"));
        let none = action(r#"{"verb":"exec","argv":["ls"]}"#);
        let s = ExecSpec::from_action("c", &none, true).unwrap();
        assert_eq!(s.timeout, Duration::from_millis(DEFAULT_TIMEOUT_MS));
        assert!(s.binary);
    }

    // ---- buffered execution ----

    #[tokio::test]
    async fn argv_exec_captures_stdout_and_exit_code() {
        let out = run_buffered(spec(ExecCommand::Argv(vec![
            "echo".into(),
            "hello".into(),
            "exec".into(),
        ])))
        .await
        .unwrap();
        assert_eq!(out.exit_code, Some(0));
        assert_eq!(out.stdout, b"hello exec\n");
        assert!(out.stderr.is_empty());
        assert!(!out.timed_out && !out.stdout_truncated && !out.stderr_truncated);
        assert!(out.duration_ms > 0.0);
    }

    #[tokio::test]
    async fn shell_form_cwd_env_stdin_and_nonzero_exit() {
        let mut s = spec(ExecCommand::Shell(
            "cat; pwd; printf '%s' \"$SHK_TEST\"; exit 3".into(),
        ));
        s.cwd = Some("/tmp".into());
        s.env = vec![("SHK_TEST".into(), "wired".into())];
        s.stdin = Some("from-stdin\n".into());
        let out = run_buffered(s).await.unwrap();
        assert_eq!(out.exit_code, Some(3));
        let text = String::from_utf8_lossy(&out.stdout);
        assert!(text.starts_with("from-stdin\n"), "{text}");
        // /tmp may be a symlink (macOS /private/tmp) — match the suffix
        assert!(text.contains("tmp"), "{text}");
        assert!(text.ends_with("wired"), "{text}");
    }

    #[tokio::test]
    async fn stderr_is_captured_separately() {
        let out = run_buffered(spec(ExecCommand::Shell("echo out; echo err 1>&2".into())))
            .await
            .unwrap();
        assert_eq!(out.stdout, b"out\n");
        assert_eq!(out.stderr, b"err\n");
    }

    #[tokio::test]
    async fn spawn_failure_is_a_typed_error() {
        let err = run_buffered(spec(ExecCommand::Argv(vec![
            "/nonexistent/definitely-not-a-binary".into(),
        ])))
        .await
        .unwrap_err();
        assert!(err.starts_with("exec_spawn_failed:"), "{err}");
    }

    #[tokio::test]
    async fn timeout_kills_the_process_group_and_reports_honestly() {
        // The nested shell stops itself after it is spawned; the parent waits for it.
        // This is a deterministic blocked process tree, not a sleep-duration race.
        let mut s = spec(ExecCommand::Shell(
            "sh -c 'kill -STOP $$' & printf 'started\\n'; wait".into(),
        ));
        s.timeout = Duration::from_millis(200);
        let t0 = Instant::now();
        let out = run_buffered(s).await.unwrap();
        assert!(out.timed_out, "deadline must be reported");
        assert_eq!(out.exit_code, None, "killed by signal → no exit code");
        // The direct shell can finish between the deadline's final try_wait and
        // killpg while a stopped descendant still owns the group. In that race the
        // truthful direct-child signal is None; timed_out + no exit code remains the
        // stable terminal contract.
        if let Some(signal) = out.signal {
            assert_eq!(signal, libc::SIGKILL);
        }
        assert_eq!(
            out.stdout, b"started\n",
            "the descendant existed before timeout"
        );
        // Group kill: the stopped descendant holds the pipe; killing only the
        // direct shell would leave that pipe open past the drain deadline.
        assert!(
            t0.elapsed() < Duration::from_secs(5),
            "group kill must release the pipes promptly"
        );
    }

    #[test]
    fn timed_out_terminal_never_exposes_a_late_success_code() {
        let status = std::process::Command::new("true").status().unwrap();
        assert_eq!(status.code(), Some(0));
        let waited = WaitOutcome {
            status,
            timed_out: true,
        };
        assert_eq!(waited.exit_code(), None);
        assert_eq!(waited.signal(), None);
    }

    #[tokio::test]
    async fn reaped_leader_does_not_leave_a_pipe_holding_descendant() {
        let marker = format!(
            "/tmp/shinken-exec-descendant-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        let command = format!("sh -c 'kill -STOP $$' & echo $! > {marker}; exit 0");
        let t0 = Instant::now();
        let out = run_buffered(spec(ExecCommand::Shell(command)))
            .await
            .unwrap();
        assert_eq!(out.exit_code, Some(0));
        assert!(!out.timed_out, "the direct shell completed naturally");
        // A background shell is only required to retain at least one inherited pipe;
        // dash/bash and scheduler timing may close the other before the drain deadline.
        // Either stalled pipe must trigger the same process-group cleanup, proved below
        // by observing the deliberately stopped descendant disappear.
        assert!(out.stdout_truncated || out.stderr_truncated);
        assert!(t0.elapsed() < Duration::from_secs(3));

        let pid: libc::pid_t = std::fs::read_to_string(&marker)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        let gone_by = Instant::now() + Duration::from_secs(2);
        loop {
            // Signal 0 checks existence without changing process state.
            let alive = unsafe { libc::kill(pid, 0) } == 0;
            if !alive {
                break;
            }
            if Instant::now() >= gone_by {
                // Keep a failing test from leaking its deliberately stopped child.
                unsafe {
                    libc::kill(pid, libc::SIGKILL);
                }
                panic!("pipe-holding descendant {pid} survived exec cleanup");
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        let _ = std::fs::remove_file(marker);
    }

    #[tokio::test]
    async fn output_past_the_cap_is_truncated_honestly() {
        // ~1 MiB of stdout against the 256 KiB cap.
        let out = run_buffered(spec(ExecCommand::Shell(
            "i=0; while [ $i -lt 16384 ]; do printf '%064d' $i; i=$((i+1)); done".into(),
        )))
        .await
        .unwrap();
        assert_eq!(out.exit_code, Some(0), "the child still ran to completion");
        assert_eq!(out.stdout.len(), BUFFERED_CAP);
        assert!(out.stdout_truncated);
        assert!(!out.stderr_truncated);
    }

    // ---- streamed execution ----

    /// Parse every frame the streamed run pushed: (exec_output events, exec_exit).
    async fn collect_stream(
        mut rx: mpsc::Receiver<WsMessage>,
    ) -> (Vec<serde_json::Value>, serde_json::Value) {
        let mut outputs = Vec::new();
        while let Some(msg) = rx.recv().await {
            let v: serde_json::Value = match msg {
                WsMessage::Text(t) => serde_json::from_str(&t).unwrap(),
                WsMessage::Binary(b) => {
                    let hlen = u32::from_le_bytes(b[..4].try_into().unwrap()) as usize;
                    let mut header: serde_json::Value =
                        serde_json::from_slice(&b[4..4 + hlen]).unwrap();
                    let off = header["data"]["off"].as_u64().unwrap() as usize;
                    let len = header["data"]["len"].as_u64().unwrap() as usize;
                    let payload = &b[4 + hlen..];
                    header["payload_b64"] =
                        serde_json::Value::String(base64_encode(&payload[off..off + len]));
                    header
                }
                other => panic!("unexpected ws message kind: {other:?}"),
            };
            if v["type"] == "exec_exit" {
                return (outputs, v);
            }
            assert_eq!(v["type"], "exec_output");
            outputs.push(v);
        }
        panic!("stream ended without exec_exit");
    }

    fn base64_encode(b: &[u8]) -> String {
        use base64::Engine as _;
        base64::engine::general_purpose::STANDARD.encode(b)
    }

    fn base64_decode(s: &str) -> Vec<u8> {
        use base64::Engine as _;
        base64::engine::general_purpose::STANDARD.decode(s).unwrap()
    }

    /// Reassemble one logical stdout/stderr stream without assuming how OS pipe reads
    /// happened to split it into WebSocket events.
    fn channel_bytes(outputs: &[serde_json::Value], channel: &str) -> Vec<u8> {
        let mut bytes = Vec::new();
        for event in outputs.iter().filter(|event| event["channel"] == channel) {
            let encoded = event
                .get("data_b64")
                .or_else(|| event.get("payload_b64"))
                .and_then(serde_json::Value::as_str)
                .expect("exec_output needs a text or binary payload");
            bytes.extend(base64_decode(encoded));
        }
        bytes
    }

    #[tokio::test]
    async fn streamed_exec_orders_chunks_and_terminates_with_exit() {
        let (out, rx) = mpsc::channel(64);
        let mut s = spec(ExecCommand::Shell(
            "printf 'a%.0s' $(seq 1 100); echo err 1>&2; printf 'b'; exit 7".into(),
        ));
        s.stream = true;
        run_streamed(s, out).await;
        let (outputs, exit) = collect_stream(rx).await;
        // seq is monotonic from 0 across BOTH channels
        for (i, ev) in outputs.iter().enumerate() {
            assert_eq!(ev["seq"].as_u64().unwrap(), i as u64);
            assert_eq!(ev["cause"], "x1");
        }
        // reassembled stdout carries everything, in order
        let stdout = channel_bytes(&outputs, "stdout");
        assert_eq!(stdout.len(), 101);
        assert!(stdout.starts_with(b"aaaa") && stdout.ends_with(b"b"));
        let stderr = channel_bytes(&outputs, "stderr");
        assert_eq!(stderr, b"err\n");
        assert_eq!(exit["exit_code"], 7);
        assert_eq!(exit["timed_out"], false);
        assert_eq!(exit["truncated"], false);
        assert_eq!(exit["cause"], "x1");
        assert!(exit["duration_ms"].as_f64().unwrap() > 0.0);
    }

    #[tokio::test]
    async fn streamed_exec_on_a_binary_session_ships_raw_byte_frames() {
        let (out, rx) = mpsc::channel(64);
        let mut s = spec(ExecCommand::Argv(vec!["echo".into(), "raw".into()]));
        s.stream = true;
        s.binary = true;
        run_streamed(s, out).await;
        let (outputs, exit) = collect_stream(rx).await;
        assert_eq!(channel_bytes(&outputs, "stdout"), b"raw\n");
        assert!(outputs.iter().all(|event| event["channel"] == "stdout"));
        assert!(outputs.iter().all(|event| event.get("data_b64").is_none()));
        assert_eq!(exit["exit_code"], 0);
    }

    #[tokio::test]
    async fn streamed_spawn_failure_terminates_with_a_typed_exec_exit() {
        let (out, rx) = mpsc::channel(8);
        let mut s = spec(ExecCommand::Argv(vec!["/nonexistent/nope".into()]));
        s.stream = true;
        run_streamed(s, out).await;
        let (outputs, exit) = collect_stream(rx).await;
        assert!(outputs.is_empty());
        assert!(exit["error"]
            .as_str()
            .unwrap()
            .starts_with("exec_spawn_failed:"));
        assert!(exit["exit_code"].is_null());
    }

    #[tokio::test]
    async fn streamed_timeout_group_kills_and_reports() {
        let (out, rx) = mpsc::channel(64);
        let mut s = spec(ExecCommand::Shell(
            "sh -c 'kill -STOP $$' & printf 'started\\n'; wait".into(),
        ));
        s.stream = true;
        s.timeout = Duration::from_millis(200);
        let t0 = Instant::now();
        run_streamed(s, out).await;
        let (outputs, exit) = collect_stream(rx).await;
        assert_eq!(channel_bytes(&outputs, "stdout"), b"started\n");
        assert_eq!(exit["timed_out"], true);
        assert!(exit["exit_code"].is_null());
        if !exit["signal"].is_null() {
            assert_eq!(exit["signal"], libc::SIGKILL);
        }
        assert!(t0.elapsed() < Duration::from_secs(5));
    }

    #[tokio::test]
    async fn streamed_budget_truncates_but_finishes() {
        let (out, mut rx_raw) = mpsc::channel(1024);
        // ~9 MiB > the 8 MiB budget, in 64 KiB-bounded chunks.
        let mut s = spec(ExecCommand::Shell(
            "i=0; while [ $i -lt 9 ]; do head -c 1048576 /dev/zero; i=$((i+1)); done".into(),
        ));
        s.stream = true;
        // drain concurrently so the bounded channel never wedges the run
        let collector = tokio::spawn(async move {
            let mut outputs = 0usize;
            let mut bytes = 0usize;
            let mut exit = None;
            while let Some(msg) = rx_raw.recv().await {
                if let WsMessage::Text(t) = msg {
                    let v: serde_json::Value = serde_json::from_str(&t).unwrap();
                    if v["type"] == "exec_exit" {
                        exit = Some(v);
                    } else {
                        outputs += 1;
                        bytes += base64_decode(v["data_b64"].as_str().unwrap()).len();
                    }
                }
            }
            (outputs, bytes, exit)
        });
        run_streamed(s, out).await;
        let (outputs, bytes, exit) = collector.await.unwrap();
        let exit = exit.expect("exec_exit must terminate the stream");
        assert_eq!(exit["truncated"], true);
        assert!(bytes <= STREAM_BUDGET, "forwarded {bytes} > budget");
        assert!(outputs > 0);
        assert_eq!(exit["exit_code"], 0, "the child still ran to completion");
    }
}
