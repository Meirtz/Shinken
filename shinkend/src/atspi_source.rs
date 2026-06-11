//! Live AT-SPI tree source (Linux) — the bus-facing half of structured observation.
//!
//! All AT-SPI/zbus work runs on ONE dedicated worker thread that owns a
//! current-thread tokio runtime, completely off the main serve runtime. Sessions
//! talk to it through a channel with a hard reply deadline, so **an AT-SPI hang can
//! never wedge the runtime**: a stuck walk leaves the worker busy, the requesting
//! session gets a typed timeout error, and pixel observation keeps working. Every
//! D-Bus call inside the worker carries its own short timeout, the walk has node /
//! depth / total-time caps, and a partial tree is returned `truncated` rather than
//! failing the observation.
//!
//! Bus discovery: `AT_SPI_BUS_ADDRESS` when set, else the session bus's
//! `org.a11y.Bus.GetAddress` (which requires `DBUS_SESSION_BUS_ADDRESS` — the
//! sandbox image's `start.sh` provides both the session bus and the a11y bus).
//! The connection is dialed lazily on the first request and re-dialed after
//! failures, mirroring the lazy X11 executor.

use std::sync::mpsc;
use std::time::{Duration, Instant};

use anyhow::{anyhow, bail, Context, Result};
use atspi::connection::AccessibilityConnection;
use atspi::proxy::accessible::{AccessibleProxy, ObjectRefExt as _};
use atspi::proxy::action::ActionProxy;
use atspi::proxy::component::ComponentProxy;
use atspi::proxy::editable_text::EditableTextProxy;
use atspi::proxy::text::TextProxy;
use atspi::proxy::value::ValueProxy;
use atspi::zbus;
use atspi::{CoordType, Interface, ObjectEvents, ObjectRefOwned, State, WindowEvents};
use futures_util::StreamExt as _;

use crate::observe::{RawNode, RawTree, TreeSource};

/// Per-D-Bus-call deadline. A toolkit that stops answering mid-walk costs at most
/// this much per node before the walk moves on (and the total budget cuts it off).
const CALL_TIMEOUT: Duration = Duration::from_millis(250);
/// Total walk budget; hitting it returns the partial tree as `truncated`.
const WALK_BUDGET: Duration = Duration::from_millis(2_500);
/// Dialing the a11y bus (session bus hop + connect).
const CONNECT_TIMEOUT: Duration = Duration::from_millis(1_500);
/// Caps on the walk — bound both the wire payload and the time on a busy desktop.
const MAX_NODES: usize = 2_000;
const MAX_DEPTH: u16 = 48;
const MAX_APPS: usize = 64;
/// Longest text-value excerpt captured per node (chars).
const VALUE_TEXT_CAP: i32 = 200;
/// Settle: the quiesce window is clamped into this range…
const SETTLE_QUIET_MIN_MS: u64 = 10;
const SETTLE_QUIET_MAX_MS: u64 = 1_000;
/// …and the TOTAL settle wait is capped regardless of event chatter.
const SETTLE_TOTAL_CAP_MS: u64 = 3_000;
/// How long a session waits for the worker before declaring it busy/hung. Covers
/// the worst legitimate request (settle cap + walk budget + connect) with margin.
const REQUEST_TIMEOUT: Duration = Duration::from_millis(8_000);

/// Agent-relevant AT-SPI states surfaced on each node (wire spelling, lower-case).
const WANTED_STATES: &[(State, &str)] = &[
    (State::Focused, "focused"),
    (State::Enabled, "enabled"),
    (State::Selected, "selected"),
    (State::Expanded, "expanded"),
    (State::Editable, "editable"),
    (State::Checked, "checked"),
    (State::Showing, "showing"),
    (State::Focusable, "focusable"),
    (State::Active, "active"),
];

