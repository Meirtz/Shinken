//! Structured-observation core (D3) — stable element identity, legible serialization,
//! and diff rendering over an accessibility tree.
//!
//! This module is deliberately **pure**: it never talks to a bus. A backend (the
//! AT-SPI worker in [`crate::atspi_source`], a fake in tests) implements
//! [`TreeSource`] and hands over a flattened pre-order [`RawTree`]; everything
//! valuable — the monotonically-minted id map, the numbered indented text the model
//! reads, the `~`/`+`/`-` diff against the previous revision — happens here and is
//! unit-testable without AT-SPI.
//!
//! ## Identity (and its honest limits)
//!
//! Ids (`e1`, `e2`, …) are minted **per session**, monotonically, and keyed to a
//! composite identity heuristic: the backend node reference (for AT-SPI: bus name +
//! D-Bus object path) + the node's role + its parent's reference. While an element
//! stays alive across re-observations it keeps its id; when it disappears its key is
//! evicted and the id is **never reused** (the counter never rewinds), so a stale id
//! can be answered with a machine-readable `stale_element_ref` error instead of
//! silently hitting a different element. Limits, honestly: AT-SPI object paths are
//! unique per live object for the toolkits we measured (GTK/Qt), but a toolkit MAY
//! recycle a path after destroying an object — a recycled path with the same role
//! under the same parent is indistinguishable from the original and keeps its id.
//! Role+parent in the key catches the common recycle case (same path, different
//! widget kind/location); content changes (name/value/states/bbox) deliberately do
//! NOT change identity — they render as `~` changed lines instead.

use std::collections::HashMap;

use anyhow::Result;

/// One accessibility node, flattened in pre-order by the backend walk.
#[derive(Debug, Clone, Default)]
pub struct RawNode {
    /// Backend identity of this node (AT-SPI: `<bus name><object path>`). Stable for
    /// the lifetime of the underlying object; the identity heuristic builds on it.
    pub backend_id: String,
    /// The parent's `backend_id` (`None` for the root) — part of the identity key.
    pub parent_backend_id: Option<String>,
    /// Depth in the walked tree (root = 0); drives the rendered indentation.
    pub depth: u16,
    pub role: String,
    pub name: String,
    pub description: String,
    /// Current value, when the node exposes one (AT-SPI Value/Text interfaces).
    pub value: Option<String>,
    /// Agent-relevant states (focused/enabled/selected/expanded/editable, …).
    pub states: Vec<String>,
    /// Action names the node advertises (AT-SPI Action interface).
    pub actions: Vec<String>,
    /// On-screen bounds `[x, y, w, h]` in screen pixels (Component interface).
    pub bbox: Option<[i32; 4]>,
    /// Whether the node currently holds keyboard focus.
    pub focused: bool,
}

/// One full tree capture: the focused app's nodes in pre-order, plus labels.
#[derive(Debug, Clone, Default)]
pub struct RawTree {
    /// Application name (the AT-SPI app the walk targeted, or `desktop` for all).
    pub app: String,
    /// Title of the active window, when one was identified.
    pub window: String,
    pub nodes: Vec<RawNode>,
    /// True when a cap (node count / depth / time budget) cut the walk short.
    pub truncated: bool,
}

/// Anything that can produce accessibility snapshots and actuate nodes. The live
/// implementation is the AT-SPI worker ([`crate::atspi_source::AtspiHandle`]); tests
/// use a fake, which is what keeps the identity/diff/serialization core testable
/// without a bus.
pub trait TreeSource: Send + Sync {
    /// Capture the current tree. `settle_ms` asks the backend to debounce a11y
    /// event notifications for that quiesce window (bounded) before walking.
    fn snapshot(&self, settle_ms: Option<u64>) -> Result<RawTree>;
    /// Invoke a node's AT-SPI action by name (`None` = the node's first action).
    fn invoke(&self, backend_id: &str, action: Option<&str>) -> Result<String>;
    /// Set a node's value via the Value (numeric) or EditableText interface.
    fn set_value(&self, backend_id: &str, value: &str) -> Result<String>;
}

/// Stable prefix of every machine-readable stale-element error. A client that gets
/// an ack whose error starts with this knows the fix is: re-observe, then retry
/// with a fresh ref. (Documented in docs/design/aci-spec.md §4.)
pub const STALE_ELEMENT_PREFIX: &str = "stale_element_ref:";

