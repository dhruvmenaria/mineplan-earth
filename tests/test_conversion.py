"""Acceptance tests on real DXF bytes with known coordinates, not mocked CAD reads."""
from dataclasses import replace
from pathlib import Path
import json
import xml.etree.ElementTree as ET
import zipfile

import ezdxf
import pytest
import converter as c

N = {"k": c.NS}


@pytest.fixture
def demo(tmp_path):
    return c.create_demo(tmp_path / "inputs")


def options(path, **changes):
    return c.Options(str(path), source_crs="EPSG:32643", location_confirmed=True, **changes)


def test_ground_export_preserves_layers_geometry_and_known_survey_point(demo, tmp_path):
    result = c.convert(options(demo), tmp_path / "plan.kml")
    root = ET.parse(result["output"]).getroot()
    names = {el.find("k:name", N).text for el in root.findall(".//k:Folder", N)}
    assert names == {"Lease Boundary", "Year 1", "Year 2", "Survey Labels"}
    assert len(root.findall(".//k:Polygon", N)) == 4  # three boundaries and the circle
    assert root.findall(".//k:Style/k:LineStyle/k:color", N)
    coords = [list(map(float, p.split(','))) for el in root.findall('.//k:coordinates', N) for p in el.text.split()]
    assert all(len(p) == 2 for p in coords)
    assert any(abs(p[0] - 73.7) < 1e-8 and abs(p[1] - 24.58) < 1e-8 for p in coords)
    assert {v.text for v in root.findall('.//k:altitudeMode', N)} == {'clampToGround'}
    assert json.loads(Path(result['report_path']).read_text())['mineplan_earth']['features_exported'] == 5


def test_kmz_layer_selection_and_absolute_heights(demo, tmp_path):
    o = options(demo, layers=("Year 1",), altitude_mode="absolute", heights_confirmed=True)
    result = c.convert(o, tmp_path / 'year1.kmz')
    with zipfile.ZipFile(result['output']) as z:
        assert z.namelist() == ['doc.kml']
        root = ET.fromstring(z.read('doc.kml'))
    assert [n.text for n in root.findall('.//k:Folder/k:name', N)] == ['Year 1']
    coords = root.find('.//k:coordinates', N).text.split()
    assert all(float(p.split(',')[2]) == pytest.approx(575) for p in coords)
    assert root.find('.//k:altitudeMode', N).text == 'absolute'


@pytest.mark.parametrize('change,match', [
    ({'source_crs':''}, 'coordinate system'),
    ({'location_confirmed':False}, 'Confirm'),
    ({'altitude_mode':'absolute'}, 'height'),
    ({'layers':()}, 'Select'),
    ({'coordinate_scale':0}, 'scale'),
])
def test_invalid_settings_do_not_write_an_export(demo, tmp_path, change, match):
    target = tmp_path / 'invalid.kml'
    with pytest.raises((ValueError, c.cad.ConversionError), match=match):
        c.convert(replace(options(demo), **change), target)
    assert not target.exists()


def test_wrong_crs_outside_world_range_is_rejected(demo, tmp_path):
    with pytest.raises(ValueError, match='longitude/latitude'):
        c.convert(replace(options(demo), source_crs='EPSG:4326'), tmp_path/'wrong.kml')


def test_drawing_preview_does_not_need_crs(demo):
    result = c.preview(c.Options(str(demo), layers=('Lease Boundary',)))
    assert len(result.preview_features) == 1
    assert result.preview_features[0]['geometry'].bounds[0] > 100000


def test_empty_nonexistent_layer_and_invalid_polygon_do_not_destroy_old_output(demo, tmp_path):
    target = tmp_path/'keep.kml'
    target.write_text('previous export')
    with pytest.raises(c.cad.ConversionError):
        c.convert(options(demo, layers=('does not exist',)), target)
    assert target.read_text() == 'previous export'
    assert (tmp_path/'keep.failed.report.json').exists()


