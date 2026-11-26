import xml.etree.ElementTree as ET
import zipfile

import ezdxf

import converter
import grouped_export


def test_grouped_export_preserves_handles_coordinates_and_counts(tmp_path):
    source = tmp_path / "survey.dxf"
    doc = ezdxf.new()
    model = doc.modelspace()
    model.add_lwpolyline([(500000, 2700000), (500100, 2700000),
                          (500100, 2700100), (500000, 2700100)], close=True,
                         dxfattribs={"layer": "LEASE BOUNDARY"})
    model.add_polyline2d([(500010, 2700010), (500020, 2700020), (500030, 2700030)],
                         dxfattribs={"layer": "Contour Line Minor"})
    doc.saveas(source)
    options = converter.Options(str(source), source_crs="EPSG:32643", location_confirmed=True)
    whole = converter.convert(options, tmp_path / "whole.kmz")
    grouped = grouped_export.export(options, tmp_path / "grouped")
    assert grouped["report"]["indexed_records"] == grouped["report"]["assigned_records"]
    assert len(grouped["report"]["parts"]) == 2
    ns = {"k": converter.NS}
    def signatures(path):
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read("doc.kml"))
        return sorted((p.findtext("k:ExtendedData/k:Data[@name='entity_handle']/k:value", namespaces=ns),
                       tuple(n.text for n in p.findall(".//k:coordinates", ns)))
                      for p in root.findall(".//k:Placemark", ns))
    assert signatures(whole["output"]) == signatures(grouped["output"])


def test_false_notes_and_existing_topsoil_are_not_proposed():
    import semantic_layers as semantics
    assert semantics.category_for_text("EXISTING TOP SOIL DUMP") == "EXIST. TOPSOIL STACK"
    for note in ("WELL DEFINED CONTACT", "AS WELL AS ROAD", "MR B M SHARMA", "SOIL TESTING LAB"):
        assert semantics.category_for_text(note) is None
