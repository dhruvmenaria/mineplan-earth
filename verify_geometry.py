"""Read local KML/KMZ and compare horizontal geometry and feature identity."""
from collections import Counter, defaultdict
from dataclasses import dataclass
import math
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

from pyproj import Geod, Transformer
from shapely import get_coordinates, normalize
from shapely.geometry import Point, LineString, Polygon, GeometryCollection
from shapely.ops import transform


KML = "http://www.opengis.net/kml/2.2"
GX = "http://www.google.com/kml/ext/2.2"
LIMIT = 64 * 1024 * 1024
GEOD = Geod(ellps="WGS84")
STATUSES = ("matched", "changed", "missing", "extra", "review")


@dataclass
class Feature:
    name: str
    folder: tuple
    fields: dict
    geometry: object = None
    reason: str = ""

    @property
    def handle(self):
        return self.fields.get("entity_handle", "")


def _read(path):
    if path.suffix.lower() == ".kml":
        with path.open("rb") as stream:
            data = stream.read(LIMIT + 1)
    elif path.suffix.lower() == ".kmz":
        try:
            with zipfile.ZipFile(path) as archive:
                entries = [entry for entry in archive.infolist()
                           if entry.filename.lower().endswith(".kml")]
                if len(entries) != 1:
                    raise ValueError("Select a KMZ containing one KML document.")
                if entries[0].file_size > LIMIT:
                    raise ValueError("The KML exceeds the verifier's 64 MiB limit.")
                with archive.open(entries[0]) as stream:
                    data = stream.read(LIMIT + 1)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            raise ValueError(f"Cannot read this KMZ archive: {exc}") from exc
    else:
        raise ValueError("Select a .kml or .kmz file to verify.")
    return data


