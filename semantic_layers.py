"""Deterministic semantic grouping for government-review Google Earth files."""
from __future__ import annotations

from collections import Counter, defaultdict
import contextlib
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
    ("PROP. YEAR WISE WORKING", (r"\bPROP(?:OSED)?\b.*\bYEAR\b.*\bWORK", 
                                  r"\bYEAR WISE (?:PIT|WORK)")),
    ("PROP. MINERAL REJECT DUMP", (r"\bMINERAL REJECT DUMP\b", r"\bMRL\b.*\bDUMP\b")),
    ("PROP. MINERAL STACK YARD", (r"\bMINERAL (?:STACK|STOCK) YARD\b",)),
    ("PROP. BACKFILL DUMP", (r"\bBACKFILL DUMP\b",)),
    ("PROP. RETAINING WALL", (r"\bRETAINING WALL\b",)),
    ("PROP. GARLAND DRAIN", (r"\bGARLAND DRAIN\b",)),
    ("PROP. SETTLING TANK", (r"\bSETTLING TANK\b",)),
    ("PROP. FENCING", (r"\bFENCING\b",)),
    ("PROP. PLANTATION", (r"\bPLANTATION\b",)),
    ("PROP. BORE HOLE", (r"\bPBH\s*[- ]?\d*\b", r"\bBORE HOLE\b")),
    ("PROP. OFFICE", (r"\bPROP(?:OSED)?\b.*\bOFFICE\b", r"\bSTORE ROOM\b.*\bOFFICE\b")),
    ("PROP. HAULAGE ROAD", (r"\bHAULAGE ROAD\b",)),
    ("EXIST. ROAD", (r"\bEXIST(?:ING)?\b.*\bROAD\b", r"\bROAD\s*/?\s*RASTA\b")),
    ("EXIST. DUMP", (r"\bEXIST(?:ING)?\b.*\bDUMP\b",)),
    ("EXIST. PIT", (r"\bEXIST(?:ING)?\b.*\bPIT\b", r"\bPIT\s*[- ]?\d+\b", r"\bQUARRY\b")),
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


def _raw_entity_style(entity, doc):
    """Return a signature compatible with extracted-record style signatures."""
    entity_type = entity.dxftype().upper()
    family = "AREA" if entity_type in {"HATCH", "SOLID", "TRACE", "3DFACE"} else "LINE"
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


def learn_legend_styles_from_document(doc, source_label="loaded drawing"):
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
        observations.append({
            "category": category,
            "label": text.strip(),
            "label_handle": getattr(label.dxf, "handle", None),
            "sample_handle": getattr(sample.dxf, "handle", None),
            "signature": _raw_entity_style(sample, doc),
        })

    votes = defaultdict(Counter)
    for observation in observations:
        votes[observation["signature"]][observation["category"]] += 1
    rules = {}
    ambiguous = []
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
    return rules, {
        "source_drawing": str(source),
        "legend_labels_found": len(observations),
        "style_rules_learned": len(rules),
        "categories_found": sorted({item["category"] for item in observations}),
        "observations": observations,
        "ambiguous_styles": ambiguous,
        "skipped": dict(skipped),
        "scope": "current_source_dxf_only",
    }


def learn_legend_styles_from_dxf(path):
    """Load one DXF and learn only the legend rules contained in that file."""
    import ezdxf

    source = Path(path).expanduser().resolve()
    return learn_legend_styles_from_document(ezdxf.readfile(source), source)


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


def classify_records(records, style_hints=None, allowed_categories=None):
    """Group records using only this drawing's explicit legend vocabulary.

    Geometry is classified by an explicit CAD layer or an unambiguous legend
    style. Proximity to a text label is deliberately not used: a nearby label
    must never pull a sheet frame, boundary, or unrelated hatch into its folder.
    """
    copied = [dict(record) for record in records]
    shared_style_map = dict(style_hints or {})
    allowed = {str(value) for value in (allowed_categories or ()) if str(value)}
    if allowed:
        shared_style_map = {
            signature: category for signature, category in shared_style_map.items()
            if category in allowed
        }
    by_plan = defaultdict(list)
    for index, record in enumerate(copied):
        by_plan[str(record.get("plan_title") or "Plan")].append(index)

    counts = Counter()
    methods = Counter()
    plan_summaries = {}
    for plan_title, indexes in by_plan.items():
        plan_counts = Counter()
        for index in indexes:
            record = copied[index]
            entity = str(record.get("entity_type") or "").upper()
            geometry = record.get("geometry")
            category = None
            method = None
            confidence = None
            if entity in TEXT_TYPES:
                category = category_for_text(record.get("text_value"))
                if category and (not allowed or category in allowed):
                    method, confidence = "text_keyword", 0.98
                else:
                    category = None
            if category is None:
                category = category_for_layer(_original_layer(record))
                if category and (not allowed or category in allowed):
                    method, confidence = "cad_layer", 0.95
                else:
                    category = None
            if category is None:
                category = shared_style_map.get(_style_signature(record))
                if category:
                    method, confidence = "legend_style", 0.90
            if category is None:
                category = "UNCLASSIFIED - REVIEW"
                method, confidence = "unclassified", 0.0

            record["original_cad_layer"] = _original_layer(record)
            record["semantic_category"] = category
            record["classification_method"] = method
            record["classification_confidence"] = confidence
            record["cad_layer"] = f"{plan_title} / {category}"
            counts[category] += 1
            methods[method] += 1
            plan_counts[category] += 1
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
        "allowed_legend_categories": sorted(allowed),
        "classification_policy": "explicit_layer_or_unambiguous_current_file_legend_style",
        "plans": plan_summaries,
        "review_required": True,
    }
