"""Sequential large DXF export with one shared plan fit and bounded bulk parts."""
from dataclasses import asdict, replace
import hashlib
import json
import mmap
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET
import zipfile

import converter
import large_dxf as raw
import semantic_layers
import review_rules


def is_bulk_layer(name):
    # Deliberately conservative: ambiguous layer 0 and mixed layers remain core.
    words = semantic_layers._normalise(name).split()
    return bool(set(words) & {"CONTOUR", "CONTOURS", "TOPO", "TOPOGRAPHY"})


def _groups(data, begin, end):
    """Yield complete entity byte ranges, retaining INSERT attributes and POLYLINE vertices."""
    matches = iter(raw._ENTITY.finditer(data, begin, end))
    current = next(matches, None)
    pending = None
    while current:
        following = next(matches, None)
        stop = following.start() if following else end
        kind = raw._decode(current.group(1)).upper()
        if kind not in {"VERTEX", "SEQEND", "ATTRIB"}:
            if pending:
                yield pending
            pending = [current.start(), stop, raw._layer_for(data, current.start(), stop), 1]
        elif pending:
            pending[1] = stop
            pending[3] += 1
        else:
            raise ValueError("Orphan DXF child record; repair the source before partitioning.")
        current = following
    if pending:
        yield pending


def partition(source, directory, *, budget=32 * 1024**2, cancel=None):
    """Copy core together and partition known bulk by complete entity byte budget."""
    source, directory = Path(source), Path(directory)
    paths, totals = [], {}
    with source.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        begin, end = raw._entities_extent(data)
        overhead = begin + len(data) - end
        if overhead > budget:
            raise ValueError("DXF BLOCKS/header sections exceed the part budget. Purge unused blocks before grouped export.")
        core = directory / "core.dxf"
        core_stream = core.open("wb")
        core_stream.write(data[:begin])
        core_bytes = 0
        bulk_stream = None
        bulk_bytes = 0
        try:
            for number, (start, stop, layer, count) in enumerate(_groups(data, begin, end)):
                if cancel and number % 1000 == 0:
                    cancel.check()
                bulk = is_bulk_layer(layer)
                totals.setdefault(layer, {"core": 0, "bulk": 0})["bulk" if bulk else "core"] += count
                size = stop - start
                if bulk:
                    if size + overhead > budget:
                        raise ValueError(f"One complete entity in {layer} exceeds the memory budget; it cannot be split safely.")
                    if bulk_stream is None or bulk_bytes + size + overhead > budget:
                        if bulk_stream:
                            bulk_stream.write(data[end:]); bulk_stream.close()
                        path = directory / f"bulk-{len(paths)+1:04d}.dxf"
                        paths.append(path)
                        bulk_stream = path.open("wb")
                        bulk_stream.write(data[:begin]); bulk_bytes = 0
                    bulk_stream.write(data[start:stop]); bulk_bytes += size
                else:
                    core_bytes += size
                    if core_bytes + overhead > budget * 2:
                        raise ValueError("The interdependent core exceeds the safe budget. Mixed/layer-0 content needs explicit separation in CAD; automatic guesses would lose context.")
                    core_stream.write(data[start:stop])
            core_stream.write(data[end:])
            if bulk_stream:
                bulk_stream.write(data[end:])
        finally:
            core_stream.close()
            if bulk_stream:
                bulk_stream.close()
    return core, paths, totals


