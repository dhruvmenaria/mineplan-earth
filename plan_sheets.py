"""Detect and align repeated mining-plan panels in a DXF modelspace.

The detector deliberately uses repeated lease geometry, not page borders.  A panel is
only considered alignable when the same four-corner closed outline occurs in several
places and a matching set of POINT survey controls exists elsewhere in the drawing.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from itertools import combinations, permutations, product
from pathlib import Path
from collections import defaultdict
import math
import re


@dataclass(frozen=True)
class PlanSheet:
    id: str
    title: str
    boundary_handle: str
    boundary_layer: str
    boundary: tuple[tuple[float, float], ...]
    crop_bounds: tuple[float, float, float, float]
    translate_x: float
    translate_y: float
    match_error: float
    affine: tuple[float, float, float, float, float, float] | None = None
    sheet_bounds: tuple[float, float, float, float] | None = None
    frame_handle: str | None = None


def _points(entity):
    if entity.dxftype() == "POLYLINE":
        return [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in entity.vertices]
    return [(float(p[0]), float(p[1])) for p in entity.get_points("xy")]


def _four_corner_loop(entity, tolerance=0.05):
    points = _points(entity)
    if len(points) == 5 and math.dist(points[0], points[-1]) <= tolerance:
        return points[:-1]
    if len(points) == 4 and bool(entity.closed):
        return points
    return None


def _closed_loop(entity, tolerance=0.05):
    points = _points(entity)
    closed = bool(entity.is_closed) if entity.dxftype() == "POLYLINE" else bool(entity.closed)
    if 4 <= len(points) <= 100 and closed:
        if len(points) > 4 and math.dist(points[0], points[-1]) <= tolerance:
            points = points[:-1]
        return points
    if 5 <= len(points) <= 101 and math.dist(points[0], points[-1]) <= tolerance:
        return points[:-1]
    return None


def _fingerprint(points, digits=2):
    # Canonicalise start vertex and direction so an identical closed boundary
    # groups even when AutoCAD rewrites it from another corner.
    candidates = []
    for sequence in (list(points), list(reversed(points))):
        for shift in range(len(sequence)):
            ordered = sequence[shift:] + sequence[:shift]
            x0, y0 = ordered[0]
            candidates.append(tuple((round(x - x0, digits), round(y - y0, digits))
                                    for x, y in ordered))
    return min(candidates)


def _deduplicate_occurrences(items, tolerance=0.25):
    kept = []
    for entity, points in items:
        cx = sum(x for x, _ in points) / len(points)
        cy = sum(y for _, y in points) / len(points)
        if all(math.hypot(cx - old[2], cy - old[3]) > tolerance for old in kept):
            kept.append((entity, points, cx, cy))
    return kept


def _match_control_points(boundary, survey_points):
    """Return (dx, dy, max_error, matched points) for a translated point-set match."""
    best = None
    for candidate in combinations(survey_points, 4):
        for ordered in permutations(candidate):
            deltas = [(ordered[i][0] - boundary[i][0], ordered[i][1] - boundary[i][1])
                      for i in range(4)]
            dx = sum(v[0] for v in deltas) / 4
            dy = sum(v[1] for v in deltas) / 4
            error = max(math.hypot(v[0] - dx, v[1] - dy) for v in deltas)
            if best is None or error < best[2]:
                best = (dx, dy, error, ordered)
    return best


def _gps_coordinates(texts):
    """Return unique embedded coordinates in drawing/entity order.

    Mining drawings commonly store latitude and longitude as two MTEXT lines
    without N/E prefixes. AutoCAD exports may also replace the degree symbol
    with the Unicode replacement character. Accept that format only when the
    first DMS value is a valid latitude and the second is a valid longitude.
    """
    pairs = []
    seen = set()
    for _, _, value in texts:
        text = value.upper().replace("�", "°").replace("º", "°")
        north = re.search(r"N\s*(\d{1,2})\D+(\d{1,2})\D+(\d+(?:\.\d+)?)", text)
        east = re.search(r"E\s*(\d{1,3})\D+(\d{1,2})\D+(\d+(?:\.\d+)?)", text)
        if north and east:
            latitude = tuple(float(north.group(i)) for i in range(1, 4))
            longitude = tuple(float(east.group(i)) for i in range(1, 4))
        else:
            # Examples: 27° 08'1.6"\n73° 22' 41.57" and 27° 08'.03".
            values = re.findall(
                r"(\d{1,3})\s*°\s*(\d{1,2})\s*['’′]\s*"
                r"((?:\d+(?:\.\d*)?)|(?:\.\d+))\s*[\"”″]?",
                text)
            if len(values) != 2:
                continue
            latitude = tuple(float(part) for part in values[0])
            longitude = tuple(float(part) for part in values[1])
        if not (0 <= latitude[0] <= 90 and 0 <= longitude[0] <= 180
                and 0 <= latitude[1] < 60 and 0 <= longitude[1] < 60
                and 0 <= latitude[2] < 60 and 0 <= longitude[2] < 60):
            continue
        lat = latitude[0] + latitude[1] / 60 + latitude[2] / 3600
        lon = longitude[0] + longitude[1] / 60 + longitude[2] / 3600
        key = (round(lat, 8), round(lon, 8))
        if key not in seen:
            seen.add(key)
            pairs.append((lon, lat))
    return pairs


def _dominant_gps_cluster(points, radius_degrees=0.1):
    """Discard unrelated coordinate labels far from the lease boundary.

    A tenth of a degree is roughly 11 km in latitude, comfortably larger than
    a mine lease while separating title-block examples and unrelated map grids.
    """
    points = list(points)
    if len(points) < 4:
        return points
    remaining = set(range(len(points)))
    components = []
    while remaining:
        seed = remaining.pop()
        component = {seed}
        pending = [seed]
        while pending:
            current = pending.pop()
            lon, lat = points[current]
            neighbours = {index for index in remaining
                          if math.hypot(points[index][0] - lon,
                                        points[index][1] - lat) <= radius_degrees}
            remaining.difference_update(neighbours)
            component.update(neighbours)
            pending.extend(neighbours)
        components.append(component)
    largest = max(components, key=len)
    return [point for index, point in enumerate(points) if index in largest] if len(largest) >= 4 else points


def _similarity(source, target):
    """Least-squares 2D similarity as a Shapely affine tuple and residuals."""
    n = len(source)
    sx = sum(p[0] for p in source) / n
    sy = sum(p[1] for p in source) / n
    tx = sum(p[0] for p in target) / n
    ty = sum(p[1] for p in target) / n
    denominator = sum((x - sx) ** 2 + (y - sy) ** 2 for x, y in source)
    if denominator <= 0:
        return None
    real = sum((X - tx) * (x - sx) + (Y - ty) * (y - sy)
               for (x, y), (X, Y) in zip(source, target)) / denominator
    imag = sum((Y - ty) * (x - sx) - (X - tx) * (y - sy)
               for (x, y), (X, Y) in zip(source, target)) / denominator
    xoff = tx - real * sx + imag * sy
    yoff = ty - imag * sx - real * sy
    affine = (real, -imag, imag, real, xoff, yoff)
    errors = []
    for (x, y), (X, Y) in zip(source, target):
        px = real * x - imag * y + xoff
        py = imag * x + real * y + yoff
        errors.append(math.hypot(px - X, py - Y))
    rms = math.sqrt(sum(v * v for v in errors) / n)
    return affine, rms, max(errors), math.hypot(real, imag), math.degrees(math.atan2(imag, real))


def _best_similarity(source, target):
    best = None
    for reversed_order in (False, True):
        base = list(reversed(target)) if reversed_order else list(target)
        for shift in range(len(base)):
            ordered = base[shift:] + base[:shift]
            fit = _similarity(source, ordered)
            if fit and (best is None or fit[1] < best[0][1]):
                best = (fit, ordered, reversed_order, shift)
    return best


def _gps_target_sequences(all_targets, count, duplicate_radius=25.0):
    """Build coordinate-table variants when later labels re-round earlier points."""
    if count > len(all_targets):
        return []
    base = list(all_targets[:count])
    options = [[value] for value in base]
    for value in all_targets[count:]:
        distance, index = min((math.dist(value, candidate), i)
                              for i, candidate in enumerate(base))
        if distance <= duplicate_radius:
            options[index].append(value)
    combinations_count = math.prod(len(values) for values in options)
    if combinations_count > 4096:
        return [base]
    return [list(values) for values in product(*options)]


def _plain_text(entity):
    try:
        value = entity.plain_text() if entity.dxftype() == "MTEXT" else entity.dxf.text
    except Exception:
        return ""
    return " ".join(str(value).replace("\\P", " ").split())


def _title_for(text):
    upper = re.sub(r"\s+", " ", text.upper())
    rules = [
        # Environment plates can contain labels referring to conceptual and
        # year-wise features, so their explicit plate title must win first.
        ("ENVIRONMENT PLAN", "Environment Plan"),
        ("ENVIROMENT PLAN", "Environment Plan"),
        ("SURFACE GEOLOGICAL", "Surface geological plan & section"),
        ("FIRST YEAR", "Proposed First Year working plan & section"),
        ("SECOND YEAR", "Second Year working plan & section"),
        ("THIRD YEAR", "Third Year working plan & section"),
        ("FOURTH YEAR", "Fourth Year working plan & section"),
        ("FIFTH YEAR", "Fifth Year working plan & section"),
        ("MINE LAYOUT", "Mine Layout Plan & Section"),
        ("MINE LAY OUT", "Mine Layout Plan & Section"),
        ("PROGRESSIVE CLOSURE", "Progressive Closure Plan & Section"),
        ("CONCEPTUAL", "Conceptual Plan & Section"),
        ("FINANCIAL ASSURANCE", "Financial Assurance Area Plan"),
        ("SURFACE PLAN", "Surface plan"),
    ]
    for needle, title in rules:
        if needle in upper:
            return title
    return ""


def _candidate_frames(modelspace):
    result = []
    for entity in modelspace.query("LWPOLYLINE"):
        points = _points(entity)
        if not (entity.closed and 4 <= len(points) <= 8):
            continue
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        bounds = (min(xs), min(ys), max(xs), max(ys))
        width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
        # Ordinary plan plates are roughly 250-500 drawing units wide.  An
        # environment plate often covers a 500 m study radius and therefore
        # uses a much larger sheet frame.  Accept that frame here so its title
        # is associated with the copied lease boundary inside it.
        if 250 <= width <= 2500 and 250 <= height <= 2000:
            result.append((entity, bounds))
    return result


def detect(path, *, margin=25.0):
    """Detect repeated plan panels and their exact translation to survey controls."""
    import ezdxf

    source = Path(path).expanduser().resolve()
    if source.suffix.lower() != ".dxf":
        return {"available": False, "reason": "Plan-sheet detection currently requires DXF input.", "plans": []}
    doc = ezdxf.readfile(source)
    model = doc.modelspace()

    texts = []
    for entity in model:
        if entity.dxftype() in {"TEXT", "MTEXT"}:
            p = entity.dxf.insert
            texts.append((float(p.x), float(p.y), _plain_text(entity)))
    embedded_gps = _dominant_gps_cluster(_gps_coordinates(texts))

    groups = defaultdict(list)
    for entity in model.query("LWPOLYLINE POLYLINE"):
        points = _closed_loop(entity)
        if points:
            groups[_fingerprint(points)].append((entity, points))

    repeated = []
    for fingerprint, items in groups.items():
        unique = _deduplicate_occurrences(items)
        if len(unique) >= 2:
            repeated.append((len(unique), len(items), fingerprint, unique))
    if not repeated:
        return {"available": False, "reason": "No repeated closed lease outline was found.", "plans": []}
    survey_points = []
    for entity in model.query("POINT"):
        p = entity.dxf.location
        if math.isfinite(float(p.x)) and math.isfinite(float(p.y)):
            survey_points.append((float(p.x), float(p.y), str(entity.dxf.handle)))
    # First support drawings that retain four explicit survey POINT controls.
    matched_groups = []
    for unique_count, duplicate_count, fingerprint, occurrences in repeated:
        if len(occurrences[0][1]) != 4:
            continue
        control = _match_control_points(occurrences[0][1], survey_points)
        # Survey control POINTs in legacy plans are often rounded to whole
        # drawing units while the copied outline retains decimals.
        if control and control[2] <= 1.5:
            xs = [item[2] for item in occurrences]
            matched_groups.append((unique_count, max(xs) - min(xs), duplicate_count,
                                   fingerprint, occurrences, control))
    alignment_kind = "points"
    suggested_crs = None
    gps_targets = None
    if matched_groups:
        _, _, duplicate_count, _, occurrences, control = max(
            matched_groups, key=lambda item: (item[0], item[1], item[2]))
        first_boundary = occurrences[0][1]
        control_dx, control_dy, control_error, control_points = control
        controls_output = [{"x": p[0], "y": p[1], "handle": p[2]} for p in control_points]
        method = "Repeated four-corner lease boundary matched to four DXF POINT survey controls"
    else:
        # Some approved-plan drawings provide every lease vertex as an embedded
        # latitude/longitude label instead of DXF POINT controls.
        gps = embedded_gps
        gps_matches = []
        if len(gps) >= 4:
            from pyproj import Transformer
            mean_lon = sum(p[0] for p in gps) / len(gps)
            mean_lat = sum(p[1] for p in gps) / len(gps)
            zone = int((mean_lon + 180) // 6) + 1
            epsg = (32600 if mean_lat >= 0 else 32700) + zone
            transformer = Transformer.from_crs(4326, epsg, always_xy=True)
            all_target_xy = [transformer.transform(lon, lat) for lon, lat in gps]
            for unique_count, duplicate_count, fingerprint, candidate_occurrences in repeated:
                boundary = candidate_occurrences[0][1]
                if len(boundary) > len(all_target_xy) or unique_count < 2:
                    continue
                # A second plan may repeat a few labels with slightly different
                # rounding. The first complete table in entity order is retained;
                # extra later duplicates must not prevent matching the boundary.
                target_sets = _gps_target_sequences(all_target_xy, len(boundary))
                for target_xy in target_sets:
                    match = _best_similarity(boundary, target_xy)
                    if not match:
                        continue
                    fit, ordered, reverse, shift = match
                    affine, rms, maximum, scale, rotation = fit
                    if .8 <= scale <= 1.2 and rms <= 5 and maximum <= 10:
                        span = max(item[2] for item in candidate_occurrences) - min(item[2] for item in candidate_occurrences)
                        gps_matches.append((rms, -unique_count, -span, duplicate_count,
                                            candidate_occurrences, ordered, fit, epsg))
        if not gps_matches:
            return {"available": False,
                    "reason": "Repeated panels were found, but neither matching DXF POINT controls nor a complete GPS-labelled lease boundary was found.",
                    "plans": []}
        _, _, _, duplicate_count, occurrences, gps_targets, first_fit, epsg = min(gps_matches)
        alignment_kind = "gps_similarity"
        suggested_crs = f"EPSG:{epsg}"
        control_error = first_fit[1]
        inverse = Transformer.from_crs(epsg, 4326, always_xy=True)
        controls_output = []
        for x, y in gps_targets:
            lon, lat = inverse.transform(x, y)
            controls_output.append({"longitude": lon, "latitude": lat, "x": x, "y": y})
        method = ("Repeated lease boundary fitted to embedded latitude/longitude labels "
                  "with a least-squares similarity transformation")

    frames = _candidate_frames(model)
    title_markers = [(x, y, title) for x, y, value in texts
                     if (title := _title_for(value))]

    sheets = []
    for _, (entity, boundary, cx, cy) in enumerate(sorted(occurrences, key=lambda item: item[2])):
        if alignment_kind == "points":
            # Every copy is an exact translation of the repeated outline.
            copy_dx = first_boundary[0][0] - boundary[0][0]
            copy_dy = first_boundary[0][1] - boundary[0][1]
            dx, dy = copy_dx + control_dx, copy_dy + control_dy
            affine = (1.0, 0.0, 0.0, 1.0, dx, dy)
            plan_error = control_error
        else:
            match = _best_similarity(boundary, gps_targets)
            fit = match[0]
            affine, plan_error = fit[0], fit[1]
            dx, dy = affine[4], affine[5]
        xmin = min(x for x, _ in boundary) - margin
        ymin = min(y for _, y in boundary) - margin
        xmax = max(x for x, _ in boundary) + margin
        ymax = max(y for _, y in boundary) + margin

        containing = [(frame_entity, bounds) for frame_entity, bounds in frames
                      if bounds[0] <= cx <= bounds[2] and bounds[1] <= cy <= bounds[3]]
        if containing:
            frame_entity, frame = min(
                containing,
                key=lambda item: (item[1][2] - item[1][0]) * (item[1][3] - item[1][1]))
            panel_text = " ".join(value for x, y, value in texts
                                  if frame[0] <= x <= frame[2] and frame[1] <= y <= frame[3])
        else:
            frame_entity, frame = None, (xmin, ymin, xmax, ymax)
            panel_text = " ".join(value for x, y, value in texts
                                  if xmin <= x <= xmax and ymin <= y <= ymax)
        title = _title_for(panel_text)
        if not title and title_markers:
            title = min(title_markers, key=lambda item: abs(item[0] - cx))[2]
        title = title or f"Plan {len(sheets) + 1}"
        sheets.append(PlanSheet(
            id=f"plan-{len(sheets) + 1:02d}", title=title,
            boundary_handle=str(entity.dxf.handle), boundary_layer=str(entity.dxf.layer),
            boundary=tuple(boundary), crop_bounds=(xmin, ymin, xmax, ymax),
            translate_x=dx, translate_y=dy, match_error=plan_error,
            affine=tuple(affine), sheet_bounds=tuple(frame),
            frame_handle=(str(frame_entity.dxf.handle) if frame_entity is not None else None)))

    geographic_controls = [
        (float(item["longitude"]), float(item["latitude"]))
        for item in controls_output if "longitude" in item and "latitude" in item]
    location_source = "matched GPS boundary labels"
    if not geographic_controls and embedded_gps:
        geographic_controls = embedded_gps
        location_source = "embedded GPS labels"
        mean_lon = sum(p[0] for p in embedded_gps) / len(embedded_gps)
        mean_lat = sum(p[1] for p in embedded_gps) / len(embedded_gps)
        zone = int((mean_lon + 180) // 6) + 1
        suggested_crs = f"EPSG:{(32600 if mean_lat >= 0 else 32700) + zone}"
    location = None
    if geographic_controls:
        longitudes = [p[0] for p in geographic_controls]
        latitudes = [p[1] for p in geographic_controls]
        if location_source == "embedded GPS labels":
            # The coordinates are internally consistent, but were not the
            # controls used to fit the repeated boundary in this branch.
            confidence = "review"
        elif control_error <= 1:
            confidence = "high"
        elif control_error <= 3:
            confidence = "review"
        else:
            confidence = "low"
        place_hint = next((value for _, _, value in texts
                           if "TEH" in value.upper() and "DIST" in value.upper()), "")
        location = {
            "center_longitude": sum(longitudes) / len(longitudes),
            "center_latitude": sum(latitudes) / len(latitudes),
            "west": min(longitudes), "south": min(latitudes),
            "east": max(longitudes), "north": max(latitudes),
            "coordinate_count": len(geographic_controls),
            "source": location_source,
            "confidence": confidence,
            "place_hint": place_hint,
        }

    return {
        "available": True,
        "reason": "",
        "method": method,
        "plans": [asdict(sheet) for sheet in sheets],
        "control_points": controls_output,
        "control_match_error": control_error,
        "suggested_crs": suggested_crs,
        "location": location,
        "duplicate_boundary_entities": duplicate_count,
        "drawing_units_code": int(doc.header.get("$INSUNITS", 0) or 0),
    }


def align_records(records, detection, selected_ids, *, margin=25.0, cancel=None,
                  include_plan_boundary=True, include_sheet_frame=True):
    """Clip every selected panel to its map area and translate it onto survey controls."""
    from shapely import affinity, force_2d, make_valid, normalize, set_precision
    from shapely.geometry import Polygon, box
    from shapely.ops import unary_union

    chosen = {str(value) for value in selected_ids}
    plans = [p for p in detection["plans"] if p["id"] in chosen]
    if not plans:
        raise ValueError("Select at least one detected plan sheet.")
    aligned = []
    seen = set()
    for plan in plans:
        plan_start = len(aligned)
        boundary = plan["boundary"]
        # A rectangular crop includes the title block, legend and certification
        # table that often sit inside the boundary's bounding rectangle.  The
        # actual repeated lease outline is the reliable map-area evidence.
        crop = Polygon(boundary)
        if not crop.is_valid:
            crop = make_valid(crop)
        # Environment plates cover a study radius much larger than the lease.
        # Use the largest closed map outline inside the detected sheet frame
        # (normally the 500 m boundary), while retaining the repeated lease
        # outline as the survey-control alignment evidence.
        if "environment" in str(plan.get("title", "")).casefold():
            sheet_bounds = plan.get("sheet_bounds")
            if sheet_bounds:
                sheet_box = box(*sheet_bounds)
                lease_envelope_area = max(1e-9, crop.envelope.area)
                sheet_area = max(1e-9, sheet_box.area)
                study_candidates = []
                for source in records:
                    geometry = source.get("geometry")
                    if geometry is None or geometry.is_empty:
                        continue
                    geometry = force_2d(geometry)
                    closed = geometry.geom_type == "Polygon" or (
                        geometry.geom_type == "LineString" and geometry.is_ring)
                    if not closed or not sheet_box.covers(geometry.representative_point()):
                        continue
                    envelope_area = geometry.envelope.area
                    if lease_envelope_area * 2.0 <= envelope_area <= sheet_area * 0.90:
                        study_candidates.append((envelope_area, geometry))
                if study_candidates:
                    crop = max(study_candidates, key=lambda item: item[0])[1]
                    if not crop.is_valid:
                        crop = make_valid(crop)
        crop = crop.buffer(margin, join_style="mitre")
        furniture_mask, furniture_handles = _sheet_furniture_mask(
            records, Polygon(boundary), crop, box)
        for source in records:
            if cancel:
                cancel.check()
            if not include_plan_boundary and str(source.get("entity_handle", "")) == \
                    str(plan.get("boundary_handle", "")):
                continue
            if not include_sheet_frame and plan.get("frame_handle") and \
                    str(source.get("entity_handle", "")) == str(plan.get("frame_handle")):
                continue
            if str(source.get("entity_handle", "")) in furniture_handles:
                continue
            geometry = source.get("geometry")
            if geometry is None or geometry.is_empty:
                continue
            geometry = force_2d(geometry)
            if not geometry.is_valid:
                geometry = make_valid(geometry)
            if not geometry.intersects(crop):
                continue
            clipped = geometry.intersection(crop)
            if furniture_mask is not None and not clipped.is_empty:
                clipped = clipped.difference(furniture_mask)
            if clipped.is_empty:
                continue
            if not clipped.is_valid:
                clipped = make_valid(clipped)
            if not clipped.is_valid:
                clipped = unary_union(clipped)
            affine_values = plan.get("affine")
            if affine_values:
                moved = affinity.affine_transform(clipped, affine_values)
            else:
                moved = affinity.translate(clipped, xoff=plan["translate_x"], yoff=plan["translate_y"])
            if not moved.is_valid:
                moved = make_valid(moved)
            if not moved.is_valid:
                moved = unary_union(moved)
            record = dict(source)
            original_layer = str(record.get("cad_layer") or "0")
            record["cad_layer"] = f"{plan['title']} / {original_layer}"
            record["plan_id"] = plan["id"]
            record["plan_title"] = plan["title"]
            record["geometry"] = moved
            # The source contains exact duplicate outlines.  Remove only exact,
            # same-layer duplicates after clipping/alignment.
            # AutoCAD frequently stores the same visible object more than once
            # using reversed vertex order or a second line/polyline entity.
            # Canonicalise at 1 mm in drawing coordinates, while retaining the
            # original CAD layer and label so distinct meanings are preserved.
            canonical = normalize(set_precision(force_2d(moved), 0.001))
            text = str(record.get("text_value") or "").strip().casefold()
            key = (plan["id"], original_layer.casefold(), text, canonical.wkb)
            if key not in seen:
                seen.add(key)
                aligned.append(record)
        if not include_sheet_frame and not plan.get("frame_handle"):
            boundary_xmin = min(point[0] for point in boundary)
            boundary_ymin = min(point[1] for point in boundary)
            boundary_xmax = max(point[0] for point in boundary)
            boundary_ymax = max(point[1] for point in boundary)
            boundary_area = max(1e-9, (boundary_xmax - boundary_xmin) *
                                (boundary_ymax - boundary_ymin))
            candidates = []
            for index in range(plan_start, len(aligned)):
                geometry = aligned[index].get("geometry")
                if geometry is None or geometry.is_empty:
                    continue
                closed = geometry.geom_type == "Polygon" or (
                    geometry.geom_type == "LineString" and geometry.is_ring)
                if not closed:
                    continue
                gxmin, gymin, gxmax, gymax = geometry.bounds
                area = max(0.0, (gxmax - gxmin) * (gymax - gymin))
                if area >= boundary_area * 1.25:
                    candidates.append((area, index))
            if candidates:
                largest_area = max(area for area, _ in candidates)
                # Remove coincident frame outlines/fills, but not unrelated
                # map geometry merely larger than the lease boundary.
                remove = {index for area, index in candidates
                          if area >= largest_area * 0.98}
                aligned[plan_start:] = [
                    record for index, record in enumerate(
                        aligned[plan_start:], start=plan_start)
                    if index not in remove]
    if not aligned:
        raise ValueError("No geometry was found inside the selected plan map areas.")
    aligned = _consolidate_shared_features(aligned, plans)
    return _place_shared_outer_boundary_with_environment(aligned, plans)


def _is_sheet_furniture_text(value):
    """Identify labels that belong to a plan sheet rather than map evidence."""
    text = re.sub(r"\s+", " ", str(value or "").replace("\\P", " ")).strip().upper()
    return bool(
        text in {"INDEX", "LEGEND", "MTS."}
        or "CERTIFIED THAT THIS PLAN" in text
        or "QUALIFIED PERSON" in text
        or "PLATE NO" in text
        or ("SCALE" in text and ("R.F." in text or "CM" in text))
        or ("LESSEE" in text and ("DISTT" in text or "TEH" in text))
        or ("PLAN" in text and "M.L.NO" in text)
    )


def _sheet_furniture_mask(records, lease, crop, box_factory):
    """Return outside-lease sheet regions associated with title/legend labels."""
    from shapely.ops import unary_union

    if not lease.is_valid:
        from shapely import make_valid
        lease = make_valid(lease)
    xmin, ymin, xmax, ymax = crop.bounds
    cx, cy = lease.centroid.x, lease.centroid.y
    safe_map = lease.buffer(2.0, join_style="mitre")
    regions = []
    handles = set()
    for record in records:
        if not _is_sheet_furniture_text(record.get("text_value")):
            continue
        geometry = record.get("geometry")
        if geometry is None or geometry.is_empty or not geometry.intersects(crop):
            continue
        handles.add(str(record.get("entity_handle", "")))
        point = geometry.representative_point()
        if safe_map.covers(point):
            continue
        dx, dy = point.x - cx, point.y - cy
        if abs(dx) >= abs(dy):
            region = box_factory(point.x - 5, ymin, xmax, ymax) if dx >= 0 \
                else box_factory(xmin, ymin, point.x + 5, ymax)
        else:
            region = box_factory(xmin, point.y - 5, xmax, ymax) if dy >= 0 \
                else box_factory(xmin, ymin, xmax, point.y + 5)
        regions.append(region.difference(safe_map))
    if not regions:
        return None, handles
    return unary_union(regions), handles


def _consolidate_shared_features(records, plans):
    """Keep geometrically identical base-map features once across plan panels.

    Repeated plan sheets copy contours, roads, lease pillars and annotations into
    every panel.  After alignment those copies stack in Google Earth.  Identical
    objects from the same original CAD layer are placed in one Shared base map
    folder.  A feature appearing in just one plan stays with that plan.
    """
    from collections import defaultdict
    from shapely import force_2d, normalize, set_precision

    if len(plans) < 2:
        return records
    groups = defaultdict(list)
    for index, record in enumerate(records):
        geometry = record.get("geometry")
        if geometry is None or geometry.is_empty:
            continue
        original_layer = str(record.get("cad_layer") or "0").split(" / ", 1)[-1]
        text = str(record.get("text_value") or "").strip().casefold()
        canonical = normalize(set_precision(force_2d(geometry), 0.001))
        groups[(original_layer.casefold(), text, canonical.wkb)].append(index)

    removed = set()
    shared = []
    for indexes in groups.values():
        plan_ids = {str(records[index].get("plan_id") or "") for index in indexes}
        if len(plan_ids) < 2:
            continue
        preferred = min(indexes)
        record = dict(records[preferred])
        original_layer = str(record.get("cad_layer") or "0").split(" / ", 1)[-1]
        record["cad_layer"] = f"Shared base map / {original_layer}"
        record["plan_id"] = "shared-base-map"
        record["plan_title"] = "Shared base map"
        record["shared_plan_count"] = len(plan_ids)
        shared.append(record)
        removed.update(indexes)
    return [record for index, record in enumerate(records) if index not in removed] + shared


def _place_shared_outer_boundary_with_environment(records, plans):
    """Keep a repeated outer plan outline once, under Environment Plan.

    Approved mine-plan sheets often copy the same map/lease outline into every
    panel. Once the panels are aligned, Google Earth draws all those copies on
    top of one another, so hiding Environment Plan appears not to hide its
    outline. If an Environment plan exists, consolidate only the largest closed
    geometry repeated across nearly every selected plan into that folder.
    """
    import math
    from collections import defaultdict
    from shapely import force_2d, get_coordinates

    def closed_signature(geometry):
        coordinates = [(round(float(x), 2), round(float(y), 2))
                       for x, y in get_coordinates(force_2d(geometry))]
        if len(coordinates) > 2 and coordinates[0] == coordinates[-1]:
            coordinates.pop()
        if len(coordinates) < 3:
            return None
        variants = []
        for sequence in (coordinates, list(reversed(coordinates))):
            for shift in range(len(sequence)):
                variants.append(tuple(sequence[shift:] + sequence[:shift]))
        return min(variants)

    environment = next((p for p in plans
                        if "environment" in str(p.get("title", "")).casefold()), None)
    if environment is None:
        return records

    groups = defaultdict(list)
    for index, record in enumerate(records):
        geometry = record.get("geometry")
        if geometry is None or geometry.is_empty:
            continue
        geometry = force_2d(geometry)
        if geometry.geom_type == "LineString" and not geometry.is_ring:
            continue
        if geometry.geom_type not in {"LineString", "Polygon"}:
            continue
        signature = closed_signature(geometry)
        if signature is not None:
            groups[signature].append(index)

    minimum_plans = max(2, math.ceil(len(plans) * 0.8)) if len(plans) >= 2 else 1
    candidates = []
    for key, indexes in groups.items():
        plan_ids = {str(records[index].get("plan_id")) for index in indexes}
        if len(plans) == 1:
            if len(indexes) < 2:
                continue
        elif len(plan_ids) < minimum_plans:
            continue
        geometry = records[indexes[0]]["geometry"]
        xmin, ymin, xmax, ymax = geometry.bounds
        envelope_area = max(0.0, (xmax - xmin) * (ymax - ymin))
        candidates.append((envelope_area, len(plan_ids), key, indexes))
    if not candidates:
        return records

    _, _, _, indexes = max(candidates, key=lambda item: (item[0], item[1]))
    environment_id = str(environment["id"])
    preferred = sorted(indexes, key=lambda index: (
        str(records[index].get("plan_id")) != environment_id,
        str(records[index].get("entity_type", "")).upper() == "HATCH",
        records[index]["geometry"].geom_type != "LineString",
    ))[0]
    shared = dict(records[preferred])
    original_layer = str(shared.get("cad_layer") or "0").split(" / ", 1)[-1]
    shared["cad_layer"] = f"{environment['title']} / {original_layer}"
    shared["plan_id"] = environment_id
    shared["plan_title"] = environment["title"]
    shared["shared_outer_boundary"] = True
    removed = set(indexes)
    return [record for index, record in enumerate(records) if index not in removed] + [shared]
