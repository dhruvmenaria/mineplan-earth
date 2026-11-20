"""MinePlan Earth: local CAD extraction and verified, styled KML/KMZ export."""
from __future__ import annotations

import copy
import io
import json
import math
import os
import tempfile
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, replace
from pathlib import Path
import xml.etree.ElementTree as ET
from shapely.geometry import Polygon, MultiPolygon
from shapely import make_valid

from vendor import cad_to_gis as cad
import plan_sheets
import semantic_layers

NS = "http://www.opengis.net/kml/2.2"
ET.register_namespace("", NS)
APP_VERSION = "0.9.2"
APP_AUTHOR = "Dhruv Menaria"


@dataclass(frozen=True)
class Options:
    input_path: str
    source_crs: str = ""
    layers: tuple[str, ...] | None = None
    altitude_mode: str = "clampToGround"
    filled_polygons: bool = False
    include_labels: bool = True
    curve_tolerance: float = 0.1
    coordinate_scale: float = 1.0
    rotation_degrees: float = 0.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    dwg_converter: str = ""
    location_confirmed: bool = False
    heights_confirmed: bool = False
    plan_ids: tuple[str, ...] | None = None
    plan_margin: float = 25.0
    document_name: str = ""
    exclude_plan_boundary: bool = False
    exclude_sheet_frame: bool = False
    semantic_folders: bool = True
    semantic_style_hints: tuple = ()
    semantic_categories: tuple[str, ...] = ()


def el(parent, tag, text=None, **attributes):
    node = ET.SubElement(parent, f"{{{NS}}}{tag}", attributes)
    if text is not None:
        node.text = str(text)
    return node


def job_for(options: Options, *, export=False):
    source = Path(options.input_path).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in {".dxf", ".dwg"}:
        raise ValueError("Choose an existing DXF or DWG file.")
    if options.layers is not None and not options.layers:
        raise ValueError("Select at least one layer.")
    if options.altitude_mode not in {"clampToGround", "absolute", "relativeToGround"}:
        raise ValueError("Unknown altitude mode.")
    if options.coordinate_scale <= 0:
        raise ValueError("Coordinate scale must be greater than zero.")
    if options.plan_ids is not None:
        if not options.plan_ids:
            raise ValueError("Select at least one detected plan sheet.")
        if not math.isfinite(options.plan_margin) or not 0 <= options.plan_margin <= 250:
            raise ValueError("Plan map margin must be between 0 and 250 drawing units.")
        if options.altitude_mode != "clampToGround":
            raise ValueError("Detected plan sheets currently export as a 2D ground overlay only.")
    if export:
        if not options.source_crs.strip():
            raise ValueError("Enter the drawing's verified coordinate system (EPSG code or WKT).")
        if not options.location_confirmed:
            raise ValueError("Confirm that the source coordinate system and units match the survey.")
        if options.altitude_mode != "clampToGround" and not options.heights_confirmed:
            raise ValueError("Confirm the units and height reference before exporting elevations.")
    # Preview works in source drawing coordinates and does not invent a CRS.
    return cad.ConversionJob(
        input_paths=(str(source),), output_directory=str(source.parent),
        dataset_name=source.stem, output_format="kml" if export else "csv",
        source_crs=options.source_crs.strip() if export else None,
        target_crs="EPSG:4326" if export else None,
        selected_layers=None if options.layers is None else frozenset(options.layers),
        include_z=True, closed_boundaries_as_polygons=True,
        curve_tolerance=options.curve_tolerance,
        coordinate_scale=options.coordinate_scale if export else 1.0,
        rotation_degrees=options.rotation_degrees if export else 0.0,
        offset_x=options.offset_x if export else 0.0,
        offset_y=options.offset_y if export else 0.0,
        dwg_converter=options.dwg_converter.strip() or None,
        repair_invalid_polygons=True, overwrite=False,
    )