def test_labels_can_be_excluded_and_xml_special_characters_are_escaped(tmp_path):
    path = tmp_path/'special.dxf'
    doc = ezdxf.new()
    doc.layers.new('A & B')
    doc.modelspace().add_line((73.7,24.58),(73.71,24.59),dxfattribs={'layer':'A & B'})
    doc.modelspace().add_text('Text <review>',dxfattribs={'insert':(73.7,24.58),'layer':'A & B'})
    doc.saveas(path)
    o = c.Options(str(path), source_crs='EPSG:4326', location_confirmed=True, include_labels=False)
    r = c.convert(o, tmp_path/'escaped.kml')
    root = ET.parse(r['output']).getroot()
    assert root.find('.//k:Folder/k:name',N).text == 'A & B'
    assert len(root.findall('.//k:Placemark',N)) == 1


def test_cancel_before_export(demo,tmp_path):
    token=c.cad.CancellationToken()
    token.cancel()
    with pytest.raises(c.cad.CancelledError):
        c.convert(options(demo),tmp_path/'cancel.kml',cancel=token)
    assert not (tmp_path/'cancel.kml').exists()


def test_geometry_invalid_polygon_rejected():
    from shapely.geometry import Polygon
    with pytest.raises(ValueError, match='invalid geometry'):
        c._check_geometry(Polygon([(73.7,24.5),(73.8,24.6),(73.7,24.6),(73.8,24.5),(73.7,24.5)]))


def test_invalid_closed_dxf_polygon_is_repaired_and_reported(tmp_path):
    path = tmp_path / 'repair.dxf'
    doc = ezdxf.new()
    doc.modelspace().add_lwpolyline(
        [(73.70, 24.50), (73.71, 24.51), (73.70, 24.51), (73.71, 24.50)],
        close=True)
    doc.saveas(path)
    opts = c.Options(str(path), source_crs='EPSG:4326', location_confirmed=True)
    result = c.convert(opts, tmp_path / 'repair.kml')
    assert Path(result['output']).exists()
    assert result['report']['statistics']['invalid_polygons_repaired'] == 1


def test_repeated_plan_sheets_are_clipped_aligned_and_grouped(tmp_path):
    path = tmp_path / 'side_by_side.dxf'
    doc = ezdxf.new()
    doc.layers.new('ROUTES')
    model = doc.modelspace()
    shape = [(0, .01), (.02, .01), (.02, 0), (0, 0), (0, .01)]
    for xoff in (1000, 1100):
        model.add_lwpolyline([(x + xoff, y + 1000) for x, y in shape],
                             dxfattribs={'layer': 'ROUTES'})
        model.add_lwpolyline([
            (xoff - .01, 999.99), (xoff + .03, 999.99),
            (xoff + .03, 1000.02), (xoff - .01, 1000.02)], close=True)
        model.add_line((xoff + .003, 1000.003), (xoff + .017, 1000.007))
    for x, y in shape[:-1]:
        model.add_point((73.7 + x, 24.58 + y))
    doc.saveas(path)

    detection = c.analyze_plan_sheets(c.Options(str(path), plan_margin=.02))
    assert detection['available'] is True
    assert len(detection['plans']) == 2
    assert detection['control_match_error'] < 1e-7

    ids = tuple(p['id'] for p in detection['plans'])
    opts = c.Options(str(path), source_crs='EPSG:4326', location_confirmed=True,
                     plan_ids=ids, plan_margin=.02)
    result = c.convert(opts, tmp_path / 'aligned.kml')
    root = ET.parse(result['output']).getroot()
    folder_names = [n.text for n in root.findall('.//k:Folder/k:name', N)]
    assert 'Plan 1' in folder_names
    assert 'Plan 2' in folder_names
    coords = [list(map(float, p.split(','))) for el in root.findall('.//k:coordinates', N)
              for p in el.text.split()]
    assert min(p[0] for p in coords) >= 73.68
    assert max(p[0] for p in coords) <= 73.74
    alignment = result['report']['plan_sheet_alignment']
    assert len(alignment['selected_plans']) == 2
    assert alignment['review_required'] is True

    without_frames = c.convert(
        replace(opts, exclude_plan_boundary=True, exclude_sheet_frame=True),
        tmp_path / 'without-frames.kml')
    assert without_frames['report']['mineplan_earth']['features_exported'] == \
        result['report']['mineplan_earth']['features_exported'] - 4
    assert without_frames['report']['plan_sheet_alignment'][
        'detected_plan_boundary_excluded'] is True
    assert without_frames['report']['plan_sheet_alignment'][
        'oversized_sheet_frame_excluded'] is True


