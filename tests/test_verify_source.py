import xml.etree.ElementTree as ET

import ezdxf
import pytest

import converter
from verify_source import build_reference


def test_reference_uses_selected_layer_and_does_not_write_beside_the_dxf(tmp_path):
    path = converter.create_demo(tmp_path)
    source_bytes = path.read_bytes()
    entries = set(tmp_path.iterdir())
    options = converter.Options(str(path), source_crs="EPSG:32643", location_confirmed=True,
                                layers=("Year 2",))
    kml, warnings = build_reference(options)
    root = ET.fromstring(kml)
    assert [el.text for el in root.findall(".//{*}Folder/{*}name")] == ["Year 2"]
    assert len(root.findall(".//{*}Placemark")) == 1
    assert warnings == []
    assert set(tmp_path.iterdir()) == entries
    assert path.read_bytes() == source_bytes


def test_unsupported_source_objects_are_reported_even_if_the_overlay_matches(tmp_path):
    path = tmp_path / "ray.dxf"
    drawing = ezdxf.new()
    drawing.modelspace().add_point((73, 24))
    drawing.modelspace().add_ray((73, 24), (1, 0))
    drawing.saveas(path)
    _, warnings = build_reference(converter.Options(str(path), source_crs="EPSG:4326",
                                                    location_confirmed=True))
    assert any("RAY (1)" in warning for warning in warnings)


def test_submission_reference_applies_member_filters_even_without_selected_plan_ids(tmp_path):
    path = tmp_path / "plans.dxf"
    drawing = ezdxf.new()
    drawing.layers.new("ROUTES")
    model = drawing.modelspace()
    shape = [(0, .01), (.02, .01), (.02, 0), (0, 0), (0, .01)]
    for xoff in (1000, 1100):
        model.add_lwpolyline([(x + xoff, y + 1000) for x, y in shape],
                             dxfattribs={"layer": "ROUTES"})
        model.add_lwpolyline([(xoff - .01, 999.99), (xoff + .03, 999.99),
                              (xoff + .03, 1000.02), (xoff - .01, 1000.02)], close=True)
        model.add_line((xoff + .003, 1000.003), (xoff + .017, 1000.007))
    for x, y in shape[:-1]:
        model.add_point((73.7 + x, 24.58 + y))
    drawing.saveas(path)
    options = converter.Options(str(path), source_crs="EPSG:4326", location_confirmed=True,
                                plan_margin=.02)
    kml, _ = build_reference(options, submission_filename="plan 1.KML")
    root = ET.fromstring(kml)
    names = [el.text for el in root.findall(".//{*}Folder/{*}name")]
    assert "Plan 1" in names and "Plan 2" not in names
    assert len(root.findall(".//{*}Placemark")) == 1
    assert list(tmp_path.iterdir()) == [path]
    with pytest.raises(ValueError, match="No submission group"):
        build_reference(options, submission_filename="nonexistent.kmz")