enum Request {
    Snapshot {
        settle_ms: Option<u64>,
        reply: mpsc::SyncSender<Result<RawTree>>,
    },
    Invoke {
        backend_id: String,
        action: Option<String>,
        reply: mpsc::SyncSender<Result<String>>,
    },
    SetValue {
        backend_id: String,
        value: String,
        reply: mpsc::SyncSender<Result<String>>,
    },
}

/// Handle to the AT-SPI worker thread; cheap to clone, shared across sessions.
/// Implements [`TreeSource`] with a bounded reply deadline per request.
#[derive(Clone)]
pub struct AtspiHandle {
    tx: mpsc::Sender<Request>,
}

impl AtspiHandle {
    /// Spawn the worker (it dials the bus lazily on the first request).
    pub fn spawn() -> Self {
        let (tx, rx) = mpsc::channel();
        std::thread::Builder::new()
            .name("atspi-worker".to_string())
            .spawn(move || worker(rx))
            .expect("spawn atspi worker thread");
        Self { tx }
    }

    fn request<T>(&self, build: impl FnOnce(mpsc::SyncSender<Result<T>>) -> Request) -> Result<T> {
        let (reply_tx, reply_rx) = mpsc::sync_channel(1);
        self.tx
            .send(build(reply_tx))
            .map_err(|_| anyhow!("structured observation unavailable: the AT-SPI worker exited"))?;
        match reply_rx.recv_timeout(REQUEST_TIMEOUT) {
            Ok(r) => r,
            Err(_) => bail!(
                "structured observation timed out after {:?} (AT-SPI worker busy or hung); \
                 the runtime stays healthy — retry, or fall back to pixel observation",
                REQUEST_TIMEOUT
            ),
        }
    }
}

impl TreeSource for AtspiHandle {
    fn snapshot(&self, settle_ms: Option<u64>) -> Result<RawTree> {
        self.request(|reply| Request::Snapshot { settle_ms, reply })
    }

    fn invoke(&self, backend_id: &str, action: Option<&str>) -> Result<String> {
        let backend_id = backend_id.to_string();
        let action = action.map(str::to_string);
        self.request(move |reply| Request::Invoke {
            backend_id,
            action,
            reply,
        })
    }

    fn set_value(&self, backend_id: &str, value: &str) -> Result<String> {
        let backend_id = backend_id.to_string();
        let value = value.to_string();
        self.request(move |reply| Request::SetValue {
            backend_id,
            value,
            reply,
        })
    }
}

/// Wrap one D-Bus call in the per-call deadline.
async fn call<T>(fut: impl std::future::Future<Output = zbus::Result<T>>) -> Result<T> {
    match tokio::time::timeout(CALL_TIMEOUT, fut).await {
        Ok(Ok(v)) => Ok(v),
        Ok(Err(e)) => Err(e.into()),
        Err(_) => bail!("AT-SPI call timed out after {CALL_TIMEOUT:?}"),
    }
}

fn worker(rx: mpsc::Receiver<Request>) {
    let rt = match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("shinkend: atspi worker failed to build its runtime: {e}");
            return; // senders observe the closed channel and answer with errors
        }
    };
    let mut conn: Option<AccessibilityConnection> = None;
    while let Ok(req) = rx.recv() {
        // (Re)dial lazily; a failed dial answers THIS request and is retried on the next.
        if conn.is_none() {
            conn = match rt.block_on(connect()) {
                Ok(c) => {
                    eprintln!("shinkend: AT-SPI a11y bus connected (structured observation live)");
                    Some(c)
                }
                Err(e) => {
                    answer_err(req, &format!("AT-SPI bus unavailable: {e:#}"));
                    continue;
                }
            };
        }
        let c = conn.as_ref().expect("connection just established");
        let failed = match req {
            Request::Snapshot { settle_ms, reply } => {
                let r = rt.block_on(async {
                    if let Some(quiet) = settle_ms {
                        settle(c, quiet).await;
                    }
                    snapshot(c).await
                });
                let failed = r.is_err();
                let _ = reply.send(r);
                failed
            }
            Request::Invoke {
                backend_id,
                action,
                reply,
            } => {
                let r = rt.block_on(invoke(c, &backend_id, action.as_deref()));
                let failed = r.is_err();
                let _ = reply.send(r);
                failed
            }
            Request::SetValue {
                backend_id,
                value,
                reply,
            } => {
                let r = rt.block_on(set_value(c, &backend_id, &value));
                let failed = r.is_err();
                let _ = reply.send(r);
                failed
            }
        };
        // A failed request may mean the bus died (registry restart): drop the
        // connection so the next request re-dials, mirroring LazyX11Executor.
        if failed {
            conn = None;
        }
    }
}

