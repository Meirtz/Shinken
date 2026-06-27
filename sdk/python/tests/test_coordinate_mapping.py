from __future__ import annotations

import pytest

import shinken
from shinken.adapters import AnthropicComputerUseAdapter
from shinken.client import _image_payload, _observation_dict


def _frame(
    *,
    source: tuple[int, int, int, int] = (320, 180, 800, 600),
    delivered: tuple[int, int] = (400, 300),
    screen: tuple[int, int] = (1920, 1080),
) -> dict:
    x, y, w, h = source
    dw, dh = delivered
    return {
        "bytes": b"pixels",
        "format": "png",
        "w": dw,
        "h": dh,
        "scope": "window:42",
        "display": {
            "origin": "top-left",
            "w": screen[0],
            "h": screen[1],
            "dpr": 1.0,
            "source_rect": {"x": x, "y": y, "w": w, "h": h},
            "delivered": {"w": dw, "h": dh},
        },
    }


def test_maps_downscaled_cropped_pixels_to_global_action_space():
    frame = _frame()
    assert shinken.map_target_from_observation({"kind": "point_px", "x": 100, "y": 75}, frame) == {
        "kind": "point_px",
        "x": 520,
        "y": 330,
    }


def test_maps_cropped_normalized_endpoints_and_midpoint():
    frame = _frame()
    assert shinken.map_target_from_observation(
        {"kind": "point_norm", "x": 0.5, "y": 0.5}, frame
    ) == {"kind": "point_px", "x": 720, "y": 480}
    assert shinken.map_target_from_observation(
        {"kind": "point_norm", "x": 1.0, "y": 1.0}, frame
    ) == {"kind": "point_px", "x": 1119, "y": 779}


def test_full_screen_unscaled_mapping_preserves_existing_pixel_coordinates():
    frame = _frame(source=(0, 0, 1280, 800), delivered=(1280, 800), screen=(1280, 800))
    assert shinken.map_target_from_observation({"kind": "point_px", "x": 123, "y": 456}, frame) == {
        "kind": "point_px",
        "x": 123,
        "y": 456,
    }


def test_rejects_missing_stale_and_offscreen_coordinate_metadata():
    with pytest.raises(ValueError, match="no coordinate metadata"):
        shinken.map_target_from_observation(
            {"kind": "point_px", "x": 1, "y": 1}, {"w": 10, "h": 10}
        )

    stale = _frame()
    stale["w"] = 401
    with pytest.raises(ValueError, match="width mismatch"):
        shinken.map_target_from_observation({"kind": "point_px", "x": 1, "y": 1}, stale)

    offscreen = _frame(source=(-100, 0, 800, 600))
    with pytest.raises(ValueError, match="outside the actionable display"):
        shinken.map_target_from_observation({"kind": "point_px", "x": 0, "y": 1}, offscreen)


def test_wire_decoders_preserve_scope_and_coordinate_metadata():
    display = _frame()["display"]
    wire = {
        "type": "observation",
        "image": {"ref": "", "w": 400, "h": 300, "scope": "window:42"},
        "display": display,
    }
    for decoded in (_image_payload(wire), _observation_dict(wire)):
        assert decoded["scope"] == "window:42"
        assert decoded["display"] == display


def test_sync_pointer_helper_applies_frame_mapping_before_wire(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        env.click(x=100, y=75, frame=_frame())
        env.click(target={"kind": "point_norm", "x": 0.5, "y": 0.5}, frame=_frame())
        assert {
            "verb": "click",
            "kind": "point_px",
            "x": 520,
            "y": 330,
        } in env.query("state")["clicks"]
        assert {
            "verb": "click",
            "kind": "point_px",
            "x": 720,
            "y": 480,
        } in env.query("state")["clicks"]


def test_step_maps_both_drag_endpoints_and_never_sends_frame(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        result = env.step(
            [
                {
                    "verb": "drag",
                    "target": {"kind": "point_px", "x": 10, "y": 20},
                    "to": {"kind": "point_px", "x": 100, "y": 75},
                    "frame": _frame(),
                }
            ]
        )
        assert result["results"][0]["ok"] is True
        assert env.query("state")["drags"][-1] == {
            "from": {"x": 340, "y": 220},
            "to": {"x": 520, "y": 330},
            "duration_ms": None,
            "button": None,
        }


def test_act_model_maps_the_tool_coordinate_from_the_frame(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        env.act_model(
            AnthropicComputerUseAdapter(),
            {"action": "left_click", "coordinate": [100, 75]},
            frame=_frame(),
        )
        assert env.query("state")["clicks"][-1] == {
            "verb": "click",
            "kind": "point_px",
            "x": 520,
            "y": 330,
        }
