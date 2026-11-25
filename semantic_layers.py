"""Deterministic semantic grouping for government-review Google Earth files."""
from __future__ import annotations

from collections import Counter, defaultdict
import contextlib
import json
import math
from pathlib import Path
import re


TEXT_TYPES = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}


def _normalise(value):
    value = str(value or "").upper().replace("&", " AND ")
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    return " ".join(value.split())


# More specific rules must come before general words such as BOUNDARY or ROAD.
CATEGORY_RULES = (
    ("Boundary pillars", (r"\bBOUNDARY PILLAR", r"\bBP\s*[- ]?\d+\b")),
    ("500 M. BOUNDARY", (r"\b500\s*M(?:ETRE|ETER)?\b.*\bBOUNDARY",)),
    ("60 M. BOUNDARY", (r"\b60\s*M(?:ETRE|ETER)?\b.*\bBOUNDARY",)),
    ("7.5M. STATUTORY BARRIER", (r"\b7\s*5\s*M\b.*\b(?:BARRIER|STATUTORY)",
                                  r"\bSTATUTORY BARRIER\b")),
    ("ULTIMATE PIT LIMIT", (r"\bULTIMATE PIT LIMIT\b",)),
    ("PROP. PIT", (r"\bPROP(?:OSED)?\b.*\b(?:PIT|QUARRY)\b",
                    r"\b(?:PIT|QUARRY)\b.*\bPROP(?:OSED)?\b")),
    ("PROP. YEAR WISE WORKING", (r"\bPROP(?:OSED)?\b.*\bYEAR\b.*\bWORK", 
                                  r"\bYEAR WISE (?:PIT|WORK)")),
    ("PROP. MINERAL REJECT DUMP", (r"\bMINERAL REJECT DUMP\b", r"\bMRL\b.*\bDUMP\b")),
    ("PROP. OVERBURDEN / WASTE DUMP", (
        r"\bPROP(?:OSED)?\b.*\b(?:OVER\s*BURDEN|OB|WASTE)\b.*\bDUMP\b",
        r"\b(?:OVER\s*BURDEN|OB|WASTE)\b.*\bDUMP\b.*\bPROP(?:OSED)?\b")),
    ("PROP. TOPSOIL STACK", (r"\bPROP(?:OSED)?\b.*\bTOP\s*SOIL\b",
                              r"\bTOP\s*SOIL\b.*\b(?:STACK|DUMP|YARD)\b")),
    ("PROP. MINERAL STACK YARD", (r"\bMINERAL (?:STACK|STOCK) YARD\b",)),
    ("PROP. BACKFILL DUMP", (r"\bBACKFILL DUMP\b",)),
    ("PROP. RETAINING WALL", (r"\bRETAINING WALL\b",)),
    ("PROP. GARLAND DRAIN", (r"\bGARLAND DRAIN\b",)),
    ("PROP. FENCING", (r"\bFENCING\b",)),
    ("PROP. PLANTATION", (r"\bPLANTATION\b",)),
    ("PROP. LABOUR SHED", (r"\bLABOU?R SHED\b",)),
    ("PROP. SETTLING TANK", (r"\bSETT(?:L)?ING TANK\b",)),
    ("EXIST. BORE HOLE", (r"\bEXIST(?:ING)?\b.*\bBORE HOLE\b",)),
    ("PROP. BORE HOLE", (r"\bPBH\s*[- ]?\d*\b", r"\bBORE HOLE\b")),
    ("PROP. OFFICE", (r"\bPROP(?:OSED)?\b.*\bOFFICE\b", r"\bSTORE ROOM\b.*\bOFFICE\b")),
    ("PROP. HAULAGE ROAD", (r"\bHAULAGE ROAD\b",)),
    ("EXIST. ROAD", (r"\bEXIST(?:ING)?\b.*\bROAD\b", r"\bROAD\s*/?\s*RASTA\b")),
    ("EXIST. OVERBURDEN / WASTE DUMP", (
        r"\bEXIST(?:ING)?\b.*\b(?:OVER\s*BURDEN|OB|WASTE)\b.*\bDUMP\b",)),
    ("EXIST. TOPSOIL STACK", (r"\bEXIST(?:ING)?\b.*\bTOP\s*SOIL\b",)),
    ("EXIST. DUMP", (r"\bEXIST(?:ING)?\b.*\bDUMP\b",)),
    ("BACKFILLED AREA", (r"\bBACK\s*FILL(?:ED)?\b.*\bAREA\b",)),
    ("RECLAIMED AREA", (r"\bRECLAIM(?:ED|ATION)?\b.*\bAREA\b",)),
    ("VIRGIN AREA", (r"\bVIRGIN\b.*\bAREA\b",)),
    ("EXIST. PIT", (r"\bEXIST(?:ING)?\b.*\b(?:PIT|QUARRY)\b", r"\bPIT\s*[- ]?\d+\b")),
    ("EXIST. WELL", (r"\bEXIST(?:ING)?\b.*\bWELL\b", r"\bWELL\b")),
    ("EXIST. SCREENING", (r"\bEXIST(?:ING)?\b.*\bSC+RE+N+ING\b",)),
    ("SURVEY STATION", (r"\bSURVEY STATION\b",)),
    ("TREE", (r"\bTREE\b",)),
    ("HUTMENTS", (r"\bHUTMENTS?\b",)),
    ("NALA", (r"\bNALA\b",)),
    ("WATER RESERVOIR AREA", (r"\bWATER RESERVOIR\b",)),
    ("SOIL", (r"\bSOIL\b",)),
    ("CONTOUR", (r"\bCONTOUR\b",)),
    ("BENCH MARK", (r"\bBENCH MARK\b", r"\bB\s*M\b")),
    ("SECTION LINE", (r"\bSECTION LINE\b",)),
    ("GRID LINE", (r"\bGRID LINE\b",)),
    ("ELECTRIC LINE", (r"\bELECTRIC (?:LINE|WIRE)\b",)),
    ("MINERALIZED BODY", (r"\bMINERALI[ZS]ED BODY\b", r"\bLIME STONE BODY\b", r"\bDOLOMITIC\b")),
    ("GEOLOGICAL FEATURES", (r"\bGEOLOGICAL\b", r"\bDIP\s*/?\s*STRIKE\b")),
    ("GOVT. LAND", (r"\bGOVT\b.*\bLAND\b", r"\bGOVERNMENT LAND\b")),
    ("PVT. LAND", (r"\bPVT\b.*\bLAND\b", r"\bPRIVATE LAND\b")),
    ("WASTE LAND", (r"\bWASTE LAND\b",)),
    ("LEASE BOUNDARY", (r"\bLEASE BOUNDARY\b", r"\bML BOUNDARY\b")),
)


