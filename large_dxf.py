"""Fast, low-memory indexing and layer filtering for very large ASCII DXFs."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import mmap
import os
import re
import tempfile


LARGE_DXF_BYTES = 250 * 1024 * 1024
_SECTION = re.compile(
    rb"(?m)^[ \t]+0\r?\nSECTION\r?\n[ \t]+2\r?\nENTITIES\r?\n")
_ENDSEC = re.compile(rb"(?m)^[ \t]+0\r?\nENDSEC\r?\n")
# Entity names always contain a letter (LINE, 3DFACE, ACAD_PROXY_ENTITY,
# and so on). Requiring one avoids mistaking a numeric value of zero for a
# group-code line in LibreDWG-generated ASCII files.
_ENTITY = re.compile(
    rb"(?m)^[ \t]+0\r?\n([A-Z0-9_]*[A-Z_][A-Z0-9_]*)\r?\n")
_LAYER = re.compile(rb"(?m)^[ \t]+8\r?\n([^\r\n]*)\r?\n")


def is_large(path, threshold=LARGE_DXF_BYTES):
    source = Path(path).expanduser().resolve()
    return source.suffix.lower() == ".dxf" and source.stat().st_size >= threshold


def _entities_extent(data):
    section = _SECTION.search(data)
    if not section:
        raise ValueError("This does not appear to be an ASCII DXF with an ENTITIES section.")
    end = _ENDSEC.search(data, section.end())
    if not end:
        raise ValueError("The DXF ENTITIES section is incomplete (ENDSEC was not found).")
    return section.end(), end.start()


def _decode(value):
    return value.strip().decode("utf-8", "replace") or "0"


def _layer_for(data, start, end, fallback="0"):
    match = _LAYER.search(data, start, end)
    return _decode(match.group(1)) if match else fallback


def index_layers(path, *, cancel=None, progress=lambda *_: None):
    """Index layers using an mmap; geometry is never constructed in memory."""
    source = Path(path).expanduser().resolve()
    size = source.stat().st_size
    layers = {}
    types = Counter()
    total = 0
    parent_layer = None
    with source.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        body_start, body_end = _entities_extent(data)
        matches = _ENTITY.finditer(data, body_start, body_end)
        for index, match in enumerate(matches):
            if cancel is not None and index % 100000 == 0:
                cancel.check()
            kind = _decode(match.group(1)).upper()
            start = match.start()
            # Layer codes occur close to the entity header. Searching only up
            # to the next entity would require look-ahead for every one of the
            # millions of VERTEX records, so vertices inherit their POLYLINE.
            if kind == "POLYLINE":
                next_match = _ENTITY.search(data, match.end(), body_end)
                end = next_match.start() if next_match else body_end
                parent_layer = _layer_for(data, start, end)
                layer = parent_layer
            elif kind in {"VERTEX", "SEQEND"} and parent_layer is not None:
                layer = parent_layer
                if kind == "SEQEND":
                    parent_layer = None
            else:
                next_match = _ENTITY.search(data, match.end(), body_end)
                end = next_match.start() if next_match else body_end
                layer = _layer_for(data, start, end)
            item = layers.setdefault(layer, {
                "name": layer, "count": 0, "vertices": 0, "types": Counter()})
            if kind == "VERTEX":
                item["vertices"] += 1
            elif kind not in {"SEQEND", "ENDSEC"}:
                item["count"] += 1
            item["types"][kind] += 1
            types[kind] += 1
            total += 1
            if index % 100000 == 0:
                progress(min(0.99, match.start() / max(1, size)),
                         f"Indexing large DXF: {index:,} records")
    progress(1, "Large DXF layer index complete")
    result_layers = []
    for item in sorted(layers.values(), key=lambda value: value["name"].casefold()):
        result_layers.append({
            "name": item["name"],
            "count": item["count"],
            "vertices": item["vertices"],
            "types": dict(item["types"].most_common()),
        })
    return {
        "path": str(source), "size_bytes": size, "records": total,
        "layers": result_layers, "types": dict(types.most_common()),
    }


def _copy_range(source, target, start, end, block_size=8 * 1024 * 1024):
    source.seek(start)
    remaining = end - start
    while remaining:
        chunk = source.read(min(block_size, remaining))
        if not chunk:
            raise ValueError("The DXF ended unexpectedly while creating the working subset.")
        target.write(chunk)
        remaining -= len(chunk)


def write_subset(path, output_path, selected_layers, *, cancel=None,
                 progress=lambda *_: None):
    """Copy selected ENTITIES layers while preserving all other DXF sections."""
    source = Path(path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    selected = {str(value) for value in selected_layers}
    if not selected:
        raise ValueError("Select at least one layer for the working DXF.")
    output.parent.mkdir(parents=True, exist_ok=True)
    kept = Counter()
    skipped = Counter()
    parent_keep = False
    parent_layer = None
    temp_name = None
    try:
        with source.open("rb") as original, mmap.mmap(
                original.fileno(), 0, access=mmap.ACCESS_READ) as data:
            body_start, body_end = _entities_extent(data)
            fd, temp_name = tempfile.mkstemp(prefix=output.name + ".", suffix=".tmp",
                                             dir=output.parent)
            with os.fdopen(fd, "wb") as target:
                _copy_range(original, target, 0, body_start)
                matches = iter(_ENTITY.finditer(data, body_start, body_end))
                current = next(matches, None)
                index = 0
                pending_start = None
                pending_end = None
                while current is not None:
                    following = next(matches, None)
                    start = current.start()
                    end = following.start() if following is not None else body_end
                    kind = _decode(current.group(1)).upper()
                    if kind == "POLYLINE":
                        parent_layer = _layer_for(data, start, end)
                        parent_keep = parent_layer in selected
                        layer, keep = parent_layer, parent_keep
                    elif kind in {"VERTEX", "SEQEND"} and parent_layer is not None:
                        layer, keep = parent_layer, parent_keep
                        if kind == "SEQEND":
                            parent_layer = None
                            parent_keep = False
                    else:
                        layer = _layer_for(data, start, end)
                        keep = layer in selected
                    if keep:
                        if pending_start is None:
                            pending_start = start
                        pending_end = end
                        kept[layer] += 1
                    else:
                        if pending_start is not None:
                            _copy_range(original, target, pending_start, pending_end)
                            pending_start = pending_end = None
                        skipped[layer] += 1
                    index += 1
                    if index % 100000 == 0:
                        if cancel is not None:
                            cancel.check()
                        progress(min(0.99, end / max(1, source.stat().st_size)),
                                 f"Creating working DXF: {index:,} records checked")
                    current = following
                if pending_start is not None:
                    _copy_range(original, target, pending_start, pending_end)
                _copy_range(original, target, body_end, source.stat().st_size)
                target.flush()
                os.fsync(target.fileno())
        os.replace(temp_name, output)
        temp_name = None
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    progress(1, "Working DXF created")
    return {
        "output": str(output), "size_bytes": output.stat().st_size,
        "kept_records": sum(kept.values()), "skipped_records": sum(skipped.values()),
        "kept_by_layer": dict(kept),
    }
