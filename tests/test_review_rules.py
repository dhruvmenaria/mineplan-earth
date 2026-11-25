import review_rules


def test_rules_are_saved_and_loaded_for_exact_source_content(tmp_path):
    source = tmp_path / "drawing.dxf"
    source.write_text("original", encoding="utf-8")
    directory = tmp_path / "rules"
    rule = {
        "scope": "entity", "plan_id": "surface", "entity_handle": "A1",
        "category": "EXIST. ROAD",
    }
    target = review_rules.add_rule(source, rule, directory)
    assert target.is_file()
    assert review_rules.load_rules(source, directory) == [rule]


def test_changed_source_invalidates_old_rules(tmp_path):
    source = tmp_path / "drawing.dxf"
    source.write_text("version one", encoding="utf-8")
    directory = tmp_path / "rules"
    review_rules.add_rule(source, {
        "scope": "entity", "plan_id": "", "entity_handle": "1",
        "category": "CONTOUR",
    }, directory)
    source.write_text("version two", encoding="utf-8")
    document = review_rules.load_document(source, directory)
    assert document["rules"] == []
    assert "changed" in document["warning"].lower()


def test_readding_same_rule_identity_replaces_category(tmp_path):
    source = tmp_path / "drawing.dxf"
    source.write_text("drawing", encoding="utf-8")
    directory = tmp_path / "rules"
    base = {"scope": "entity", "plan_id": "surface", "entity_handle": "A1"}
    review_rules.add_rule(source, {**base, "category": "CONTOUR"}, directory)
    review_rules.add_rule(source, {**base, "category": "EXIST. PIT"}, directory)
    rules = review_rules.load_rules(source, directory)
    assert len(rules) == 1
    assert rules[0]["category"] == "EXIST. PIT"