def category_for_text(value):
    text = _normalise(value)
    if not text:
        return None
    for category, patterns in CATEGORY_RULES:
        if any(re.search(pattern, text) for pattern in patterns):
            return category
    return None


def category_for_layer(value):
    text = _normalise(value)
    if not text or text == "0":
        return None
    # Apply the same vocabulary to genuine CAD layer names.
    category = category_for_text(text)
    if category:
        return category
    aliases = {
        "EXIST ROAD": "EXIST. ROAD",
        "EXISTING ROAD": "EXIST. ROAD",
        "QUARRY": "EXIST. PIT",
        # A recurring drafting typo in supplied mine plans. Keep this exact
        # rather than applying fuzzy layer-name matching, which could silently
        # assign unrelated geometry to a government-review folder.
        "OUARRY": "EXIST. PIT",
        "CONTOURS": "CONTOUR",
        "LEASE": "LEASE BOUNDARY",
    }
    return aliases.get(text)


def category_for_block(record):
    """Read semantic evidence retained from named AutoCAD block references."""
    values = [record.get("block_name"), record.get("block_path")]
    attributes = record.get("block_attributes_json")
    if attributes:
        with contextlib.suppress(TypeError, ValueError):
            parsed = json.loads(attributes) if isinstance(attributes, str) else attributes
            if isinstance(parsed, dict):
                values.extend(str(value) for value in parsed.values())
    for value in values:
        category = category_for_text(value)
        if category:
            return category
    return None


def category_for_map_annotation(value, plan_title=""):
    """Recognise terse map labels without teaching them as legend samples."""
    text = _normalise(value)
    title = _normalise(plan_title)
    if re.fullmatch(r"SC+RE+N+ING", text):
        return "EXIST. SCREENING"
    if "MINERAL REJECT BACK FILL" in text:
        return "PROP. BACKFILL DUMP"
    if "YEAR" in title and re.fullmatch(r"(?:I|II|III|IV|V)\s*YR", text):
        return "PROP. YEAR WISE WORKING"
    if "ENVIRONMENT" in title or "ENVIROMENT" in title:
        if text in {"60M", "60 M"}:
            return "60 M. BOUNDARY"
        if text in {"500M", "500 M"}:
            return "500 M. BOUNDARY"
    return None


def _original_layer(record):
    value = str(record.get("cad_layer") or "0")
    return value.split(" / ", 1)[-1]


def _style_signature(record):
    entity = str(record.get("entity_type") or "").upper()
    family = "TEXT" if entity in TEXT_TYPES else (
        "AREA" if getattr(record.get("geometry"), "geom_type", "") in {"Polygon", "MultiPolygon"}
        else "LINE")
    color = str(record.get("color_hex") or "").upper()
    linetype = _normalise(record.get("linetype") or "CONTINUOUS")
    return family, color, linetype


def style_signature(record):
    """Public, JSON-friendly style identity used by manual review rules."""
    return _style_signature(record)


def available_categories():
    return sorted({category for category, _ in CATEGORY_RULES})