def scan(options, *, cancel=None, log=lambda _: None, progress=lambda *_: None):
    return cad.scan_cad_files(job_for(options), cancel=cancel, log=log, progress=progress)


def analyze_plan_sheets(options):
    return plan_sheets.detect(options.input_path, margin=options.plan_margin)


def preview(options, *, cancel=None, log=lambda _: None, progress=lambda *_: None):
    if options.plan_ids is not None:
        job = job_for(options)
        token = cancel or cad.CancellationToken()
        report = cad.QualityReport(job)
        detection = analyze_plan_sheets(options)
        if not detection.get("available"):
            raise ValueError(detection.get("reason") or "Plan sheets could not be aligned.")
        extractor = cad.DXFExtractor(job, report, token, progress, log)
        records = extractor.extract_all()
        records = plan_sheets.align_records(records, detection, options.plan_ids,
                                             margin=options.plan_margin, cancel=token)
        features = cad.prepare_preview_features(
            records, job, report, cancel=token, progress=progress,
            feature_limit=cad.DEFAULT_PREVIEW_FEATURE_LIMIT,
            vertex_limit=cad.DEFAULT_PREVIEW_VERTEX_LIMIT)
        return cad.ConversionResult([], None, report.to_dict(), features)
    return cad.convert_job(job_for(options), write=False, cancel=cancel, log=log,
                           progress=progress)


def _color(value, alpha="ff"):
    value = str(value or "#66B5FF").lstrip("#")
    if len(value) != 6 or any(c not in "0123456789abcdefABCDEF" for c in value):
        value = "66B5FF"
    return alpha + value[4:6] + value[2:4] + value[:2]


def _check_geometry(geometry):
    if geometry.is_empty:
        raise ValueError("An empty geometry reached the KML writer.")
    for point in cad.shapely.get_coordinates(geometry, include_z=True):
        x, y = float(point[0]), float(point[1])
        if not (math.isfinite(x) and math.isfinite(y) and -180 <= x <= 180 and -90 <= y <= 90):
            raise ValueError("Coordinates are outside longitude/latitude limits. Check the source CRS and units.")
    if isinstance(geometry, (Polygon, MultiPolygon)) and not geometry.is_valid:
        raise ValueError("An invalid geometry was found. Correct the drawing before exporting this layer.")


def _geometry(parent, geometry, altitude):
    """Use the upstream geometry writer, then explicitly define elevation behavior."""
    cad._append_kml_geometry(parent, geometry)
    for node in parent.iter():
        if node.tag.rsplit("}", 1)[-1] in {"Point", "LineString", "Polygon"}:
            # KML schema order: altitudeMode before coordinates/boundary children.
            child = ET.Element(f"{{{NS}}}altitudeMode")
            child.text = altitude
            index = next((i for i, item in enumerate(node)
                          if item.tag.rsplit("}", 1)[-1] in
                          {"coordinates", "outerBoundaryIs", "innerBoundaryIs"}), len(node))
            node.insert(index, child)


