"""Reconstruct the expected overlay from the source drawing and export settings."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import zipfile

import converter


def _source_warnings(report):
    warnings = []
    unsupported = report.get("unsupported_entities", {})
    if unsupported:
        counts = ", ".join(f"{name} ({count})" for name, count in sorted(unsupported.items()))
        warnings.append(f"DXF entity types omitted during extraction: {counts}.")
    issues = report.get("issues", [])
    for key, count in sorted(report.get("issue_counts", {}).items()):
        severity, _, code = key.partition(":")
        if not count or severity not in {"warning", "error"}:
            continue
        if code == "unsupported_entity" and unsupported:
            continue
        example = next((i for i in issues if i.get("code") == code), {})
        handle = example.get("entity_handle")
        detail = example.get("message", "")
        if handle:
            detail = f"DXF handle {handle}: {detail}"
        warnings.append(f"DXF {code} ({count}): {detail}".rstrip(": "))
    fixed = report.get("mineplan_earth", {}).get("invalid_plan_geometries_repaired_for_kml", 0)
    if fixed:
        warnings.append(f"{fixed} invalid geometries were repaired while reconstructing the KML.")
    return warnings


def _submission_group(options, filename):
    detection = converter.analyze_plan_sheets(options)
    if not detection.get("available"):
        raise ValueError(detection.get("reason") or "No aligned plan sheets found in the DXF.")
    available = {str(plan["id"]) for plan in detection["plans"]}
    chosen = available if options.plan_ids is None else set(options.plan_ids)
    if chosen - available:
        raise ValueError("Unrecognised source plan IDs: " + ", ".join(sorted(chosen - available)))
    wanted = Path(filename).stem.casefold()
    groups = converter.submission_groups(detection, chosen)
    for group in groups:
        if Path(group["filename"]).stem.casefold() == wanted:
            return group
    raise ValueError(f"No submission group named '{filename}' in the selected DXF plans. "
                     "Available groups: " + ", ".join(g["filename"] for g in groups))


def build_reference(options: converter.Options, *, submission_filename: str | None = None,
                    cancel=None, log=lambda _: None, progress=lambda *_: None) -> tuple[bytes, list[str]]:
    """Use the actual full or submission exporter in an isolated temporary folder.

    Supply the canonical submission member filename explicitly to check one member
    of a government package. Otherwise the expected scope is the exporter's normal
    layer and plan selection. No scope is inferred from the candidate's contents.
    """
    if Path(options.input_path).suffix.casefold() != ".dxf":
        raise ValueError("Choose the source DXF used for this export.")
    converter.job_for(options, export=True)
    group = _submission_group(options, submission_filename) if submission_filename else None
    with tempfile.TemporaryDirectory(prefix="mineplan-verify-") as directory:
        temporary = Path(directory)
        try:
            if group:
                options = replace(options, plan_ids=tuple(group["plan_ids"]))
                result = converter.convert_submission(options, temporary, cancel=cancel,
                                                      log=log, progress=progress)
                member = Path(result["output"]) / group["filename"]
                with zipfile.ZipFile(member) as bundle:
                    payload = bundle.read("doc.kml")
                report_path = Path(result["qa_folder"]) / member.with_suffix(".report.json").name
                report = json.loads(report_path.read_text(encoding="utf-8"))
            else:
                reference = temporary / "reference.kml"
                result = converter.convert(options, reference, cancel=cancel, log=log, progress=progress)
                report = result["report"]
                payload = reference.read_bytes()
                if options.plan_ids is not None:
                    selected = report["plan_sheet_alignment"]["selected_plans"]
                    missing = set(options.plan_ids) - {str(plan["id"]) for plan in selected}
                    if missing:
                        raise ValueError("Unrecognised source plan IDs: " + ", ".join(sorted(missing)))
        except ValueError as exc:
            # Exporter diagnostics live in the temporary folder. Surface an actual
            # source entity/example before cleanup instead of referring to that path.
            failure = next(temporary.rglob("*.failed.report.json"), None)
            if failure:
                info = json.loads(failure.read_text(encoding="utf-8"))
                issue = next((i for i in info.get("issues", []) if i.get("severity") == "error"), None)
                if issue:
                    handle = issue.get("entity_handle") or "unknown"
                    raise ValueError(f"Cannot reconstruct DXF reference. {issue['code']} at "
                                     f"handle {handle}: {issue['message']}") from exc
            raise
        return payload, _source_warnings(report)
