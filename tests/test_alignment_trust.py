import json
import math
from dataclasses import replace

import ezdxf
import pytest
from pyproj import Transformer

import converter
import plan_sheets


def drawing(tmp_path, scale, angle):
    source = tmp_path / 'gps_panels.dxf'
    doc = ezdxf.new()
    doc.units = 6
    model = doc.modelspace()
    inverse = Transformer.from_crs(32643,4326,always_xy=True)
    target = [(500000+x,2700000+y) for x,y in [(0,0),(100,10),(140,80),(70,140),(-20,90)]]
    radians = math.radians(angle)
    for dx in (1000,2000):
        boundary = [(dx+scale*((x-500000)*math.cos(radians)-(y-2700000)*math.sin(radians)),
                               1000+scale*((x-500000)*math.sin(radians)+(y-2700000)*math.cos(radians)))
                              for x,y in target]
        model.add_lwpolyline(boundary,close=True)
        model.add_circle((sum(x for x,y in boundary)/len(boundary),sum(y for x,y in boundary)/len(boundary)),
                         2,dxfattribs={'layer':'EXIST. WELL'})
    def dms(number):
        deg = int(number); minutes = int((number-deg)*60)
        seconds = ((number-deg)*60-minutes)*60
        return f'{deg}°{minutes}\'{seconds:.6f}"'
    for x,y in target:
        lon,lat = inverse.transform(x,y)
        model.add_mtext(f'N{dms(lat)} E{dms(lon)}',dxfattribs={'insert':(1000,1000)})
    doc.saveas(source)
    return source


@pytest.mark.parametrize('scale,angle', [(1.15,0),(1,12),(1.3,0)])
def test_scaled_or_rotated_gps_fit_is_detected_but_not_trusted_or_exported(tmp_path, scale, angle):
    source = drawing(tmp_path,scale,angle)
    options = converter.Options(str(source),source_crs='EPSG:32643',location_confirmed=True)
    detection = plan_sheets.detect(source)
    assert detection['available']
    assert detection['location']['confidence'] == 'review'
    assert detection['fit_diagnostics']['review_required']
    fit = detection['fit_diagnostics']['per_plan'][0]
    assert fit['scale'] == pytest.approx(1/scale, abs=1e-6)
    assert fit['rotation_degrees'] == pytest.approx(-angle, abs=1e-5)
    options = replace(options,plan_ids=tuple(p['id'] for p in detection['plans']))
    with pytest.raises(ValueError,match='needs survey review'):
        converter.convert(options,tmp_path/'unsafe.kmz')
    assert not (tmp_path/'unsafe.kmz').exists()
    # Frozen legacy detections cannot bypass the gate by omitting diagnostics.
    detection.pop('fit_diagnostics')
    options = replace(options,frozen_plan_detection_json=json.dumps(detection))
    with pytest.raises(ValueError,match='needs survey review'):
        converter._plan_preflight(options)
    with pytest.raises(ValueError,match='needs survey review'):
        converter.convert_submission(options,tmp_path/'gov')
    assert not (tmp_path/'gov').exists()
    import grouped_export
    with pytest.raises(ValueError,match='needs survey review'):
        grouped_export.export(options,tmp_path/'grouped')
    assert not list((tmp_path/'grouped').rglob('*.kmz'))


def test_normal_gps_fit_remains_eligible_and_reports_transform(tmp_path):
    source = drawing(tmp_path,1,0)
    detection = plan_sheets.detect(source)
    assert detection['location']['confidence'] == 'high'
    assert not detection['fit_diagnostics']['review_required']
    options = converter.Options(str(source),source_crs='EPSG:32643',location_confirmed=True,
                                plan_ids=tuple(p['id'] for p in detection['plans']))
    result = converter.convert(options,tmp_path/'normal.kmz')
    fit = result['report']['plan_sheet_alignment']['fit_diagnostics']
    assert fit['scale'] == pytest.approx(1, abs=1e-6)
    assert fit['rotation_degrees'] == pytest.approx(0, abs=1e-5)


@pytest.mark.parametrize('affine', [(1,0,0,1.1,0,0),(1,0,0,-1,0,0),(float('nan'),0,0,1,0,0)])
def test_invalid_frozen_affines_require_review(affine):
    assert plan_sheets.alignment_fit_diagnostics([{'id':'p','affine':affine}])['review_required']