class _TreeBuilder(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise ValueError("Document type declarations are unsupported in verification KML.")


def _coordinates(node, ns):
    containers = node.findall(ns + "coordinates")
    if len(containers) != 1:
        raise ValueError("expected one coordinates element")
    coords = []
    for token in (containers[0].text or "").split():
        values = [float(part) for part in token.split(",")]
        if len(values) not in (2, 3) or not all(map(math.isfinite, values)):
            raise ValueError("invalid coordinate tuple")
        lon, lat = values[:2]
        if abs(lon) > 180 or abs(lat) > 90:
            raise ValueError("longitude/latitude outside WGS84 bounds")
        coords.append((lon, lat))
    return coords


def _ring(boundary, ns):
    elements = boundary.findall(ns + "LinearRing")
    if len(elements) != 1:
        raise ValueError("expected one LinearRing per polygon boundary")
    coords = _coordinates(elements[0], ns)
    if len(coords) < 4 or coords[0] != coords[-1]:
        raise ValueError("polygon ring is open or has fewer than four coordinates")
    return coords


def _geometry(node, ns):
    if node.tag == ns + "Point":
        coords = _coordinates(node, ns)
        if len(coords) != 1:
            raise ValueError("expected one point coordinate")
        return Point(coords[0])
    if node.tag == ns + "LineString":
        coords = _coordinates(node, ns)
        if len(coords) < 2:
            raise ValueError("line has fewer than two coordinates")
        return LineString(coords)
    if node.tag == ns + "Polygon":
        outer = node.findall(ns + "outerBoundaryIs")
        if len(outer) != 1:
            raise ValueError("expected one outer polygon boundary")
        polygon = Polygon(_ring(outer[0], ns), [_ring(h, ns) for h in node.findall(ns + "innerBoundaryIs")])
        if not polygon.is_valid:
            raise ValueError("invalid polygon topology")
        return polygon
    if node.tag == ns + "MultiGeometry" and len(node):
        return GeometryCollection([_geometry(child, ns) for child in node])
    raise ValueError(f"unsupported or empty geometry {node.tag.rsplit('}', 1)[-1]}")


def _features(data, label, cancel):
    if len(data) > LIMIT:
        raise ValueError(f"{label} KML exceeds the verifier's 64 MiB limit.")
    try:
        root = ET.fromstring(data, parser=ET.XMLParser(target=_TreeBuilder()))
    except ET.ParseError as exc:
        raise ValueError(f"{label} KML is not readable XML: {exc}") from exc
    ns = f"{{{KML}}}" if root.tag == f"{{{KML}}}kml" else ""
    if root.tag != ns + "kml":
        raise ValueError(f"{label} file has no supported KML root element.")
    shapes = {ns + tag for tag in ("Point", "LineString", "Polygon", "MultiGeometry", "LinearRing", "Model")}
    shapes.update({f"{{{GX}}}Track", f"{{{GX}}}MultiTrack"})
    unsupported = {ns + tag for tag in ("GroundOverlay", "ScreenOverlay", "PhotoOverlay", "NetworkLink")}
    unsupported.update({ns + "NetworkLinkControl", f"{{{GX}}}Tour"})
    result = []
    stack = [(root, ())]
    while stack:
        node, folder = stack.pop()
        if cancel:
            cancel.check()
        name = (node.findtext(ns + "name") or "").strip()
        if node.tag in {root.tag, ns + "Document", ns + "Folder"}:
            if node.tag == ns + "Folder":
                folder = folder + (name,)
            stack.extend((child, folder) for child in reversed(node))
        elif node.tag in unsupported:
            result.append(Feature(name, folder, {}, reason=f"Unsupported KML content: {node.tag.rsplit('}', 1)[-1]}"))
        elif node.tag == ns + "Placemark":
            fields = {}
            for item in node.findall(f"{ns}ExtendedData/{ns}Data"):
                key = item.get("name", "")
                if key in fields:
                    raise ValueError(f"{label} placemark {name!r} has duplicate metadata {key!r}.")
                fields[key] = (item.findtext(ns + "value") or "").strip()
            feature = Feature(name, folder, fields)
            children = [child for child in node if child.tag in shapes]
            try:
                if len(children) != 1:
                    raise ValueError("expected one supported geometry or a MultiGeometry")
                feature.geometry = normalize(_geometry(children[0], ns))
            except (ValueError, RecursionError) as exc:
                feature.reason = f"{label} placemark {name!r}: {exc}"
            result.append(feature)
    return result


def _structure(geom):
    if geom.geom_type == "GeometryCollection":
        return ("MultiGeometry", tuple(_structure(child) for child in geom.geoms))
    if geom.geom_type == "Polygon":
        return ("Polygon", len(geom.exterior.coords), tuple(len(r.coords) for r in geom.interiors))
    return (geom.geom_type, len(geom.coords))


def _distance(a, b):
    """Maximum corresponding-vertex displacement on the WGS84 ellipsoid, in metres."""
    if a == b:
        return 0.0
    if _structure(a) != _structure(b):
        return None
    axy, bxy = get_coordinates(a), get_coordinates(b)
    if len(axy) == 1:
        return float(GEOD.inv(*axy[0], *bxy[0])[2])
    _, _, distances = GEOD.inv(axy[:, 0], axy[:, 1], bxy[:, 0], bxy[:, 1])
    return float(max(distances))


def _shape_distance(a, b):
    """Return Hausdorff distance and polygon overlap after local metre projection."""
    if a is None or b is None or a.is_empty or b.is_empty:
        return None, None
    centre = a.union(b).centroid
    zone = max(1, min(60, int((centre.x + 180) // 6) + 1))
    epsg = (32600 if centre.y >= 0 else 32700) + zone
    projector = Transformer.from_crs(4326, epsg, always_xy=True).transform
    projected_a, projected_b = transform(projector, a), transform(projector, b)
    distance = float(projected_a.hausdorff_distance(projected_b))
    overlap = None
    if projected_a.geom_type in {"Polygon", "MultiPolygon"} and \
            projected_b.geom_type in {"Polygon", "MultiPolygon"}:
        union_area = float(projected_a.union(projected_b).area)
        overlap = (100.0 * float(projected_a.intersection(projected_b).area) / union_area
                   if union_area else 100.0)
    return distance, overlap


def _assess(a, b, tolerance):
    if a.reason or b.reason:
        return "review", a.reason or b.reason, None
    distance = _distance(a.geometry, b.geometry)
    overlap = None
    if distance is None or distance > tolerance:
        hausdorff, overlap = _shape_distance(a.geometry, b.geometry)
        if hausdorff is None or hausdorff > tolerance:
            detail = ("Geometry type, component structure or vertex count differs."
                      if distance is None else
                      f"Corresponding vertices differ by up to {distance:.3f} m.")
            if hausdorff is not None:
                detail += f" Shape/Hausdorff difference is {hausdorff:.3f} m."
                if overlap is not None:
                    detail += f" Polygon overlap is {overlap:.2f}%."
            return "changed", detail, hausdorff if hausdorff is not None else distance
        distance = hausdorff
    if a.name != b.name or a.folder != b.folder:
        return "changed", f"Expected {' / '.join((*a.folder, a.name))}; found {' / '.join((*b.folder, b.name))}.", distance
    absent = []
    for key in ("cad_layer", "entity_type", "entity_handle", "text_value", "original_cad_layer", "semantic_category"):
        if key in a.fields and key not in b.fields:
            absent.append(key)
        elif a.fields.get(key) != b.fields.get(key):
            return "changed", f"{key}: expected {a.fields.get(key)!r}, found {b.fields.get(key)!r}.", distance
    if absent:
        return "review", "Geometry agrees; missing CAD metadata: " + ", ".join(absent), distance
    method = "shape/Hausdorff" if _structure(a.geometry) != _structure(b.geometry) else "corresponding vertices"
    detail = f"Horizontal geometry agrees by {method}; name, folder and available CAD identifiers agree."
    if overlap is not None:
        detail += f" Polygon overlap is {overlap:.2f}%."
    return "matched", detail, distance


def compare_kml(expected: bytes, actual: Path, tolerance_m: float, cancel=None):
    """Pair placemarks once. Geometry equivalence requires corresponding 2D vertices."""
    if not math.isfinite(tolerance_m) or tolerance_m < 0:
        raise ValueError("Horizontal tolerance must be a finite, nonnegative number in metres.")
    left = _features(expected, "Reference", cancel)
    right = _features(_read(Path(actual)), "Candidate", cancel)
    if not left:
        raise ValueError("The reference contains no comparable features.")
    if not right:
        raise ValueError("The candidate contains no comparable features.")
    unused_a, unused_b = set(range(len(left))), set(range(len(right)))
    findings = []

    def record(feature, status, detail, distance=None):
        findings.append({"status": status, "name": feature.name, "layer": " / ".join(feature.folder),
                         "handle": feature.handle, "detail": detail, "max_error_m": distance})

    def pair(i, j):
        if cancel:
            cancel.check()
        record(left[i], *_assess(left[i], right[j], tolerance_m))
        unused_a.remove(i)
        unused_b.remove(j)

    def unique_keys(key, compatible):
        groups_a, groups_b = defaultdict(list), defaultdict(list)
        for indices, features, groups in ((unused_a, left, groups_a), (unused_b, right, groups_b)):
            for idx in sorted(indices):
                value = key(features[idx])
                if value is not None:
                    groups[value].append(idx)
        for value, group in groups_a.items():
            candidates = groups_b.get(value, [])
            if len(group) == len(candidates) == 1 and compatible(left[group[0]], right[candidates[0]]):
                pair(group[0], candidates[0])
        return groups_a, groups_b

    def compatible(a, b):
        return not a.handle or not b.handle or a.handle == b.handle

    unique_keys(lambda f: f.handle or None, compatible)
    unique_keys(lambda f: (f.handle, f.folder, f.name) if f.handle else None, compatible)
    unique_keys(lambda f: (f.folder, f.name) if f.name else None, compatible)
    # For renamed features or reused handles, exact normalized geometry provides
    # additional identity evidence. Duplicate, indistinguishable items need review.
    unique_keys(lambda f: (f.handle, f.geometry.wkb) if f.geometry is not None and f.handle else None, compatible)
    groups_a, groups_b = unique_keys(lambda f: f.geometry.wkb if f.geometry is not None else None, compatible)
    ambiguous_a, ambiguous_b = set(), set()
    for key in groups_a.keys() & groups_b.keys():
        ai = [i for i in groups_a[key] if i in unused_a]
        bj = [j for j in groups_b[key] if j in unused_b]
        if ai and bj and (len(ai) > 1 or len(bj) > 1):
            ambiguous_a.update(ai)
            ambiguous_b.update(bj)
    for indices, features, ambiguous, status, missing_message in (
        (unused_a, left, ambiguous_a, "missing", "No corresponding candidate placemark found."),
        (unused_b, right, ambiguous_b, "extra", "No corresponding reference placemark found."),
    ):
        for idx in sorted(indices):
            feature = features[idx]
            if idx in ambiguous:
                record(feature, "review", f"Ambiguous {status} item: overlapping features have no unique pairing.")
            elif feature.reason:
                record(feature, "review", feature.reason)
            else:
                record(feature, status, missing_message)

    totals = Counter(row["status"] for row in findings)
    counts = {status: totals[status] for status in STATUSES}
    status = "FAIL" if any(counts[k] for k in ("changed", "missing", "extra")) else "REVIEW" if counts["review"] else "PASS"
    return {"status": status, "expected_count": len(left), "actual_count": len(right),
            "tolerance_m": tolerance_m, "counts": counts, "findings": findings}
