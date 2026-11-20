from shapely.geometry import LineString, Point
import ezdxf

import semantic_layers


def _record(entity, geometry, *, text=None, layer="0", color="#FF0000"):
    return {
        "entity_type": entity, "geometry": geometry, "text_value": text,
        "cad_layer": f"Surface Plan / {layer}", "plan_title": "Surface Plan",
        "color_hex": color, "linetype": "CONTINUOUS",
    }


def test_keyword_and_layer_classification_create_semantic_folders():
    records = [
        _record("TEXT", Point(0, 0), text="LEASE BOUNDARY"),
        _record("LWPOLYLINE", LineString([(0, 0), (5, 0)]), layer="LEASE BOUNDARY"),
        _record("LWPOLYLINE", LineString([(0, 2), (5, 2)]), layer="CONTOUR"),
        _record("TEXT", Point(2, 2), text="312"),
    ]
    result, summary = semantic_layers.classify_records(records)
    assert result[0]["cad_layer"] == "Surface Plan / LEASE BOUNDARY"
    assert result[1]["cad_layer"] == "Surface Plan / LEASE BOUNDARY"
    assert result[2]["cad_layer"] == "Surface Plan / CONTOUR"
    assert result[3]["cad_layer"] == "Surface Plan / UNCLASSIFIED - REVIEW"
    assert summary["classified_percent"] == 75


def test_uncertain_geometry_is_not_silently_guessed():
    records = [_record("LINE", LineString([(50, 50), (60, 60)]), color="#123456")]
    result, summary = semantic_layers.classify_records(records)
    assert result[0]["semantic_category"] == "UNCLASSIFIED - REVIEW"
    assert summary["unclassified_records"] == 1


def test_known_quarry_layer_typo_is_classified_as_existing_pit():
    records = [
        _record(
            "SPLINE",
            LineString([(0, 0), (4, 0), (4, 3), (0, 3), (0, 0)]),
            layer="OUARRY",
            color="#FFFFFF",
        )
    ]
    result, summary = semantic_layers.classify_records(
        records, allowed_categories={"EXIST. PIT"}
    )
    assert result[0]["semantic_category"] == "EXIST. PIT"
    assert result[0]["classification_method"] == "cad_layer"
    assert summary["unclassified_records"] == 0


def test_nearby_label_never_claims_unrelated_frame_or_boundary():
    records = [
        _record("TEXT", Point(0, 0), text="ELECTRIC LINE", color="#139B48"),
        _record("LWPOLYLINE", LineString([(-2, 0), (2, 0)]), color="#139B48"),
        _record("LWPOLYLINE", LineString([(-20, -20), (20, -20), (20, 20),
                                           (-20, 20), (-20, -20)]), color="#FFFFFF"),
    ]
    result, _ = semantic_layers.classify_records(
        records,
        style_hints={("LINE", "#139B48", "CONTINUOUS"): "ELECTRIC LINE"},
        allowed_categories={"ELECTRIC LINE"},
    )
    assert result[0]["semantic_category"] == "ELECTRIC LINE"
    assert result[1]["semantic_category"] == "ELECTRIC LINE"
    assert result[2]["semantic_category"] == "UNCLASSIFIED - REVIEW"
    assert result[2]["classification_method"] == "unclassified"


def test_autocad_legend_rules_are_learned_only_from_current_dxf(tmp_path):
    source = tmp_path / "with-legend.dxf"
    drawing = ezdxf.new("R2010")
    model = drawing.modelspace()
    label = model.add_mtext("LEASE BOUNDARY", dxfattribs={"char_height": 2.5})
    label.set_location((100, 50))
    model.add_line((80, 48), (92, 48), dxfattribs={"color": 1})
    drawing.saveas(source)

    rules, report = semantic_layers.learn_legend_styles_from_dxf(source)
    assert rules[("LINE", "#FF0000", "CONTINUOUS")] == "LEASE BOUNDARY"
    assert report["scope"] == "current_source_dxf_only"
    assert report["source_drawing"].endswith("with-legend.dxf")

    other = tmp_path / "without-legend.dxf"
    drawing = ezdxf.new("R2010")
    drawing.modelspace().add_line((0, 0), (10, 0), dxfattribs={"color": 1})
    drawing.saveas(other)
    other_rules, _ = semantic_layers.learn_legend_styles_from_dxf(other)
    assert other_rules == {}
