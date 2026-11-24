from shapely.geometry import LineString, Point

import plan_sheets


def test_plan_crop_follows_lease_shape_and_removes_sheet_furniture():
    plans = [{
        "id": "surface", "title": "Surface Plan", "boundary_handle": "lease",
        "boundary": ((0, 0), (10, 0), (0, 10)),
        "translate_x": 0, "translate_y": 0, "affine": None,
    }]
    records = [
        {"geometry": Point(2, 2), "cad_layer": "MINE", "entity_type": "POINT",
         "entity_handle": "mine"},
        # Inside the triangle's bounding box, but outside the lease polygon.
        {"geometry": Point(8, 8), "cad_layer": "LEGEND", "entity_type": "TEXT",
         "entity_handle": "legend", "text_value": "INDEX"},
    ]
    result = plan_sheets.align_records(records, {"plans": plans}, ("surface",), margin=5)
    assert [record["entity_handle"] for record in result] == ["mine"]


def test_shared_base_geometry_is_kept_once_across_plans():
    road = LineString([(1, 1), (5, 5)])
    workings = LineString([(3, 1), (4, 2)])
    records = []
    for plan in ({"id": "one", "title": "Year 1"},
                 {"id": "two", "title": "Year 2"}):
        records.extend([
            {"geometry": road, "cad_layer": f"{plan['title']} / ROAD",
             "entity_type": "LINE", "plan_id": plan["id"], "plan_title": plan["title"]},
            {"geometry": workings, "cad_layer": f"{plan['title']} / WORKING",
             "entity_type": "LINE", "plan_id": plan["id"], "plan_title": plan["title"]},
        ])
    # A different layer retains its separate meaning even with the same geometry.
    records.append({"geometry": road, "cad_layer": "Year 2 / LEASE BOUNDARY",
                    "entity_type": "LINE", "plan_id": "two", "plan_title": "Year 2"})

    result = plan_sheets._consolidate_shared_features(
        records, [{"id": "one"}, {"id": "two"}])
    assert len(result) == 3
    assert {record["cad_layer"] for record in result} == {
        "Shared base map / ROAD", "Shared base map / WORKING", "Year 2 / LEASE BOUNDARY"}


def test_shared_outer_boundary_is_owned_by_environment_plan():
    boundary = LineString([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])
    plans = [
        {"id": "surface", "title": "Surface Plan"},
        {"id": "year-1", "title": "First Year Plan"},
        {"id": "environment", "title": "Environment Plan"},
    ]
    records = []
    for plan in plans:
        records.append({
            "geometry": boundary, "cad_layer": f"{plan['title']} / 0",
            "entity_type": "LWPOLYLINE", "plan_id": plan["id"],
            "plan_title": plan["title"],
        })
        records.append({
            "geometry": LineString([(1, 1), (2, 2)]),
            "cad_layer": f"{plan['title']} / WORKINGS",
            "entity_type": "LINE", "plan_id": plan["id"],
            "plan_title": plan["title"],
        })

    result = plan_sheets._place_shared_outer_boundary_with_environment(records, plans)
    shared = [record for record in result if record.get("shared_outer_boundary")]
    assert len(shared) == 1
    assert shared[0]["plan_id"] == "environment"
    assert shared[0]["cad_layer"] == "Environment Plan / 0"
    assert len(result) == 4  # three unique working lines plus one shared outline