def test_multi_vertex_plan_uses_embedded_gps_labels(tmp_path):
    from pyproj import Transformer

    path = tmp_path / 'gps_labelled_plans.dxf'
    doc = ezdxf.new()
    doc.units = 6
    model = doc.modelspace()
    labels = [
        (26, 53, 30.00, 73, 32, 0.00),
        (26, 53, 31.00, 73, 32, 1.00),
        (26, 53, 30.20, 73, 32, 2.00),
        (26, 53, 29.20, 73, 32, 1.50),
        (26, 53, 29.00, 73, 32, 0.20),
    ]
    to_utm = Transformer.from_crs(4326, 32643, always_xy=True)
    target = []
    for lat_d, lat_m, lat_s, lon_d, lon_m, lon_s in labels:
        lon = lon_d + lon_m / 60 + lon_s / 3600
        lat = lat_d + lat_m / 60 + lat_s / 3600
        target.append(to_utm.transform(lon, lat))
    first = [(x + 1000, y - 2000) for x, y in target]
    second = [(x + 1600, y - 2000) for x, y in target]
    # Legacy POLYLINE storage is still common in mine-plan DXFs.
    model.add_polyline2d(first, close=True)
    model.add_polyline2d(second, close=True)
    for lat_d, lat_m, lat_s, lon_d, lon_m, lon_s in labels:
        model.add_mtext(
            f'N{lat_d}°{lat_m}\'{lat_s}" E{lon_d}°{lon_m}\'{lon_s}"',
            dxfattribs={'insert': (first[0][0], first[0][1])})
    doc.saveas(path)

    detection = c.analyze_plan_sheets(c.Options(str(path), plan_margin=20))
    assert detection['available'] is True
    assert detection['suggested_crs'] == 'EPSG:32643'
    assert len(detection['plans']) == 2
    assert detection['control_match_error'] < .01
    location = detection['location']
    assert location['coordinate_count'] == 5
    assert location['confidence'] == 'high'
    assert abs(location['center_latitude'] - sum(
        lat_d + lat_m / 60 + lat_s / 3600
        for lat_d, lat_m, lat_s, *_ in labels) / len(labels)) < 1e-7
    assert abs(location['center_longitude'] - sum(
        lon_d + lon_m / 60 + lon_s / 3600
        for *_, lon_d, lon_m, lon_s in labels) / len(labels)) < 1e-7


def test_bare_two_line_dms_labels_with_corrupt_degree_symbol_are_parsed():
    import plan_sheets

    texts = [
        (0, 0, '27� 08\'1.6"\n73� 22\' 41.57"'),
        (0, 0, '27� 08\'.03"\n73� 22\' 52.32"'),
    ]
    coordinates = plan_sheets._gps_coordinates(texts)
    assert len(coordinates) == 2
    assert abs(coordinates[0][1] - (27 + 8 / 60 + 1.6 / 3600)) < 1e-10
    assert abs(coordinates[0][0] - (73 + 22 / 60 + 41.57 / 3600)) < 1e-10
    assert abs(coordinates[1][1] - (27 + 8 / 60 + .03 / 3600)) < 1e-10
