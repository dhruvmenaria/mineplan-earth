import json

import pytest
from pyproj import Transformer

import converter
import coordinate_controls


def _known_points(bad_last=False):
    source = [(0, 0), (100, 0), (100, 80), (0, 80)]
    scale, rotation = 1.0002, 7.5
    offset_x, offset_y = 500000.0, 3000000.0
    inverse = Transformer.from_crs(32643, 4326, always_xy=True)
    points = []
    for index, (x, y) in enumerate(source):
        east, north = coordinate_controls.apply_similarity(
            x, y, scale, rotation, offset_x, offset_y)
        if bad_last and index == len(source) - 1:
            east += 20
        longitude, latitude = inverse.transform(east, north)
        points.append(coordinate_controls.ControlPoint(
            name=chr(65 + index), source_x=x, source_y=y,
            latitude=latitude, longitude=longitude, precision_metres=0.01))
    return points


def test_parse_decimal_and_dms_controls():
    text = (
        "name,source_x,source_y,latitude,longitude\n"
        'A,10,20,N26°44\'56.20",E75°13\'37.99"\n'
        "B,30,40,26.7489444444,75.2272194444\n")
    points = coordinate_controls.parse_control_text(text)
    assert len(points) == 2
    assert points[0].latitude == pytest.approx(26 + 44 / 60 + 56.2 / 3600)
    assert points[0].longitude == pytest.approx(75 + 13 / 60 + 37.99 / 3600)
    assert points[0].precision_metres == pytest.approx(0.3092)


def test_similarity_fit_recovers_known_transformation():
    report = coordinate_controls.solve(_known_points(), "EPSG:32643")
    assert report["status"] == "pass"
    assert report["scale"] == pytest.approx(1.0002, abs=1e-10)
    assert report["rotation_degrees"] == pytest.approx(7.5, abs=1e-9)
    assert report["offset_x"] == pytest.approx(500000, abs=1e-7)
    assert report["offset_y"] == pytest.approx(3000000, abs=1e-7)
    assert report["rms_error_metres"] < 1e-7


def test_two_point_exact_fit_requires_review_because_it_has_no_redundancy():
    report = coordinate_controls.solve(_known_points()[:2], "EPSG:32643")
    assert report["rms_error_metres"] < 1e-7
    assert report["status"] == "review"
    assert "no redundant" in report["quality_notes"][0]


def test_last_control_can_be_an_independent_holdout():
    report = coordinate_controls.solve(
        _known_points(), "EPSG:32643", holdout_index=3)
    assert report["status"] == "pass"
    assert report["fit_point_count"] == 3
    assert report["controls"][3]["role"] == "independent_verification"
    assert report["holdout_error_metres"] < 1e-6


def test_plan_alignment_correction_prevents_double_transformation():
    report = coordinate_controls.solve(_known_points(), "EPSG:32643")
    angle = __import__("math").radians(report["rotation_degrees"])
    real = report["scale"] * __import__("math").cos(angle)
    imag = report["scale"] * __import__("math").sin(angle)
    existing = (real, -imag, imag, real, report["offset_x"], report["offset_y"])
    correction = coordinate_controls.correction_after_existing_affine(report, existing)
    assert correction["scale"] == pytest.approx(1.0, abs=1e-12)
    assert correction["rotation_degrees"] == pytest.approx(0.0, abs=1e-10)
    assert correction["offset_x"] == pytest.approx(0.0, abs=1e-7)
    assert correction["offset_y"] == pytest.approx(0.0, abs=1e-7)


def test_bad_control_fails_and_is_reported_as_possible_outlier():
    report = coordinate_controls.solve(_known_points(bad_last=True), "EPSG:32643")
    assert report["status"] == "fail"
    assert report["suspected_outlier"]["name"] == "D"
    assert report["suspected_outlier"]["predicted_error_metres"] > 10


def test_export_preflight_rejects_changed_calibration_settings(tmp_path):
    source = tmp_path / "drawing.dxf"
    source.write_text("test", encoding="utf-8")
    report = coordinate_controls.solve(_known_points(), "EPSG:32643")
    options = converter.Options(
        input_path=str(source), source_crs="EPSG:32643",
        coordinate_scale=report["scale"], rotation_degrees=report["rotation_degrees"],
        offset_x=report["offset_x"] + 1, offset_y=report["offset_y"],
        coordinate_control_json=json.dumps(report))
    with pytest.raises(ValueError, match="changed after calibration"):
        converter._coordinate_control_preflight(options)


def test_detected_controls_pair_boundary_and_geographic_targets():
    detection = {
        "plans": [{"boundary": [(1, 2), (3, 4)]}],
        "control_points": [
            {"latitude": 26.1, "longitude": 75.1},
            {"latitude": 26.2, "longitude": 75.2},
        ],
    }
    points = coordinate_controls.detected_control_points(detection)
    assert [(point.source_x, point.source_y) for point in points] == [(1, 2), (3, 4)]
    assert points[1].latitude == 26.2