def export(options, directory, *, government=False, cancel=None, log=lambda _: None, progress=lambda *_: None):
    """Create a self-contained combined KMZ plus individually reviewable parts."""
    token = cancel or converter.cad.CancellationToken()
    source = Path(options.input_path).resolve()
    source_identity = (source.stat().st_size, source.stat().st_mtime_ns)
    converter.job_for(options, export=True)
    destination = Path(directory).resolve() / (source.stem + " - GROUPED EXPORT")
    destination.mkdir(parents=True, exist_ok=True)
    master = destination / "COMPLETE PLAN.kmz"
    if master.exists():
        raise ValueError("This grouped export already exists. Choose a new destination folder.")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(8*1024**2), b""):
            token.check(); digest.update(block)
    index = raw.index_layers(source, cancel=token)
    with tempfile.TemporaryDirectory(prefix="mineplan-grouped-") as temporary:
        core, bulk, totals = partition(source, temporary, cancel=token)
        if source_identity != (source.stat().st_size, source.stat().st_mtime_ns):
            raise ValueError("The source DXF changed during indexing/partitioning. Restart with an unchanged source.")
        indexed = sum(x["core"] + x["bulk"] for x in totals.values())
        if indexed != index["records"]:
            raise ValueError("Partition record reconciliation failed.")
        core_options = replace(options, input_path=str(core), layers=None)
        detection = converter.analyze_plan_sheets(core_options)
        if detection.get("available") and options.plan_ids is None:
            raise ValueError("Repeated plates detected. Create/analyse the core working DXF first and choose its plan sheets before grouped export.")
        hints, legend = semantic_layers.learn_legend_styles_from_dxf(core, plan_detection=detection)
        frozen = replace(core_options,
            source_legend_json=json.dumps(legend),
            semantic_manual_rules=tuple(review_rules.load_document(source).get("rules", ())),
            frozen_plan_detection_json=json.dumps(detection),
            semantic_categories=tuple(legend.get("categories_found", ())),
            semantic_style_hints=tuple((*key, value) for key, value in hints.items()),
            semantic_dominant_style_hints=tuple((*r["signature"], r["category"])
                                                for r in legend.get("dominant_style_rules", ())),
            semantic_plan_style_hints=tuple((str(r["plan_id"]), *r["signature"], r["category"])
                                           for r in legend.get("plan_style_rules", ())))
        if government:
            result = converter.convert_submission(frozen, destination, cancel=token,
                                                  log=log, progress=progress)
            manifest = {"source": str(source), "sha256": digest.hexdigest(),
                        "indexed_records": index["records"], "assigned_records": indexed,
                        "counts_by_layer": totals, "bulk_excluded_before_conversion": True,
                        "frozen_settings": asdict(frozen), "package": result["output"],
                        "review_required": True}
            manifest["frozen_settings"]["input_path"] = str(source)
            converter._atomic_bytes(destination / "manifest.json", json.dumps(manifest, indent=2, default=str).encode())
            return result
        parts = []
        combined = Path(temporary) / "combined.kml"
        with combined.open("wb") as output:
            output.write(b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Complete plan</name>')
            for number, part in enumerate([core, *bulk]):
                token.check()
                progress(number / (len(bulk)+1), f"Converting grouped part {number+1}")
                path = destination / ("CORE.kmz" if number == 0 else f"BULK-{number:04d}.kmz")
                result = converter.convert(replace(frozen, input_path=str(part)), path, cancel=token, log=log)
                with zipfile.ZipFile(path) as archive:
                    root = ET.fromstring(archive.read("doc.kml"))
                document = root.find("{" + converter.NS + "}Document")
                folder = ET.Element("{" + converter.NS + "}Folder")
                converter.el(folder, "name", path.stem)
                # Give every style a unique id before combining documents.
                prefix = f"part{number}-"
                for node in document.iter():
                    if "id" in node.attrib:
                        node.set("id", prefix + node.get("id"))
                    if node.tag == "{" + converter.NS + "}styleUrl" and (node.text or "").startswith("#"):
                        node.text = "#" + prefix + node.text[1:]
                for child in document:
                    if child.tag not in {"{" + converter.NS + "}name"}:
                        folder.append(child)
                output.write(ET.tostring(folder))
                parts.append({"file": path.name, "summary": result["report"]["mineplan_earth"]})
            output.write(b'</Document></kml>')
        with zipfile.ZipFile(master, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(combined, "doc.kml")
        manifest = {"source": str(source), "sha256": digest.hexdigest(),
                    "frozen_settings": asdict(frozen), "indexed_records": index["records"],
                    "assigned_records": indexed, "counts_by_layer": totals,
                    "parts": parts, "complete_file": master.name,
                    "review_required": True}
        manifest["frozen_settings"]["input_path"] = str(source)
        converter._atomic_bytes(destination / "manifest.json", json.dumps(manifest, indent=2, default=str).encode())
    progress(1, "Grouped export complete")
    return {"output": str(master), "report_path": str(destination / "manifest.json"), "report": manifest}
