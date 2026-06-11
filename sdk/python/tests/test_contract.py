"""v0.0.1 contract & release-gate tests (#89).

One consolidated suite that fails on schema/runtime drift across the ACI wire
vocabulary and verifier receipts — plus packaged-vs-repo schema parity.
The human-facing gate is `docs/release-gate.md`. CI runs this as the named
`contract` job.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from shinken import protocol
from shinken.eval import RECEIPT_SCHEMA, VerifierReceipt, check


def _act(verb, **kw):
    return {"type": "action", "call_id": "c", "action": {"verb": verb, **kw}}


# --- ACI wire contract: the implemented vocabulary must validate (fails on drift) ---


@pytest.mark.parametrize(
    "msg",
    [
        {"type": "hello", "v": 0, "client": {"name": "x", "version": "0"}},
        # the authenticated handshake the Rust runtime requires and the client sends must
        # validate against the schema (the schema previously forbade the `token` field)
        {"type": "hello", "v": 0, "client": {"name": "x", "version": "0"}, "token": "shk_abc"},
        _act("start_screencast", fps=10, max_long_edge=640),
        _act("start_screencast", fps=10, resume_stream="sc-old"),
        _act("start_screencast", fps=10, format="jpeg", quality=80),
        _act("start_screencast", fps=10, delta=True),
        _act("start_screencast", fps=10, delta=False),
        _act("stop_screencast"),
        # guest-side readiness query (S8): the cheap boot-time poll
        {"type": "query", "call_id": "q1", "q": "ready"},
        {"type": "query", "call_id": "q1", "q": "platform"},
        # EWMH window enumeration — the Linux "enumerate apps" read primitive
        {"type": "query", "call_id": "q1", "q": "list_windows"},
        # coordinate-tier gesture verbs: drag (target → to) + decomposed button halves
        _act(
            "drag",
            target={"kind": "point_px", "x": 1, "y": 2},
            to={"kind": "point_px", "x": 300, "y": 200},
            duration_ms=250,
            button="left",
        ),
        _act("mouse_down", target={"kind": "point_px", "x": 1, "y": 2}, button="middle"),
        _act("mouse_up"),  # release needs no target (acts at the current position)
        # act-returns-observation: a mutating verb carrying the observe levers
        _act(
            "click",
            target={"kind": "point_px", "x": 1, "y": 2},
            observe={"format": "jpeg", "quality": 80, "max_long_edge": 640, "scope": "screen"},
        ),
        _act("type_text", text="hi", observe={}),
        _act("screenshot", scope="active_window"),
        _act("screenshot", scope="window:0x1f"),
        _act("screenshot", format="jpeg", quality=50),
        _act("screenshot", format="png"),
        # content-negotiated screenshot: offer a previously seen frame_hash
        _act("screenshot", if_none_match="00ff00ff00ff00ff"),
        _act("screenshot", format="jpeg", quality=80, if_none_match="00ff00ff00ff00ff"),
        # a full screenshot observation carrying the frame's raw-pixel hash
        {
            "type": "observation",
            "obs_id": "o",
            "cause": "c1",
            "frame_hash": "00ff00ff00ff00ff",
            "image": {"ref": "x", "w": 8, "h": 8, "scope": "screen"},
        },
        # the compact not_modified answer: cause + frame_hash, NO payload
        {
            "type": "observation",
            "obs_id": "o",
            "cause": "c1",
            "not_modified": True,
            "frame_hash": "00ff00ff00ff00ff",
        },
        # structured observation (M1b): observe request + element verbs
        _act("observe", structured=True),
        _act("observe", structured=True, diff=True, settle_ms=120),
        _act("observe"),
        _act("invoke_action", target={"kind": "element_ref", "ref": "e7"}, text="click"),
        _act("invoke_action", target={"kind": "element_ref", "ref": "e7"}),
        _act("set_value", target={"kind": "element_ref", "ref": "e3"}, text="hello"),
        _act("click", target={"kind": "element_ref", "ref": "e2"}),
        # the structured observation reply: tree_text + full elements + revision
        {
            "type": "observation",
            "obs_id": "obs-c1",
            "cause": "c1",
            "tree": "full",
            "tree_text": 'app: zenity  (revision 1, 2 nodes)\ne1 frame "Win"\nfocus: (none)',
            "revision": 1,
            "node_count": 2,
            "capture_ms": 12.5,
            "elements": [
                {"ref": "e1", "role": "frame", "name": "Win", "bbox": [0, 0, 800, 600]},
                {
                    "ref": "e2",
                    "role": "push button",
                    "name": "OK",
                    "bbox": [10, 10, 80, 30],
                    "states": ["enabled"],
                    "actions": ["click"],
                    "focused": True,
                    "source": "atspi",
                },
            ],
        },
        # …and the diff form, carrying diff_of + focus
        {
            "type": "observation",
            "obs_id": "obs-c2",
            "cause": "c2",
            "tree": "diff",
            "tree_text": "app: zenity  (revision 2, diff of revision 1)\n~ e2 …\nfocus: e2",
            "revision": 2,
            "diff_of": 1,
            "focus": "e2",
            "node_count": 2,
            "capture_ms": 3.0,
            "elements": [],
        },
        {
            "type": "observation",
            "obs_id": "o",
            "stream": "s",
            "seq": 0,
            "image": {"ref": "x", "w": 8, "h": 8, "scope": "screen"},
        },
        {
            "type": "observation",
            "obs_id": "o",
            "stream": "s",
            "seq": 0,
            "image": {"ref": "x", "w": 8, "h": 8, "scope": "screen", "format": "jpeg"},
        },
        # a dirty-tile delta frame: tiles INSTEAD of image (B2)
        {
            "type": "observation",
            "obs_id": "o",
            "stream": "s",
            "seq": 1,
            "tiles": [
                {"x": 0, "y": 0, "w": 64, "h": 64, "ref": "abc"},
                {"x": 64, "y": 64, "w": 36, "h": 6, "ref": "def"},  # edge tile
            ],
        },
        # welcome advertising the codec + act-returns-observation capabilities
        {
            "type": "welcome",
            "v": 0,
            "server": {"name": "shinkend", "version": "0", "platform": "linux"},
            "capabilities": {
                "schema_version": 0,
                "verbs": ["click", "drag", "mouse_down", "mouse_up"],
                "targets": ["point_px"],
                "observation_types": ["screenshot"],
                "max_long_edge": 2576,
                "image_formats": ["png", "jpeg"],
                "observe_after_act": True,
            },
        },
        # welcome advertising content-negotiated screenshots (frame_dedup)
        {
            "type": "welcome",
            "v": 0,
            "server": {"name": "shinkend", "version": "0", "platform": "linux"},
            "capabilities": {
                "schema_version": 0,
                "verbs": ["click"],
                "targets": ["point_px"],
                "observation_types": ["screenshot"],
                "max_long_edge": 2576,
                "image_formats": ["png", "jpeg"],
                "binary_frames": True,
                "frame_dedup": True,
            },
        },
    ],
)
def test_aci_wire_vocab_validates(msg):
    protocol.validate(msg)


@pytest.mark.parametrize(
    "msg",
    [
        _act("teleport"),
        # the query vocabulary is a closed enum — drift fails loudly
        {"type": "query", "call_id": "q1", "q": "healthz"},
        _act("screenshot", scope="window:bad"),
        # drag requires BOTH endpoints; button names are a closed enum
        _act("drag", target={"kind": "point_px", "x": 1, "y": 2}),
        _act(
            "drag",
            target={"kind": "point_px", "x": 1, "y": 2},
            to={"kind": "point_px", "x": 3, "y": 4},
            button="wheel",
        ),
        _act("mouse_down", button="Left"),  # names are lowercase, exactly
        # observe admits only mutating verbs and only the screenshot-shaped keys
        _act("screenshot", observe={}),
        _act("wait", ms=10, observe={}),
        _act("start_screencast", fps=5, observe={}),
        _act("click", target={"kind": "point_px", "x": 1, "y": 2}, observe={"fps": 30}),
        _act(
            "click",
            target={"kind": "point_px", "x": 1, "y": 2},
            observe={"format": "webp"},
        ),
        _act(
            "click",
            target={"kind": "point_px", "x": 1, "y": 2},
            observe={"quality": 0},
        ),
        _act("start_screencast", resume_stream=7),  # must be a stream id string
        # codec contract: enum is exactly png|jpeg; quality bounded 1-100 (the runtime
        # REJECTS out-of-range rather than clamping — schema and runtime must agree)
        _act("screenshot", format="webp"),
        _act("screenshot", format="jpg"),
        _act("screenshot", format="jpeg", quality=0),
        _act("screenshot", format="jpeg", quality=101),
        # delta is a strict boolean, not truthy-anything
        _act("start_screencast", delta="yes"),
        _act("start_screencast", delta=1),
        # if_none_match is a hash STRING, never a number/bool
        _act("screenshot", if_none_match=7),
        _act("screenshot", if_none_match=True),
        # not_modified is const true — a false value is not a wire shape
        {
            "type": "observation",
            "obs_id": "o",
            "cause": "c1",
            "not_modified": False,
            "frame_hash": "00ff00ff00ff00ff",
        },
        # not_modified must carry frame_hash (the client's cache key) ...
        {"type": "observation", "obs_id": "o", "cause": "c1", "not_modified": True},
        # ... and cause (it answers a one-shot screenshot, never a stream frame)
        {"type": "observation", "obs_id": "o", "not_modified": True, "frame_hash": "00ff"},
        # not_modified means NO payload — image/tiles are contradictions
        {
            "type": "observation",
            "obs_id": "o",
            "cause": "c1",
            "not_modified": True,
            "frame_hash": "00ff00ff00ff00ff",
            "image": {"ref": "x", "w": 8, "h": 8},
        },
        # element verbs: set_value requires text, both require a target
        _act("set_value", target={"kind": "element_ref", "ref": "e3"}),
        _act("invoke_action"),
        _act("set_value", text="orphan value"),
        # observe knobs are strictly typed
        _act("observe", structured="yes"),
        _act("observe", settle_ms=-5),
        # observe knobs are gated to the observe verb
        _act("click", target={"kind": "point_px", "x": 1, "y": 2}, structured=True),
        _act("screenshot", diff=True),
        # a tile requires all of x/y/w/h/ref and admits nothing else
        {
            "type": "observation",
            "obs_id": "o",
            "stream": "s",
            "seq": 0,
            "tiles": [{"x": 0, "y": 0, "w": 64, "h": 64}],  # missing ref
        },
        {
            "type": "observation",
            "obs_id": "o",
            "stream": "s",
            "seq": 0,
            "tiles": [{"x": 0, "y": 0, "w": 64, "h": 64, "ref": "abc", "format": "png"}],
        },
        {  # tiles must not be empty — an unchanged frame is suppressed, not sent
            "type": "observation",
            "obs_id": "o",
            "stream": "s",
            "seq": 0,
            "tiles": [],
        },
        {  # tiles INSTEAD of image — never both
            "type": "observation",
            "obs_id": "o",
            "stream": "s",
            "seq": 0,
            "image": {"ref": "x", "w": 8, "h": 8},
            "tiles": [{"x": 0, "y": 0, "w": 64, "h": 64, "ref": "abc"}],
        },
        {  # tiles only make sense within a stream (relative to its keyframe)
            "type": "observation",
            "obs_id": "o",
            "tiles": [{"x": 0, "y": 0, "w": 64, "h": 64, "ref": "abc"}],
        },
        {  # image_formats admits only the schema's ImageFormat enum
            "type": "welcome",
            "v": 0,
            "server": {"name": "s", "version": "0", "platform": "linux"},
            "capabilities": {
                "schema_version": 0,
                "verbs": [],
                "targets": [],
                "observation_types": [],
                "image_formats": ["png", "webp"],
            },
        },
    ],
)
def test_aci_invalid_rejected(msg):
    with pytest.raises(jsonschema.ValidationError):
        protocol.validate(msg)


# --- binary media framing contract ---
# The binary frame's JSON header is a $def (NOT a top-level text message): validate
# header instances against $defs.BinaryFrameHeader directly, so the Rust emitter and
# the SDK parser share one published shape.


def _validate_binary_header(header):
    schema = dict(protocol.aci_schema())
    jsonschema.validate(header, {"$ref": "#/$defs/BinaryFrameHeader", "$defs": schema["$defs"]})


@pytest.mark.parametrize(
    "header",
    [
        # one-shot screenshot: cause, image with off/len
        {
            "type": "observation",
            "obs_id": "obs-c1",
            "cause": "c1",
            "image": {"off": 0, "len": 1234, "w": 8, "h": 8, "scope": "screen", "format": "jpeg"},
        },
        # one-shot screenshot header carrying the raw-pixel frame_hash (frame_dedup)
        {
            "type": "observation",
            "obs_id": "obs-c1",
            "cause": "c1",
            "frame_hash": "00ff00ff00ff00ff",
            "image": {"off": 0, "len": 1234, "w": 8, "h": 8, "scope": "screen", "format": "png"},
        },
        # stream keyframe
        {
            "type": "observation",
            "obs_id": "s-0",
            "stream": "s",
            "seq": 0,
            "image": {"off": 0, "len": 9, "w": 8, "h": 8},
        },
        # dirty-tile frame: contiguous off/len per tile
        {
            "type": "observation",
            "obs_id": "s-1",
            "stream": "s",
            "seq": 1,
            "tiles": [
                {"x": 0, "y": 0, "w": 64, "h": 64, "off": 0, "len": 900},
                {"x": 64, "y": 64, "w": 36, "h": 6, "off": 900, "len": 120},
            ],
        },
    ],
)
def test_binary_frame_header_validates(header):
    _validate_binary_header(header)


@pytest.mark.parametrize(
    "header",
    [
        # a base64 ref does not belong in a binary header
        {
            "type": "observation",
            "obs_id": "o",
            "stream": "s",
            "seq": 0,
            "image": {"ref": "x", "w": 8, "h": 8},
        },
        # a binary tile requires off/len, admits nothing else
        {
            "type": "observation",
            "obs_id": "o",
            "stream": "s",
            "seq": 0,
            "tiles": [{"x": 0, "y": 0, "w": 64, "h": 64}],
        },
        # tiles INSTEAD of image — never both
        {
            "type": "observation",
            "obs_id": "o",
            "stream": "s",
            "seq": 0,
            "image": {"off": 0, "len": 1, "w": 8, "h": 8},
            "tiles": [{"x": 0, "y": 0, "w": 64, "h": 64, "off": 0, "len": 1}],
        },
        # a stream frame still requires seq
        {
            "type": "observation",
            "obs_id": "o",
            "stream": "s",
            "image": {"off": 0, "len": 1, "w": 8, "h": 8},
        },
    ],
)
def test_binary_frame_header_invalid_rejected(header):
    with pytest.raises(jsonschema.ValidationError):
        _validate_binary_header(header)


def test_binary_negotiation_fields_validate():
    # hello offering binary framing
    protocol.validate(
        {
            "type": "hello",
            "v": 0,
            "client": {"name": "x", "version": "0"},
            "accept": {"binary_frames": True},
        }
    )
    # welcome advertising it
    protocol.validate(
        {
            "type": "welcome",
            "v": 0,
            "server": {"name": "shinkend", "version": "0", "platform": "linux"},
            "capabilities": {
                "schema_version": 0,
                "verbs": ["click"],
                "targets": ["point_px"],
                "observation_types": ["screenshot"],
                "max_long_edge": 2576,
                "image_formats": ["png", "jpeg"],
                "binary_frames": True,
            },
        }
    )


# --- verifier receipt contract ---


def test_verifier_receipt_contract():
    r = VerifierReceipt.from_checks([check("c", True, {"e": 1})])
    jsonschema.validate(r.to_dict(), RECEIPT_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"passed": "no", "checks": []}, RECEIPT_SCHEMA)


# --- packaged schemas == repo source-of-truth (no wheel/repo drift) ---


def test_packaged_schemas_match_repo():
    repo = Path(__file__).resolve().parents[3] / "schema"
    if not repo.exists():  # running from a wheel install — repo source not present
        pytest.skip("repo schema/ not present")
    pkg = Path(__file__).resolve().parents[1] / "src" / "shinken" / "schemas"
    name = "aci.schema.json"
    assert json.loads((repo / name).read_text()) == json.loads((pkg / name).read_text()), (
        f"{name}: packaged copy drifted from repo source-of-truth"
    )
