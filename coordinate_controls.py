"""Survey-control parsing, fitting, and accuracy reporting for MinePlan Earth."""
from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass

from pyproj import CRS, Transformer
from shapely.geometry import Polygon


@dataclass(frozen=True)
class ControlPoint:
    name: str
    source_x: float
    source_y: float
    latitude: float
    longitude: float
    precision_metres: float | None = None


def _finite(value, label):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number.")
    return number


def parse_angle(value, kind):
    """Parse decimal degrees or DMS and return (degrees, estimated precision m)."""
    kind = str(kind).lower()
    if kind not in {"latitude", "longitude"}:
        raise ValueError("Coordinate kind must be latitude or longitude.")
    text = (str(value).strip().upper().replace("Â°", "°")
            .replace("º", "°").replace("�", "°"))
    if not text:
        raise ValueError(f"Missing {kind}.")
    hemisphere = next((letter for letter in text if letter in "NSEW"), "")
    clean = re.sub(r"[NSEW]", "", text).strip()
    decimal_match = re.fullmatch(r"[+-]?\d+(?:\.\d+)?", clean)
    precision = None
    if decimal_match:
        result = float(clean)
        decimals = len(clean.partition(".")[2])
        precision = 111_320.0 * (10 ** -decimals) if decimals else 111_320.0
    else:
        numbers = re.findall(r"\d+(?:\.\d+)?", clean)
        if len(numbers) != 3:
            raise ValueError(
                f"Could not read {kind} '{value}'. Use decimal degrees or DMS.")
        degrees, minutes, seconds = map(float, numbers)
        if not (0 <= minutes < 60 and 0 <= seconds < 60):
            raise ValueError(f"Invalid minutes or seconds in {kind} '{value}'.")
        result = degrees + minutes / 60 + seconds / 3600
        second_decimals = len(numbers[2].partition(".")[2])
        precision = 30.92 * (10 ** -second_decimals) if second_decimals else 30.92
    if hemisphere in {"S", "W"}:
        result = -abs(result)
    elif hemisphere in {"N", "E"}:
        result = abs(result)
    limit = 90 if kind == "latitude" else 180
    if not -limit <= result <= limit:
        raise ValueError(f"{kind.title()} is outside -{limit} to {limit} degrees.")
    return result, precision


def parse_control_text(text):
    """Read CSV text: name, source_x, source_y, latitude, longitude."""
    rows = []
    reader = csv.reader(io.StringIO(str(text)))
    for line_number, values in enumerate(reader, 1):
        if not values or not any(str(value).strip() for value in values):
            continue
        if str(values[0]).strip().startswith("#"):
            continue
        if len(values) < 5:
            raise ValueError(
                f"Line {line_number}: expected name, source X, source Y, latitude, longitude.")
        if line_number == 1 and str(values[1]).strip().lower() in {"source_x", "x", "easting"}:
            continue
        name = str(values[0]).strip() or f"P{len(rows) + 1}"
        source_x = _finite(values[1], f"Line {line_number} source X")
        source_y = _finite(values[2], f"Line {line_number} source Y")
        latitude, lat_precision = parse_angle(values[3], "latitude")
        longitude, lon_precision = parse_angle(values[4], "longitude")
        rows.append(ControlPoint(
            name=name, source_x=source_x, source_y=source_y,
            latitude=latitude, longitude=longitude,
            precision_metres=max(lat_precision, lon_precision)))
    if len(rows) < 2:
        raise ValueError("At least two matched control points are required; four or more are recommended.")
    return rows


def controls_to_text(points):
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(("name", "source_x", "source_y", "latitude", "longitude"))
    for point in points:
        writer.writerow((point.name, f"{point.source_x:.8f}", f"{point.source_y:.8f}",
                         f"{point.latitude:.10f}", f"{point.longitude:.10f}"))
    return stream.getvalue()


