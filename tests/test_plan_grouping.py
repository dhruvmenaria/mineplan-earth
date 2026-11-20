from shapely.geometry import LineString

import plan_sheets


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
