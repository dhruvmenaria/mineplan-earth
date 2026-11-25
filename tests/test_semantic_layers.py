from shapely.geometry import LineString, Point, Polygon
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
    assert result[3]["cad_layer"] == "Surface Plan / CONTOUR"
    assert result[3]["classification_method"] == "attached_contour_label"
    assert summary["classified_percent"] == 100


def test_common_mining_abbreviations_and_spelling_are_recognised():
    assert semantic_layers.category_for_map_annotation("SCRENNING") == "EXIST. SCREENING"
    assert semantic_layers.category_for_map_annotation(
        "Prop. IVth year Mineral Reject Back Fill") == "PROP. BACKFILL DUMP"
    assert semantic_layers.category_for_map_annotation(
        "III Yr", "Third Year working plan") == "PROP. YEAR WISE WORKING"
    assert semantic_layers.category_for_text("PROPOSED QUARRY") == "PROP. PIT"
    assert semantic_layers.category_for_text(
        "PROPOSED OVER BURDEN DUMP") == "PROP. OVERBURDEN / WASTE DUMP"
    assert semantic_layers.category_for_text("RECLAIMED AREA") == "RECLAIMED AREA"
    assert semantic_layers.category_for_text("QUARRY") is None


def test_mining_geometry_audit_checks_containment_pillars_area_and_buffers():
    lease = _record(
        "LWPOLYLINE", Polygon([(0, 0), (100, 0), (100, 100), (0, 100)]),
        layer="LEASE BOUNDARY")
    pit = _record(
        "LWPOLYLINE", Polygon([(10, 10), (30, 10), (30, 30), (10, 30)]),
        layer="EXIST. PIT")
    outside_dump = _record(
        "LWPOLYLINE", Polygon([(90, 90), (110, 90), (110, 110), (90, 110)]),
        layer="EXIST. DUMP")
    pillar = _record("POINT", Point(50, 50), layer="BOUNDARY PILLAR")
    barrier = _record(
        "LWPOLYLINE", Polygon([(-20, -20), (120, -20), (120, 120), (-20, 120)]),
        layer="7.5M STATUTORY BARRIER")
    classified, _ = semantic_layers.classify_records(
        [lease, pit, outside_dump, pillar, barrier])
    audit = semantic_layers.mining_geometry_audit(classified)
    assert audit["lease_areas"][0]["area_hectares"] == 1.0
    codes = {issue["code"] for issue in audit["issues"]}
    assert "feature_outside_lease" in codes
    assert "pillar_off_boundary" in codes
    assert "buffer_distance_mismatch" in codes


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


def test_named_block_and_attributes_are_semantic_evidence():
    block_child = _record("CIRCLE", Point(0, 0), color="#0000FF")
    block_child["block_name"] = "EXISTING_WELL_SYMBOL"
    attribute_child = _record("LINE", LineString([(0, 0), (1, 0)]), color="#FFFFFF")
    attribute_child["block_attributes_json"] = '{"A1": {"FEATURE": "SURVEY STATION"}}'
    result, _ = semantic_layers.classify_records([block_child, attribute_child])
    assert result[0]["semantic_category"] == "EXIST. WELL"
    assert result[0]["classification_method"] == "named_block_or_attribute"
    assert result[1]["semantic_category"] == "SURVEY STATION"


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


def test_manual_entity_rule_overrides_only_the_reviewed_object():
    first = _record("LINE", LineString([(0, 0), (1, 0)]), color="#123456")
    second = _record("LINE", LineString([(0, 1), (1, 1)]), color="#123456")
    first.update({"plan_id": "surface", "entity_handle": "A1"})
    second.update({"plan_id": "surface", "entity_handle": "A2"})
    result, summary = semantic_layers.classify_records(
        [first, second],
        manual_rules=[{
            "scope": "entity", "plan_id": "surface",
            "entity_handle": "A1", "category": "EXIST. ROAD",
        }],
    )
    assert result[0]["semantic_category"] == "EXIST. ROAD"
    assert result[0]["classification_method"] == "manual_entity_rule"
    assert result[1]["semantic_category"] == "UNCLASSIFIED - REVIEW"
    assert summary["manual_rules_loaded"] == 1