/// Default diff line budget: a diff whose body exceeds this many lines falls back
/// to the full tree (at that size the full render reads better than a wall of `~`).
/// Configurable via `SHINKEND_DIFF_BUDGET`.
pub const DEFAULT_DIFF_BUDGET: usize = 200;

fn diff_budget_from_env() -> usize {
    std::env::var("SHINKEND_DIFF_BUDGET")
        .ok()
        .and_then(|v| v.trim().parse::<usize>().ok())
        .filter(|&n| n > 0)
        .unwrap_or(DEFAULT_DIFF_BUDGET)
}

/// What the session remembers about one live element — enough to resolve an
/// `element_ref` to a click point or a backend node for `invoke_action`/`set_value`.
#[derive(Debug, Clone)]
pub struct ElementRecord {
    pub backend_id: String,
    pub bbox: Option<[i32; 4]>,
}

/// One rendered observation, ready to go on the wire.
#[derive(Debug, Clone)]
pub struct Rendered {
    pub revision: u64,
    /// The revision this diff was rendered against (`None` on a full render).
    pub diff_of: Option<u64>,
    /// `"full"` or `"diff"` — what `text` contains.
    pub tree_kind: &'static str,
    /// The legible serialization (numbered indented lines / diff lines).
    pub text: String,
    /// The raw structured array (ACI `Element` objects) for tooling.
    pub elements: serde_json::Value,
    /// `element_ref` of the focused node, when one is focused.
    pub focus: Option<String>,
    pub node_count: usize,
}

impl Rendered {
    /// Serialize this observation as the ACI `observation` reply to `call_id`.
    pub fn to_observation_text(&self, call_id: &str, capture_ms: f64) -> String {
        let mut msg = serde_json::json!({
            "type": "observation",
            "obs_id": format!("obs-{call_id}"),
            "cause": call_id,
            "tree": self.tree_kind,
            "tree_text": self.text,
            "revision": self.revision,
            "node_count": self.node_count,
            "capture_ms": capture_ms,
            "elements": self.elements,
        });
        if let Some(d) = self.diff_of {
            msg["diff_of"] = d.into();
        }
        if let Some(f) = &self.focus {
            msg["focus"] = f.as_str().into();
        }
        serde_json::to_string(&msg).unwrap_or_else(|e| {
            crate::protocol::error_result_text(call_id, &format!("encode observation: {e}"))
        })
    }
}

/// A previously rendered per-element line: `(id, depth, content)`. Content excludes
/// the indentation so the diff compares semantics, not whitespace shifts.
type Line = (u64, u16, String);

/// Per-session structured-observation state: the identity map, the last rendered
/// revision (the diff baseline), and the live `element_ref` index.
#[derive(Default)]
pub struct ObserveState {
    next_id: u64,
    /// identity key → id, for elements alive in the LAST snapshot only.
    ids: HashMap<String, u64>,
    /// id → record, for elements alive in the LAST snapshot only (stale ids resolve
    /// to a machine-readable error, never to a guess).
    live: HashMap<u64, ElementRecord>,
    revision: u64,
    last_lines: Option<Vec<Line>>,
    /// True once at least one observation has been taken on this session.
    observed: bool,
}

/// The composite identity heuristic (see the module docs for its limits).
fn identity_key(node: &RawNode) -> String {
    format!(
        "{}\u{1f}{}\u{1f}{}",
        node.parent_backend_id.as_deref().unwrap_or(""),
        node.role,
        node.backend_id
    )
}

/// Render one node's line content (everything after the indentation):
/// `e<id> <role> [(states)] ["title"] [Value:"…"] [Actions:[a,b]]`.
fn render_line(id: u64, node: &RawNode) -> String {
    let mut s = format!(
        "e{id} {}",
        if node.role.is_empty() {
            "unknown"
        } else {
            &node.role
        }
    );
    if !node.states.is_empty() {
        s.push_str(&format!(" ({})", node.states.join(",")));
    }
    if !node.name.trim().is_empty() {
        s.push_str(&format!(" {:?}", node.name));
    }
    if let Some(v) = &node.value {
        s.push_str(&format!(" Value:{v:?}"));
    }
    if !node.actions.is_empty() {
        s.push_str(&format!(" Actions:[{}]", node.actions.join(",")));
    }
    s
}

