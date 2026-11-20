from pathlib import Path

import ezdxf

import large_dxf


def _drawing(path):
    doc = ezdxf.new("R2013")
    model = doc.modelspace()
    model.add_line((0, 0), (10, 10), dxfattribs={"layer": "KEEP"})
    model.add_text("lease", dxfattribs={"layer": "KEEP", "insert": (1, 1)})
    model.add_line((20, 20), (30, 30), dxfattribs={"layer": "DROP"})
    model.add_polyline2d([(0, 0), (1, 0), (1, 1)], close=True,
                         dxfattribs={"layer": "SURVEY"})
    doc.saveas(path)


def test_large_dxf_index_reports_layers_and_legacy_vertices(tmp_path):
    source = tmp_path / "source.dxf"
    _drawing(source)
    result = large_dxf.index_layers(source)
    layers = {item["name"]: item for item in result["layers"]}
    assert layers["KEEP"]["count"] == 2
    assert layers["DROP"]["count"] == 1
    assert layers["SURVEY"]["count"] == 1
    assert layers["SURVEY"]["vertices"] == 3


def test_large_dxf_subset_preserves_selected_entities_and_valid_dxf(tmp_path):
    source = tmp_path / "source.dxf"
    output = tmp_path / "working.dxf"
    _drawing(source)
    result = large_dxf.write_subset(source, output, {"KEEP", "SURVEY"})
    assert Path(result["output"]).is_file()
    doc = ezdxf.readfile(output)
    entities = list(doc.modelspace())
    assert {entity.dxf.layer for entity in entities} == {"KEEP", "SURVEY"}
    assert [entity.dxftype() for entity in entities].count("POLYLINE") == 1
    assert output.read_bytes().rstrip().endswith(b"EOF")
