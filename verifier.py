"""Compare an existing Earth overlay with the selected scope of a source drawing."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path

import converter
from verify_geometry import compare_kml
from verify_source import build_reference


SCOPE = (
    "Horizontal export consistency with the supplied drawing, CRS and export settings. "
    "The reference uses the same CAD extraction and alignment rules as MinePlan Earth. "
    "Actual survey location, elevations, styling and engineering approval require separate checks."
)


def verify_export(options, candidate, *, tolerance_m=0.5, submission_group=None,
                  cancel=None, log=lambda _: None, progress=lambda *_: None):
    """Rebuild a local reference and compare the actual on-disk KML or KMZ with it."""
    drawing = Path(options.input_path).expanduser().resolve()
    overlay = Path(candidate).expanduser().resolve()
    if drawing.suffix.lower() != ".dxf" or not drawing.is_file():
        raise ValueError("Choose an existing source DXF. For a DWG, select its converted DXF.")
    if overlay.suffix.lower() not in {".kml", ".kmz"} or not overlay.is_file():
        raise ValueError("Choose the existing KML or KMZ you want to check.")
    if not math.isfinite(tolerance_m) or tolerance_m <= 0:
        raise ValueError("The horizontal tolerance must be a positive, finite number in metres.")
    if submission_group is not None and submission_group not in converter.GOVERNMENT_FILENAMES:
        raise ValueError("Choose a recognized government submission group.")

    token = cancel or converter.cad.CancellationToken()
    token.check()
    progress(0.0, "Reading the source drawing")
    reference, warnings = build_reference(
        options, submission_filename=submission_group, cancel=token, log=log,
        progress=lambda fraction, message: progress(0.8 * fraction, message))
    token.check()
    progress(0.82, "Comparing the exported placemarks with the drawing")
    comparison = compare_kml(reference, overlay, tolerance_m, cancel=token)
    token.check()
    status = {"PASS": "match", "FAIL": "differences", "REVIEW": "review"}[comparison["status"]]
    if warnings and status == "match":
        status = "review"
    settings = asdict(options)
    for key in ("input_path", "dwg_converter", "semantic_style_hints"):
        settings.pop(key)
    result = {
        "status": status, "checked_utc": datetime.now(timezone.utc).isoformat(),
        "application_version": converter.APP_VERSION, "source": str(drawing),
        "candidate": str(overlay), "submission_group": submission_group,
        "settings": settings, "scope": SCOPE, "warnings": warnings, "comparison": comparison,
    }
    progress(1.0, "Verification complete")
    return result


def format_report(result):
    comparison = result["comparison"]
    counts = comparison["counts"]
    status_label = {"match": "MATCH", "differences": "DIFFERENCES FOUND", "review": "REVIEW NEEDED"}
    lines = [
        status_label[result["status"]],
        f"Drawing: {Path(result['source']).name}    Overlay: {Path(result['candidate']).name}",
        f"Scope: {result['submission_group'] or 'Current drawing selection'}",
        f"Source CRS: {result['settings']['source_crs']}    Horizontal tolerance: {comparison['tolerance_m']:g} m",
        f"Expected placemarks: {comparison['expected_count']}    In overlay: {comparison['actual_count']}",
        "    ".join(f"{key.capitalize()}: {counts[key]}"
                    for key in ("matched", "changed", "missing", "extra", "review")),
    ]
    if result["warnings"]:
        lines.extend(["", "Source issues", *result["warnings"]])
    issues = [row for row in comparison["findings"] if row["status"] != "matched"]
    if issues:
        lines.extend(["", "Features to check"])
        for row in issues[:50]:
            identity = " / ".join(str(value) for value in (row["layer"], row["name"]) if value)
            handle = f" [CAD {row['handle']}]" if row["handle"] else ""
            lines.append(f"{row['status'].upper()}  {identity}{handle} — {row['detail']}")
        if len(issues) > 50:
            lines.append(f"Showing 50 of {len(issues)} findings. The JSON report includes every row.")
    lines.extend(["", result["scope"]])
    return "\n".join(lines)


def save_report(result, path):
    """Write a complete report atomically in the chosen JSON file."""
    target = Path(path).expanduser().resolve()
    if target.suffix.lower() != ".json":
        raise ValueError("Choose a .json filename for the verification report.")
    converter._atomic_bytes(target, json.dumps(result, ensure_ascii=False, indent=2,
                                               allow_nan=False).encode("utf-8"))