/// Collapse a sorted id list into a compact range summary: `e3, e7-e9`.
fn summarize_ids(mut ids: Vec<u64>) -> String {
    ids.sort_unstable();
    let mut parts: Vec<String> = Vec::new();
    let mut i = 0;
    while i < ids.len() {
        let start = ids[i];
        let mut end = start;
        while i + 1 < ids.len() && ids[i + 1] == end + 1 {
            end = ids[i + 1];
            i += 1;
        }
        parts.push(if start == end {
            format!("e{start}")
        } else {
            format!("e{start}-e{end}")
        });
        i += 1;
    }
    parts.join(", ")
}

fn indent(depth: u16) -> String {
    "  ".repeat(depth as usize)
}

impl ObserveState {
    /// Ingest one snapshot: assign stable ids, refresh the live element index,
    /// bump the revision, and render — a full tree, or (when `diff` is set and a
    /// baseline exists) a diff against the previous revision under the line budget.
    pub fn observe(&mut self, tree: &RawTree, diff: bool) -> Rendered {
        let prev_revision = self.revision;
        self.revision += 1;
        self.observed = true;

        // ---- identity: reuse live ids, mint for new keys, evict the vanished ----
        let mut ids = HashMap::with_capacity(tree.nodes.len());
        let mut live = HashMap::with_capacity(tree.nodes.len());
        let mut lines: Vec<Line> = Vec::with_capacity(tree.nodes.len());
        let mut elements: Vec<serde_json::Value> = Vec::with_capacity(tree.nodes.len());
        let mut focus: Option<String> = None;
        for node in &tree.nodes {
            let key = identity_key(node);
            // A duplicate key within one snapshot (path recycled mid-tree — should
            // not happen, but never trust a bus) gets a fresh id rather than
            // aliasing two on-screen nodes onto one ref.
            let id = match self.ids.get(&key) {
                Some(&prev) if !ids.contains_key(&key) => prev,
                _ => {
                    self.next_id += 1;
                    self.next_id
                }
            };
            ids.insert(key, id);
            live.insert(
                id,
                ElementRecord {
                    backend_id: node.backend_id.clone(),
                    bbox: node.bbox,
                },
            );
            if focus.is_none() && node.focused {
                focus = Some(format!("e{id}"));
            }
            lines.push((id, node.depth, render_line(id, node)));
            elements.push(element_json(id, node));
        }
        self.ids = ids;
        self.live = live;

        let header = self.header(tree, None);
        let trailer = match &focus {
            Some(f) => format!("focus: {f}"),
            None => "focus: (none)".to_string(),
        };

        let (tree_kind, diff_of, text) = match (&self.last_lines, diff) {
            (Some(prev), true) => {
                match render_diff(prev, &lines, diff_budget_from_env()) {
                    Some(body) if body.is_empty() => (
                        "diff",
                        Some(prev_revision),
                        format!(
                            "{}\nno change since revision {prev_revision}\n{trailer}",
                            self.header(tree, Some(prev_revision))
                        ),
                    ),
                    Some(body) => (
                        "diff",
                        Some(prev_revision),
                        format!(
                            "{}\n{}\n{trailer}",
                            self.header(tree, Some(prev_revision)),
                            body.join("\n")
                        ),
                    ),
                    // Over the line budget: the full tree reads better.
                    None => ("full", None, render_full(&header, &lines, &trailer)),
                }
            }
            _ => ("full", None, render_full(&header, &lines, &trailer)),
        };
        self.last_lines = Some(lines);

        Rendered {
            revision: self.revision,
            diff_of,
            tree_kind,
            text,
            elements: serde_json::Value::Array(elements),
            focus,
            node_count: tree.nodes.len(),
        }
    }

    fn header(&self, tree: &RawTree, diff_of: Option<u64>) -> String {
        let mut h = format!(
            "app: {}",
            if tree.app.is_empty() {
                "(unknown)"
            } else {
                &tree.app
            }
        );
        if !tree.window.is_empty() {
            h.push_str(&format!("  window: {:?}", tree.window));
        }
        match diff_of {
            Some(d) => h.push_str(&format!(
                "  (revision {}, diff of revision {d})",
                self.revision
            )),
            None => h.push_str(&format!(
                "  (revision {}, {} nodes{})",
                self.revision,
                self.live.len(),
                if tree.truncated { ", truncated" } else { "" }
            )),
        }
        h
    }