def test_repeated_symmetric_copies_share_one_orientation(tmp_path):
    source = tmp_path/'symmetric.dxf'
    doc = ezdxf.new()
    model = doc.modelspace()
    shape = [(500000,2700000),(500100,2700000),(500100,2700100),(500000,2700100)]
    for n in range(4):
        reordered = shape[n:]+shape[:n]
        if n % 2:
            reordered.reverse()
        model.add_lwpolyline([(x+1000*n,y) for x,y in reordered],close=True)
    inverse = Transformer.from_crs(32643,4326,always_xy=True)
    for x,y in shape:
        lon,lat = inverse.transform(x,y)
        def dms(value):
            deg=int(value); minute=int((value-deg)*60); sec=((value-deg)*60-minute)*60
            return f'{deg}°{minute}\'{sec:.8f}"'
        model.add_mtext(f'N{dms(lat)} E{dms(lon)}')
    doc.saveas(source)
    detection = plan_sheets.detect(source)
    assert detection['available']
    transforms=[p['affine'][:4] for p in detection['plans']]
    assert all(t == transforms[0] for t in transforms)


def test_acknowledged_warning_exports_and_stays_unverified_in_kmz_and_report(tmp_path):
    import zipfile
    source = drawing(tmp_path,1.15,12)
    original = source.read_bytes()
    detection = plan_sheets.detect(source)
    options = converter.Options(str(source),source_crs='EPSG:32643',location_confirmed=True,
                                plan_ids=tuple(p['id'] for p in detection['plans']),
                                allow_unverified_alignment=True)
    result = converter.convert(options,tmp_path/'with-warning.kmz')
    report = result['report']
    assert report['status'] == 'complete_with_warnings'
    alignment = report['plan_sheet_alignment']
    assert alignment['alignment_verification_status'] == 'unverified'
    assert alignment['unverified_alignment_acknowledged'] is True
    assert alignment['fit_diagnostics']['review_required'] is True
    assert alignment['detected_location']['confidence'] == 'review'
    with zipfile.ZipFile(result['output']) as archive:
        assert b'UNVERIFIED ALIGNMENT' in archive.read('doc.kml')
    assert source.read_bytes() == original
    gov = converter.convert_submission(options,tmp_path/'acknowledged-gov')
    assert gov['report']['alignment_review']['unverified_alignment_acknowledged']
    assert any('Unverified alignment' in w for w in gov['report']['warnings'])
    import grouped_export
    grouped = grouped_export.export(options,tmp_path/'acknowledged-grouped')
    assert grouped['report']['alignment_review']['unverified_alignment_acknowledged']


def test_acknowledgement_does_not_bypass_invalid_matrix_or_wrong_crs(tmp_path):
    source = drawing(tmp_path,1.15,12)
    detection = plan_sheets.detect(source)
    options = converter.Options(str(source),source_crs='EPSG:32644',location_confirmed=True,
                                plan_ids=tuple(p['id'] for p in detection['plans']),
                                allow_unverified_alignment=True,
                                frozen_plan_detection_json=json.dumps(detection))
    with pytest.raises(ValueError,match='selected source CRS'):
        converter._plan_preflight(options)
    detection['plans'][0]['affine']=[1,0,0,-1,0,0]
    options=replace(options,source_crs='EPSG:32643',frozen_plan_detection_json=json.dumps(detection))
    with pytest.raises(ValueError,match='invalid transform'):
        converter._plan_preflight(options)


def test_desktop_warning_yes_continues_and_no_cancels(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import app
    source=drawing(tmp_path,1.15,0)
    detection=plan_sheets.detect(source)
    instance=SimpleNamespace(plan_info=detection)
    options=converter.Options(str(source),plan_ids=tuple(p['id'] for p in detection['plans']))
    messages=[]
    def confirm(title,message,**kwargs):
        messages.append(message)
        return True
    monkeypatch.setattr(app.messagebox,'askyesno',confirm)
    approved=app.App._confirm_alignment_export(instance,options)
    assert approved.allow_unverified_alignment
    assert not options.allow_unverified_alignment
    assert 'Continue export?' in messages[0]
    assert 'not been independently verified' in messages[0]
    monkeypatch.setattr(app.messagebox,'askyesno',lambda *a,**kw: False)
    assert app.App._confirm_alignment_export(instance,options) is None