def make_kml(output_layers, options, report, cancel=None):
    root = ET.Element(f"{{{NS}}}kml")
    doc = el(root, "Document")
    el(doc, "name", options.document_name.strip() or Path(options.input_path).stem)
    el(doc, "description", f"Converted by MinePlan Earth | {APP_AUTHOR}. Verify survey control points before use.")
    folders, plan_folders, styles = {}, {}, {}
    for plan in report.get("plan_sheet_alignment", {}).get("selected_plans", []):
        plan_name = str(plan["title"])
        plan_folders[plan_name] = el(doc, "Folder")
        el(plan_folders[plan_name], "name", plan_name)
        el(plan_folders[plan_name], "open", "0")
    counts, missing_heights, repaired_for_kml = Counter(), 0, 0
    bounds = [math.inf, math.inf, -math.inf, -math.inf]
    for item in output_layers:
        for _, row in item.frame.iterrows():
            if cancel:
                cancel.check()
            is_text = str(row.get("entity_type", "")) in cad.DXFExtractor.TEXT_TYPES
            if is_text and not options.include_labels:
                counts["labels_excluded"] += 1
                continue
            geometry = row.geometry
            if isinstance(geometry, (Polygon, MultiPolygon)) and not geometry.is_valid:
                geometry = make_valid(geometry)
                repaired_for_kml += 1
            _check_geometry(geometry)
            if options.altitude_mode != "clampToGround":
                coords = cad.shapely.get_coordinates(geometry, include_z=True)
                if any(not math.isfinite(float(p[2])) for p in coords):
                    missing_heights += 1
            xmin, ymin, xmax, ymax = geometry.bounds
            bounds = [min(bounds[0], xmin), min(bounds[1], ymin),
                      max(bounds[2], xmax), max(bounds[3], ymax)]
            layer = str(row.get("cad_layer", "0"))
            if layer not in folders:
                parent = doc
                folder_name = layer
                if " / " in layer:
                    plan_name, folder_name = layer.split(" / ", 1)
                    if plan_name not in plan_folders:
                        plan_folders[plan_name] = el(doc, "Folder")
                        el(plan_folders[plan_name], "name", plan_name)
                        el(plan_folders[plan_name], "open", "0")
                    parent = plan_folders[plan_name]
                folders[layer] = el(parent, "Folder")
                el(folders[layer], "name", folder_name)
                el(folders[layer], "open", "0")
            color = str(row.get("color_hex") or "#66B5FF")
            raw_weight = row.get("lineweight")
            width = max(1.5, min(6.0, float(raw_weight) / 25)) if raw_weight is not None else 2.0
            key = (color, round(width, 2), is_text)
            if key not in styles:
                sid = f"style_{len(styles)}"
                style = ET.Element(f"{{{NS}}}Style", {"id": sid})
                icon = el(style, "IconStyle")
                el(icon, "color", _color(color))
                el(icon, "scale", "0.45" if is_text else "0.7")
                label = el(style, "LabelStyle")
                el(label, "color", _color(color))
                el(label, "scale", "0.85" if is_text else "0")
                line = el(style, "LineStyle")
                el(line, "color", _color(color))
                el(line, "width", f"{width:.2f}")
                polygon = el(style, "PolyStyle")
                el(polygon, "color", _color(color, "44"))
                el(polygon, "fill", "1" if options.filled_polygons else "0")
                el(polygon, "outline", "1")
                doc.insert(2 + len(styles), style)
                styles[key] = sid
            pm = el(folders[layer], "Placemark")
            name = row.get("text_value") if is_text else None
            if not name or not isinstance(name, str):
                name = f"{row.get('entity_type', 'Feature')} {row.get('entity_handle', '')}"
            el(pm, "name", name)
            el(pm, "description", f"Layer: {layer}\nEntity: {row.get('entity_type')}\nSource: {Path(options.input_path).name}")
            el(pm, "styleUrl", "#" + styles[key])
            ext = el(pm, "ExtendedData")
            for field in ["cad_layer", "original_cad_layer", "semantic_category",
                          "classification_method", "classification_confidence",
                          "entity_type", "entity_handle", "text_value", "block_name",
                          "color_hex", "linetype"]:
                value = row.get(field)
                if value is not None and str(value) not in {"nan", "None"}:
                    el(el(ext, "Data", name=field), "value", value)
            if options.altitude_mode == "clampToGround":
                geometry = cad.force_2d(geometry)
            _geometry(pm, geometry, options.altitude_mode)
            counts[layer] += 1
    if missing_heights:
        raise ValueError(f"{missing_heights} feature(s) have missing/non-finite elevations. Use ground mode or correct the source heights.")
    count = sum(1 for _ in root.iter(f"{{{NS}}}Placemark"))
    if not count:
        raise ValueError("No features remain after filtering. Select different layers or enable labels.")
    ET.indent(root, space="  ")
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    # Parse our actual bytes and inspect every coordinate tuple before writing.
    verified = ET.fromstring(data)
    tuples = 0
    for node in verified.iter(f"{{{NS}}}coordinates"):
        for token in (node.text or "").split():
            numbers = list(map(float, token.split(",")))
            if len(numbers) not in (2, 3) or not all(map(math.isfinite, numbers)):
                raise ValueError("The generated KML contains invalid coordinates.")
            tuples += 1
    report["mineplan_earth"] = {
        "version": APP_VERSION, "features_exported": count,
        "coordinate_tuples": tuples, "layer_counts": dict(counts),
        "wgs84_bounds": bounds, "altitude_mode": options.altitude_mode,
        "source_crs_confirmed_by_user": options.location_confirmed,
        "height_reference_confirmed_by_user": options.heights_confirmed,
        "invalid_plan_geometries_repaired_for_kml": repaired_for_kml,
        "notes": ["Detected plan sheets are clipped and aligned only when Plan mode is enabled.",
                  "Height values are not converted between vertical datums.",
                  "Text is exported at its insertion point; CAD typography and hatch patterns are not reproduced.",
                  "Curve tolerance is measured in source drawing units."]}
    return data


