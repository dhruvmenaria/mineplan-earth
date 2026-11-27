from converter import classification_metrics


def test_coverage_targets_use_counts_and_do_not_claim_accuracy():
    for known, status, to80, to90 in [
        (79, "below_80_percent_review_needed", 1, 11),
        (80, "80_percent_minimum_met", 0, 10),
        (90, "90_percent_goal_met", 0, 0),
    ]:
        metrics = classification_metrics({"total_records": 100, "unclassified_records": 100-known})
        assert metrics["coverage_target_status"] == status
        assert metrics["additional_assignments_for_80_percent"] == to80
        assert metrics["additional_assignments_for_90_percent"] == to90
        assert metrics["independently_verified_accuracy_out_of_100"] is None
    assert classification_metrics(None)["coverage_target_status"] == "not_assessed"


def test_coverage_excludes_pending_roles_and_does_not_claim_accuracy():
    metrics = classification_metrics({"total_records": 100, "unclassified_records": 10,
                                      "feature_role_review_records": 20})
    assert metrics["classification_coverage_out_of_100"] == 70
    assert metrics["independently_verified_accuracy_out_of_100"] is None
    assert classification_metrics(None)["classification_coverage_out_of_100"] is None


def test_free_legend_category_is_not_counted_as_standard_or_ibm_verified():
    from shapely.geometry import Point
    import semantic_layers
    records = [
        {'entity_type':'TEXT', 'geometry':Point(0,0), 'cad_layer':'0', 'text_value':'CONTOUR'},
        {'entity_type':'LINE', 'geometry':Point(50,50), 'cad_layer':'0', 'color_hex':'#123456', 'linetype':'CONTINUOUS'},
    ]
    _, summary = semantic_layers.classify_records(records,
        style_hints={('LINE','#123456','CONTINUOUS'):'CUSTOM INDEX TERM'})
    metrics = classification_metrics(summary)
    assert metrics['classification_coverage_out_of_100'] == 100
    assert metrics['standard_taxonomy_coverage_out_of_100'] == 50
    assert metrics['ibm_category_mapping_coverage_out_of_100'] is None
    assert metrics['ibm_category_mapping_status'] == 'not_assessed_approved_taxonomy_not_configured'
