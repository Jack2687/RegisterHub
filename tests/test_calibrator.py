"""Window layout auto-generation & anti-clipping zoom regression tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from core.calibrator import (
    auto_generate_layout,
    build_auto_layout,
    build_zoom_init_script,
    get_window_geometry,
    get_window_layout,
)


def test_build_auto_layout_multi_resolution_symmetry_and_no_overflow() -> None:
    cases = [
        (1536, 864, 0, 0, 1.25, 1920, 1080),
        (1920, 1040, 0, 0, 1.0, 1920, 1080),
        (2560, 1400, 0, 0, 1.0, 2560, 1440),
        (1366, 728, 0, 0, 1.0, 1366, 768),
    ]
    for W, H, X0, Y0, dpi, pw, ph in cases:
        payload = build_auto_layout(W, H, work_x=X0, work_y=Y0, dpi=dpi, phys_w=pw, phys_h=ph)
        qc = payload["quad_presets"]["centered_compact"]["slots"]
        assert qc[0]["x"] >= X0
        assert qc[1]["x"] + qc[1]["width"] <= X0 + W
        assert (qc[0]["x"] - X0) == (X0 + W - (qc[1]["x"] + qc[1]["width"]))

        hw = payload["hex_presets"]["wing_cascade"]["slots"]
        for row in range(3):
            ls, rs = hw[row * 2], hw[row * 2 + 1]
            assert ls["y"] == rs["y"]
            assert ls["x"] + ls["width"] + rs["x"] == 2 * X0 + W
            assert ls["y"] + ls["height"] <= Y0 + H


def test_auto_generate_layout_preserves_mode_and_zoom_script(tmp_path: Path) -> None:
    cfg = tmp_path / "window_geometry.json"
    cfg.write_text(json.dumps({"quad_mode": "edge_2x2", "hex_mode": "edge_3x2"}), encoding="utf-8")
    fake_wa = {"left": 0, "top": 0, "width": 1920, "height": 1040, "dpi": 1.0, "phys_width": 1920, "phys_height": 1080}
    with mock.patch("core.calibrator.get_logical_work_area", return_value=fake_wa):
        out = auto_generate_layout(save_path=cfg)
    assert out["quad_mode"] == "edge_2x2"
    assert out["hex_mode"] == "edge_3x2"
    lay = get_window_layout(0, 4, config_path=cfg)
    assert 0.4 <= lay["zoom"] <= 1.5
    geom = get_window_geometry(0, 1, config_path=cfg)
    assert len(geom) == 4
    js = build_zoom_init_script(lay["zoom"])
    assert f"DEFAULT_ZOOM = {lay['zoom']}" in js