def _atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".mineplan-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def convert(options, output_path, *, cancel=None, log=lambda _: None, progress=lambda *_: None):
    output = Path(output_path).expanduser().resolve()
    if output.suffix.lower() not in {".kml", ".kmz"}:
        raise ValueError("The output filename must end in .kml or .kmz.")
    if output == Path(options.input_path).resolve():
        raise ValueError("The output must not replace the source drawing.")
    cad.require_runtime()
    job = cad.validate_job(job_for(options, export=True), for_writing=False)
    token = cancel or cad.CancellationToken()
    quality = cad.QualityReport(job)
    report_path = output.with_suffix(".report.json")
    try:
        extractor = cad.DXFExtractor(job, quality, token, progress, log)
        records = extractor.extract_all()
        plan_detection = None
        semantic_summary = None
        if options.plan_ids is not None:
            progress(0.66, "Detecting and aligning repeated plan sheets")
            plan_detection = analyze_plan_sheets(options)
            if not plan_detection.get("available"):
                raise ValueError(plan_detection.get("reason") or "Plan sheets could not be aligned.")
            records = plan_sheets.align_records(
                records, plan_detection, options.plan_ids,
                margin=options.plan_margin, cancel=token,
                include_plan_boundary=not options.exclude_plan_boundary,
                include_sheet_frame=not options.exclude_sheet_frame)
        legend_report = None
        if options.semantic_folders:
            progress(0.70, "Reading this drawing's legend folders")
            hints = {
                (family, color, linetype): category
                for family, color, linetype, category in options.semantic_style_hints
            }
            allowed_categories = set(options.semantic_categories)
            if not allowed_categories:
                try:
                    local_hints, legend_report = semantic_layers.learn_legend_styles_from_document(
                        extractor.doc, options.input_path)
                    hints.update(local_hints)
                    allowed_categories.update(legend_report.get("categories_found", ()))
                    log(
                        f"This drawing contains {len(allowed_categories)} recognised legend "
                        f"folder(s) and {len(local_hints)} unambiguous style rule(s).")
                except Exception as exc:
                    legend_report = {
                        "source_drawing": str(Path(options.input_path).expanduser().resolve()),
                        "warning": f"Legend reading was unavailable: {exc}",
                        "scope": "current_source_dxf_only",
                    }
                    log(legend_report["warning"])
            if allowed_categories:
                progress(0.72, "Grouping features by this drawing's AutoCAD legend")
                records, semantic_summary = semantic_layers.classify_records(
                    records, style_hints=hints,
                    allowed_categories=allowed_categories)
                semantic_summary["source_legend"] = legend_report
            else:
                log("No recognised AutoCAD legend was found; preserving original CAD layer folders.")
        # Broken CAD hatch fills are not required for a line-based Google Earth
        # overlay. Keep them visible in QA, but do not reject an otherwise valid
        # export solely because a decorative fill could not be reconstructed.
        hatch_warnings = 0
        for issue in quality.issues:
            if issue.severity == "error" and issue.code == "entity_conversion_failed" \
                    and issue.entity_type == "HATCH":
                issue.severity = "warning"
                hatch_warnings += 1
        if hatch_warnings:
            quality.issue_counts["error:entity_conversion_failed"] -= hatch_warnings
            quality.issue_counts["warning:entity_conversion_failed"] += hatch_warnings
        layers = cad.prepare_output_layers(records, job, quality, cancel=token,
                                           progress=progress, log=log)
        report = quality.to_dict()
        report["application"] = "MinePlan Earth"
        report["application_version"] = APP_VERSION
        if semantic_summary is not None:
            report["semantic_classification"] = semantic_summary
        if legend_report is not None:
            report["source_legend"] = legend_report
        if plan_detection is not None:
            selected = {value for value in options.plan_ids or ()}
            report["plan_sheet_alignment"] = {
                "method": plan_detection["method"],
                "selected_plans": [p for p in plan_detection["plans"] if p["id"] in selected],
                "survey_control_points": plan_detection["control_points"],
                "control_match_error_drawing_units": plan_detection["control_match_error"],
                "detected_location": plan_detection.get("location"),
                "detected_plan_boundary_excluded": options.exclude_plan_boundary,
                "oversized_sheet_frame_excluded": options.exclude_sheet_frame,
                "crop_margin_drawing_units": options.plan_margin,
                "review_required": True,
            }
        # Do not label a partially failed conversion as successful.
        if any(issue.severity == "error" for issue in quality.issues):
            raise ValueError("Some drawing objects failed extraction or transformation. See the report and log; correct or deselect those layers.")
        data = make_kml(layers, options, report, token)
        token.check()
        progress(0.92, "Writing and checking the export")
        if output.suffix.lower() == ".kmz":
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("doc.kml", data)
            payload = buffer.getvalue()
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                if archive.read("doc.kml") != data:
                    raise ValueError("KMZ archive verification failed.")
        else:
            payload = data
        report["status"] = "complete_with_warnings" if quality.issues else "complete"
        report["finished_utc"] = cad._utc_now()
        report["outputs"] = [str(output)]
        report["review_required"] = True
        report_bytes = json.dumps(report, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        _atomic_bytes(output, payload)
        _atomic_bytes(report_path, report_bytes)
        progress(1.0, "Export ready - verify against your survey control points")
        return {"output": str(output), "report_path": str(report_path), "report": report}
    except cad.CancelledError:
        raise
    except Exception as exc:
        quality.finish("failed")
        failed = quality.to_dict()
        failed["application"] = "MinePlan Earth"
        failed["error"] = str(exc)
        # Preserve a previous successful export's report if a replacement fails.
        failure_path = output.with_name(output.stem + ".failed.report.json")
        try:
            _atomic_bytes(failure_path, json.dumps(failed, indent=2, default=str).encode())
        except OSError:
            pass
        raise


GOVERNMENT_FILENAMES = (
    "SURFACE PLAN.kmz",
    "SURFACE GEOLOGICAL PLAN & SECTION.kmz",
    "PROPOSED ALL YEAR COMBINED.kmz",
    "MINE LAY OUT PLAN & SECTION.kmz",
    "PROGRESSIVE CLOSURE PLAN & SECTION.kmz",
    "FINANCIAL ASSURANCE AREA PLAN.kmz",
    "CONCEPTUAL PLAN & SECTION.kmz",
    "ENVIRONMENT PLAN.kmz",
)


def _submission_filename(title):
    value = " ".join(str(title).replace("�", "-").split())
    folded = value.casefold()
    if "environment" in folded:
        return "ENVIRONMENT PLAN.kmz"
    if "financial assurance" in folded:
        return "FINANCIAL ASSURANCE AREA PLAN.kmz"
    if "progressive closure" in folded:
        return "PROGRESSIVE CLOSURE PLAN & SECTION.kmz"
    if "mine layout" in folded or "mine lay out" in folded:
        return "MINE LAY OUT PLAN & SECTION.kmz"
    if "conceptual" in folded:
        return "CONCEPTUAL PLAN & SECTION.kmz"
    if "surface geological" in folded:
        return "SURFACE GEOLOGICAL PLAN & SECTION.kmz"
    if folded.strip() == "surface plan" or folded.startswith("surface plan "):
        return "SURFACE PLAN.kmz"
    if "year" in folded and ("working" in folded or "proposed" in folded):
        return "PROPOSED ALL YEAR COMBINED.kmz"
    safe = "".join(character if character.isalnum() or character in " -_&" else "-"
                   for character in value).strip(" .-")
    return (safe or "ADDITIONAL PLAN").upper() + ".kmz"


def submission_groups(detection, selected_ids):
    selected = {str(value) for value in selected_ids}
    grouped = {}
    for plan in detection.get("plans", []):
        if str(plan["id"]) not in selected:
            continue
        filename = _submission_filename(plan["title"])
        item = grouped.setdefault(filename, {"filename": filename, "plan_ids": [], "plan_titles": []})
        item["plan_ids"].append(str(plan["id"]))
        item["plan_titles"].append(str(plan["title"]))
    order = {name: index for index, name in enumerate(GOVERNMENT_FILENAMES)}
    return sorted(grouped.values(), key=lambda item: (
        order.get(item["filename"], len(order)), item["filename"]))


def submission_folder_names(input_path):
    source_name = Path(input_path).stem.strip() or "Mine Plan"
    source_name = "".join(
        character if character not in '<>:"/\\|?*' else "-"
        for character in source_name).strip(" .-")
    source_name = source_name[:120].rstrip(" .-") or "Mine Plan"
    return f"{source_name} - FINAL KMZ", f"{source_name} - FINAL KMZ QA"


def convert_submission(options, parent_directory, *, cancel=None,
                       log=lambda _: None, progress=lambda *_: None):
    """Create the reviewed eight-file government KMZ package plus separate QA."""
    job_for(options, export=True)
    if options.plan_ids is None:
        raise ValueError("Enable Plan mode and select the detected plan sheets first.")
    token = cancel or cad.CancellationToken()
    detection = analyze_plan_sheets(options)
    if not detection.get("available"):
        raise ValueError(detection.get("reason") or "Plan sheets could not be detected.")
    groups = submission_groups(detection, options.plan_ids)
    if not groups:
        raise ValueError("No selected plans are available for the submission package.")

    parent = Path(parent_directory).expanduser().resolve()
    kmz_name, qa_name = submission_folder_names(options.input_path)
    if parent.name.casefold() == kmz_name.casefold():
        kmz_folder = parent
        qa_folder = parent.with_name(qa_name)
    else:
        kmz_folder = parent / kmz_name
        qa_folder = parent / qa_name
    kmz_folder.mkdir(parents=True, exist_ok=True)
    qa_folder.mkdir(parents=True, exist_ok=True)

    progress(0.01, "Reading this drawing's AutoCAD legends")
    legend_styles = {}
    legend_report = {
        "source_drawing": str(Path(options.input_path).expanduser().resolve()),
        "legend_labels_found": 0,
        "style_rules_learned": 0,
        "categories_found": [],
        "scope": "current_source_dxf_only",
    }
    try:
        legend_styles, legend_report = semantic_layers.learn_legend_styles_from_dxf(
            options.input_path)
        log(
            f"Read {legend_report['legend_labels_found']} recognised legend entries "
            f"and learned {len(legend_styles)} file-local style rule(s).")
    except Exception as exc:
        # A DWG needs the configured converter before its legend can be read.
        # Reuse the upstream loader so DXF and DWG receive the same policy.
        try:
            legend_job = cad.validate_job(job_for(options, export=True), for_writing=False)
            legend_quality = cad.QualityReport(legend_job)
            legend_extractor = cad.DXFExtractor(
                legend_job, legend_quality, token,
                lambda value, status: progress(value * 0.04, status), log)
            legend_extractor.extract_all()
            legend_styles, legend_report = semantic_layers.learn_legend_styles_from_document(
                legend_extractor.doc, options.input_path)
            log(
                f"Read {legend_report['legend_labels_found']} recognised legend entries "
                f"after preparing the drawing and learned {len(legend_styles)} rule(s).")
        except Exception as fallback_exc:
            legend_report["warning"] = (
                f"Legend reading was unavailable: {exc}; prepared-drawing fallback: "
                f"{fallback_exc}")
            log(legend_report["warning"])

    style_hints = tuple(
        (family, color, linetype, category)
        for (family, color, linetype), category in legend_styles.items())
    legend_categories = tuple(legend_report.get("categories_found", ()))
    log(f"Using {len(style_hints)} file-local semantic CAD style rule(s).")

    files = []
    total_features = 0
    for index, group in enumerate(groups):
        token.check()
        filename = group["filename"]
        progress(0.10 + 0.90 * index / len(groups),
                 f"Government package: creating {filename}")
        group_options = replace(
            options, plan_ids=tuple(group["plan_ids"]),
            document_name=Path(filename).stem,
            exclude_plan_boundary=filename != "ENVIRONMENT PLAN.kmz",
            exclude_sheet_frame=filename != "ENVIRONMENT PLAN.kmz",
            semantic_folders=True, semantic_style_hints=style_hints,
            semantic_categories=legend_categories)

        def group_progress(value, status, i=index):
            progress(0.10 + 0.90 * (i + max(0.0, min(1.0, value))) / len(groups), status)

        result = convert(group_options, kmz_folder / filename, cancel=token,
                         log=log, progress=group_progress)
        report_source = Path(result["report_path"])
        report_target = qa_folder / report_source.name
        if report_target.exists():
            report_target.unlink()
        os.replace(report_source, report_target)
        info = result["report"]["mineplan_earth"]
        total_features += int(info["features_exported"])
        files.append({
            "filename": filename,
            "plan_titles": group["plan_titles"],
            "features": int(info["features_exported"]),
            "size_bytes": (kmz_folder / filename).stat().st_size,
            "wgs84_bounds": info["wgs84_bounds"],
            "qa_report": str(report_target),
            "status": result["report"]["status"],
            "semantic_classification": result["report"].get("semantic_classification", {}),
        })

    produced = {item["filename"] for item in files}
    missing = [name for name in GOVERNMENT_FILENAMES if name not in produced]
    warnings = []
    if missing:
        warnings.append("Expected package categories not generated: " + ", ".join(missing))
    unclassified = sum(item.get("semantic_classification", {}).get(
        "unclassified_records", 0) for item in files)
    classified_total = sum(item.get("semantic_classification", {}).get(
        "total_records", 0) for item in files)
    if unclassified:
        warnings.append(
            f"{unclassified} of {classified_total} records remain in UNCLASSIFIED folders; review before submission.")
    summary = {
        "application": "MinePlan Earth",
        "application_version": APP_VERSION,
        "status": "complete_with_warnings" if warnings else "complete",
        "finished_utc": cad._utc_now(),
        "source_drawing": str(Path(options.input_path).expanduser().resolve()),
        "source_crs": options.source_crs,
        "source_crs_confirmed_by_user": options.location_confirmed,
        "review_required": True,
        "kmz_folder": str(kmz_folder),
        "qa_folder": str(qa_folder),
        "files": files,
        "missing_expected_files": missing,
        "warnings": warnings,
        "total_features": total_features,
        "semantic_records": classified_total,
        "unclassified_records": unclassified,
        "learned_semantic_style_rules": len(style_hints),
        "source_legend": legend_report,
    }
    manifest = qa_folder / "submission-manifest.json"
    checklist = qa_folder / "REVIEW BEFORE SUBMISSION.txt"
    _atomic_bytes(manifest, json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8"))
    checklist_lines = [
        "MINEPLAN EARTH - GOVERNMENT SUBMISSION REVIEW", "",
        f"Source: {summary['source_drawing']}",
        f"CRS: {summary['source_crs']}", "",
        "FILES CREATED:",
    ]
    checklist_lines.extend(
        f"[ ] {item['filename']} - {item['features']} features"
        for item in files)
    checklist_lines.extend([
        "", "MANDATORY MANUAL REVIEW:",
        "[ ] Open every KMZ in Google Earth Pro.",
        "[ ] Verify lease-boundary control points against the approved survey.",
        "[ ] Verify plan names, yearly progression, buffers, pits, dumps and roads.",
        "[ ] Confirm that Environment Plan alone contains environmental buffers.",
        "[ ] Review every UNCLASSIFIED folder and correct the source/mapping if necessary.",
        "[ ] Confirm the package requirements with the responsible qualified person.",
    ])
    if warnings:
        checklist_lines.extend(["", "WARNINGS:"] + ["- " + value for value in warnings])
    _atomic_bytes(checklist, "\n".join(checklist_lines).encode("utf-8"))
    progress(1, "Government submission package ready - manual review required")
    return {
        "output": str(kmz_folder), "qa_folder": str(qa_folder),
        "report_path": str(manifest), "report": summary,
    }


def create_demo(directory):
    """Synthetic drawing near Udaipur, not a real mining plan."""
    import ezdxf
    from pyproj import Transformer
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "DEMO_ONLY_Udaipur_UTM43N.dxf"
    doc = ezdxf.new("R2010")
    doc.units = 6  # metres
    x, y = Transformer.from_crs(4326, 32643, always_xy=True).transform(73.7, 24.58)
    model = doc.modelspace()
    for layer, color in [("Lease Boundary", 3), ("Year 1", 1), ("Year 2", 5), ("Survey Labels", 2)]:
        doc.layers.new(layer, dxfattribs={"color": color})
    model.add_lwpolyline([(x,y),(x+450,y),(x+450,y+320),(x,y+320)],
                        close=True, dxfattribs={"layer":"Lease Boundary", "elevation":580})
    model.add_lwpolyline([(x+40,y+40),(x+180,y+40),(x+180,y+140),(x+40,y+140)],
                        close=True, dxfattribs={"layer":"Year 1", "elevation":575})
    model.add_lwpolyline([(x+220,y+70),(x+380,y+70),(x+380,y+210),(x+220,y+210)],
                        close=True, dxfattribs={"layer":"Year 2", "elevation":570})
    model.add_text("DEMO ONLY - synthetic plan", dxfattribs={"layer":"Survey Labels", "height":8,
                   "insert":(x+30,y+280,580)})
    model.add_circle((x+300,y+250,580), 15, dxfattribs={"layer":"Survey Labels"})
    doc.saveas(path)
    (folder / "DEMO_README.txt").write_text(
        "Synthetic demonstration only. Not a surveyed lease or an approved plan.\n"
        "Source CRS: EPSG:32643 (WGS 84 / UTM zone 43N). Drawing units: metres.\n"
        "First lease vertex: longitude 73.7, latitude 24.58.\n"
        "Use ground mode for the initial demonstration. Heights are synthetic.\n", encoding="utf-8")
    return path