def test_manual_style_rule_is_constrained_to_plan_and_original_layer():
    matched = _record("LINE", LineString([(0, 0), (1, 0)]), layer="DETAIL", color="#123456")
    other_layer = _record("LINE", LineString([(0, 1), (1, 1)]), layer="OTHER", color="#123456")
    other_plan = _record("LINE", LineString([(0, 2), (1, 2)]), layer="DETAIL", color="#123456")
    matched.update({"plan_id": "surface", "entity_handle": "A1"})
    other_layer.update({"plan_id": "surface", "entity_handle": "A2"})
    other_plan.update({"plan_id": "year-1", "plan_title": "Year 1", "entity_handle": "A3"})
    signature = list(semantic_layers.style_signature(matched))
    result, _ = semantic_layers.classify_records(
        [matched, other_layer, other_plan],
        manual_rules=[{
            "scope": "style", "plan_id": "surface", "signature": signature,
            "original_layer": "DETAIL", "category": "EXIST. PIT",
        }],
    )
    assert result[0]["semantic_category"] == "EXIST. PIT"
    assert result[0]["classification_method"] == "manual_matching_style_rule"
    assert result[1]["semantic_category"] == "UNCLASSIFIED - REVIEW"
    assert result[2]["semantic_category"] == "UNCLASSIFIED - REVIEW"


def test_manual_style_rule_does_not_override_explicit_classification():
    record = _record(
        "LINE", LineString([(0, 0), (1, 0)]),
        layer="LEASE BOUNDARY", color="#123456")
    record.update({"plan_id": "surface", "entity_handle": "A1"})
    result, _ = semantic_layers.classify_records(
        [record],
        manual_rules=[{
            "scope": "style", "plan_id": "surface",
            "signature": list(semantic_layers.style_signature(record)),
            "original_layer": "LEASE BOUNDARY", "category": "EXIST. PIT",
        }],
    )
    assert result[0]["semantic_category"] == "LEASE BOUNDARY"
    assert result[0]["classification_method"] == "cad_layer"


def test_dominant_contour_style_requires_open_linear_geometry():
    line = _record("POLYLINE", LineString([(0, 0), (4, 1)]), color="#C79500")
    area = _record(
        "LWPOLYLINE", Polygon([(0, 0), (4, 0), (4, 4), (0, 4)]),
        color="#C79500")
    signature = semantic_layers.style_signature(line)
    result, _ = semantic_layers.classify_records(
        [line, area], dominant_style_hints={signature: "CONTOUR"})
    assert result[0]["semantic_category"] == "CONTOUR"
    assert result[0]["classification_method"] == "dominant_contour_legend_style"
    assert result[1]["semantic_category"] == "UNCLASSIFIED - REVIEW"


def test_label_inside_closed_area_classifies_generic_layer_geometry():
    label = _record("TEXT", Point(2, 2), text="PROP. OFFICE", color="#FFFFFF")
    area = _record(
        "LWPOLYLINE", Polygon([(0, 0), (5, 0), (5, 5), (0, 5)]),
        color="#FFFFFF")
    result, _ = semantic_layers.classify_records([label, area])
    assert result[1]["semantic_category"] == "PROP. OFFICE"
    assert result[1]["classification_method"] == "label_inside_closed_area"


def test_connected_symbol_requires_nearby_labels_to_agree():
    well = _record("TEXT", Point(0, 0), text="EXIST. WELL", color="#0000FF")
    symbol = _record("ARC", LineString([(1, 0), (1.5, 0.5)]), color="#0000FF")
    result, _ = semantic_layers.classify_records([well, symbol])
    assert result[1]["semantic_category"] == "EXIST. WELL"
    assert result[1]["classification_method"] == "connected_symbol_or_leader"

    bore = _record("TEXT", Point(1, 1), text="PBH-1", color="#0000FF")
    ambiguous, _ = semantic_layers.classify_records([well, bore, symbol])
    assert ambiguous[2]["semantic_category"] == "UNCLASSIFIED - REVIEW"


def test_same_colour_linear_label_classifies_only_when_category_is_unique():
    label = _record("TEXT", Point(5, 1), text="ELECTRIC LINE", color="#00FF00")
    line = _record("LINE", LineString([(0, 0), (10, 0)]), color="#00FF00")
    result, _ = semantic_layers.classify_records([label, line])
    assert result[1]["semantic_category"] == "ELECTRIC LINE"
    assert result[1]["classification_method"] == "same_colour_linear_label"

    road = _record("TEXT", Point(6, 1), text="EXISTING ROAD", color="#00FF00")
    ambiguous, _ = semantic_layers.classify_records([label, road, line])
    assert ambiguous[2]["semantic_category"] == "UNCLASSIFIED - REVIEW"


def test_repeated_aligned_geometry_propagates_only_unique_category():
    explicit = _record("LINE", LineString([(0, 0), (10, 0)]), layer="EXIST. ROAD")
    unknown = _record("LINE", LineString([(0, 0), (10, 0)]), layer="0")
    explicit.update({"plan_id": "plan-1", "plan_title": "Plan 1"})
    unknown.update({"plan_id": "plan-2", "plan_title": "Plan 2"})
    result, _ = semantic_layers.classify_records([explicit, unknown])
    assert result[1]["semantic_category"] == "EXIST. ROAD"
    assert result[1]["classification_method"] == "repeated_aligned_geometry"
