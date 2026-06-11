//! X11 CLIPBOARD selection support (G2) — typed `clipboard_get` / `clipboard_set`
//! with **no external helper** (no `xclip` subprocess dance): shinkend itself speaks
//! the ICCCM selection protocol on a dedicated worker thread with its own X
//! connection, so selection events never contend with the action/capture connection.
//!
//! - **set**: become the CLIPBOARD selection owner and keep a tiny owner task alive,
//!   serving `SelectionRequest` events (`TARGETS` / `UTF8_STRING` / `STRING`) until
//!   another client takes the selection (`SelectionClear`).
//! - **get**: `ConvertSelection` → wait (bounded) for `SelectionNotify`, **servicing
//!   our own SelectionRequests while waiting** so reading back a clipboard we
//!   ourselves own cannot deadlock — then read the transfer property. `UTF8_STRING`
//!   is preferred; a refusal gets one `STRING` retry (old-school owners like xterm).
//!
//! v1 is **text-only and size-capped** ([`MAX_CLIPBOARD_BYTES`]); an INCR
//! (incremental) transfer — i.e. an owner whose payload exceeds what fits a single
//! property — is refused with a typed error rather than half-read. Binary clipboard
//! formats (image/*, file lists) are future work, negotiated as new targets.

use std::sync::mpsc;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use anyhow::{bail, ensure, Context, Result};
use x11rb::connection::Connection;
use x11rb::protocol::xproto::{
    AtomEnum, ConnectionExt as _, CreateWindowAux, EventMask, PropMode, SelectionNotifyEvent,
    SelectionRequestEvent, Window, WindowClass, SELECTION_NOTIFY_EVENT,
};
use x11rb::protocol::Event;
use x11rb::wrapper::ConnectionExt as _;

/// Clipboard payload cap (bytes), both directions. Far under the 16 MiB WS message
/// bound; big enough for any text an OSWorld-class task copies. Oversized payloads
/// answer a typed error — never a silent truncation.
pub const MAX_CLIPBOARD_BYTES: usize = 1024 * 1024;

/// How long one `get` attempt waits for the selection owner to answer the
/// `ConvertSelection` (per target; the `STRING` retry gets its own window).
const GET_TIMEOUT: Duration = Duration::from_millis(1500);

/// Worker command-loop tick: how long the owner thread parks between checking for
/// commands while also pumping X selection events (paste requests land within this).
const TICK: Duration = Duration::from_millis(10);

/// Bound on a caller waiting for the worker to answer one command (covers the get
/// path's UTF8 attempt + STRING retry with slack).
const CMD_TIMEOUT: Duration = Duration::from_secs(5);

/// The interned atoms the selection protocol needs.
pub(crate) struct Atoms {
    pub clipboard: u32,
    pub utf8: u32,
    pub targets: u32,
    pub incr: u32,
    /// Our transfer property (`SHINKEND_CLIP`) on the owner window.
    pub prop: u32,
}

/// What to serve a `SelectionRequest` asking for `target` — the pure half of the
/// owner-side state machine, unit-testable without X.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Serve {
    /// `TARGETS`: advertise [TARGETS, UTF8_STRING, STRING].
    Targets,
    /// `UTF8_STRING`: the text, verbatim UTF-8.
    Utf8,
    /// `STRING` (Latin-1): served as the same bytes — exact for ASCII, lossy beyond;
    /// modern clients ask for UTF8_STRING first.
    Latin1,
    /// Anything else (image targets, MULTIPLE, …): refuse (notify with property None).
    Refuse,
}

pub(crate) fn classify_target(target: u32, atoms: &Atoms) -> Serve {
    if target == atoms.targets {
        Serve::Targets
    } else if target == atoms.utf8 {
        Serve::Utf8
    } else if target == u32::from(AtomEnum::STRING) {
        Serve::Latin1
    } else {
        Serve::Refuse
    }
}

/// ICCCM: an obsolete requestor may pass `property = None`; the convention is to
/// write the reply to a property named after the target atom.
pub(crate) fn reply_property(requested: u32, target: u32) -> u32 {
    if requested == 0 {
        target
    } else {
        requested
    }
}