def mining_geometry_audit(records, *, metre_units=True, lease_boundaries=None):
    """Run conservative, report-only mining geometry checks.

    The audit never changes geometry or classifications. Distance and area
    checks are only meaningful when source coordinates are projected metres.
    """
    from shapely.geometry import Point
    from shapely.ops import unary_union

    issues = []
    lease_boundaries = dict(lease_boundaries or {})
    plans = defaultdict(list)
    for record in records:
        plans[str(record.get("plan_title") or record.get("plan_id") or "Drawing")].append(record)

    def add(severity, code, plan, message, **details):
        issues.append({"severity": severity, "code": code, "plan": plan,
                       "message": message, **details})

    def polygonal(geometry):
        return geometry is not None and not geometry.is_empty and \
            geometry.geom_type in {"Polygon", "MultiPolygon"}

    def boundary_distance_samples(geometry, lease_boundary):
        from shapely import get_coordinates
        target = geometry.boundary if polygonal(geometry) else geometry
        if target is None or target.is_empty:
            return []
        coordinates = list(get_coordinates(target))
        if not coordinates:
            return []
        step = max(1, len(coordinates) // 100)
        return sorted(Point(xy[:2]).distance(lease_boundary)
                      for xy in coordinates[::step])

    checked_features = 0
    lease_areas = []
    for plan, members in plans.items():
        leases = [record["geometry"] for record in members
                  if record.get("semantic_category") == "LEASE BOUNDARY"
                  and polygonal(record.get("geometry"))]
        fallback = lease_boundaries.get(plan)
        if not leases and polygonal(fallback):
            leases = [fallback]
        if not leases:
            add("review", "lease_boundary_missing", plan,
                "No closed LEASE BOUNDARY polygon was available for geometric QA.")
            continue
        lease = unary_union(leases)
        if lease.is_empty:
            add("error", "lease_boundary_empty", plan,
                "The classified lease boundary is empty.")
            continue
        if not lease.is_valid:
            add("error", "lease_boundary_invalid", plan,
                "The classified lease boundary has invalid polygon topology.")
        if metre_units:
            area_ha = float(lease.area) / 10_000.0
            lease_areas.append({"plan": plan, "area_hectares": area_ha})
        else:
            add("review", "non_metre_geometry", plan,
                "Containment is checked, but area and statutory distances were skipped because the CRS is not projected metres.")

        controlled_areas = {
            "EXIST. PIT", "PROP. PIT", "EXIST. DUMP",
            "PROP. MINERAL REJECT DUMP", "PROP. OVERBURDEN / WASTE DUMP",
            "EXIST. OVERBURDEN / WASTE DUMP", "PROP. TOPSOIL STACK",
            "EXIST. TOPSOIL STACK", "BACKFILLED AREA", "RECLAIMED AREA",
        }
        containment_scope = "ENVIRONMENT" not in _normalise(plan) and \
            "SHARED BASE" not in _normalise(plan)
        for record in members:
            category = record.get("semantic_category")
            geometry = record.get("geometry")
            if not containment_scope or category not in controlled_areas or not polygonal(geometry):
                continue
            checked_features += 1
            outside = geometry.difference(lease.buffer(0.5 if metre_units else 0.0))
            if not outside.is_empty:
                ratio = (float(outside.area) / float(geometry.area) * 100.0
                         if polygonal(geometry) and geometry.area else None)
                add("error", "feature_outside_lease", plan,
                    f"{category} contains geometry outside the lease boundary.",
                    category=category, entity_handle=record.get("entity_handle"),
                    outside_percent=ratio)

        for record in members:
            if record.get("semantic_category") != "Boundary pillars":
                continue
            geometry = record.get("geometry")
            if geometry is None or geometry.is_empty:
                continue
            distance = geometry.representative_point().distance(lease.boundary)
            if metre_units and distance > 2.0:
                add("review", "pillar_off_boundary", plan,
                    f"A boundary pillar is {distance:.2f} m from the lease boundary.",
                    entity_handle=record.get("entity_handle"), distance_metres=distance)

        expected_distances = {
            "7.5M. STATUTORY BARRIER": 7.5,
            "60 M. BOUNDARY": 60.0,
            "500 M. BOUNDARY": 500.0,
        }
        if metre_units:
            for record in members:
                category = record.get("semantic_category")
                if category not in expected_distances:
                    continue
                geometry = record.get("geometry")
                if geometry is None or geometry.is_empty or geometry.geom_type in {"Point", "MultiPoint"}:
                    continue
                distances = boundary_distance_samples(geometry, lease.boundary)
                if not distances:
                    continue
                measured = distances[len(distances) // 2]
                expected = expected_distances[category]
                tolerance = max(1.5, expected * 0.10)
                if abs(measured - expected) > tolerance:
                    add("review", "buffer_distance_mismatch", plan,
                        f"{category} is approximately {measured:.2f} m from the lease boundary; expected about {expected:g} m.",
                        category=category, measured_metres=measured,
                        expected_metres=expected, tolerance_metres=tolerance)

    counts = Counter(issue["severity"] for issue in issues)
    return {
        "status": "fail" if counts["error"] else "review" if counts["review"] else "pass",
        "metre_units": bool(metre_units),
        "plans_checked": len(plans),
        "controlled_features_checked": checked_features,
        "lease_areas": lease_areas,
        "counts": dict(counts),
        "issues": issues,
        "review_required": True,
    }


def _raw_entity_style(entity, doc):
    """Return a signature compatible with extracted-record style signatures."""
    entity_type = entity.dxftype().upper()
    closed_curve = False
    with contextlib.suppress(Exception):
        closed_curve = bool(entity.closed if entity_type == "LWPOLYLINE" else entity.is_closed)
    family = "AREA" if entity_type in {"HATCH", "SOLID", "TRACE", "3DFACE"} \
        or closed_curve else "LINE"
    layer_name = str(getattr(entity.dxf, "layer", "0") or "0")
    layer = None
    with contextlib.suppress(Exception):
        layer = doc.layers.get(layer_name)

    raw_color = int(getattr(entity.dxf, "color", 256) or 256)
    true_color = getattr(entity.dxf, "true_color", None)
    if true_color is None and raw_color == 256 and layer is not None:
        raw_color = abs(int(getattr(layer.dxf, "color", 7) or 7))
        true_color = getattr(layer.dxf, "true_color", None)
    elif true_color is None and raw_color in {0, 257}:
        raw_color = 7

    color = ""
    with contextlib.suppress(Exception):
        import ezdxf
        rgb = (ezdxf.colors.int2rgb(int(true_color)) if true_color is not None
               else ezdxf.colors.aci2rgb(raw_color))
        color = f"#{rgb.r:02X}{rgb.g:02X}{rgb.b:02X}"

    linetype = str(getattr(entity.dxf, "linetype", "BYLAYER") or "BYLAYER")
    if linetype.upper() == "BYLAYER" and layer is not None:
        linetype = str(getattr(layer.dxf, "linetype", "CONTINUOUS") or "CONTINUOUS")
    elif linetype.upper() == "BYBLOCK":
        linetype = "CONTINUOUS"
    return family, color.upper(), _normalise(linetype)


def _entity_box(entity):
    """Small, dependency-local bounding box helper for DXF legend samples."""
    entity_type = entity.dxftype().upper()
    points = []
    with contextlib.suppress(Exception):
        if entity_type == "LINE":
            points = [entity.dxf.start, entity.dxf.end]
        elif entity_type == "LWPOLYLINE":
            points = [(item[0], item[1]) for item in entity.get_points("xy")]
        elif entity_type in {"POLYLINE", "3DFACE", "TRACE", "SOLID"}:
            points = [vertex.dxf.location for vertex in entity.vertices]
        elif entity_type == "SPLINE":
            points = list(entity.control_points) or list(entity.fit_points)
        elif entity_type in {"CIRCLE", "ARC"}:
            centre, radius = entity.dxf.center, float(entity.dxf.radius)
            points = [(centre.x - radius, centre.y - radius),
                      (centre.x + radius, centre.y + radius)]
        elif entity_type == "ELLIPSE":
            centre, major = entity.dxf.center, entity.dxf.major_axis
            radius = math.hypot(major.x, major.y)
            points = [(centre.x - radius, centre.y - radius),
                      (centre.x + radius, centre.y + radius)]
        elif entity_type in {"POINT", "INSERT"}:
            points = [entity.dxf.location if entity_type == "POINT" else entity.dxf.insert]
    if points:
        xy = [(float(point[0]), float(point[1])) for point in points]
        return (min(point[0] for point in xy), min(point[1] for point in xy),
                max(point[0] for point in xy), max(point[1] for point in xy))
    return None


def learn_legend_styles_from_document(doc, source_label="loaded drawing", plan_detection=None):
    """Learn style/category rules from one already loaded DXF document.

    Rules are returned to the caller and are never persisted. A legend entry is
    interpreted as semantic text with a short graphic sample immediately to its
    left. Repeated legend panels provide confidence; ambiguous styles are not
    propagated to drawing geometry.
    """
    source = Path(source_label).expanduser().resolve()
    entities = list(doc.modelspace())
    graphic_types = {
        "LINE", "LWPOLYLINE", "POLYLINE", "SPLINE", "ARC", "CIRCLE",
        "ELLIPSE", "HATCH", "SOLID", "TRACE", "3DFACE", "POINT", "INSERT",
    }
    graphics = []
    for entity in entities:
        if entity.dxftype().upper() not in graphic_types:
            continue
        box = _entity_box(entity)
        if box is not None:
            graphics.append((entity, box))

    observations = []
    skipped = Counter()
    for label in entities:
        if label.dxftype().upper() not in TEXT_TYPES:
            continue
        if label.dxftype().upper() == "MTEXT":
            text = label.plain_text()
            height = float(getattr(label.dxf, "char_height", 1.0) or 1.0)
        else:
            text = str(getattr(label.dxf, "text", "") or "")
            height = float(getattr(label.dxf, "height", 1.0) or 1.0)
        category = category_for_text(text)
        insertion = getattr(label.dxf, "insert", None)
        if not category or insertion is None:
            continue

        # CAD legends conventionally place a short sample immediately left of
        # the explanatory label. Scaling by text height works across drawings
        # expressed in metres, millimetres, or arbitrary paper coordinates.
        maximum_left_distance = max(25.0, height * 18.0)
        maximum_vertical_distance = max(4.0, height * 2.2)
        maximum_sample_size = max(35.0, height * 22.0)
        candidates = []
        for entity, (xmin, ymin, xmax, ymax) in graphics:
            if xmin == xmax and ymin == ymax and entity.dxftype().upper() == "POINT":
                pass
            width, box_height = xmax - xmin, ymax - ymin
            if width > maximum_sample_size or box_height > maximum_sample_size:
                continue  # Usually a legend table border or plan geometry.
            if xmax > insertion.x + height:
                continue
            horizontal_gap = insertion.x - xmax
            if horizontal_gap < -height or horizontal_gap > maximum_left_distance:
                continue
            centre_y = (ymin + ymax) / 2.0
            vertical_gap = abs(insertion.y - centre_y)
            if vertical_gap > maximum_vertical_distance:
                continue
            score = vertical_gap / max(height, 1e-9) + horizontal_gap / max(height * 8.0, 1e-9)
            candidates.append((score, entity))
        if not candidates:
            skipped["no_sample"] += 1
            continue
        candidates.sort(key=lambda item: item[0])
        sample = candidates[0][1]
        plan = None
        if plan_detection:
            matches = []
            for candidate in plan_detection.get("plans", ()):
                bounds = candidate.get("sheet_bounds") or candidate.get("crop_bounds")
                if not bounds:
                    continue
                if bounds[0] <= insertion.x <= bounds[2] and bounds[1] <= insertion.y <= bounds[3]:
                    area = max(0.0, (bounds[2] - bounds[0]) * (bounds[3] - bounds[1]))
                    matches.append((area, candidate))
            if matches:
                plan = min(matches, key=lambda item: item[0])[1]
        best_score = candidates[0][0]
        best_signature = _raw_entity_style(sample, doc)
        # A legend symbol is often composed from several same-style entities
        # (for example three arcs plus a closed polyline for a tree).  Capture
        # each geometry family in that symbol, while rejecting differently
        # coloured table borders or neighbouring legend rows.
        sample_entities = []
        seen_signatures = set()
        for score, candidate in candidates:
            signature = _raw_entity_style(candidate, doc)
            if score > best_score + 1.0:
                continue
            if signature[1:] != best_signature[1:]:
                continue
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            sample_entities.append((candidate, signature))
        for candidate, signature in sample_entities:
            observation = {
                "category": category,
                "label": text.strip(),
                "label_handle": getattr(label.dxf, "handle", None),
                "sample_handle": getattr(candidate.dxf, "handle", None),
                "signature": signature,
            }
            if plan is not None:
                observation["plan_id"] = str(plan.get("id"))
                observation["plan_title"] = str(plan.get("title"))
            observations.append(observation)

    votes = defaultdict(Counter)
    for observation in observations:
        votes[observation["signature"]][observation["category"]] += 1
    rules = {}
    ambiguous = []
    dominant_rules = []
    for signature, categories in votes.items():
        category, count = categories.most_common(1)[0]
        total = sum(categories.values())
        # One occurrence is accepted only when there is no conflict. Repeated
        # legends naturally increase confidence; any conflicting reuse of the
        # same style remains unclassified instead of being guessed.
        # A style is safe only when every observed use in this drawing agrees.
        # Shared/ambiguous CAD styles must remain available for manual review.
        if count == total:
            rules[signature] = category
        else:
            ambiguous.append({
                "signature": signature,
                "votes": dict(categories.most_common()),
            })
            ratio = count / total
            # Contours are the only deliberately supported dominant-but-not-
            # unanimous style: they are open linear geometry and are commonly
            # repeated many times, while a single bore-hole sample may reuse
            # the same colour. Geometry compatibility is checked again later.
            if signature[0] == "LINE" and category == "CONTOUR" \
                    and count >= 5 and ratio >= 0.90:
                dominant_rules.append({
                    "signature": signature, "category": category,
                    "votes": count, "total_observations": total,
                    "confidence": round(ratio, 4),
                })
    plan_votes = defaultdict(Counter)
    for observation in observations:
        plan_id = observation.get("plan_id")
        if plan_id:
            plan_votes[(plan_id, observation["signature"])][observation["category"]] += 1
    plan_rules = []
    for (plan_id, signature), categories in plan_votes.items():
        category, count = categories.most_common(1)[0]
        total = sum(categories.values())
        if count == total:
            plan_title = next((item.get("plan_title") for item in observations
                               if item.get("plan_id") == plan_id), "")
            plan_rules.append({
                "plan_id": plan_id,
                "plan_title": plan_title,
                "signature": signature,
                "category": category,
                "observations": total,
            })
    return rules, {
        "source_drawing": str(source),
        "legend_labels_found": len(observations),
        "style_rules_learned": len(rules),
        "plan_style_rules_learned": len(plan_rules),
        "plan_style_rules": plan_rules,
        "categories_found": sorted({item["category"] for item in observations}),
        "observations": observations,
        "ambiguous_styles": ambiguous,
        "dominant_style_rules": dominant_rules,
        "skipped": dict(skipped),
        "scope": "current_source_dxf_only",
    }


def learn_legend_styles_from_dxf(path, plan_detection=None):
    """Load one DXF and learn only the legend rules contained in that file."""
    import ezdxf

    source = Path(path).expanduser().resolve()
    return learn_legend_styles_from_document(
        ezdxf.readfile(source), source, plan_detection=plan_detection)


def learn_style_map(records):
    """Learn only repeated, high-confidence style/category relationships."""
    by_plan = defaultdict(list)
    for index, record in enumerate(records):
        by_plan[str(record.get("plan_title") or "Plan")].append(index)
    votes = defaultdict(Counter)
    for indexes in by_plan.values():
        valid = [records[index].get("geometry") for index in indexes]
        valid = [geometry for geometry in valid
                 if geometry is not None and not geometry.is_empty]
        if not valid:
            continue
        xmin = min(geometry.bounds[0] for geometry in valid)
        ymin = min(geometry.bounds[1] for geometry in valid)
        xmax = max(geometry.bounds[2] for geometry in valid)
        ymax = max(geometry.bounds[3] for geometry in valid)
        diagonal = max(1e-9, math.hypot(xmax - xmin, ymax - ymin))
        for index in indexes:
            record = records[index]
            if str(record.get("entity_type") or "").upper() not in TEXT_TYPES:
                continue
            category = category_for_text(record.get("text_value"))
            point = record.get("geometry")
            if not category or point is None or point.is_empty:
                continue
            nearest = None
            for candidate_index in indexes:
                candidate = records[candidate_index]
                if str(candidate.get("entity_type") or "").upper() in TEXT_TYPES:
                    continue
                geometry = candidate.get("geometry")
                if geometry is None or geometry.is_empty:
                    continue
                distance = point.distance(geometry)
                if distance <= diagonal * 0.025 and (nearest is None or distance < nearest[0]):
                    nearest = (distance, candidate)
            if nearest is not None:
                votes[_style_signature(nearest[1])][category] += 1
    result = {}
    for signature, categories in votes.items():
        category, category_votes = categories.most_common(1)[0]
        total = sum(categories.values())
        if category_votes >= 2 and category_votes / total >= 0.75:
            result[signature] = category
    return result


def classify_records(records, style_hints=None, plan_style_hints=None,
                     dominant_style_hints=None, allowed_categories=None,
                     manual_rules=None):
    """Group records with file-local evidence and exact human-review rules.

    Explicit layers and unambiguous legend styles are preferred. A small set of
    constrained spatial relationships may attach details to already classified
    geometry; anything uncertain remains reviewable rather than being guessed.
    """
    copied = [dict(record) for record in records]

    def polygon_parts(geometry):
        if geometry is None or geometry.is_empty:
            return []
        if geometry.geom_type == "Polygon":
            return [geometry]
        if geometry.geom_type in {"MultiPolygon", "GeometryCollection"}:
            result = []
            for part in geometry.geoms:
                result.extend(polygon_parts(part))
            return result
        return []
    shared_style_map = dict(style_hints or {})
    plan_style_map = dict(plan_style_hints or {})
    dominant_style_map = dict(dominant_style_hints or {})
    allowed = {str(value) for value in (allowed_categories or ()) if str(value)}
    if allowed:
        shared_style_map = {
            signature: category for signature, category in shared_style_map.items()
            if category in allowed
        }
        plan_style_map = {
            key: category for key, category in plan_style_map.items()
            if category in allowed
        }
        dominant_style_map = {
            signature: category for signature, category in dominant_style_map.items()
            if category in allowed
        }
    by_plan = defaultdict(list)
    for index, record in enumerate(copied):
        by_plan[str(record.get("plan_title") or "Plan")].append(index)

    for plan_title, indexes in by_plan.items():
        environment_boundaries = {}
        if "environment" in plan_title.casefold():
            closed = [copied[index] for index in indexes
                      if str(copied[index].get("entity_type") or "").upper()
                      in {"LWPOLYLINE", "POLYLINE", "SPLINE"}
                      and getattr(copied[index].get("geometry"), "geom_type", "")
                      in {"Polygon", "MultiPolygon"}]
            route_boundaries = [record for record in closed
                                if _normalise(_original_layer(record)) == "ROUTES"]
            if route_boundaries:
                outer = max(route_boundaries, key=lambda item: item["geometry"].area)
                outer_area = outer["geometry"].area
                environment_boundaries[str(outer.get("entity_handle"))] = "500 M. BOUNDARY"
                middle = [record for record in closed
                          if _normalise(_original_layer(record)) == "0"
                          and outer_area * 0.05 <= record["geometry"].area <= outer_area * 0.50]
                if middle:
                    barrier = max(middle, key=lambda item: item["geometry"].area)
                    environment_boundaries[str(barrier.get("entity_handle"))] = "60 M. BOUNDARY"
        for index in indexes:
            record = copied[index]
            entity = str(record.get("entity_type") or "").upper()
            geometry = record.get("geometry")
            category = None
            method = None
            confidence = None
            boundary_category = environment_boundaries.get(str(record.get("entity_handle")))
            if boundary_category and (not allowed or boundary_category in allowed):
                category = boundary_category
                method, confidence = "environment_boundary_geometry", 0.94
            if entity in TEXT_TYPES:
                direct_map_category = category_for_map_annotation(
                    record.get("text_value"), plan_title)
                category = category_for_text(record.get("text_value")) or direct_map_category
                if category and (direct_map_category or not allowed or category in allowed):
                    method, confidence = (
                        ("map_annotation", 0.95) if direct_map_category
                        else ("text_keyword", 0.98))
                else:
                    category = None
            if category is None:
                category = category_for_layer(_original_layer(record))
                if category and (not allowed or category in allowed):
                    method, confidence = "cad_layer", 0.95
                else:
                    category = None
            if category is None:
                category = category_for_block(record)
                if category and (not allowed or category in allowed):
                    method, confidence = "named_block_or_attribute", 0.96
                else:
                    category = None
            if category is None:
                plan_id = str(record.get("plan_id") or "")
                category = plan_style_map.get((plan_id, _style_signature(record)))
                if category:
                    method, confidence = "plan_legend_style", 0.93
            if category is None:
                category = shared_style_map.get(_style_signature(record))
                if category:
                    method, confidence = "legend_style", 0.90
            if category is None:
                dominant = dominant_style_map.get(_style_signature(record))
                geometry_type = getattr(geometry, "geom_type", "")
                if dominant == "CONTOUR" and entity in {
                        "LINE", "LWPOLYLINE", "POLYLINE", "SPLINE"} \
                        and geometry_type not in {"Polygon", "MultiPolygon"}:
                    category = dominant
                    method, confidence = "dominant_contour_legend_style", 0.84
            if category is None:
                category = "UNCLASSIFIED - REVIEW"
                method, confidence = "unclassified", 0.0

            record["original_cad_layer"] = _original_layer(record)
            record["semantic_category"] = category
            record["classification_method"] = method
            record["classification_confidence"] = confidence
            record["cad_layer"] = f"{plan_title} / {category}"

    # Use spatial propagation only from already explicit, high-confidence
    # geometry.  This captures pit bench/RL details and contour labels without
    # letting an arbitrary nearby annotation relabel unrelated sheet content.
    for plan_title, indexes in by_plan.items():
        pit_polygons = [part for index in indexes
                        if copied[index].get("semantic_category") == "EXIST. PIT"
                        for part in polygon_parts(copied[index].get("geometry"))]
        for index in indexes:
            record = copied[index]
            if record.get("semantic_category") != "UNCLASSIFIED - REVIEW":
                continue
            geometry = record.get("geometry")
            if geometry is None or geometry.is_empty:
                continue
            if pit_polygons:
                covered = any(polygon.covers(geometry.representative_point())
                              and geometry.envelope.area <= polygon.envelope.area * 1.05
                              for polygon in pit_polygons)
                if covered:
                    record["semantic_category"] = "EXIST. PIT"
                    record["classification_method"] = "inside_explicit_pit"
                    record["classification_confidence"] = 0.86
                    record["cad_layer"] = f"{plan_title} / EXIST. PIT"

        contour_geometries = [copied[index]["geometry"] for index in indexes
                              if copied[index].get("semantic_category") == "CONTOUR"
                              and copied[index].get("geometry") is not None]
        symbol_categories = {
            "EXIST. WELL", "EXIST. BORE HOLE", "PROP. BORE HOLE",
            "BENCH MARK", "SURVEY STATION", "TREE", "Boundary pillars",
        }
        area_categories = {
            "PROP. OFFICE", "PROP. LABOUR SHED", "PROP. SETTLING TANK",
            "PROP. MINERAL STACK YARD", "PROP. MINERAL REJECT DUMP",
            "PROP. BACKFILL DUMP", "PROP. RETAINING WALL", "PROP. PLANTATION",
            "EXIST. PIT", "EXIST. DUMP", "EXIST. SCREENING", "HUTMENTS",
            "WATER RESERVOIR AREA", "SOIL", "MINERALIZED BODY",
        }
        linear_categories = {
            "LEASE BOUNDARY", "7.5M. STATUTORY BARRIER", "60 M. BOUNDARY",
            "500 M. BOUNDARY", "ELECTRIC LINE", "EXIST. ROAD",
            "PROP. HAULAGE ROAD", "PROP. FENCING", "PROP. GARLAND DRAIN",
            "SECTION LINE", "GRID LINE", "ULTIMATE PIT LIMIT",
        }
        semantic_labels = [copied[index] for index in indexes
                           if copied[index].get("semantic_category") not in {
                               None, "UNCLASSIFIED - REVIEW"}
                           and str(copied[index].get("entity_type") or "").upper() in TEXT_TYPES
                           and copied[index].get("geometry") is not None]
        labelled_points = [record for record in semantic_labels
                           if record.get("semantic_category") in symbol_categories]
        plan_geometries = [copied[index].get("geometry") for index in indexes]
        plan_geometries = [geometry for geometry in plan_geometries
                           if geometry is not None and not geometry.is_empty]
        if plan_geometries:
            plan_bounds = (
                min(geometry.bounds[0] for geometry in plan_geometries),
                min(geometry.bounds[1] for geometry in plan_geometries),
                max(geometry.bounds[2] for geometry in plan_geometries),
                max(geometry.bounds[3] for geometry in plan_geometries),
            )
            plan_diagonal = math.hypot(
                plan_bounds[2] - plan_bounds[0], plan_bounds[3] - plan_bounds[1])
        else:
            plan_diagonal = 1.0
        for index in indexes:
            record = copied[index]
            if record.get("semantic_category") != "UNCLASSIFIED - REVIEW":
                continue
            geometry = record.get("geometry")
            if geometry is None or geometry.is_empty:
                continue
            entity = str(record.get("entity_type") or "").upper()
            text = _normalise(record.get("text_value"))
            height = float(record.get("text_height") or 1.0)
            if entity in TEXT_TYPES and re.fullmatch(r"\d{2,4}(?:\s+\d+)?", text):
                if contour_geometries and min(geometry.distance(line) for line in contour_geometries) \
                        <= max(4.0, height * 2.5):
                    record["semantic_category"] = "CONTOUR"
                    record["classification_method"] = "attached_contour_label"
                    record["classification_confidence"] = 0.88
                    record["cad_layer"] = f"{plan_title} / CONTOUR"
                    continue
            # A semantic label placed inside one closed area is strong local
            # evidence even when both objects live on generic layer 0.
            if geometry.geom_type in {"Polygon", "MultiPolygon"}:
                inside = {label["semantic_category"] for label in semantic_labels
                          if label.get("semantic_category") in area_categories
                          and geometry.covers(label["geometry"].representative_point())}
                if len(inside) == 1:
                    category = inside.pop()
                    record["semantic_category"] = category
                    record["classification_method"] = "label_inside_closed_area"
                    record["classification_confidence"] = 0.91
                    record["cad_layer"] = f"{plan_title} / {category}"
                    continue

            # Symbols are frequently exploded into several arcs, circles and
            # short polylines. Attach each small component only when nearby
            # semantic labels agree on one category and colour/distance gives
            # direct visual evidence. Long map lines are deliberately excluded.
            if entity not in {
                    "CIRCLE", "HATCH", "INSERT", "POINT", "ARC", "ELLIPSE",
                    "LINE", "LWPOLYLINE", "POLYLINE", "SPLINE"}:
                continue
            xmin, ymin, xmax, ymax = geometry.bounds
            span = math.hypot(xmax - xmin, ymax - ymin)
            candidates = []
            for label in labelled_points:
                label_height = float(label.get("text_height") or 1.0)
                maximum_span = max(label_height * 24.0, plan_diagonal * 0.035)
                if span > maximum_span:
                    continue
                distance = geometry.distance(label["geometry"])
                same_color = str(record.get("color_hex") or "").upper() == \
                    str(label.get("color_hex") or "").upper()
                limit = max(4.0, label_height * (10.0 if same_color else 3.0))
                if distance <= limit:
                    candidates.append((distance, label["semantic_category"], same_color))
            categories = {candidate[1] for candidate in candidates}
            if len(categories) == 1 and candidates:
                distance, category, same_color = min(candidates, key=lambda item: item[0])
                record["semantic_category"] = category
                record["classification_method"] = "connected_symbol_or_leader"
                record["classification_confidence"] = 0.89 if same_color else 0.84
                record["cad_layer"] = f"{plan_title} / {category}"
                continue

            # A long linear feature may have its name printed directly on it.
            # Require the label and line to share a colour and require every
            # nearby eligible label to agree, preventing a crossing line from
            # being claimed by an unrelated annotation.
            if entity in {"LINE", "LWPOLYLINE", "POLYLINE", "SPLINE"} \
                    and geometry.geom_type not in {"Polygon", "MultiPolygon"}:
                line_candidates = []
                for label in semantic_labels:
                    if label.get("semantic_category") not in linear_categories:
                        continue
                    if str(record.get("color_hex") or "").upper() != \
                            str(label.get("color_hex") or "").upper():
                        continue
                    label_height = float(label.get("text_height") or 1.0)
                    distance = geometry.distance(label["geometry"])
                    if distance <= max(4.0, label_height * 10.0):
                        line_candidates.append((distance, label["semantic_category"]))
                line_categories_found = {candidate[1] for candidate in line_candidates}
                if len(line_categories_found) == 1:
                    category = line_categories_found.pop()
                    record["semantic_category"] = category
                    record["classification_method"] = "same_colour_linear_label"
                    record["classification_confidence"] = 0.87
                    record["cad_layer"] = f"{plan_title} / {category}"

    # Near-identical geometry repeated on separately copied/aligned plan plates
    # represents the same real-world feature. Propagate only when multiple plan
    # copies agree on one already-classified category.
    with contextlib.suppress(Exception):
        from shapely import force_2d, normalize, set_precision
        repeated = defaultdict(list)
        for index, record in enumerate(copied):
            geometry = record.get("geometry")
            if geometry is None or geometry.is_empty:
                continue
            canonical = normalize(set_precision(force_2d(geometry), 0.25))
            text = _normalise(record.get("text_value"))
            repeated[(geometry.geom_type, text, canonical.wkb)].append(index)
        for indexes in repeated.values():
            plan_ids = {str(copied[index].get("plan_id") or "") for index in indexes}
            if len(plan_ids) < 2:
                continue
            categories = {copied[index].get("semantic_category") for index in indexes
                          if copied[index].get("semantic_category") != "UNCLASSIFIED - REVIEW"}
            if len(categories) != 1:
                continue
            category = categories.pop()
            for index in indexes:
                record = copied[index]
                if record.get("semantic_category") != "UNCLASSIFIED - REVIEW":
                    continue
                plan_title = str(record.get("plan_title") or "Plan")
                record["semantic_category"] = category
                record["classification_method"] = "repeated_aligned_geometry"
                record["classification_confidence"] = 0.92
                record["cad_layer"] = f"{plan_title} / {category}"

    # Human review is the final authority for this exact drawing. Entity rules
    # affect one source object; matching-style rules remain constrained to the
    # reviewed plan and original CAD layer.
    manual_rules = [rule for rule in (manual_rules or ()) if isinstance(rule, dict)]
    for record in copied:
        # The review window only resolves uncertain objects. Never let a broad
        # style decision replace an explicit layer or legend classification.
        if record.get("semantic_category") != "UNCLASSIFIED - REVIEW":
            continue
        plan_id = str(record.get("plan_id") or "")
        handle = str(record.get("entity_handle") or "")
        original_layer = str(record.get("original_cad_layer") or _original_layer(record))
        signature = _style_signature(record)
        matching = None
        for rule in manual_rules:
            if str(rule.get("plan_id") or "") != plan_id:
                continue
            scope = str(rule.get("scope") or "entity")
            if scope == "entity" and handle and \
                    str(rule.get("entity_handle") or "") == handle:
                matching = rule
                break
            if scope == "style" and tuple(rule.get("signature") or ()) == signature and \
                    str(rule.get("original_layer") or "") == original_layer:
                matching = rule
        if matching is None:
            continue
        category = str(matching.get("category") or "").strip()
        if not category or category == "UNCLASSIFIED - REVIEW":
            continue
        plan_title = str(record.get("plan_title") or "Plan")
        record["semantic_category"] = category
        record["classification_method"] = (
            "manual_entity_rule" if matching.get("scope") == "entity"
            else "manual_matching_style_rule")
        record["classification_confidence"] = 1.0
        record["cad_layer"] = f"{plan_title} / {category}"

    counts = Counter(record.get("semantic_category") for record in copied)
    methods = Counter(record.get("classification_method") for record in copied)
    plan_summaries = {}
    for plan_title, indexes in by_plan.items():
        plan_counts = Counter(copied[index].get("semantic_category") for index in indexes)
        plan_summaries[plan_title] = dict(plan_counts.most_common())

    total = len(copied)
    unclassified = counts.get("UNCLASSIFIED - REVIEW", 0)
    return copied, {
        "total_records": total,
        "classified_records": total - unclassified,
        "unclassified_records": unclassified,
        "classified_percent": round((total - unclassified) * 100 / total, 2) if total else 0,
        "categories": dict(counts.most_common()),
        "methods": dict(methods.most_common()),
        "shared_style_rules": len(shared_style_map),
        "plan_style_rules": len(plan_style_map),
        "dominant_style_rules": len(dominant_style_map),
        "manual_rules_loaded": len(manual_rules),
        "allowed_legend_categories": sorted(allowed),
        "classification_policy": (
            "explicit_layers_named_blocks_file_local_legend_constrained_geometry_"
            "repeated_plan_evidence_and_exact_manual_review"),
        "plans": plan_summaries,
        "review_required": True,
    }