fn answer_err(req: Request, msg: &str) {
    match req {
        Request::Snapshot { reply, .. } => {
            let _ = reply.send(Err(anyhow!("{msg}")));
        }
        Request::Invoke { reply, .. } => {
            let _ = reply.send(Err(anyhow!("{msg}")));
        }
        Request::SetValue { reply, .. } => {
            let _ = reply.send(Err(anyhow!("{msg}")));
        }
    }
}

async fn connect() -> Result<AccessibilityConnection> {
    let dial = async {
        // Explicit address first (lets an operator point shinkend at a specific
        // a11y bus); otherwise the session bus's org.a11y.Bus.GetAddress.
        if let Ok(addr) = std::env::var("AT_SPI_BUS_ADDRESS") {
            if !addr.trim().is_empty() {
                let parsed = addr
                    .trim()
                    .parse()
                    .with_context(|| format!("parse AT_SPI_BUS_ADDRESS {addr:?}"))?;
                let conn = AccessibilityConnection::from_address(parsed).await?;
                return anyhow::Ok(conn);
            }
        }
        anyhow::Ok(AccessibilityConnection::new().await?)
    };
    let conn: AccessibilityConnection = tokio::time::timeout(CONNECT_TIMEOUT, dial)
        .await
        .map_err(|_| anyhow!("a11y bus connect timed out after {CONNECT_TIMEOUT:?}"))??;
    // Subscribe to the change notifications the settle debounce listens for. Failure
    // is non-fatal: settle degrades to a plain bounded sleep.
    if let Err(e) = conn.register_event::<ObjectEvents>().await {
        eprintln!("shinkend: atspi object-event registration failed ({e}); settle degrades");
    }
    if let Err(e) = conn.register_event::<WindowEvents>().await {
        eprintln!("shinkend: atspi window-event registration failed ({e}); settle degrades");
    }
    Ok(conn)
}

/// Debounce a11y events: wait until the bus has been quiet for `quiet_ms`
/// (clamped), but never longer than [`SETTLE_TOTAL_CAP_MS`] in total — a busy app
/// repainting forever must not park the observation.
async fn settle(conn: &AccessibilityConnection, quiet_ms: u64) {
    let quiet = Duration::from_millis(quiet_ms.clamp(SETTLE_QUIET_MIN_MS, SETTLE_QUIET_MAX_MS));
    let deadline = Instant::now() + Duration::from_millis(SETTLE_TOTAL_CAP_MS);
    let mut stream = std::pin::pin!(conn.event_stream());
    // Drain events buffered since the last request — only NEW activity counts.
    while let Ok(Some(_)) = tokio::time::timeout(Duration::from_millis(2), stream.next()).await {}
    let mut last_event = Instant::now();
    loop {
        let now = Instant::now();
        if now >= deadline {
            break; // total cap
        }
        let since_last = now.duration_since(last_event);
        if since_last >= quiet {
            break; // quiesced
        }
        let wait = (quiet - since_last).min(deadline - now);
        match tokio::time::timeout(wait, stream.next()).await {
            Ok(Some(_)) => last_event = Instant::now(),
            Ok(None) => break, // stream ended (bus gone) — observe what we can
            Err(_) => {}       // window elapsed without events — loop re-checks
        }
    }
}