/// Requestor-side wait state machine for one `get`: which `SelectionNotify`
/// outcomes fetch, retry with `STRING`, or give up. Pure — the X loop feeds it.
#[derive(Debug, Default)]
pub(crate) struct GetWait {
    tried_string: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum GetStep {
    /// The owner answered: read this property.
    Fetch(u32),
    /// UTF8_STRING was refused — re-convert once with the `STRING` target.
    RetryString,
    /// Both targets refused (or no owner): the clipboard has no readable text.
    Empty,
}

impl GetWait {
    pub(crate) fn on_notify(&mut self, property: u32) -> GetStep {
        if property != 0 {
            GetStep::Fetch(property)
        } else if !self.tried_string {
            self.tried_string = true;
            GetStep::RetryString
        } else {
            GetStep::Empty
        }
    }
}

// ---- the worker ----

enum Cmd {
    Set(String, mpsc::Sender<Result<()>>),
    Get(mpsc::Sender<Result<String>>),
}

/// Handle to the clipboard worker thread (one per X11 executor, spawned on first
/// use). The `Sender` is mutex-wrapped only because `mpsc::Sender` is not `Sync`.
pub struct ClipboardWorker {
    tx: Mutex<mpsc::Sender<Cmd>>,
}

impl ClipboardWorker {
    /// Spawn the owner thread and wait for its X connection to come up (or fail).
    pub fn spawn() -> Result<Self> {
        let (tx, rx) = mpsc::channel::<Cmd>();
        let (ready_tx, ready_rx) = mpsc::channel::<Result<()>>();
        std::thread::Builder::new()
            .name("shinkend-clipboard".into())
            .spawn(move || worker_main(rx, ready_tx))
            .context("spawn clipboard worker thread")?;
        ready_rx
            .recv()
            .context("clipboard worker died during startup")??;
        Ok(Self { tx: Mutex::new(tx) })
    }

    fn call<T>(&self, make: impl FnOnce(mpsc::Sender<Result<T>>) -> Cmd) -> Result<T> {
        let (tx, rx) = mpsc::channel();
        let sent = self
            .tx
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .send(make(tx));
        if sent.is_err() {
            bail!("clipboard worker exited (X connection lost?)");
        }
        rx.recv_timeout(CMD_TIMEOUT)
            .context("clipboard worker did not answer in time")?
    }

    pub fn set(&self, text: String) -> Result<()> {
        self.call(|tx| Cmd::Set(text, tx))
    }

    pub fn get(&self) -> Result<String> {
        self.call(Cmd::Get)
    }
}

/// Lazily-spawned shared worker handle, held by the X11 executor. A dead worker
/// (lost X connection) is dropped so the next call respawns against the live
/// display — same self-healing posture as the lazy X11 executor itself.
#[derive(Default)]
pub struct SharedClipboard(Mutex<Option<ClipboardWorker>>);

impl SharedClipboard {
    fn with_worker<T>(&self, f: impl Fn(&ClipboardWorker) -> Result<T>) -> Result<T> {
        let mut slot = self
            .0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if slot.is_none() {
            *slot = Some(ClipboardWorker::spawn()?);
        }
        let res = f(slot.as_ref().expect("worker just ensured"));
        if let Err(e) = &res {
            // Only a vanished worker is fatal to the handle; a typed protocol error
            // (empty clipboard, timeout) keeps the live worker cached.
            if e.to_string().contains("clipboard worker exited") {
                *slot = None;
            }
        }
        res
    }

    /// Publish `text` as the CLIPBOARD selection. Size-capped BEFORE any X work.
    pub fn set(&self, text: &str) -> Result<()> {
        ensure!(
            text.len() <= MAX_CLIPBOARD_BYTES,
            "clipboard_set text too large: {} bytes (cap {MAX_CLIPBOARD_BYTES})",
            text.len()
        );
        self.with_worker(|w| w.set(text.to_string()))
    }