def suggested_utm(points):
    mean_lon = sum(point.longitude for point in points) / len(points)
    mean_lat = sum(point.latitude for point in points) / len(points)
    zone = max(1, min(60, int((mean_lon + 180) // 6) + 1))
    return f"EPSG:{(32600 if mean_lat >= 0 else 32700) + zone}"


def _fit_similarity(source, target):
    if len(source) != len(target) or len(source) < 2:
        raise ValueError("Similarity fitting requires at least two matched points.")
    count = len(source)
    sx = sum(point[0] for point in source) / count
    sy = sum(point[1] for point in source) / count
    tx = sum(point[0] for point in target) / count
    ty = sum(point[1] for point in target) / count
    denominator = sum((x - sx) ** 2 + (y - sy) ** 2 for x, y in source)
    if denominator <= 1e-18:
        raise ValueError("The source control points do not span a usable distance.")
    real = sum((X - tx) * (x - sx) + (Y - ty) * (y - sy)
               for (x, y), (X, Y) in zip(source, target)) / denominator
    imag = sum((Y - ty) * (x - sx) - (X - tx) * (y - sy)
               for (x, y), (X, Y) in zip(source, target)) / denominator
    scale = math.hypot(real, imag)
    if scale <= 0 or not math.isfinite(scale):
        raise ValueError("The fitted coordinate scale is invalid.")
    rotation = math.degrees(math.atan2(imag, real))
    offset_x = tx - real * sx + imag * sy
    offset_y = ty - imag * sx - real * sy
    return scale, rotation, offset_x, offset_y


def apply_similarity(x, y, scale, rotation_degrees, offset_x, offset_y):
    angle = math.radians(rotation_degrees)
    cosine, sine = math.cos(angle), math.sin(angle)
    return (x * scale * cosine - y * scale * sine + offset_x,
            x * scale * sine + y * scale * cosine + offset_y)


def correction_after_existing_affine(report, existing_affine):
    """Return the similarity applied after an existing CAD-to-target affine."""
    if len(existing_affine) != 6:
        raise ValueError("The existing plan-alignment transformation is invalid.")
    ea, eb, ed, ee, ex, ey = map(float, existing_affine)
    determinant = ea * ee - eb * ed
    if abs(determinant) <= 1e-18:
        raise ValueError("The existing plan-alignment transformation cannot be inverted.")
    inverse = (ee / determinant, -eb / determinant,
               -ed / determinant, ea / determinant)
    angle = math.radians(float(report["rotation_degrees"]))
    scale = float(report["scale"])
    da, db = scale * math.cos(angle), -scale * math.sin(angle)
    dd, de = scale * math.sin(angle), scale * math.cos(angle)
    ca = da * inverse[0] + db * inverse[2]
    cb = da * inverse[1] + db * inverse[3]
    cd = dd * inverse[0] + de * inverse[2]
    ce = dd * inverse[1] + de * inverse[3]
    # Similarity matrices must retain the [a -b; b a] structure.
    mismatch = max(abs(ca - ce), abs(cb + cd))
    if mismatch > 1e-7:
        raise ValueError(
            "The detected plan alignment is not a similarity transformation; "
            "a survey professional must review the drawing.")
    ca = (ca + ce) / 2
    cd = (cd - cb) / 2
    cb, ce = -cd, ca
    desired_x, desired_y = float(report["offset_x"]), float(report["offset_y"])
    offset_x = desired_x - (ca * ex + cb * ey)
    offset_y = desired_y - (cd * ex + ce * ey)
    return {
        "scale": math.hypot(ca, cd),
        "rotation_degrees": math.degrees(math.atan2(cd, ca)),
        "offset_x": offset_x, "offset_y": offset_y,
    }


def _polygon_area(points):
    if len(points) < 3:
        return None
    polygon = Polygon(points)
    return abs(float(polygon.area)) if not polygon.is_empty else None


def solve(points, target_crs=None, holdout_index=None):
    """Fit local CAD XY to projected survey controls and report residuals."""
    points = list(points)
    if len(points) < 2:
        raise ValueError("At least two matched control points are required.")
    target_crs = target_crs or suggested_utm(points)
    crs = CRS.from_user_input(target_crs)
    if not crs.is_projected:
        raise ValueError("Use a projected CRS with metre-based coordinates, such as a WGS 84 UTM EPSG code.")
    units = {str(axis.unit_name).lower() for axis in crs.axis_info}
    factors = {float(axis.unit_conversion_factor or 1.0) for axis in crs.axis_info}
    if not any("metre" in unit or "meter" in unit for unit in units) or \
            any(abs(factor - 1.0) > 1e-9 for factor in factors):
        raise ValueError("The coordinate-control wizard currently requires a projected CRS in metres.")
    transformer = Transformer.from_crs(4326, crs, always_xy=True)
    source = [(point.source_x, point.source_y) for point in points]
    target = [transformer.transform(point.longitude, point.latitude, errcheck=True)
              for point in points]
    if holdout_index is not None:
        holdout_index = int(holdout_index)
        if not 0 <= holdout_index < len(points):
            raise ValueError("The verification-point index is outside the control list.")
        training = [index for index in range(len(points)) if index != holdout_index]
        if len(training) < 2:
            raise ValueError("At least two fitting points must remain after selecting a holdout.")
    else:
        training = list(range(len(points)))
    scale, rotation, offset_x, offset_y = _fit_similarity(
        [source[index] for index in training], [target[index] for index in training])
    rows = []
    training_errors = []
    all_errors = []
    transformed = []
    for index, (point, source_xy, expected) in enumerate(zip(points, source, target)):
        calculated = apply_similarity(*source_xy, scale, rotation, offset_x, offset_y)
        transformed.append(calculated)
        dx, dy = calculated[0] - expected[0], calculated[1] - expected[1]
        error = math.hypot(dx, dy)
        all_errors.append(error)
        if index in training:
            training_errors.append(error)
        rows.append({
            "name": point.name, "source_x": point.source_x, "source_y": point.source_y,
            "latitude": point.latitude, "longitude": point.longitude,
            "expected_x": expected[0], "expected_y": expected[1],
            "calculated_x": calculated[0], "calculated_y": calculated[1],
            "error_x_metres": dx, "error_y_metres": dy, "error_metres": error,
            "role": "independent_verification" if index == holdout_index else "fit",
        })
    rms = math.sqrt(sum(error ** 2 for error in training_errors) / len(training_errors))
    maximum = max(training_errors)
    holdout_error = all_errors[holdout_index] if holdout_index is not None else None
    decision_maximum = max(maximum, holdout_error or 0.0)
    if rms <= 0.5 and decision_maximum <= 1.0:
        status = "pass"
    elif rms <= 2.0 and decision_maximum <= 5.0:
        status = "review"
    else:
        status = "fail"
    precision_metres = max((point.precision_metres or 0.0) for point in points) or None
    quality_notes = []
    plausibility_warnings = []
    if len(training) < 3 and holdout_index is None and status == "pass":
        status = "review"
        quality_notes.append(
            "Only two fitting controls were supplied, so the fit has no redundant accuracy check.")
    if precision_metres is not None and precision_metres > 1.0 and status == "pass":
        status = "review"
        quality_notes.append(
            "The written coordinate precision is coarser than the strict one-metre pass limit.")
    mean_latitude = sum(point.latitude for point in points) / len(points)
    mean_longitude = sum(point.longitude for point in points) / len(points)
    if 68 <= mean_latitude <= 90 and 8 <= mean_longitude <= 37:
        plausibility_warnings.append(
            "Latitude and longitude resemble reversed Indian coordinates. Confirm their order and hemispheres.")
    # Only apply near-identity expectations when the source already resembles a
    # metre-based projected grid. Local/paper CAD coordinates may legitimately
    # need a large rotation or scale and are therefore not judged here.
    median_x = sorted(abs(point.source_x) for point in points)[len(points) // 2]
    median_y = sorted(abs(point.source_y) for point in points)[len(points) // 2]
    projected_grid_like = 100_000 <= median_x <= 1_000_000 and median_y >= 100_000
    if projected_grid_like and abs(scale - 1.0) > 0.01:
        plausibility_warnings.append(
            f"The source resembles a projected metre grid but fitted scale is {scale:.8f}; confirm units and CRS.")
    if projected_grid_like and abs(rotation) > 5.0:
        plausibility_warnings.append(
            f"The source resembles a projected grid but fitted rotation is {rotation:.4f}°; confirm axis order and CRS.")
    if plausibility_warnings and status == "pass":
        status = "review"
        quality_notes.extend(plausibility_warnings)
    source_area = _polygon_area(source)
    expected_area = _polygon_area(target)
    transformed_area = _polygon_area(transformed)
    area_difference = None
    if expected_area and transformed_area is not None:
        area_difference = abs(transformed_area - expected_area) * 100 / expected_area

    suspected = None
    if len(points) >= 4 and holdout_index is None:
        candidates = []
        for excluded in range(len(points)):
            indexes = [index for index in range(len(points)) if index != excluded]
            candidate = _fit_similarity(
                [source[index] for index in indexes], [target[index] for index in indexes])
            predicted = apply_similarity(*source[excluded], *candidate)
            excluded_error = math.dist(predicted, target[excluded])
            fit_errors = [math.dist(apply_similarity(*source[index], *candidate), target[index])
                          for index in indexes]
            candidate_rms = math.sqrt(sum(value ** 2 for value in fit_errors) / len(fit_errors))
            candidates.append((candidate_rms, excluded_error, excluded))
        candidate_rms, excluded_error, excluded = min(candidates)
        if excluded_error > max(2.0, candidate_rms * 4) and candidate_rms < rms * 0.5:
            suspected = {
                "index": excluded, "name": points[excluded].name,
                "predicted_error_metres": excluded_error,
                "rms_without_point_metres": candidate_rms,
                "action": "Review this control; it was not automatically excluded.",
            }

    return {
        "target_crs": crs.to_string(), "target_crs_name": crs.name,
        "point_count": len(points), "fit_point_count": len(training),
        "holdout_index": holdout_index,
        "scale": scale, "rotation_degrees": rotation,
        "offset_x": offset_x, "offset_y": offset_y,
        "rms_error_metres": rms, "maximum_fit_error_metres": maximum,
        "holdout_error_metres": holdout_error,
        "status": status, "thresholds_metres": {
            "pass_rms": 0.5, "pass_maximum": 1.0,
            "review_rms": 2.0, "review_maximum": 5.0,
        },
        "input_coordinate_precision_metres": precision_metres,
        "quality_notes": quality_notes,
        "plausibility_warnings": plausibility_warnings,
        "source_control_polygon_area": source_area,
        "target_control_polygon_area_square_metres": expected_area,
        "transformed_control_polygon_area_square_metres": transformed_area,
        "area_difference_percent": area_difference,
        "suspected_outlier": suspected,
        "controls": rows,
        "review_required": True,
        "scope": (
            "Mathematical fit between matched controls. Accuracy still depends on the survey "
            "datum, coordinate precision, correct point identity, and independent field verification."),
    }


def detected_control_points(plan_detection):
    """Pair a GPS-fitted first plan boundary with its ordered target controls."""
    controls = list((plan_detection or {}).get("control_points") or ())
    plans = list((plan_detection or {}).get("plans") or ())
    if not plans or not controls or not all(
            "latitude" in control and "longitude" in control for control in controls):
        return []
    boundary = list(plans[0].get("boundary") or ())
    if len(boundary) != len(controls):
        return []
    result = []
    for index, (source, control) in enumerate(zip(boundary, controls), 1):
        result.append(ControlPoint(
            name=f"P{index}", source_x=float(source[0]), source_y=float(source[1]),
            latitude=float(control["latitude"]), longitude=float(control["longitude"]),
            precision_metres=None))
    return result