/// `<bus name><object path>` — the node identity the observe core keys on.
fn backend_id_of(obj: &ObjectRefOwned) -> String {
    format!("{}{}", obj.name_as_str().unwrap_or(""), obj.path_as_str())
}

/// Split a `backend_id` back into `(bus name, object path)`.
fn parse_backend_id(backend_id: &str) -> Result<(&str, &str)> {
    let slash = backend_id
        .find('/')
        .with_context(|| format!("malformed backend id {backend_id:?}"))?;
    Ok((&backend_id[..slash], &backend_id[slash..]))
}

/// Build a typed interface proxy at (bus name, path) without property caching.
/// Expands to a builder — append `.build().await` at the use site.
macro_rules! iface_proxy {
    ($proxy:ident, $conn:expr, $name:expr, $path:expr) => {
        $proxy::builder($conn)
            .destination($name.to_string())?
            .path($path.to_string())?
            .cache_properties(zbus::proxy::CacheProperties::No)
    };
}

/// Capture the focused app's tree (or every app when no active window is found).
async fn snapshot(conn: &AccessibilityConnection) -> Result<RawTree> {
    let zconn = conn.connection();
    let deadline = Instant::now() + WALK_BUDGET;
    let root: AccessibleProxy<'_> = conn
        .root_accessible_on_registry()
        .await
        .context("registry root accessible")?;
    let apps = call(root.get_children())
        .await
        .context("desktop children")?;

    // Find the app that owns the ACTIVE window (the focused app).
    let mut target: Option<(ObjectRefOwned, String, String)> = None;
    'apps: for app in apps.iter().take(MAX_APPS) {
        if Instant::now() > deadline {
            break;
        }
        let Ok(proxy) = app.as_accessible_proxy(zconn).await else {
            continue;
        };
        let app_name = call(proxy.name()).await.unwrap_or_default();
        let Ok(windows) = call(proxy.get_children()).await else {
            continue;
        };
        for win in windows.iter().take(MAX_APPS) {
            let Ok(wproxy) = win.as_accessible_proxy(zconn).await else {
                continue;
            };
            let Ok(states) = call(wproxy.get_state()).await else {
                continue;
            };
            if states.contains(State::Active) {
                let title = call(wproxy.name()).await.unwrap_or_default();
                target = Some((app.clone(), app_name.clone(), title));
                break 'apps;
            }
        }
    }

    let mut tree = RawTree::default();
    let roots: Vec<ObjectRefOwned> = match &target {
        Some((app, name, window)) => {
            tree.app = name.clone();
            tree.window = window.clone();
            vec![app.clone()]
        }
        None => {
            // No active AT-SPI window (e.g. focus is on a tree-less app like a
            // terminal): observe everything on the bus, within the caps.
            tree.app = "desktop".to_string();
            apps.into_iter().take(MAX_APPS).collect()
        }
    };

    for root_obj in roots {
        walk_subtree(zconn, root_obj, deadline, &mut tree).await;
        if tree.truncated {
            break;
        }
    }
    Ok(tree)
}

/// Iterative pre-order DFS under the node/depth/time caps.
async fn walk_subtree(
    zconn: &zbus::Connection,
    root: ObjectRefOwned,
    deadline: Instant,
    tree: &mut RawTree,
) {
    let mut stack: Vec<(ObjectRefOwned, u16, Option<String>)> = vec![(root, 0, None)];
    while let Some((obj, depth, parent)) = stack.pop() {
        if tree.nodes.len() >= MAX_NODES || Instant::now() > deadline {
            tree.truncated = true;
            return;
        }
        let backend_id = backend_id_of(&obj);
        let (node, children) = visit(zconn, &obj, depth, parent).await;
        tree.nodes.push(node);
        if depth < MAX_DEPTH {
            // Reverse so the stack pops children left-to-right (document order).
            for child in children.into_iter().rev() {
                if !child.is_null() {
                    stack.push((child, depth + 1, Some(backend_id.clone())));
                }
            }
        } else {
            tree.truncated = true;
        }
    }
}

