from converter import classification_metrics


def test_coverage_excludes_pending_roles_and_does_not_claim_accuracy():
    metrics = classification_metrics({"total_records": 100, "unclassified_records": 10,
                                      "feature_role_review_records": 20})
    assert metrics["classification_coverage_out_of_100"] == 70
    assert metrics["independently_verified_accuracy_out_of_100"] is None
    assert classification_metrics(None)["classification_coverage_out_of_100"] is None