    /// Resolve an `element_ref` (`"e12"`) against the LAST observation. Errors are
    /// machine-readable: a stale/unknown ref starts with [`STALE_ELEMENT_PREFIX`].
    pub fn resolve(&self, element_ref: &str) -> Result<&ElementRecord, String> {
        if !self.observed {
            return Err(format!(
                "{STALE_ELEMENT_PREFIX} no structured observation on this session yet \
                 (send an `observe` action first)"
            ));
        }
        let id: u64 = element_ref
            .strip_prefix('e')
            .and_then(|n| n.parse().ok())
            .ok_or_else(|| {
                format!(
                    "{STALE_ELEMENT_PREFIX} malformed element_ref {element_ref:?} (expected e<N>)"
                )
            })?;
        self.live.get(&id).ok_or_else(|| {
            format!(
                "{STALE_ELEMENT_PREFIX} {element_ref} is not in the live tree \
                 (it disappeared or was never minted) — re-observe and retry"
            )
        })
    }

    /// Resolve an `element_ref` to its bbox-centre click point (screen px).
    pub fn resolve_point(&self, element_ref: &str) -> Result<(i32, i32), String> {
        let rec = self.resolve(element_ref)?;
        match rec.bbox {
            Some([x, y, w, h]) if w > 0 && h > 0 => Ok((x + w / 2, y + h / 2)),
            _ => Err(format!(
                "element_ref {element_ref} has no usable on-screen bounds; \
                 re-observe, or use invoke_action (the AX path needs no geometry)"
            )),
        }
    }
}

fn render_full(header: &str, lines: &[Line], trailer: &str) -> String {
    let mut out = String::with_capacity(lines.len() * 48 + header.len() + trailer.len() + 2);
    out.push_str(header);
    for (_, depth, content) in lines {
        out.push('\n');
        out.push_str(&indent(*depth));
        out.push_str(content);
    }
    out.push('\n');
    out.push_str(trailer);
    out
}

/// Body lines of a diff: `~` changed / `+` added / `-` removed (summarized as id
/// ranges). `None` when the body would exceed `budget` (caller falls back to full);
/// `Some(vec![])` when nothing changed.
fn render_diff(prev: &[Line], curr: &[Line], budget: usize) -> Option<Vec<String>> {
    let prev_by_id: HashMap<u64, &String> = prev.iter().map(|(id, _, c)| (*id, c)).collect();
    let curr_ids: std::collections::HashSet<u64> = curr.iter().map(|(id, _, _)| *id).collect();
    let mut body: Vec<String> = Vec::new();
    for (id, depth, content) in curr {
        match prev_by_id.get(id) {
            None => body.push(format!("+ {}{}", indent(*depth), content)),
            Some(old) if *old != content => body.push(format!("~ {}{}", indent(*depth), content)),
            Some(_) => {}
        }
        if body.len() > budget {
            return None;
        }
    }
    let removed: Vec<u64> = prev
        .iter()
        .map(|(id, _, _)| *id)
        .filter(|id| !curr_ids.contains(id))
        .collect();
    if !removed.is_empty() {
        let n = removed.len();
        body.push(format!(
            "- removed: {} ({n} element{})",
            summarize_ids(removed),
            if n == 1 { "" } else { "s" }
        ));
    }
    if body.len() > budget {
        return None;
    }
    Some(body)
}