/// Read one node: role/name/description, states, interfaces → bbox / actions /
/// value, plus its children. Every sub-call is individually timeout-bounded and
/// failure-tolerant — a misbehaving node degrades to a sparse entry, not an error.
async fn visit(
    zconn: &zbus::Connection,
    obj: &ObjectRefOwned,
    depth: u16,
    parent: Option<String>,
) -> (RawNode, Vec<ObjectRefOwned>) {
    let mut node = RawNode {
        backend_id: backend_id_of(obj),
        parent_backend_id: parent,
        depth,
        role: "unknown".to_string(),
        ..RawNode::default()
    };
    let Ok(proxy) = obj.as_accessible_proxy(zconn).await else {
        return (node, Vec::new());
    };
    if let Ok(role) = call(proxy.get_role_name()).await {
        if !role.is_empty() {
            node.role = role;
        }
    }
    node.name = call(proxy.name()).await.unwrap_or_default();
    node.description = call(proxy.description()).await.unwrap_or_default();
    if let Ok(states) = call(proxy.get_state()).await {
        for (state, label) in WANTED_STATES {
            if states.contains(*state) {
                node.states.push((*label).to_string());
            }
        }
        node.focused = states.contains(State::Focused);
    }
    let (name, path) = (
        obj.name_as_str().unwrap_or("").to_string(),
        obj.path_as_str().to_string(),
    );
    let interfaces = call(proxy.get_interfaces()).await.ok();
    if let Some(ifaces) = &interfaces {
        if ifaces.contains(Interface::Component) {
            node.bbox = component_bbox(zconn, &name, &path).await;
        }
        if ifaces.contains(Interface::Action) {
            node.actions = action_names(zconn, &name, &path).await;
        }
        if ifaces.contains(Interface::Value) {
            node.value = numeric_value(zconn, &name, &path).await;
        } else if ifaces.contains(Interface::Text) {
            node.value = text_value(zconn, &name, &path).await;
        }
    }
    let children = call(proxy.get_children()).await.unwrap_or_default();
    (node, children)
}

async fn component_bbox(zconn: &zbus::Connection, name: &str, path: &str) -> Option<[i32; 4]> {
    let fut = async {
        let p = iface_proxy!(ComponentProxy, zconn, name, path)
            .build()
            .await?;
        call(p.get_extents(CoordType::Screen)).await
    };
    fut.await.ok().map(|(x, y, w, h)| [x, y, w, h])
}

async fn action_names(zconn: &zbus::Connection, name: &str, path: &str) -> Vec<String> {
    let fut = async {
        let p = iface_proxy!(ActionProxy, zconn, name, path).build().await?;
        call(p.get_actions()).await
    };
    match fut.await {
        Ok(actions) => actions
            .into_iter()
            .take(8)
            .map(|a| a.name)
            .filter(|n| !n.is_empty())
            .collect(),
        Err(_) => Vec::new(),
    }
}

async fn numeric_value(zconn: &zbus::Connection, name: &str, path: &str) -> Option<String> {
    let fut = async {
        let p = iface_proxy!(ValueProxy, zconn, name, path).build().await?;
        call(p.current_value()).await
    };
    fut.await.ok().map(|v| format!("{v}"))
}

async fn text_value(zconn: &zbus::Connection, name: &str, path: &str) -> Option<String> {
    let fut = async {
        let p = iface_proxy!(TextProxy, zconn, name, path).build().await?;
        let count = call(p.character_count()).await?;
        if count <= 0 {
            return Ok(String::new());
        }
        call(p.get_text(0, count.min(VALUE_TEXT_CAP))).await
    };
    fut.await.ok()
}

