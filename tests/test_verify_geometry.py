from shapely.geometry import LineString, Polygon

from verify_geometry import Feature, _assess


def _feature(geometry):
    return Feature(name="feature", folder=("plan",), fields={}, geometry=geometry)


def test_shape_comparison_accepts_same_line_with_different_vertex_density():
    simple = _feature(LineString([(75.0, 26.0), (75.001, 26.0)]))
    densified = _feature(LineString([
        (75.0, 26.0), (75.0005, 26.0), (75.001, 26.0)]))
    status, detail, distance = _assess(simple, densified, 0.05)
    assert status == "matched"
    assert "Hausdorff" in detail
    assert distance < 0.01


def test_shape_comparison_reports_polygon_overlap_for_changed_geometry():
    expected = _feature(Polygon([
        (75.0, 26.0), (75.001, 26.0), (75.001, 26.001), (75.0, 26.001)]))
    shifted = _feature(Polygon([
        (75.002, 26.0), (75.003, 26.0), (75.003, 26.001), (75.002, 26.001)]))
    status, detail, _ = _assess(expected, shifted, 0.5)
    assert status == "changed"
    assert "Polygon overlap" in detail