/// Build the ACI `Element` JSON for one node (`schema/aci.schema.json $defs.Element`).
fn element_json(id: u64, node: &RawNode) -> serde_json::Value {
    let [x, y, w, h] = node.bbox.unwrap_or([0, 0, 0, 0]);
    let mut el = serde_json::json!({
        "ref": format!("e{id}"),
        "role": if node.role.is_empty() { "unknown" } else { &node.role },
        "bbox": [x, y, w, h],
        "source": "atspi",
    });
    if !node.name.trim().is_empty() {
        el["name"] = node.name.as_str().into();
    }
    if !node.description.trim().is_empty() {
        el["description"] = node.description.as_str().into();
    }
    if let Some(v) = &node.value {
        el["value"] = v.as_str().into();
    }
    if !node.states.is_empty() {
        el["states"] = serde_json::json!(node.states);
    }
    if !node.actions.is_empty() {
        el["actions"] = serde_json::json!(node.actions);
    }
    if node.focused {
        el["focused"] = true.into();
    }
    el
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use std::sync::Mutex;

    /// A scripted [`TreeSource`] — the fake that keeps this core testable without a
    /// bus. Also used by the connection-level tests.
    pub struct FakeSource {
        pub tree: Mutex<RawTree>,
        pub invoked: Mutex<Vec<(String, Option<String>)>>,
        pub set: Mutex<Vec<(String, String)>>,
    }

    impl FakeSource {
        pub fn new(tree: RawTree) -> Self {
            Self {
                tree: Mutex::new(tree),
                invoked: Mutex::new(Vec::new()),
                set: Mutex::new(Vec::new()),
            }
        }
    }

    impl TreeSource for FakeSource {
        fn snapshot(&self, _settle_ms: Option<u64>) -> Result<RawTree> {
            Ok(self.tree.lock().unwrap().clone())
        }
        fn invoke(&self, backend_id: &str, action: Option<&str>) -> Result<String> {
            self.invoked
                .lock()
                .unwrap()
                .push((backend_id.to_string(), action.map(str::to_string)));
            Ok(format!(
                "invoked {} on {backend_id}",
                action.unwrap_or("(default)")
            ))
        }
        fn set_value(&self, backend_id: &str, value: &str) -> Result<String> {
            self.set
                .lock()
                .unwrap()
                .push((backend_id.to_string(), value.to_string()));
            Ok(format!("set value on {backend_id}"))
        }
    }

    pub fn node(
        backend_id: &str,
        parent: Option<&str>,
        depth: u16,
        role: &str,
        name: &str,
    ) -> RawNode {
        RawNode {
            backend_id: backend_id.to_string(),
            parent_backend_id: parent.map(str::to_string),
            depth,
            role: role.to_string(),
            name: name.to_string(),
            ..RawNode::default()
        }
    }

    pub fn sample_tree() -> RawTree {
        let mut frame = node(":1.9/f", None, 0, "frame", "Login");
        frame.states = vec!["active".into()];
        let mut button = node(":1.9/b", Some(":1.9/f"), 1, "push button", "OK");
        button.bbox = Some([10, 20, 80, 30]);
        button.actions = vec!["click".into()];
        button.states = vec!["enabled".into(), "focusable".into()];
        let mut entry = node(":1.9/e", Some(":1.9/f"), 1, "entry", "");
        entry.bbox = Some([10, 60, 200, 30]);
        entry.states = vec!["editable".into(), "focused".into()];
        entry.value = Some(String::new());
        entry.focused = true;
        RawTree {
            app: "zenity".to_string(),
            window: "Login".to_string(),
            nodes: vec![frame, button, entry],
            truncated: false,
        }
    }

    #[test]
    fn ids_are_stable_across_reobservation_and_monotonic_for_new_nodes() {
        let mut st = ObserveState::default();
        let tree = sample_tree();
        let r1 = st.observe(&tree, false);
        assert_eq!(r1.revision, 1);
        assert_eq!(r1.node_count, 3);
        let refs1: Vec<String> = r1
            .elements
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e["ref"].as_str().unwrap().to_string())
            .collect();
        assert_eq!(refs1, ["e1", "e2", "e3"]);

        // Same tree again: the SAME ids (stability across re-observations).
        let r2 = st.observe(&tree, false);
        let refs2: Vec<String> = r2
            .elements
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e["ref"].as_str().unwrap().to_string())
            .collect();
        assert_eq!(refs1, refs2);
        assert_eq!(r2.revision, 2);

        // A new sibling appears: existing ids unchanged, the new one minted ABOVE
        // every id ever issued (monotonic, no reuse).
        let mut grown = tree.clone();
        grown
            .nodes
            .push(node(":1.9/x", Some(":1.9/f"), 1, "label", "hint"));
        let r3 = st.observe(&grown, false);
        let refs3: Vec<String> = r3
            .elements
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e["ref"].as_str().unwrap().to_string())
            .collect();
        assert_eq!(refs3, ["e1", "e2", "e3", "e4"]);
    }

    #[test]
    fn vanished_ids_are_evicted_and_never_reused() {
        let mut st = ObserveState::default();
        let tree = sample_tree();
        st.observe(&tree, false);
        // Drop the button (e2); its id must become stale and never come back.
        let mut shrunk = tree.clone();
        shrunk.nodes.remove(1);
        st.observe(&shrunk, false);
        let err = st.resolve("e2").unwrap_err();
        assert!(
            err.starts_with(STALE_ELEMENT_PREFIX),
            "stale error must be machine-readable: {err}"
        );
        assert!(err.contains("re-observe"));
        // Re-adding an element with a DIFFERENT backend path mints a fresh id (e4),
        // it does not resurrect e2.
        let mut regrown = shrunk.clone();
        let mut b2 = node(":1.9/b2", Some(":1.9/f"), 1, "push button", "OK");
        b2.bbox = Some([10, 20, 80, 30]);
        regrown.nodes.insert(1, b2);
        let r = st.observe(&regrown, false);
        let refs: Vec<&str> = r
            .elements
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e["ref"].as_str().unwrap())
            .collect();
        assert!(
            refs.contains(&"e4") && !refs.contains(&"e2"),
            "refs: {refs:?}"
        );
    }

    #[test]
    fn same_path_different_role_is_a_different_identity() {
        // The composite key (path + role + parent) catches the common path-recycle
        // case: the same object path reappearing as a different widget kind.
        let mut st = ObserveState::default();
        let t1 = RawTree {
            app: "a".into(),
            window: String::new(),
            nodes: vec![node(":1/p", None, 0, "push button", "Go")],
            truncated: false,
        };
        st.observe(&t1, false);
        let t2 = RawTree {
            app: "a".into(),
            window: String::new(),
            nodes: vec![node(":1/p", None, 0, "label", "Go")],
            truncated: false,
        };
        let r = st.observe(&t2, false);
        assert_eq!(
            r.elements[0]["ref"], "e2",
            "recycled path with a new role must re-mint"
        );
    }

    #[test]
    fn resolve_point_uses_bbox_centre_and_flags_unusable_bounds() {
        let mut st = ObserveState::default();
        st.observe(&sample_tree(), false);
        assert_eq!(st.resolve_point("e2").unwrap(), (50, 35)); // (10+80/2, 20+30/2)
                                                               // The frame (e1) has no bbox: actionable error pointing at the AX path.
        let err = st.resolve_point("e1").unwrap_err();
        assert!(err.contains("no usable on-screen bounds") && err.contains("invoke_action"));
        // Unknown / malformed refs are stale-typed.
        assert!(st
            .resolve_point("e99")
            .unwrap_err()
            .starts_with(STALE_ELEMENT_PREFIX));
        assert!(st
            .resolve_point("button-7")
            .unwrap_err()
            .starts_with(STALE_ELEMENT_PREFIX));
        // And resolving before ANY observation is stale-typed too.
        let fresh = ObserveState::default();
        assert!(fresh
            .resolve("e1")
            .unwrap_err()
            .starts_with(STALE_ELEMENT_PREFIX));
    }

    #[test]
    fn full_render_is_numbered_indented_with_header_and_focus_trailer() {
        let mut st = ObserveState::default();
        let r = st.observe(&sample_tree(), false);
        let lines: Vec<&str> = r.text.lines().collect();
        assert_eq!(
            lines[0],
            "app: zenity  window: \"Login\"  (revision 1, 3 nodes)"
        );
        assert_eq!(lines[1], "e1 frame (active) \"Login\"");
        assert_eq!(
            lines[2],
            "  e2 push button (enabled,focusable) \"OK\" Actions:[click]"
        );
        assert_eq!(lines[3], "  e3 entry (editable,focused) Value:\"\"");
        assert_eq!(lines[4], "focus: e3");
        assert_eq!(r.focus.as_deref(), Some("e3"));
        assert_eq!(r.tree_kind, "full");
        // The raw structured array rides along in the same observation.
        assert_eq!(r.elements[1]["role"], "push button");
        assert_eq!(r.elements[1]["bbox"], serde_json::json!([10, 20, 80, 30]));
        assert_eq!(r.elements[1]["actions"], serde_json::json!(["click"]));
        assert_eq!(r.elements[2]["focused"], true);
        assert_eq!(r.elements[0]["source"], "atspi");
    }

    #[test]
    fn diff_renders_changed_added_removed_and_no_change() {
        let mut st = ObserveState::default();
        let tree = sample_tree();
        st.observe(&tree, true); // first observe: no baseline → full
                                 // 1) value change on the entry → one ~ line
        let mut typed = tree.clone();
        typed.nodes[2].value = Some("hi".to_string());
        let r = st.observe(&typed, true);
        assert_eq!(r.tree_kind, "diff");
        assert_eq!(r.diff_of, Some(1));
        assert!(
            r.text
                .contains("~   e3 entry (editable,focused) Value:\"hi\""),
            "{}",
            r.text
        );
        assert!(
            !r.text.contains("e2 push button"),
            "unchanged lines must not appear"
        );
        // 2) nothing changed → explicit no-change
        let r = st.observe(&typed, true);
        assert!(r.text.contains("no change since revision 2"), "{}", r.text);
        assert_eq!(r.tree_kind, "diff");
        // 3) add one node, remove two → + line and a summarized - line
        let mut mutated = typed.clone();
        mutated
            .nodes
            .push(node(":1.9/n", Some(":1.9/f"), 1, "label", "new"));
        mutated.nodes.remove(2); // entry e3
        mutated.nodes.remove(1); // button e2
        let r = st.observe(&mutated, true);
        assert!(r.text.contains("+   e4 label \"new\""), "{}", r.text);
        assert!(
            r.text.contains("- removed: e2-e3 (2 elements)"),
            "{}",
            r.text
        );
    }

    #[test]
    fn over_budget_diff_falls_back_to_full() {
        std::env::set_var("SHINKEND_DIFF_BUDGET", "5");
        let mut st = ObserveState::default();
        let mk = |val: &str| RawTree {
            app: "a".into(),
            window: String::new(),
            nodes: (0..10)
                .map(|i| {
                    let mut n = node(&format!(":1/n{i}"), None, 0, "label", "x");
                    n.value = Some(val.to_string());
                    n
                })
                .collect(),
            truncated: false,
        };
        st.observe(&mk("a"), true);
        let r = st.observe(&mk("b"), true); // 10 changed lines > budget 5
        std::env::remove_var("SHINKEND_DIFF_BUDGET");
        assert_eq!(r.tree_kind, "full");
        assert_eq!(r.diff_of, None);
        assert!(r.text.contains("(revision 2, 10 nodes)"));
    }

    #[test]
    fn removed_id_ranges_summarize() {
        assert_eq!(summarize_ids(vec![3]), "e3");
        assert_eq!(summarize_ids(vec![9, 7, 8, 3]), "e3, e7-e9");
        assert_eq!(summarize_ids(vec![1, 2, 4, 5, 9]), "e1-e2, e4-e5, e9");
    }

    #[test]
    fn truncated_capture_is_labeled() {
        let mut st = ObserveState::default();
        let mut t = sample_tree();
        t.truncated = true;
        let r = st.observe(&t, false);
        assert!(r.text.lines().next().unwrap().contains("truncated"));
    }

    #[test]
    fn observation_wire_shape_carries_text_and_structured_array() {
        let mut st = ObserveState::default();
        let r = st.observe(&sample_tree(), false);
        let text = r.to_observation_text("c7", 12.5);
        let v: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(v["type"], "observation");
        assert_eq!(v["obs_id"], "obs-c7");
        assert_eq!(v["cause"], "c7");
        assert_eq!(v["tree"], "full");
        assert_eq!(v["revision"], 1);
        assert_eq!(v["node_count"], 3);
        assert_eq!(v["capture_ms"], 12.5);
        assert_eq!(v["focus"], "e3");
        assert!(v["tree_text"].as_str().unwrap().starts_with("app: zenity"));
        assert_eq!(v["elements"].as_array().unwrap().len(), 3);
        assert!(v.get("diff_of").is_none());
        assert!(v.get("image").is_none());
    }
}