    /// Read the CLIPBOARD selection as text (UTF8_STRING, one STRING retry).
    pub fn get(&self) -> Result<String> {
        self.with_worker(ClipboardWorker::get)
    }
}

fn worker_main(rx: mpsc::Receiver<Cmd>, ready: mpsc::Sender<Result<()>>) {
    let mut owner = match Owner::connect() {
        Ok(o) => {
            let _ = ready.send(Ok(()));
            o
        }
        Err(e) => {
            let _ = ready.send(Err(e));
            return;
        }
    };
    loop {
        // Serve pending paste requests even while idle; a dead X connection ends
        // the thread — the handle then answers "worker exited" and respawns lazily.
        if owner.pump().is_err() {
            return;
        }
        match rx.recv_timeout(TICK) {
            Ok(Cmd::Set(text, reply)) => {
                let _ = reply.send(owner.set(text));
            }
            Ok(Cmd::Get(reply)) => {
                let _ = reply.send(owner.get());
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => return,
        }
    }
}

/// The owner-side state: one X connection, one unmapped 1×1 window that owns the
/// selection and receives transfer properties, and the current text (when we own).
struct Owner {
    conn: x11rb::rust_connection::RustConnection,
    win: Window,
    atoms: Atoms,
    text: Option<String>,
}

impl Owner {
    fn connect() -> Result<Self> {
        let (conn, screen_num) = x11rb::connect(None).context("connect clipboard display")?;
        let root = conn.setup().roots[screen_num].root;
        let win = conn.generate_id()?;
        conn.create_window(
            x11rb::COPY_DEPTH_FROM_PARENT,
            win,
            root,
            -1,
            -1,
            1,
            1,
            0,
            WindowClass::INPUT_OUTPUT,
            0, // CopyFromParent visual
            &CreateWindowAux::new().event_mask(EventMask::PROPERTY_CHANGE),
        )?;
        let atom =
            |name: &[u8]| -> Result<u32> { Ok(conn.intern_atom(false, name)?.reply()?.atom) };
        let atoms = Atoms {
            clipboard: atom(b"CLIPBOARD")?,
            utf8: atom(b"UTF8_STRING")?,
            targets: atom(b"TARGETS")?,
            incr: atom(b"INCR")?,
            prop: atom(b"SHINKEND_CLIP")?,
        };
        conn.flush()?;
        Ok(Self {
            conn,
            win,
            atoms,
            text: None,
        })
    }

    /// Drain pending X events, serving paste requests. Stray `SelectionNotify`s
    /// (only `get()` expects them) are dropped here.
    fn pump(&mut self) -> Result<()> {
        while let Some(ev) = self.conn.poll_for_event()? {
            self.handle(ev)?;
        }
        Ok(())
    }

    /// Handle one event; returns a `SelectionNotify` for the caller (`get`'s wait
    /// loop) to inspect, after serving any interleaved requests.
    fn handle(&mut self, ev: Event) -> Result<Option<SelectionNotifyEvent>> {
        match ev {
            Event::SelectionRequest(req) => {
                self.serve(req)?;
                Ok(None)
            }
            Event::SelectionClear(c) if c.selection == self.atoms.clipboard => {
                self.text = None; // another client took the clipboard
                Ok(None)
            }
            Event::SelectionNotify(n) => Ok(Some(n)),
            _ => Ok(None),
        }
    }

    /// Answer one `SelectionRequest` per ICCCM: write the data (or targets list) to
    /// the requestor's property and send the matching `SelectionNotify` (property
    /// None = refusal).
    fn serve(&self, req: SelectionRequestEvent) -> Result<()> {
        let prop = reply_property(req.property, req.target);
        let served = match (classify_target(req.target, &self.atoms), self.text.as_ref()) {
            (Serve::Targets, Some(_)) => {
                let targets = [
                    self.atoms.targets,
                    self.atoms.utf8,
                    u32::from(AtomEnum::STRING),
                ];
                self.conn.change_property32(
                    PropMode::REPLACE,
                    req.requestor,
                    prop,
                    AtomEnum::ATOM,
                    &targets,
                )?;
                true
            }
            (Serve::Utf8, Some(text)) => {
                self.conn.change_property8(
                    PropMode::REPLACE,
                    req.requestor,
                    prop,
                    self.atoms.utf8,
                    text.as_bytes(),
                )?;
                true
            }
            (Serve::Latin1, Some(text)) => {
                self.conn.change_property8(
                    PropMode::REPLACE,
                    req.requestor,
                    prop,
                    AtomEnum::STRING,
                    text.as_bytes(),
                )?;
                true
            }
            _ => false,
        };
        let notify = SelectionNotifyEvent {
            response_type: SELECTION_NOTIFY_EVENT,
            sequence: 0,
            time: req.time,
            requestor: req.requestor,
            selection: req.selection,
            target: req.target,
            property: if served { prop } else { 0 },
        };
        self.conn
            .send_event(false, req.requestor, EventMask::NO_EVENT, notify)?;
        self.conn.flush()?;
        Ok(())
    }

    fn set(&mut self, text: String) -> Result<()> {
        self.text = Some(text);
        self.conn
            .set_selection_owner(self.win, self.atoms.clipboard, x11rb::CURRENT_TIME)?;
        self.conn.flush()?;
        let owner = self
            .conn
            .get_selection_owner(self.atoms.clipboard)?
            .reply()?
            .owner;
        ensure!(
            owner == self.win,
            "failed to acquire the CLIPBOARD selection (owner is {owner:#x})"
        );
        Ok(())
    }

    fn get(&mut self) -> Result<String> {
        let mut wait = GetWait::default();
        self.convert(self.atoms.utf8)?;
        let deadline = Instant::now() + GET_TIMEOUT;
        loop {
            if let Some(ev) = self.conn.poll_for_event()? {
                let Some(n) = self.handle(ev)? else { continue };
                if n.requestor != self.win || n.selection != self.atoms.clipboard {
                    continue;
                }
                match wait.on_notify(n.property) {
                    GetStep::Fetch(prop) => return self.read_property(prop),
                    GetStep::RetryString => self.convert(u32::from(AtomEnum::STRING))?,
                    GetStep::Empty => {
                        bail!("clipboard is empty (no owner answered UTF8_STRING or STRING)")
                    }
                }
            } else {
                std::thread::sleep(Duration::from_millis(2));
            }
            if Instant::now() > deadline {
                bail!(
                    "clipboard_get timed out after {}ms waiting for the selection owner",
                    GET_TIMEOUT.as_millis()
                );
            }
        }
    }

    fn convert(&self, target: u32) -> Result<()> {
        self.conn.delete_property(self.win, self.atoms.prop)?;
        self.conn.convert_selection(
            self.win,
            self.atoms.clipboard,
            target,
            self.atoms.prop,
            x11rb::CURRENT_TIME,
        )?;
        self.conn.flush()?;
        Ok(())
    }

    fn read_property(&self, prop: u32) -> Result<String> {
        let reply = self
            .conn
            .get_property(
                true, // delete after read
                self.win,
                prop,
                0u32, // AnyPropertyType
                0,
                (MAX_CLIPBOARD_BYTES / 4) as u32,
            )?
            .reply()?;
        if reply.type_ == self.atoms.incr {
            bail!(
                "clipboard transfer is INCR (payload exceeds the v1 cap of \
                 {MAX_CLIPBOARD_BYTES} bytes); incremental transfers are not supported"
            );
        }
        ensure!(
            reply.bytes_after == 0,
            "clipboard text too large: more than {MAX_CLIPBOARD_BYTES} bytes"
        );
        Ok(String::from_utf8_lossy(&reply.value).into_owned())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn atoms() -> Atoms {
        Atoms {
            clipboard: 100,
            utf8: 101,
            targets: 102,
            incr: 103,
            prop: 104,
        }
    }

    // ---- owner-side state machine (what to serve a SelectionRequest) ----

    #[test]
    fn classify_target_serves_targets_utf8_and_string_only() {
        let a = atoms();
        assert_eq!(classify_target(a.targets, &a), Serve::Targets);
        assert_eq!(classify_target(a.utf8, &a), Serve::Utf8);
        assert_eq!(
            classify_target(u32::from(AtomEnum::STRING), &a),
            Serve::Latin1
        );
        // an image target / MULTIPLE / arbitrary atom is refused, never guessed
        assert_eq!(classify_target(999, &a), Serve::Refuse);
        assert_eq!(classify_target(0, &a), Serve::Refuse);
    }

    #[test]
    fn reply_property_falls_back_to_the_target_atom_per_icccm() {
        // a modern requestor names the property — use it verbatim
        assert_eq!(reply_property(55, 101), 55);
        // an obsolete requestor passes None — the reply lands on a property named
        // after the target atom (ICCCM §2.2)
        assert_eq!(reply_property(0, 101), 101);
    }

    // ---- requestor-side state machine (the get wait) ----

    #[test]
    fn get_wait_fetches_on_a_served_property() {
        let mut w = GetWait::default();
        assert_eq!(w.on_notify(104), GetStep::Fetch(104));
    }

    #[test]
    fn get_wait_retries_string_once_then_reports_empty() {
        let mut w = GetWait::default();
        // UTF8_STRING refused → exactly one STRING retry
        assert_eq!(w.on_notify(0), GetStep::RetryString);
        // STRING refused too → empty, not an infinite retry loop
        assert_eq!(w.on_notify(0), GetStep::Empty);
    }

    #[test]
    fn get_wait_fetch_after_string_retry() {
        let mut w = GetWait::default();
        assert_eq!(w.on_notify(0), GetStep::RetryString);
        assert_eq!(w.on_notify(104), GetStep::Fetch(104));
    }

    // ---- size cap (checked before any X work) ----

    #[test]
    fn shared_clipboard_rejects_oversized_set_without_touching_x() {
        let clip = SharedClipboard::default();
        let huge = "x".repeat(MAX_CLIPBOARD_BYTES + 1);
        // No X server in unit tests: the cap must reject BEFORE any connect attempt.
        let err = clip.set(&huge).unwrap_err().to_string();
        assert!(err.contains("too large"), "unexpected error: {err}");
    }
}
