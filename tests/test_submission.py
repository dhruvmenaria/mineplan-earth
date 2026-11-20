import converter


def test_submission_groups_use_official_names_and_combine_years():
    titles = [
        "Surface plan",
        "Surface geological plan & section",
        "Second Year working plan & section",
        "Third Year working plan & section",
        "Mine Layout Plan & Section",
        "Progressive Closure Plan & Section",
        "Financial Assurance Area Plan",
        "Conceptual Plan & Section",
        "Environment Plan",
    ]
    detection = {"plans": [
        {"id": f"plan-{index}", "title": title}
        for index, title in enumerate(titles)
    ]}
    selected = tuple(plan["id"] for plan in detection["plans"])
    groups = converter.submission_groups(detection, selected)
    assert [group["filename"] for group in groups] == list(converter.GOVERNMENT_FILENAMES)
    annual = next(group for group in groups
                  if group["filename"] == "PROPOSED ALL YEAR COMBINED.kmz")
    assert len(annual["plan_ids"]) == 2


def test_unrecognised_plan_title_gets_safe_additional_filename():
    assert converter._submission_filename("Transport / Utility Plan") == \
        "TRANSPORT - UTILITY PLAN.kmz"


def test_submission_folder_names_include_source_drawing_name():
    assert converter.submission_folder_names(r"C:\\Plans\\03-2020 PLAN .dxf") == (
        "03-2020 PLAN - FINAL KMZ", "03-2020 PLAN - FINAL KMZ QA")