/// AT-SPI Action by name (`None` → the node's first action) — the AX-path fallback
/// for elements without usable geometry.
async fn invoke(
    conn: &AccessibilityConnection,
    backend_id: &str,
    action: Option<&str>,
) -> Result<String> {
    let zconn = conn.connection();
    let (name, path) = parse_backend_id(backend_id)?;
    let p = iface_proxy!(ActionProxy, zconn, name, path)
        .build()
        .await
        .context("Action interface proxy")?;
    let actions = call(p.get_actions()).await.context("GetActions")?;
    if actions.is_empty() {
        bail!("element exposes no AT-SPI actions");
    }
    let index = match action {
        None => 0usize,
        Some(wanted) => actions
            .iter()
            .position(|a| a.name.eq_ignore_ascii_case(wanted))
            .with_context(|| {
                format!(
                    "element has no action named {wanted:?} (available: {})",
                    actions
                        .iter()
                        .map(|a| a.name.as_str())
                        .collect::<Vec<_>>()
                        .join(", ")
                )
            })?,
    };
    let ok = call(p.do_action(index as i32)).await.context("DoAction")?;
    if !ok {
        bail!("AT-SPI DoAction({index}) returned false");
    }
    Ok(format!("invoked action {:?}", actions[index].name))
}

/// Set a node's value: numeric Value interface when the input parses as a number
/// and the interface exists, else EditableText `SetTextContents`.
async fn set_value(
    conn: &AccessibilityConnection,
    backend_id: &str,
    value: &str,
) -> Result<String> {
    let zconn = conn.connection();
    let (name, path) = parse_backend_id(backend_id)?;
    // Which interfaces does the node actually implement?
    let acc = iface_proxy!(AccessibleProxy, zconn, name, path)
        .build()
        .await
        .context("Accessible proxy")?;
    let ifaces = call(acc.get_interfaces()).await.context("GetInterfaces")?;
    if ifaces.contains(Interface::Value) {
        if let Ok(num) = value.trim().parse::<f64>() {
            let p = iface_proxy!(ValueProxy, zconn, name, path)
                .build()
                .await
                .context("Value interface proxy")?;
            call(p.set_current_value(num)).await.context("SetValue")?;
            return Ok(format!("set numeric value {num}"));
        }
    }
    if ifaces.contains(Interface::EditableText) {
        let p = iface_proxy!(EditableTextProxy, zconn, name, path)
            .build()
            .await
            .context("EditableText interface proxy")?;
        let ok = call(p.set_text_contents(value))
            .await
            .context("SetTextContents")?;
        if !ok {
            bail!("AT-SPI SetTextContents returned false");
        }
        return Ok(format!("set text value ({} chars)", value.chars().count()));
    }
    bail!("element implements neither Value nor EditableText — set_value unsupported")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backend_id_round_trips_through_parse() {
        let (name, path) = parse_backend_id(":1.23/org/a11y/atspi/accessible/42").unwrap();
        assert_eq!(name, ":1.23");
        assert_eq!(path, "/org/a11y/atspi/accessible/42");
        assert!(parse_backend_id("no-slash-here").is_err());
    }

    /// Without a bus (CI, macOS dev hosts) the worker must answer — with an error —
    /// instead of hanging the caller. This is the hang-isolation NFR in miniature.
    #[test]
    fn snapshot_without_a_bus_errors_quickly_instead_of_hanging() {
        // Force discovery away from any real session bus the host might have.
        std::env::set_var(
            "AT_SPI_BUS_ADDRESS",
            "unix:path=/nonexistent/shinken-a11y-test",
        );
        let handle = AtspiHandle::spawn();
        let t0 = Instant::now();
        let r = handle.snapshot(None);
        std::env::remove_var("AT_SPI_BUS_ADDRESS");
        assert!(r.is_err(), "no bus must be an error, not a tree");
        let msg = format!("{:#}", r.unwrap_err());
        assert!(msg.contains("AT-SPI bus unavailable"), "got: {msg}");
        assert!(
            t0.elapsed() < REQUEST_TIMEOUT,
            "the dial failure must answer well before the request deadline"
        );
    }
}
