#!/usr/bin/env python3
"""CAD to GIS Studio - high-fidelity DXF/DWG to GIS conversion with Tkinter.

The graphical interface uses only the Python standard-library Tk/ttk widgets.
The conversion engine uses ezdxf, Shapely, pyproj, GeoPandas and pyogrio.
DWG reading is performed through an installed ODA File Converter (preferred)
or LibreDWG ``dwg2dxf`` executable; DXF reading is native.

The default source and target coordinate reference system is ETRS89 / Portugal
TM06 (EPSG:3763). GeoPackage is the default output format, closed CAD boundaries
are promoted to polygons by default, and preview uses a bounded display-only
sample for responsiveness. Run without arguments for the GUI, or use ``--help``
for headless modes.
"""

# WINDOWS 10/11 QUICK START (Command Prompt; 64-bit Python 3.12 recommended)
# ---------------------------------------------------------------------------
# Create and activate the virtual environment:
#   py -m venv .venv
#   .venv\Scripts\activate
#   .venv\Scripts\python.exe -m pip install --upgrade pip
#
# Install the runtime dependencies:
#   .venv\Scripts\python.exe -m pip install ^
#       "ezdxf>=1.4.4,<1.5" "Shapely>=2.1.2,<2.2" "pyproj>=3.7.2,<3.8" ^
#       "pandas>=2.2,<3.1" "GeoPandas>=1.1,<1.2" "pyogrio>=0.11.1,<0.14"
#
# Run the Tkinter application:
#   .venv\Scripts\python.exe script.py
#
# Check Python syntax (py_compile does not create a standalone executable):
#   .venv\Scripts\python.exe -m py_compile script.py
#
# Recommended standalone one-file EXE from the complete project package:
#   .venv\Scripts\python.exe -m pip install "PyInstaller>=6.15,<7" "pyinstaller-hooks-contrib>=2025.8"
#   .venv\Scripts\python.exe -m PyInstaller --noconfirm --clean script.spec
#   dist\script.exe
#
# Script-only fallback build when the spec/hooks are unavailable:
#   .venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile ^
#       --windowed --name script --collect-all pyogrio --collect-all pyproj ^
#       --collect-all shapely --collect-all geopandas --collect-all ezdxf script.py
#
# Tkinter is included with the standard python.org Windows installer. DWG input
# additionally needs ODA File Converter or LibreDWG; DXF input is native.

from __future__ import annotations

import argparse
import contextlib
import csv
import ctypes
import errno
import gc
import hashlib
import json
import math
import os
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

APP_NAME = "CAD to GIS Studio"
APP_VERSION = "1.0.0"
REPORT_SCHEMA_VERSION = 1

DEFAULT_CRS = "EPSG:3763"
DEFAULT_CRS_NAME = "ETRS89 / Portugal TM06"
WGS84_CRS = "EPSG:4326"
DEFAULT_OUTPUT_FORMAT = "gpkg"
DEFAULT_PREVIEW_FEATURE_LIMIT = 3500
DEFAULT_PREVIEW_VERTEX_LIMIT = 60_000
DEFAULT_PREVIEW_TOP_LEVEL_LIMIT = 3000
SUPPORTED_OUTPUT_FORMATS = ("gpkg_shp_kml", "gpkg", "shp", "geojson", "fgb", "kml", "csv")
WGS84_OUTPUT_FORMATS = frozenset({"geojson", "kml"})
GIS_OUTPUT_FORMATS = frozenset({"gpkg_shp_kml", "gpkg", "shp", "geojson", "fgb", "kml"})

_ACTIVE_COMMIT_LOCKS: set[str] = set()
_ACTIVE_COMMIT_LOCKS_GUARD = threading.Lock()


# Heavy imports are optional at module import time so the GUI can explain a
# missing installation instead of terminating before a window appears.
_IMPORT_ERRORS: dict[str, str] = {}

try:
    import ezdxf
    from ezdxf import recover as ezdxf_recover
    from ezdxf import transform as ezdxf_transform
    from ezdxf.addons import geo as ezdxf_geo
    from ezdxf.path import make_path as ezdxf_make_path
    from ezdxf.render import MeshBuilder
    from ezdxf.xclip import XClip as EzdxfXClip
except Exception as exc:  # pragma: no cover - exercised by packaged diagnostics
    ezdxf = None  # type: ignore[assignment]
    ezdxf_recover = None  # type: ignore[assignment]
    ezdxf_transform = None  # type: ignore[assignment]
    ezdxf_geo = None  # type: ignore[assignment]
    ezdxf_make_path = None  # type: ignore[assignment]
    MeshBuilder = None  # type: ignore[assignment]
    EzdxfXClip = None  # type: ignore[assignment,misc]
    _IMPORT_ERRORS["ezdxf"] = str(exc)

try:
    import pandas as pd
except Exception as exc:  # pragma: no cover
    pd = None  # type: ignore[assignment]
    _IMPORT_ERRORS["pandas"] = str(exc)

try:
    import geopandas as gpd
except Exception as exc:  # pragma: no cover
    gpd = None  # type: ignore[assignment]
    _IMPORT_ERRORS["geopandas"] = str(exc)

try:
    import pyogrio
except Exception as exc:  # pragma: no cover
    pyogrio = None  # type: ignore[assignment]
    _IMPORT_ERRORS["pyogrio"] = str(exc)

try:
    from pyproj import CRS, Transformer
except Exception as exc:  # pragma: no cover
    CRS = None  # type: ignore[assignment]
    Transformer = None  # type: ignore[assignment]
    _IMPORT_ERRORS["pyproj"] = str(exc)

try:
    import shapely
    from shapely import force_2d, force_3d, get_num_coordinates, has_z, make_valid
    from shapely.affinity import rotate as geometry_rotate
    from shapely.affinity import scale as geometry_scale
    from shapely.affinity import translate as geometry_translate
    from shapely.geometry import (
        GeometryCollection,
        LineString,
        MultiLineString,
        MultiPoint,
        MultiPolygon,
        Point,
        Polygon,
        shape,
    )
    from shapely.ops import transform as geometry_transform
except Exception as exc:  # pragma: no cover
    shapely = None  # type: ignore[assignment]
    _IMPORT_ERRORS["shapely"] = str(exc)


LogCallback = Callable[[str], None]
ProgressCallback = Callable[[float, str], None]


class ConversionError(RuntimeError):
    """A user-actionable conversion failure."""


class CancelledError(ConversionError):
    """Raised when cooperative cancellation is requested."""


class CancellationToken:
    def __init__(self, event: threading.Event | None = None):
        self._event = event or threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise CancelledError("Operation cancelled by the user.")


@dataclass(frozen=True)
class ConversionJob:
    input_paths: tuple[str, ...]
    output_directory: str
    dataset_name: str = "cad_to_gis"
    output_format: str = DEFAULT_OUTPUT_FORMAT
    source_crs: str | None = DEFAULT_CRS
    target_crs: str | None = DEFAULT_CRS
    selected_layers: frozenset[str] | None = None
    curve_tolerance: float = 0.01
    coordinate_scale: float = 1.0
    rotation_degrees: float = 0.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    include_z: bool = True
    closed_boundaries_as_polygons: bool = True
    block_mode: str = "explode"  # explode | points | both
    include_paperspace: bool = False
    audit_and_recover: bool = True
    repair_invalid_polygons: bool = False
    strict_transform: bool = True
    overwrite: bool = True
    dwg_converter: str | None = None
    dwg_timeout_seconds: int = 600


@dataclass
class Issue:
    severity: str
    code: str
    message: str
    source_file: str | None = None
    entity_handle: str | None = None
    entity_type: str | None = None


@dataclass
class OutputLayer:
    logical_name: str
    source_file: str
    source_id: str
    cad_layer: str
    geometry_family: str
    frame: Any


@dataclass
class ConversionResult:
    outputs: list[str]
    report_path: str | None
    report: dict[str, Any]
    preview_features: list[dict[str, Any]] = field(default_factory=list)


class QualityReport:
    MAX_ISSUES = 2000

    def __init__(self, job: ConversionJob):
        self.started_at = _utc_now()
        self.finished_at: str | None = None
        self.status = "running"
        self.job = job
        self.inputs: list[dict[str, Any]] = []
        self.outputs: list[dict[str, Any]] = []
        self.artifacts: list[str] = []
        self.issues: list[Issue] = []
        self.issue_counts: Counter[str] = Counter()
        self.unsupported_entities: Counter[str] = Counter()
        self.statistics: Counter[str] = Counter()
        self.name_map: list[dict[str, str]] = []
        self.transform: dict[str, Any] = {}

    def add_issue(
        self,
        severity: str,
        code: str,
        message: str,
        *,
        source_file: str | None = None,
        entity_handle: str | None = None,
        entity_type: str | None = None,
    ) -> None:
        self.issue_counts[f"{severity}:{code}"] += 1
        if len(self.issues) < self.MAX_ISSUES:
            self.issues.append(
                Issue(
                    severity=severity,
                    code=code,
                    message=message,
                    source_file=source_file,
                    entity_handle=entity_handle,
                    entity_type=entity_type,
                )
            )

    def finish(self, status: str) -> None:
        self.status = status
        self.finished_at = _utc_now()

    def to_dict(self) -> dict[str, Any]:
        options = asdict(self.job)
        if options.get("selected_layers") is not None:
            options["selected_layers"] = sorted(options["selected_layers"])
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "application": APP_NAME,
            "application_version": APP_VERSION,
            "status": self.status,
            "started_utc": self.started_at,
            "finished_utc": self.finished_at,
            "options": _json_safe(options),
            "inputs": _json_safe(self.inputs),
            "transform": _json_safe(self.transform),
            "statistics": dict(sorted(self.statistics.items())),
            "unsupported_entities": dict(sorted(self.unsupported_entities.items())),
            "issue_counts": dict(sorted(self.issue_counts.items())),
            "issues_recorded": len(self.issues),
            "issues_suppressed": max(0, sum(self.issue_counts.values()) - len(self.issues)),
            "issues": [_json_safe(asdict(i)) for i in self.issues],
            "layer_name_map": self.name_map,
            "outputs": _json_safe(self.outputs),
            "artifacts": list(self.artifacts),
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    return str(value)


def _optional_text(value: Any) -> str | None:
    """Return clean text while treating pandas/NumPy missing scalars as absent."""
    if value is None:
        return None
    if pd is not None:
        with contextlib.suppress(TypeError, ValueError):
            if bool(pd.isna(value)):
                return None
    text = str(value).strip()
    return text or None


def require_runtime() -> None:
    required = ("ezdxf", "shapely", "pyproj", "pandas", "geopandas", "pyogrio")
    missing = [name for name in required if name in _IMPORT_ERRORS]
    if missing:
        details = "; ".join(f"{name}: {_IMPORT_ERRORS[name]}" for name in missing)
        raise ConversionError(
            "Missing conversion dependencies: "
            + ", ".join(missing)
            + ". Install requirements.txt. Details: "
            + details
        )


def runtime_diagnostics() -> dict[str, Any]:
    versions: dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "frozen": bool(getattr(sys, "frozen", False)),
        "imports_ok": not _IMPORT_ERRORS,
        "import_errors": dict(_IMPORT_ERRORS),
    }
    for name, module in (
        ("ezdxf", ezdxf),
        ("shapely", shapely),
        ("pyproj", sys.modules.get("pyproj")),
        ("pandas", pd),
        ("geopandas", gpd),
        ("pyogrio", pyogrio),
    ):
        if module is not None:
            versions[name] = getattr(module, "__version__", "unknown")
    if pyogrio is not None:
        try:
            versions["gdal_version"] = getattr(pyogrio, "__gdal_version__", None)
            versions["gdal_data_path"] = pyogrio.get_gdal_data_path()
            versions["drivers"] = {
                k: v
                for k, v in pyogrio.list_drivers().items()
                if k in {"GPKG", "ESRI Shapefile", "GeoJSON", "FlatGeobuf", "LIBKML", "KML"}
            }
        except Exception as exc:
            versions["driver_error"] = str(exc)
    versions["dwg_converter"] = detect_dwg_converter(None)
    return versions


def parse_crs(value: str | None) -> Any:
    if value is None or not str(value).strip():
        return None
    require_runtime()
    try:
        return CRS.from_user_input(str(value).strip())
    except Exception as exc:
        raise ConversionError(f"Invalid CRS '{value}': {exc}") from exc


def canonical_crs_text(value: str | None) -> str | None:
    crs = parse_crs(value)
    if crs is None:
        return None
    authority = crs.to_authority()
    return f"{authority[0]}:{authority[1]}" if authority else crs.to_wkt()


def _safe_component(value: str, max_length: int = 80) -> str:
    text = unicodedata.normalize("NFKD", str(value or "dataset"))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", text)
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip(" ._")
    if not text:
        text = "dataset"
    # Windows reserves device names even when followed by an extension
    # (for example ``CON.txt``).
    device_stem = text.split(".", 1)[0].upper()
    if device_stem in {
        "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5",
        "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4",
        "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }:
        text = f"_{text}"
    return text[:max_length].rstrip(" ._") or "dataset"


def _stable_layer_name(raw: str, used: set[str], max_length: int = 62) -> str:
    """Return a short deterministic layer name, adding only a numeric suffix on collision."""
    base = _safe_component(raw, max_length=max_length).lower()
    candidate = base
    index = 2
    while candidate.casefold() in used:
        suffix = f"_{index}"
        candidate = f"{base[:max_length-len(suffix)]}{suffix}"
        index += 1
    used.add(candidate.casefold())
    return candidate


DXF_UNIT_NAMES = {
    0: "Unitless",
    1: "Inches",
    2: "Feet",
    3: "Miles",
    4: "Millimetres",
    5: "Centimetres",
    6: "Metres",
    7: "Kilometres",
    8: "Microinches",
    9: "Mils",
    10: "Yards",
    11: "Angstroms",
    12: "Nanometres",
    13: "Microns",
    14: "Decimetres",
    15: "Decametres",
    16: "Hectometres",
    17: "Gigametres",
    18: "Astronomical units",
    19: "Light years",
    20: "Parsecs",
    21: "US survey feet",
}

DXF_TO_METRE = {
    1: 0.0254,
    2: 0.3048,
    3: 1609.344,
    4: 0.001,
    5: 0.01,
    6: 1.0,
    7: 1000.0,
    8: 2.54e-8,
    9: 2.54e-5,
    10: 0.9144,
    11: 1e-10,
    12: 1e-9,
    13: 1e-6,
    14: 0.1,
    15: 10.0,
    16: 100.0,
    17: 1e9,
    18: 149_597_870_700.0,
    19: 9.4607304725808e15,
    20: 3.085677581491367e16,
    21: 1200.0 / 3937.0,
}


def _find_executable(candidate: str | None) -> str | None:
    if not candidate:
        return None
    path = Path(os.path.expandvars(os.path.expanduser(candidate)))
    if path.is_file():
        return str(path.resolve())
    found = shutil.which(candidate)
    return str(Path(found).resolve()) if found else None


def detect_dwg_converter(preferred: str | None) -> dict[str, str] | None:
    candidates: list[tuple[str, str | None]] = []
    if preferred:
        lower = Path(preferred).name.lower()
        engine = "libredwg" if "dwg2dxf" in lower else "oda"
        candidates.append((engine, preferred))
    candidates.extend(
        [
            ("oda", os.environ.get("ODA_FILE_CONVERTER")),
            ("oda", os.environ.get("ODA_CONVERTER")),
            ("oda", "ODAFileConverter.exe"),
            ("oda", "ODAFileConverter"),
            ("libredwg", "dwg2dxf.exe"),
            ("libredwg", "dwg2dxf"),
        ]
    )
    for engine, value in candidates:
        executable = _find_executable(value)
        if executable:
            return {"engine": engine, "path": executable}

    if os.name == "nt":
        roots = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        ]
        patterns = (
            "ODA/ODAFileConverter*/ODAFileConverter.exe",
            "ODAFileConverter*/ODAFileConverter.exe",
            "Open Design Alliance/**/ODAFileConverter.exe",
        )
        for root in roots:
            for pattern in patterns:
                try:
                    match = next(root.glob(pattern), None)
                except OSError:
                    match = None
                if match and match.is_file():
                    return {"engine": "oda", "path": str(match.resolve())}
    return None


def build_oda_command(
    executable: str,
    input_folder: str,
    output_folder: str,
    filename: str,
    *,
    version: str = "ACAD2018",
) -> list[str]:
    """Build the documented ODA File Converter command line."""
    return [
        executable,
        input_folder,
        output_folder,
        version,
        "DXF",
        "0",  # do not recurse
        "1",  # audit input
        filename,
    ]


def _run_converter_process(
    command: Sequence[str],
    *,
    timeout_seconds: int,
    cancel: CancellationToken,
) -> str:
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    try:
        proc = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            startupinfo=startupinfo,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        raise ConversionError(f"Could not start DWG converter: {exc}") from exc

    deadline = time.monotonic() + max(1, int(timeout_seconds))
    output = ""

    def stop_process_tree(*, force: bool) -> None:
        if proc.poll() is not None:
            return
        if os.name == "nt":
            command = ["taskkill", "/PID", str(proc.pid), "/T"]
            if force:
                command.append("/F")
            with contextlib.suppress(Exception):
                subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        else:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.kill() if force else proc.terminate()

    try:
        while True:
            if cancel.cancelled:
                stop_process_tree(force=False)
                with contextlib.suppress(Exception):
                    proc.wait(timeout=3)
                if proc.poll() is None:
                    stop_process_tree(force=True)
                raise CancelledError("DWG conversion cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop_process_tree(force=True)
                with contextlib.suppress(Exception):
                    output, _ = proc.communicate(timeout=2)
                raise ConversionError(
                    f"DWG converter timed out after {timeout_seconds} seconds.\n{output[-4000:]}"
                )
            try:
                output, _ = proc.communicate(timeout=min(0.25, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if proc.poll() is None:
            stop_process_tree(force=True)
    if proc.returncode != 0:
        raise ConversionError(
            f"DWG converter failed with exit code {proc.returncode}.\n{output[-8000:]}"
        )
    return output


@contextlib.contextmanager
def prepared_cad_input(
    source: Path,
    job: ConversionJob,
    report: QualityReport | None,
    cancel: CancellationToken,
    log: LogCallback,
) -> Iterator[tuple[Path, str]]:
    extension = source.suffix.lower()
    if extension == ".dxf":
        yield source, "ezdxf"
        return
    if extension != ".dwg":
        raise ConversionError(f"Unsupported CAD input '{source.suffix}'. Use DXF or DWG.")

    converter = detect_dwg_converter(job.dwg_converter)
    if converter is None:
        raise ConversionError(
            "DWG requires an installed ODA File Converter (recommended) or LibreDWG "
            "dwg2dxf. Select its executable in Advanced options, or save the drawing as DXF."
        )
    cancel.check()
    log(f"DWG backend: {converter['engine']} — {converter['path']}")
    with tempfile.TemporaryDirectory(prefix="cadgis_dwg_") as temp_root:
        root = Path(temp_root)
        input_dir = root / "input"
        output_dir = root / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        isolated = input_dir / source.name
        shutil.copy2(source, isolated)
        expected = output_dir / f"{source.stem}.dxf"
        if converter["engine"] == "oda":
            command = build_oda_command(
                converter["path"], str(input_dir), str(output_dir), isolated.name
            )
        else:
            command = [converter["path"], "-o", str(expected), str(isolated)]
            if report is not None:
                report.add_issue(
                    "warning",
                    "reduced_fidelity_dwg_backend",
                    "LibreDWG was used. Review the QA report and geometry against the source drawing.",
                    source_file=source.name,
                )
        converter_output = _run_converter_process(
            command, timeout_seconds=job.dwg_timeout_seconds, cancel=cancel
        )
        candidates = [
            p
            for p in output_dir.rglob("*")
            if p.is_file() and p.suffix.lower() == ".dxf" and p.stem.casefold() == source.stem.casefold()
        ]
        if len(candidates) != 1:
            raise ConversionError(
                "DWG converter completed but did not produce exactly one matching DXF. "
                f"Found {len(candidates)}. Converter output:\n{converter_output[-4000:]}"
            )
        yield candidates[0], converter["engine"]


def _load_dxf_document(
    path: Path,
    *,
    audit_and_recover: bool,
    report: QualityReport | None,
    source_name: str,
    log: LogCallback,
    perform_audit: bool = True,
) -> tuple[Any, dict[str, Any]]:
    require_runtime()
    recovered = False
    auditor = None
    try:
        doc = ezdxf.readfile(str(path))
    except Exception as first_exc:
        if not audit_and_recover:
            raise ConversionError(f"Failed to read DXF '{source_name}': {first_exc}") from first_exc
        log(f"Normal DXF read failed; starting recovery: {first_exc}")
        try:
            doc, auditor = ezdxf_recover.readfile(str(path))
            recovered = True
        except Exception as recovery_exc:
            raise ConversionError(
                f"Failed to read or recover DXF '{source_name}': {recovery_exc}"
            ) from recovery_exc

    if audit_and_recover and auditor is None and perform_audit:
        try:
            auditor = doc.audit()
        except Exception as exc:
            if report is not None:
                report.add_issue(
                    "warning", "audit_failed", str(exc), source_file=source_name
                )
    error_count = len(getattr(auditor, "errors", []) or []) if auditor else 0
    fix_count = len(getattr(auditor, "fixes", []) or []) if auditor else 0
    if report is not None and (error_count or fix_count or recovered):
        report.add_issue(
            "warning" if not error_count else "error",
            "dxf_recovery_or_audit",
            f"Recovered={recovered}; audit errors={error_count}; audit fixes={fix_count}.",
            source_file=source_name,
        )
    units_code = int(doc.header.get("$INSUNITS", 0) or 0)
    info = {
        "source_file": source_name,
        "working_file": path.name,
        "dxf_version": getattr(doc, "dxfversion", None),
        "encoding": getattr(doc, "encoding", None),
        "dwg_codepage": doc.header.get("$DWGCODEPAGE"),
        "insunits_code": units_code,
        "insunits_name": DXF_UNIT_NAMES.get(units_code, f"Unknown ({units_code})"),
        "metre_scale_if_local": DXF_TO_METRE.get(units_code),
        "recovered": recovered,
        "audit_errors": error_count,
        "audit_fixes": fix_count,
    }
    return doc, info


def scan_cad_files(
    job: ConversionJob,
    *,
    cancel: CancellationToken | None = None,
    progress: ProgressCallback | None = None,
    log: LogCallback | None = None,
) -> dict[str, Any]:
    require_runtime()
    token = cancel or CancellationToken()
    say = log or (lambda _message: None)
    update = progress or (lambda _value, _message: None)
    aggregate: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "types": Counter(), "files": set()}
    )
    file_results: list[dict[str, Any]] = []
    paths = [Path(p) for p in job.input_paths]
    for file_index, source in enumerate(paths):
        token.check()
        if not source.is_file():
            raise ConversionError(f"Input file not found: {source}")
        say(f"Scanning {source.name}…")
        with prepared_cad_input(source, job, None, token, say) as (working, backend):
            doc, info = _load_dxf_document(
                working,
                audit_and_recover=job.audit_and_recover,
                report=None,
                source_name=source.name,
                log=say,
            )
            info["backend"] = backend
            info["file_size_bytes"] = source.stat().st_size
            counts: Counter[str] = Counter()
            types_by_layer: dict[str, Counter[str]] = defaultdict(Counter)
            layouts = [doc.modelspace()]
            if job.include_paperspace:
                layouts.extend(layout for layout in doc.layouts if layout.name != "Model")
            for layout in layouts:
                for entity in layout:
                    token.check()
                    layer = str(getattr(entity.dxf, "layer", "0") or "0")
                    entity_type = entity.dxftype()
                    counts[layer] += 1
                    types_by_layer[layer][entity_type] += 1
            for layer_entry in doc.layers:
                layer_name = str(layer_entry.dxf.name)
                counts.setdefault(layer_name, 0)
                types_by_layer.setdefault(layer_name, Counter())
            for layer_name in sorted(counts, key=str.casefold):
                aggregate[layer_name]["count"] += counts[layer_name]
                aggregate[layer_name]["types"].update(types_by_layer[layer_name])
                # Paths, not basenames, distinguish two inputs such as
                # C:\\survey-a\\plan.dxf and C:\\survey-b\\plan.dxf.
                aggregate[layer_name]["files"].add(str(source.resolve()))
            info["top_level_entities"] = sum(counts.values())
            info["layers"] = len(counts)
            file_results.append(info)
        update((file_index + 1) / max(1, len(paths)), f"Scanned {source.name}")
    layers = [
        {
            "name": name,
            "count": data["count"],
            "types": dict(sorted(data["types"].items())),
            "files": sorted(data["files"]),
        }
        for name, data in sorted(aggregate.items(), key=lambda item: item[0].casefold())
    ]
    return {"files": file_results, "layers": layers}


@dataclass
class _WalkContext:
    effective_layer: str | None = None
    parent_handle: str | None = None
    parent_type: str | None = None
    block_name: str | None = None
    block_path: tuple[str, ...] = ()
    block_attributes: dict[str, dict[str, str]] = field(default_factory=dict)
    paper_space: bool = False
    byblock_color_aci: int | None = None
    byblock_true_color: int | None = None
    byblock_linetype: str | None = None
    byblock_lineweight: int | None = None
    block_stack: tuple[str, ...] = ()
    block_transform: tuple[float, ...] | None = None
    # Intersection of every active, non-inverted ancestor XCLIP boundary in
    # world XY coordinates.  It is intentionally runtime-only metadata.
    clip_geometry: Any | None = None


class DXFExtractor:
    PATH_TYPES = {
        "LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE"
    }
    SURFACE_TYPES = {"HATCH", "MPOLYGON"}
    FACE_TYPES = {"SOLID", "TRACE", "3DFACE"}
    TEXT_TYPES = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}
    VIRTUAL_TYPES = {"DIMENSION", "LEADER", "MLEADER", "MLINE"}
    UNSUPPORTED_INFINITE = {"RAY", "XLINE"}

    def __init__(
        self,
        job: ConversionJob,
        report: QualityReport,
        cancel: CancellationToken,
        progress: ProgressCallback,
        log: LogCallback,
        *,
        preview_mode: bool = False,
        preview_top_level_limit: int = DEFAULT_PREVIEW_TOP_LEVEL_LIMIT,
        preview_record_limit: int = DEFAULT_PREVIEW_FEATURE_LIMIT,
    ):
        self.job = job
        self.report = report
        self.cancel = cancel
        self.progress = progress
        self.log = log
        self.preview_mode = bool(preview_mode)
        self.preview_top_level_limit = max(1, int(preview_top_level_limit))
        self.preview_record_limit = max(1, int(preview_record_limit))
        self.records: list[dict[str, Any]] = []
        self.doc: Any = None
        self.source: Path | None = None
        self.source_id: str | None = None
        self.source_order = 0
        self._unsupported_logged: Counter[str] = Counter()
        self._layer_entry_cache: dict[str, Any | None] = {}
        self._preview_file_record_start = 0
        self._preview_file_record_limit = self.preview_record_limit
        self._preview_record_limit_hit = False

    def extract_all(self) -> list[dict[str, Any]]:
        paths = [Path(p) for p in self.job.input_paths]
        preview_total_top = 0
        preview_processed_top = 0
        preview_skipped_top = 0

        for file_index, source in enumerate(paths):
            self.cancel.check()
            if not source.is_file():
                raise ConversionError(f"Input file not found: {source}")
            self.source = source
            resolved_key = os.path.normcase(str(source.resolve()))
            self.source_id = (
                f"{_safe_component(source.stem, 48)}__"
                f"{hashlib.sha1(resolved_key.encode('utf-8', 'surrogatepass')).hexdigest()[:8]}"
            )
            self.log(f"Reading {source.name}…")
            with prepared_cad_input(
                source, self.job, self.report, self.cancel, self.log
            ) as (working, backend):
                self.doc, info = _load_dxf_document(
                    working,
                    audit_and_recover=self.job.audit_and_recover,
                    report=self.report,
                    source_name=source.name,
                    log=self.log,
                    perform_audit=not self.preview_mode,
                )
                self._layer_entry_cache.clear()
                info.update(
                    {
                        "backend": backend,
                        "original_path": str(source.resolve()),
                        "file_size_bytes": source.stat().st_size,
                    }
                )
                before = len(self.records)
                self._preview_file_record_start = before
                self._preview_record_limit_hit = False
                if self.preview_mode:
                    self._preview_file_record_limit = max(
                        1, math.ceil(self.preview_record_limit / max(1, len(paths)))
                    )

                layouts = [self.doc.modelspace()]
                if self.job.include_paperspace:
                    layouts.extend(
                        layout for layout in self.doc.layouts if layout.name != "Model"
                    )
                top_total = sum(len(layout) for layout in layouts)
                top_seen = 0
                top_processed = 0
                top_skipped = 0
                stop_file = False

                file_top_limit = (
                    max(1, math.ceil(self.preview_top_level_limit / max(1, len(paths))))
                    if self.preview_mode
                    else max(1, top_total)
                )
                # An explicit layer subset must not be sampled at top level: a
                # sparse selected layer could otherwise disappear from preview.
                stride = (
                    max(1, math.ceil(top_total / file_top_limit))
                    if (
                        self.preview_mode
                        and self.job.selected_layers is None
                        and top_total > file_top_limit
                    )
                    else 1
                )

                for layout in layouts:
                    is_paper = layout.name != "Model"
                    for entity in layout:
                        self.cancel.check()
                        top_seen += 1
                        if top_seen % 500 == 0 or top_seen == top_total:
                            within = top_seen / max(1, top_total)
                            overall = (file_index + within) / max(1, len(paths))
                            self.progress(
                                overall * 0.68,
                                f"Extracting {source.name}: {top_seen}/{top_total}",
                            )

                        should_process = (
                            not self.preview_mode
                            or stride == 1
                            or (top_seen - 1) % stride == 0
                            or top_seen == top_total
                        )
                        if not should_process:
                            top_skipped += 1
                            continue

                        context = _WalkContext(paper_space=is_paper)
                        self._walk_entity(entity, context, depth=0)
                        top_processed += 1

                        if self.preview_mode and self._preview_record_limit_hit:
                            stop_file = True
                            break
                    if stop_file:
                        break

                if stop_file and top_seen < top_total:
                    top_skipped += top_total - top_seen

                info["features_extracted"] = len(self.records) - before
                info["top_level_entities"] = top_total
                if self.preview_mode:
                    info["preview_top_level_processed"] = top_processed
                    info["preview_top_level_skipped"] = top_skipped
                    info["preview_stride"] = stride
                    info["preview_record_limit_hit"] = self._preview_record_limit_hit
                    preview_total_top += top_total
                    preview_processed_top += top_processed
                    preview_skipped_top += top_skipped
                self.report.inputs.append(info)
                self.log(
                    f"Extracted {info['features_extracted']} GIS features from {source.name}."
                )

        self.report.statistics["features_extracted"] = len(self.records)
        if self.preview_mode:
            self.report.statistics["preview_top_level_total"] = preview_total_top
            self.report.statistics["preview_top_level_processed"] = preview_processed_top
            self.report.statistics["preview_top_level_skipped"] = preview_skipped_top
        if not self.records:
            raise ConversionError(
                "No supported geometry was extracted. Check the selected layers and the QA log."
            )
        return self.records

    def _selected(self, layer: str) -> bool:
        selected = self.job.selected_layers
        return selected is None or layer in selected

    @staticmethod
    def _source_entity(entity: Any) -> Any:
        """Return the original CAD entity behind a transformed virtual copy."""
        return getattr(entity, "origin_of_copy", None) or entity

    @staticmethod
    def _handle(entity: Any) -> str | None:
        source = DXFExtractor._source_entity(entity)
        value = getattr(getattr(source, "dxf", None), "handle", None)
        return str(value) if value else None

    def _report_entity_failure(self, code: str, message: str, entity: Any) -> None:
        """Record a failed entity operation with consistent source metadata."""
        self.report.statistics["entities_failed"] += 1
        self.report.add_issue(
            "error",
            code,
            message,
            source_file=self.source.name if self.source else None,
            entity_handle=self._handle(entity),
            entity_type=self._source_entity(entity).dxftype(),
        )

    def _effective_layer(self, entity: Any, context: _WalkContext) -> str:
        raw = str(getattr(entity.dxf, "layer", "0") or "0")
        if raw == "0" and context.effective_layer:
            return context.effective_layer
        return raw

    def _resolve_style(
        self, entity: Any, layer: str, context: _WalkContext
    ) -> dict[str, Any]:
        style_source = self._source_entity(entity)
        raw_color_value = getattr(style_source.dxf, "color", 256)
        # ACI 0 is meaningful: it means BYBLOCK.  Do not coalesce it as a
        # false-y missing value or nested entities incorrectly inherit their
        # effective layer colour instead of the INSERT colour.
        raw_color = 256 if raw_color_value is None else int(raw_color_value)
        true_color = getattr(style_source.dxf, "true_color", None)
        raw_linetype = str(
            getattr(style_source.dxf, "linetype", "BYLAYER") or "BYLAYER"
        )
        lineweight = getattr(style_source.dxf, "lineweight", -1)
        if layer in self._layer_entry_cache:
            layer_entry = self._layer_entry_cache[layer]
        else:
            layer_entry = None
            with contextlib.suppress(Exception):
                layer_entry = self.doc.layers.get(layer)
            self._layer_entry_cache[layer] = layer_entry
        effective_aci = raw_color
        # A true-colour value belongs to the entity and always overrides its
        # ACI setting, including BYBLOCK/BYLAYER.  Only resolve inherited
        # colour when the entity has no true-colour override of its own.
        effective_true_color = int(true_color) if true_color is not None else None
        if effective_true_color is None and raw_color == 0:
            if context.byblock_color_aci is not None:
                effective_aci = context.byblock_color_aci
                effective_true_color = context.byblock_true_color
            else:
                # BYBLOCK outside a block reference uses the layout default
                # colour.  ACI 7 is the portable DXF representation of that
                # black/white default; the entity's layer colour is not used.
                effective_aci = 7
        elif effective_true_color is None and raw_color == 256 and layer_entry is not None:
            effective_aci = abs(int(getattr(layer_entry.dxf, "color", 7) or 7))
            layer_true_color = getattr(layer_entry.dxf, "true_color", None)
            if layer_true_color is not None:
                effective_true_color = int(layer_true_color)
        elif effective_true_color is None and raw_color == 257:
            effective_aci = 7
        effective_linetype = raw_linetype
        if raw_linetype.upper() == "BYBLOCK":
            effective_linetype = context.byblock_linetype or "CONTINUOUS"
        elif raw_linetype.upper() == "BYLAYER" and layer_entry is not None:
            effective_linetype = str(getattr(layer_entry.dxf, "linetype", raw_linetype))
        lineweight_value = int(lineweight) if lineweight is not None else -1
        raw_default_lineweight = self.doc.header.get("$LWDEFAULT", 25)
        default_lineweight = (
            25 if raw_default_lineweight is None else int(raw_default_lineweight)
        )
        if lineweight_value == -2:  # BYBLOCK
            lineweight = (
                context.byblock_lineweight
                if context.byblock_lineweight is not None
                else default_lineweight
            )
        elif lineweight_value == -1:  # BYLAYER
            lineweight = (
                getattr(layer_entry.dxf, "lineweight", default_lineweight)
                if layer_entry is not None
                else default_lineweight
            )
            if int(lineweight) < 0:
                lineweight = default_lineweight
        elif lineweight_value == -3:  # DEFAULT
            lineweight = default_lineweight
        color_hex = None
        try:
            if effective_true_color is not None:
                rgb = ezdxf.colors.int2rgb(effective_true_color)
            elif 1 <= effective_aci <= 255:
                rgb = ezdxf.colors.aci2rgb(effective_aci)
            else:
                rgb = None
            if rgb is not None:
                color_hex = f"#{rgb.r:02X}{rgb.g:02X}{rgb.b:02X}"
        except Exception:
            color_hex = None
        return {
            "cad_color": raw_color,
            "effective_color_aci": effective_aci,
            "effective_true_color": effective_true_color,
            "color_hex": color_hex,
            "linetype": effective_linetype,
            "lineweight": int(lineweight) if lineweight is not None else None,
            "transparency": getattr(entity.dxf, "transparency", None),
        }

    def _metadata(
        self,
        entity: Any,
        layer: str,
        context: _WalkContext,
        **extra: Any,
    ) -> dict[str, Any]:
        self.source_order += 1
        source_entity = self._source_entity(entity)
        data: dict[str, Any] = {
            "source_file": self.source.name if self.source else None,
            "source_id": self.source_id,
            "cad_layer": layer,
            "entity_type": source_entity.dxftype(),
            "geometry_entity_type": entity.dxftype(),
            "entity_handle": self._handle(entity),
            "parent_handle": context.parent_handle,
            "parent_type": context.parent_type,
            "block_name": context.block_name,
            "block_path": "/".join(context.block_path) or None,
            "block_id": (
                hashlib.sha1(
                    f"{self.source_id or ''}/{'/'.join(context.block_path)}".encode("utf-8")
                ).hexdigest()[:16]
                if context.block_path
                else None
            ),
            "attributes_json": (
                json.dumps(context.block_attributes, ensure_ascii=False, sort_keys=True)
                if context.block_attributes
                else None
            ),
            "block_transform_json": (
                json.dumps(context.block_transform)
                if context.block_transform is not None
                else None
            ),
            "paper_space": bool(context.paper_space),
            "thickness": _finite_or_none(
                getattr(source_entity.dxf, "thickness", None)
            ),
            "source_order": self.source_order,
        }
        data.update(self._resolve_style(entity, layer, context))
        if entity.dxftype() in {"HATCH", "MPOLYGON"}:
            data["hatch_pattern"] = str(getattr(entity.dxf, "pattern_name", "") or "").upper()
            data["hatch_angle"] = float(getattr(entity.dxf, "pattern_angle", 0) or 0)
            data["hatch_scale"] = float(getattr(entity.dxf, "pattern_scale", 1) or 1)
            data["hatch_solid"] = bool(getattr(entity.dxf, "solid_fill", 0))
        data.update(extra)
        return data

    def _record(
        self,
        entity: Any,
        layer: str,
        context: _WalkContext,
        geometry: Any,
        **extra: Any,
    ) -> None:
        if self.preview_mode and self._preview_record_limit_hit:
            return
        if context.clip_geometry is not None:
            self.report.statistics["features_tested_against_xclip"] += 1
            clipped = geometry.intersection(context.clip_geometry)
            geometry = self._same_family_geometry(geometry, clipped)
            if geometry.is_empty:
                self.report.statistics["features_removed_by_xclip"] += 1
                return
        if geometry is None or geometry.is_empty:
            self.report.add_issue(
                "warning",
                "empty_geometry",
                "Entity produced an empty geometry.",
                source_file=self.source.name if self.source else None,
                entity_handle=self._handle(entity),
                entity_type=self._source_entity(entity).dxftype(),
            )
            return

        if self.preview_mode:
            source_entity = self._source_entity(entity)
            self.records.append(
                {
                    "source_file": self.source.name if self.source else None,
                    "source_id": self.source_id,
                    "cad_layer": layer,
                    "entity_type": source_entity.dxftype(),
                    "entity_handle": self._handle(entity),
                    "geometry": geometry,
                }
            )
            if (
                len(self.records) - self._preview_file_record_start
                >= self._preview_file_record_limit
            ):
                self._preview_record_limit_hit = True
            return

        record = self._metadata(entity, layer, context, **extra)
        record["geometry"] = geometry
        self.records.append(record)

    @staticmethod
    def _same_family_geometry(original: Any, clipped: Any) -> Any:
        """Discard lower-dimensional contact artifacts created by clipping."""
        if isinstance(original, (Point, MultiPoint)):
            accepted = (Point, MultiPoint)
        elif isinstance(original, (LineString, MultiLineString)):
            accepted = (LineString, MultiLineString)
        elif isinstance(original, (Polygon, MultiPolygon)):
            accepted = (Polygon, MultiPolygon)
        else:
            return clipped

        parts: list[Any] = []

        def collect(value: Any) -> None:
            if isinstance(value, accepted):
                parts.append(value)
            elif isinstance(value, GeometryCollection):
                for child in value.geoms:
                    collect(child)

        collect(clipped)
        if not parts:
            return GeometryCollection()
        if len(parts) == 1:
            return parts[0]
        return GeometryCollection(parts)

    def _walk_entity(
        self,
        entity: Any,
        context: _WalkContext,
        depth: int,
        parent_matrix: Any | None = None,
    ) -> None:
        """Walk source entities while carrying an exact block-to-world matrix.

        Nested INSERT entities must remain in their source block coordinate system
        until their matrices have been composed.  Transforming an INSERT first can
        force ezdxf to approximate or flatten an affine transform containing shear,
        which loses both geometry accuracy and block lineage.
        """
        self.cancel.check()
        if self.preview_mode and self._preview_record_limit_hit:
            return
        if depth > 32:
            source_entity = self._source_entity(entity)
            self.report.add_issue(
                "error",
                "block_recursion_limit",
                "Nested block/decomposition depth exceeded 32 levels.",
                source_file=self.source.name if self.source else None,
                entity_handle=self._handle(entity),
                entity_type=source_entity.dxftype(),
            )
            return
        entity_type = entity.dxftype()
        layer = self._effective_layer(entity, context)

        if entity_type == "INSERT":
            self._walk_insert(entity, context, depth, layer, parent_matrix)
            return

        if entity_type in self.VIRTUAL_TYPES:
            self._walk_virtual(entity, context, depth, layer, parent_matrix)
            return

        if not self._selected(layer):
            return

        if parent_matrix is not None:
            self._walk_transformed_leaf(entity, context, depth, parent_matrix)
            return

        try:
            if entity_type == "POLYLINE" and (
                getattr(entity, "is_poly_face_mesh", False)
                or getattr(entity, "is_polygon_mesh", False)
            ):
                self._record_mesh(entity, layer, context, polyface=True)
            elif entity_type == "MESH":
                self._record_mesh(entity, layer, context, polyface=False)
            elif entity_type in self.PATH_TYPES or entity_type in self.SURFACE_TYPES:
                self._record_proxy_geometry(entity, layer, context)
            elif entity_type in self.FACE_TYPES:
                self._record_face(entity, layer, context)
            elif entity_type == "POINT":
                point = _vec_to_point(entity.dxf.location, self.job.include_z)
                self._record(entity, layer, context, point)
            elif entity_type in self.TEXT_TYPES:
                self._record_text(entity, layer, context)
            elif entity_type in {"IMAGE", "WIPEOUT"}:
                self._record_image_boundary(entity, layer, context)
            elif entity_type == "HELIX":
                path = ezdxf_make_path(entity)
                coords = [_vec_tuple(v, self.job.include_z) for v in path.flattening(self.job.curve_tolerance, 4)]
                if len(coords) >= 2:
                    self._record(entity, layer, context, LineString(coords))
            else:
                self._unsupported(entity, entity_type)
        except CancelledError:
            raise
        except Exception as exc:
            self._report_entity_failure("entity_conversion_failed", str(exc), entity)

    def _walk_transformed_leaf(
        self, entity: Any, context: _WalkContext, depth: int, matrix: Any
    ) -> None:
        """Copy and transform one non-INSERT source entity by a composed matrix."""
        if entity.dxftype() in {"LWPOLYLINE", "POLYLINE"} and bool(
            getattr(entity, "has_arc", False)
        ):
            # ezdxf has to decompose a bulged polyline into separate LINE and
            # ELLIPSE entities under a non-uniform affine transform.  That is a
            # sound DXF representation, but treating those pieces as unrelated
            # GIS features loses the source path and prevents a closed boundary
            # from becoming one polygon.  Flatten the complete source path and
            # transform its resulting GIS geometry as a unit instead.
            try:
                geometry = self._proxy_geometry(
                    entity, preserve_z_for_transform=True
                )
                geometry = self._transform_geometry_by_matrix(geometry, matrix)
                if not self.job.include_z:
                    geometry = force_2d(geometry)
                self._record(
                    entity,
                    self._effective_layer(entity, context),
                    context,
                    geometry,
                )
                return
            except CancelledError:
                raise
            except Exception:
                # Retain ezdxf's established transform path as a safe fallback
                # for unusual legacy POLYLINE variants that geo.proxy rejects.
                pass

        if entity.dxftype() in self.SURFACE_TYPES:
            # DXFPolygon.transform() does not transform MPOLYGON.offset_vector.
            # Construct the fill in its source OCS first, then transform every
            # resulting coordinate by the already composed affine matrix.
            try:
                geometry = self._hatch_geometry(
                    entity,
                    preserve_z_for_transform=True
                )

                geometry = self._transform_geometry_by_matrix(geometry, matrix)
                if not self.job.include_z:
                    geometry = force_2d(geometry)
                self._record(
                    entity,
                    self._effective_layer(entity, context),
                    context,
                    geometry,
                    hatch_style=int(getattr(entity.dxf, "hatch_style", 0) or 0),
                )
            except CancelledError:
                raise
            except Exception as exc:
                self._report_entity_failure("block_child_transform_failed", str(exc), entity)
            return

        try:
            transform_log, transformed = ezdxf_transform.copies([entity], m=matrix)
        except Exception as exc:
            self._report_entity_failure("block_child_transform_failed", str(exc), entity)
            return

        for entry in list(transform_log)[:20]:
            self._report_entity_failure(
                "block_child_transform_failed", entry.message, entity
            )
        if not transformed:
            if not transform_log:
                self._report_entity_failure(
                    "block_child_transform_failed",
                    "Entity could not be copied and transformed.",
                    entity,
                )
            return

        source_entity = self._source_entity(entity)
        for transformed_entity in transformed:
            self.cancel.check()
            # ezdxf type conversions (for example CIRCLE -> ELLIPSE under
            # anisotropic scale) do not always retain origin_of_copy.
            with contextlib.suppress(Exception):
                transformed_entity.set_source_of_copy(source_entity)
            self._walk_entity(transformed_entity, context, depth)

    @staticmethod
    def _transform_geometry_by_matrix(geometry: Any, matrix: Any) -> Any:
        """Apply an ezdxf Matrix44 to every Shapely coordinate."""

        def transform_coordinate(
            x: Any, y: Any, z: Any = None
        ) -> tuple[float, ...]:
            point = matrix.transform(
                (float(x), float(y), 0.0 if z is None else float(z))
            )
            if z is None:
                return (float(point.x), float(point.y))
            return (float(point.x), float(point.y), float(point.z))

        return geometry_transform(transform_coordinate, geometry)

    def _walk_insert(
        self,
        entity: Any,
        context: _WalkContext,
        depth: int,
        layer: str,
        parent_matrix: Any | None,
    ) -> None:
        block_clip_vertices: tuple[Any, ...] | None = None
        if EzdxfXClip is not None:
            try:
                clip = EzdxfXClip(entity)
                if clip.has_clipping_path and clip.is_clipping_enabled:
                    clipping_path = clip.get_block_clipping_path()
                    if clipping_path.is_inverted_clip:
                        self.report.statistics["xclip_boundaries_not_applied"] += 1
                        self.report.add_issue(
                            "warning",
                            "inverted_xclip_not_applied",
                            "This INSERT has an active inverted XCLIP boundary. Inverted "
                            "XCLIP metadata is ambiguous, so its block geometry is exported "
                            "unclipped for review.",
                            source_file=self.source.name if self.source else None,
                            entity_handle=self._handle(entity),
                            entity_type="INSERT",
                        )
                    elif len(clipping_path.vertices) >= 3:
                        block_clip_vertices = tuple(clipping_path.vertices)
                    else:
                        self.report.statistics["xclip_boundaries_not_applied"] += 1
                        self.report.add_issue(
                            "warning",
                            "xclip_not_applied",
                            "This INSERT has an active XCLIP boundary with fewer than three "
                            "usable vertices, so its block geometry is exported unclipped.",
                            source_file=self.source.name if self.source else None,
                            entity_handle=self._handle(entity),
                            entity_type="INSERT",
                        )
            except Exception as exc:
                self.report.add_issue(
                    "warning",
                    "xclip_inspection_failed",
                    f"Could not inspect this INSERT's XCLIP metadata: {exc}",
                    source_file=self.source.name if self.source else None,
                    entity_handle=self._handle(entity),
                    entity_type="INSERT",
                )
        try:
            instances = list(entity.multi_insert()) if int(getattr(entity, "mcount", 1)) > 1 else [entity]
        except Exception:
            instances = [entity]
        for instance_index, instance in enumerate(instances):
            self.cancel.check()
            block_name = str(getattr(instance.dxf, "name", "") or "")
            handle = self._handle(instance) or self._handle(entity) or f"virtual-{depth}"
            segment = f"{block_name}:{handle}:{instance_index}"
            try:
                block_layout = instance.block()
                if block_layout is None:
                    raise ValueError(f"Block definition '{block_name}' does not exist.")
                local_matrix = instance.matrix44()
                world_matrix = (
                    local_matrix
                    if parent_matrix is None
                    else local_matrix @ parent_matrix
                )
                clip_geometry = context.clip_geometry
                if block_clip_vertices is not None:
                    clip_points = [
                        world_matrix.transform((float(vertex.x), float(vertex.y), 0.0))
                        for vertex in block_clip_vertices
                    ]
                    current_clip = Polygon(
                        [(float(point.x), float(point.y)) for point in clip_points]
                    )
                    if not current_clip.is_valid:
                        current_clip = make_valid(current_clip)
                        current_clip = self._same_family_geometry(
                            Polygon(), current_clip
                        )
                    if current_clip.is_empty:
                        raise ValueError("XCLIP boundary produced no usable polygon.")
                    clip_geometry = (
                        current_clip
                        if clip_geometry is None
                        else clip_geometry.intersection(current_clip)
                    )
                    self.report.statistics["xclip_boundaries_applied"] += 1
            except Exception as exc:
                self.report.add_issue(
                    "error",
                    "block_expansion_failed",
                    f"Block '{block_name}': {exc}",
                    source_file=self.source.name if self.source else None,
                    entity_handle=self._handle(entity),
                    entity_type="INSERT",
                )
                continue

            block_key = str(
                getattr(block_layout, "block_record_handle", None)
                or f"name:{block_name.casefold()}"
            )
            attrs = {} if self.preview_mode else _insert_attributes(instance)
            all_attrs = {} if self.preview_mode else dict(context.block_attributes)
            if attrs:
                all_attrs["/".join(context.block_path + (segment,))] = attrs
            insert_style = {} if self.preview_mode else self._resolve_style(instance, layer, context)
            new_context = _WalkContext(
                effective_layer=layer,
                parent_handle=self._handle(entity),
                parent_type="INSERT",
                block_name=block_name,
                block_path=context.block_path + (segment,),
                block_attributes=all_attrs,
                paper_space=context.paper_space,
                byblock_color_aci=insert_style.get("effective_color_aci"),
                byblock_true_color=insert_style.get("effective_true_color"),
                byblock_linetype=insert_style.get("linetype"),
                byblock_lineweight=insert_style.get("lineweight"),
                block_stack=context.block_stack + (block_key,),
                block_transform=tuple(float(value) for value in world_matrix),
                clip_geometry=clip_geometry,
            )
            if self.job.block_mode in {"points", "both"} and self._selected(layer):
                base_point = block_layout.block.dxf.base_point
                insert_point = _vec_to_point(
                    world_matrix.transform(base_point), self.job.include_z
                )
                extra = _insert_pose(instance)
                extra["insert_instance"] = instance_index
                for tag, value in attrs.items():
                    extra[f"attr_{_safe_field_token(tag)}"] = value
                point_context = _WalkContext(
                    effective_layer=layer,
                    parent_handle=context.parent_handle,
                    parent_type=context.parent_type,
                    block_name=block_name,
                    block_path=new_context.block_path,
                    block_attributes=all_attrs,
                    paper_space=context.paper_space,
                    byblock_color_aci=new_context.byblock_color_aci,
                    byblock_true_color=new_context.byblock_true_color,
                    byblock_linetype=new_context.byblock_linetype,
                    byblock_lineweight=new_context.byblock_lineweight,
                    block_stack=context.block_stack,
                    block_transform=new_context.block_transform,
                    # An INSERT's own XCLIP clips its contents, not the
                    # semantic insertion-point feature.  Ancestor clips still
                    # determine whether a nested insertion point is visible.
                    clip_geometry=context.clip_geometry,
                )
                self._record(instance, layer, point_context, insert_point, **extra)
            points_only = self.job.block_mode == "points"
            if self.job.block_mode not in {"explode", "points", "both"}:
                continue

            if block_key in context.block_stack:
                self.report.add_issue(
                    "error",
                    "block_reference_cycle",
                    f"Cyclic block reference detected at '{block_name}'.",
                    source_file=self.source.name if self.source else None,
                    entity_handle=self._handle(entity),
                    entity_type="INSERT",
                )
                continue
            try:
                child_count = 0
                for child in block_layout:
                    # ATTDEF is a schema/default definition, not visible text.
                    # Attached ATTRIB values are preserved on the INSERT record.
                    if child.dxftype() == "ATTDEF":
                        continue
                    self.cancel.check()
                    child_count += 1
                    # In insertion-point mode, descend only through nested
                    # INSERT/MINSERT references.  This preserves composed
                    # transforms and lineage for every nested cell without
                    # emitting any leaf geometry from block definitions.
                    if points_only and child.dxftype() != "INSERT":
                        continue
                    self._walk_entity(
                        child, new_context, depth + 1, parent_matrix=world_matrix
                    )
                if child_count == 0:
                    self.report.add_issue(
                        "warning",
                        "empty_or_unresolved_block",
                        f"Block '{block_name}' contains no convertible virtual entities.",
                        source_file=self.source.name if self.source else None,
                        entity_handle=self._handle(entity),
                        entity_type="INSERT",
                    )
            except CancelledError:
                raise
            except Exception as exc:
                self.report.add_issue(
                    "error",
                    "block_expansion_failed",
                    f"Block '{block_name}': {exc}",
                    source_file=self.source.name if self.source else None,
                    entity_handle=self._handle(entity),
                    entity_type="INSERT",
                )

    def _walk_virtual(
        self,
        entity: Any,
        context: _WalkContext,
        depth: int,
        layer: str,
        parent_matrix: Any | None,
    ) -> None:
        try:
            children = entity.virtual_entities()
            child_context = _WalkContext(
                effective_layer=layer,
                parent_handle=self._handle(entity),
                parent_type=entity.dxftype(),
                block_name=context.block_name,
                block_path=context.block_path,
                block_attributes=context.block_attributes,
                paper_space=context.paper_space,
                byblock_color_aci=context.byblock_color_aci,
                byblock_true_color=context.byblock_true_color,
                byblock_linetype=context.byblock_linetype,
                byblock_lineweight=context.byblock_lineweight,
                block_stack=context.block_stack,
                block_transform=context.block_transform,
                clip_geometry=context.clip_geometry,
            )
            count = 0
            for child in children:
                count += 1
                self._walk_entity(
                    child, child_context, depth + 1, parent_matrix=parent_matrix
                )
            if count == 0:
                self._unsupported(entity, entity.dxftype())
        except CancelledError:
            raise
        except Exception as exc:
            self.report.add_issue(
                "warning",
                "virtual_decomposition_failed",
                str(exc),
                source_file=self.source.name if self.source else None,
                entity_handle=self._handle(entity),
                entity_type=entity.dxftype(),
            )

    def _record_proxy_geometry(
        self, entity: Any, layer: str, context: _WalkContext
    ) -> None:
        entity_type = entity.dxftype()
        if entity_type in self.SURFACE_TYPES:
            geometry = self._hatch_geometry(entity)
            self._record(
                entity,
                layer,
                context,
                geometry,
                hatch_style=int(getattr(entity.dxf, "hatch_style", 0) or 0),
            )
            return
        geometry = self._proxy_geometry(entity)
        self._record(entity, layer, context, geometry)

    def _proxy_geometry(
        self, entity: Any, *, preserve_z_for_transform: bool = False
    ) -> Any:
        """Return one GIS geometry for a complete non-HATCH CAD path."""
        entity_type = entity.dxftype()
        force_line = (
            not self.job.closed_boundaries_as_polygons
            and entity_type not in self.SURFACE_TYPES
        )
        tolerance = self._effective_curve_tolerance(entity)
        proxy = ezdxf_geo.proxy(
            entity,
            distance=tolerance,
            force_line_string=force_line,
        )
        proxy.places = 15
        geometry = shape(proxy.__geo_interface__)
        if self.job.include_z or preserve_z_for_transform:
            geometry = force_3d(geometry, z=0.0)
        else:
            geometry = force_2d(geometry)
        return geometry

    def _effective_curve_tolerance(self, entity: Any) -> float:
        """Prevent very coarse user settings from degenerating full curved rings."""
        tolerance = self.job.curve_tolerance
        radius = None
        entity_type = entity.dxftype()
        if entity_type in {"CIRCLE", "ARC"}:
            radius = abs(_finite_or_none(getattr(entity.dxf, "radius", None)) or 0.0)
        elif entity_type == "ELLIPSE":
            try:
                major = float(entity.dxf.major_axis.magnitude)
                radius = major * abs(float(entity.dxf.ratio))
            except Exception:
                radius = None
        if radius and radius > 0:
            # At least 16 chords for a complete ring; short arcs may still use fewer.
            tolerance = min(tolerance, radius * (1.0 - math.cos(math.pi / 16.0)))
        return max(tolerance, 1e-15)

    def _hatch_geometry(
        self, entity: Any, *, preserve_z_for_transform: bool = False
    ) -> Any:
        """Build HATCH fill polygons without approximating arcs as Bézier curves."""
        from ezdxf.entities import (
            ArcEdge,
            EdgePath,
            EllipseEdge,
            LineEdge,
            PolylinePath,
            SplineEdge,
        )
        from ezdxf.math import ConstructionArc, Vec2, Vec3, bulge_to_arc, fit_points_to_cad_cv
        from shapely.ops import unary_union

        tolerance = self.job.curve_tolerance
        elevation = float(entity.dxf.elevation.z)
        ocs = entity.ocs()
        offset = (
            Vec3(getattr(entity.dxf, "offset_vector", (0.0, 0.0, 0.0)))
            if entity.dxftype() == "MPOLYGON"
            else Vec3()
        )
        join_tolerance = max(1e-12, tolerance * 1e-9)

        def is_close(a: Any, b: Any) -> bool:
            return Vec2(a).isclose(Vec2(b), rel_tol=1e-12, abs_tol=join_tolerance)

        def arc_tolerance(radius: float) -> float:
            # A tolerance larger than a circle radius can legally collapse a
            # complete ring to one chord.  Retain at least 16 chords while
            # still treating the user's value as an upper error bound.
            return min(
                tolerance,
                abs(radius) * (1.0 - math.cos(math.pi / 16.0)),
            )

        def arc_vertices(start: Any, end: Any, bulge: float) -> list[Any]:
            p0 = Vec2(start)
            p1 = Vec2(end)
            chord = p0.distance(p1)
            if chord <= join_tolerance or abs(bulge) * chord * 0.5 <= tolerance:
                return [p0, p1]
            center, start_angle, end_angle, radius = bulge_to_arc(p0, p1, bulge)
            arc = ConstructionArc(
                center=center,
                radius=radius,
                start_angle=math.degrees(start_angle),
                end_angle=math.degrees(end_angle),
            )
            vertices = list(arc.flattening(arc_tolerance(radius)))
            if vertices and not is_close(vertices[0], p0):
                vertices.reverse()
            return vertices or [p0, p1]

        def polyline_loop(path: Any) -> list[Any]:
            vertices = list(path.vertices)
            if len(vertices) < 2:
                return []
            result: list[Any] = []
            segment_count = len(vertices) - 1
            if not is_close(vertices[0], vertices[-1]):
                segment_count += 1
            for index in range(segment_count):
                start = vertices[index]
                end = vertices[(index + 1) % len(vertices)]
                p0 = Vec2(start[:2])
                p1 = Vec2(end[:2])
                bulge = float(start[2])
                segment = arc_vertices(p0, p1, bulge) if bulge else [p0, p1]
                if not result:
                    result.extend(segment)
                elif segment:
                    result.extend(segment[1:] if is_close(result[-1], segment[0]) else segment)
            return result

        def edge_segment(edge: Any) -> list[Any]:
            if isinstance(edge, LineEdge):
                return [Vec2(edge.start), Vec2(edge.end)]
            if isinstance(edge, ArcEdge):
                if abs(float(edge.radius)) <= 1e-15:
                    return []
                vertices = list(
                    edge.construction_tool().flattening(arc_tolerance(edge.radius))
                )
                if not edge.ccw:
                    vertices.reverse()
                return vertices
            if isinstance(edge, EllipseEdge):
                if Vec2(edge.major_axis).is_null:
                    return []
                vertices = list(edge.construction_tool().flattening(tolerance, 4))
                if not edge.ccw:
                    vertices.reverse()
                return [Vec2(vertex) for vertex in vertices]
            if isinstance(edge, SplineEdge):
                if edge.control_points:
                    spline = edge.construction_tool()
                elif edge.fit_points:
                    tangents = None
                    if edge.start_tangent is not None and edge.end_tangent is not None:
                        tangents = (edge.start_tangent, edge.end_tangent)
                    spline = fit_points_to_cad_cv(edge.fit_points, tangents=tangents)
                else:
                    return []
                return [Vec2(vertex) for vertex in spline.flattening(tolerance, 4)]
            raise TypeError(type(edge))

        def edge_loops(path: Any) -> list[list[Any]]:
            loops: list[list[Any]] = []
            current: list[Any] = []
            for edge in path.edges:
                segment = edge_segment(edge)
                if len(segment) < 2:
                    continue
                if not current:
                    current = segment
                elif is_close(current[-1], segment[0]):
                    current.extend(segment[1:])
                elif is_close(current[-1], segment[-1]):
                    current.extend(list(reversed(segment))[1:])
                elif is_close(current[0], segment[-1]):
                    current = segment[:-1] + current
                elif is_close(current[0], segment[0]):
                    current = list(reversed(current))
                    current.extend(segment[1:])
                elif is_close(current[0], current[-1]):
                    loops.append(current)
                    current = segment
                else:
                    # Match ezdxf's HATCH path behavior: close a gap by a
                    # straight segment instead of silently dropping an edge.
                    current.extend(segment)
            if current:
                loops.append(current)
            return loops

        def to_wcs(vertex: Any) -> Any:
            point = Vec2(vertex)
            return ocs.to_wcs(
                Vec3(point.x + offset.x, point.y + offset.y, elevation)
            )

        hatch_style = int(getattr(entity.dxf, "hatch_style", 0) or 0)
        # DXF boundary path flags are authoritative for OUTERMOST and IGNORE
        # hatch styles.  In particular, DEFAULT paths must not be inferred as
        # first-level islands merely because they are geometrically contained.
        # rendering_paths() applies the documented EXTERNAL/OUTERMOST filter
        # while retaining every path for the normal NESTED style.
        boundaries = entity.paths.rendering_paths(hatch_style)

        rings: list[tuple[Any, list[tuple[float, ...]]]] = []
        for boundary in boundaries:
            self.cancel.check()
            if isinstance(boundary, PolylinePath):
                boundary_loops = [polyline_loop(boundary)]
            elif isinstance(boundary, EdgePath):
                boundary_loops = edge_loops(boundary)
            else:
                raise TypeError(type(boundary))
            for loop in boundary_loops:
                coords = [
                    _vec_tuple(
                        to_wcs(vertex),
                        self.job.include_z or preserve_z_for_transform,
                    )
                    for vertex in loop
                ]
                if len(coords) < 3:
                    continue
                if coords[0] != coords[-1]:
                    coords.append(coords[0])
                polygon = Polygon(coords)
                if polygon.is_empty or polygon.area <= 0:
                    continue
                rings.append((polygon, coords))
        if not rings:
            raise ValueError("HATCH has no usable closed boundary rings.")

        # Only full containment establishes parentage.  A representative point
        # can lie inside a crossing loop even when most of that loop is outside,
        # incorrectly turning an overlap into a hole.
        parents: list[int | None] = [None] * len(rings)
        for index, (polygon, _coords) in enumerate(rings):
            candidates: list[tuple[float, int]] = []
            for candidate_index, (candidate, _candidate_coords) in enumerate(rings):
                if candidate_index == index or candidate.area <= polygon.area:
                    continue
                try:
                    if candidate.covers(polygon):
                        candidates.append((candidate.area, candidate_index))
                except Exception:
                    continue
            if candidates:
                parents[index] = min(candidates)[1]

        depths: list[int] = [0] * len(rings)
        for index in range(len(rings)):
            seen: set[int] = set()
            parent = parents[index]
            while parent is not None and parent not in seen:
                seen.add(parent)
                depths[index] += 1
                parent = parents[parent]

        ring_polygons = [polygon for polygon, _coords in rings]
        if hatch_style == 2:  # IGNORE: fill through every internal boundary.
            geometry = unary_union(ring_polygons)
        elif hatch_style == 1:  # OUTERMOST: stop at the first island.
            outer_parts: list[Any] = []
            for index, (polygon, _coords) in enumerate(rings):
                if depths[index] != 0:
                    continue
                holes = [
                    rings[child][0]
                    for child, parent in enumerate(parents)
                    if parent == index and depths[child] == 1
                ]
                outer_parts.append(
                    polygon.difference(unary_union(holes)) if holes else polygon
                )
            geometry = unary_union(outer_parts)
        else:  # NESTED: the HATCH normal style uses the even-odd fill rule.
            # Do not use shapely.symmetric_difference_all(): Shapely 2.1
            # deprecates it because of known incorrect results (GH-2027).
            geometry = ring_polygons[0]
            for polygon in ring_polygons[1:]:
                geometry = geometry.symmetric_difference(polygon)

        polygon_parts: list[Any] = []

        def collect_polygons(value: Any) -> None:
            if isinstance(value, Polygon):
                if not value.is_empty:
                    polygon_parts.append(value)
            elif isinstance(value, (MultiPolygon, GeometryCollection)):
                for child in value.geoms:
                    collect_polygons(child)

        collect_polygons(geometry)
        if not polygon_parts:
            raise ValueError("HATCH boundary containment produced no filled polygons.")
        geometry = polygon_parts[0] if len(polygon_parts) == 1 else MultiPolygon(polygon_parts)
        if self.job.include_z or preserve_z_for_transform:
            return force_3d(geometry, z=0.0)
        return force_2d(geometry)

    def _record_face(self, entity: Any, layer: str, context: _WalkContext) -> None:
        vertices = list(entity.wcs_vertices(close=False))
        while len(vertices) > 3 and vertices[-1].isclose(vertices[-2]):
            vertices.pop()
        coords = [_vec_tuple(v, self.job.include_z) for v in vertices]
        if len(coords) >= 3:
            coords.append(coords[0])
            self._record(entity, layer, context, Polygon(coords), face_index=0)

    def _record_mesh(
        self, entity: Any, layer: str, context: _WalkContext, *, polyface: bool
    ) -> None:
        builder = MeshBuilder.from_polyface(entity) if polyface else MeshBuilder.from_mesh(entity)
        count = 0
        for face_index, face in enumerate(builder.faces_as_vertices()):
            coords = [_vec_tuple(v, self.job.include_z) for v in face]
            if len(coords) < 3:
                continue
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            self._record(entity, layer, context, Polygon(coords), face_index=face_index)
            count += 1
        if not count:
            raise ValueError("Mesh has no polygonal faces.")

    def _record_text(self, entity: Any, layer: str, context: _WalkContext) -> None:
        entity_type = entity.dxftype()
        if entity_type in {"TEXT", "ATTRIB", "ATTDEF"}:
            _alignment, point, _point2 = entity.get_placement()
            with contextlib.suppress(Exception):
                point = entity.ocs().to_wcs(point)
            text_value = entity.plain_text()
            text_height = getattr(entity.dxf, "height", None)
            text_rotation = getattr(entity.dxf, "rotation", None)
            text_style = getattr(entity.dxf, "style", None)
            tag = getattr(entity.dxf, "tag", None)
        else:
            point = entity.dxf.insert
            with contextlib.suppress(Exception):
                point = entity.ocs().to_wcs(point)
            text_value = entity.plain_text()
            text_height = getattr(entity.dxf, "char_height", None)
            text_rotation = getattr(entity.dxf, "rotation", None)
            text_style = getattr(entity.dxf, "style", None)
            tag = None
        self._record(
            entity,
            layer,
            context,
            _vec_to_point(point, self.job.include_z),
            text_value=text_value,
            text_height=_finite_or_none(text_height),
            text_rotation=_finite_or_none(text_rotation),
            text_style=str(text_style) if text_style is not None else None,
            attribute_tag=str(tag) if tag is not None else None,
        )

    def _record_image_boundary(
        self, entity: Any, layer: str, context: _WalkContext
    ) -> None:
        vertices = list(entity.boundary_path_wcs())
        coords = [_vec_tuple(v, self.job.include_z) for v in vertices]
        if len(coords) >= 3:
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            self._record(entity, layer, context, Polygon(coords))

    def _unsupported(self, entity: Any, entity_type: str) -> None:
        self.report.unsupported_entities[entity_type] += 1
        self.report.statistics["entities_unsupported"] += 1
        if self._unsupported_logged[entity_type] < 5:
            reason = (
                "Infinite CAD construction geometry has no finite GIS representation."
                if entity_type in self.UNSUPPORTED_INFINITE
                else "No safe GIS geometry conversion is available for this entity type."
            )
            self.report.add_issue(
                "warning",
                "unsupported_entity",
                reason,
                source_file=self.source.name if self.source else None,
                entity_handle=self._handle(entity),
                entity_type=entity_type,
            )
        self._unsupported_logged[entity_type] += 1


def _finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _vec_tuple(vector: Any, include_z: bool) -> tuple[float, ...]:
    x = float(vector.x if hasattr(vector, "x") else vector[0])
    y = float(vector.y if hasattr(vector, "y") else vector[1])
    if not include_z:
        return (x, y)
    try:
        z = float(vector.z if hasattr(vector, "z") else vector[2])
    except (AttributeError, IndexError, TypeError):
        z = 0.0
    return (x, y, z)


def _vec_to_point(vector: Any, include_z: bool) -> Any:
    return Point(_vec_tuple(vector, include_z))


def _safe_field_token(value: str) -> str:
    token = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    token = re.sub(r"[^A-Za-z0-9_]+", "_", token).strip("_").lower()
    return token[:40] or "value"


def _insert_attributes(entity: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for attribute in getattr(entity, "attribs", ()) or ():
        try:
            tag = str(attribute.dxf.tag).strip()
            if tag:
                result[tag] = str(attribute.dxf.text or "")
        except Exception:
            continue
    return result


def _insert_pose(entity: Any) -> dict[str, Any]:
    insert = entity.dxf.insert
    result = {
        "insert_x_src": _finite_or_none(getattr(insert, "x", None)),
        "insert_y_src": _finite_or_none(getattr(insert, "y", None)),
        "insert_z_src": _finite_or_none(getattr(insert, "z", None)),
        "insert_rotation": _finite_or_none(getattr(entity.dxf, "rotation", None)),
        "insert_scale_x": _finite_or_none(getattr(entity.dxf, "xscale", None)),
        "insert_scale_y": _finite_or_none(getattr(entity.dxf, "yscale", None)),
        "insert_scale_z": _finite_or_none(getattr(entity.dxf, "zscale", None)),
    }
    return result


def validate_job(job: ConversionJob, *, for_writing: bool = True) -> ConversionJob:
    if not job.input_paths:
        raise ConversionError("Add at least one DXF or DWG input file.")
    for raw_path in job.input_paths:
        path = Path(raw_path)
        if not path.is_file():
            raise ConversionError(f"Input file not found: {path}")
        if path.suffix.lower() not in {".dxf", ".dwg"}:
            raise ConversionError(f"Unsupported input format: {path.suffix}")
    if not math.isfinite(job.curve_tolerance) or job.curve_tolerance <= 0:
        raise ConversionError("Curve tolerance must be a finite number greater than zero.")
    for label, value in (
        ("coordinate scale", job.coordinate_scale),
        ("rotation", job.rotation_degrees),
        ("X offset", job.offset_x),
        ("Y offset", job.offset_y),
    ):
        if not math.isfinite(value):
            raise ConversionError(f"{label.capitalize()} must be finite.")
    if job.coordinate_scale == 0:
        raise ConversionError("Coordinate scale cannot be zero.")
    if job.block_mode not in {"explode", "points", "both"}:
        raise ConversionError(f"Invalid block mode: {job.block_mode}")
    fmt = job.output_format.lower()
    if fmt not in SUPPORTED_OUTPUT_FORMATS:
        raise ConversionError(f"Unsupported output format: {job.output_format}")

    source = canonical_crs_text(job.source_crs)
    target = canonical_crs_text(job.target_crs)
    if fmt in GIS_OUTPUT_FORMATS and not source:
        raise ConversionError(
            "A source CRS is required for GIS output. "
            f"The production default is {DEFAULT_CRS}."
        )
    if target and not source:
        raise ConversionError("A source CRS is required before coordinates can be reprojected.")
    if fmt in WGS84_OUTPUT_FORMATS:
        target = WGS84_CRS
    selected = job.selected_layers
    if selected is not None and len(selected) == 0:
        raise ConversionError("No CAD layers are selected.")
    output_directory = job.output_directory
    if for_writing:
        if not output_directory:
            raise ConversionError("Choose an output directory.")
        out = Path(output_directory)
        out.mkdir(parents=True, exist_ok=True)
        if not out.is_dir():
            raise ConversionError(f"Output path is not a directory: {out}")
    return ConversionJob(
        **{
            **asdict(job),
            "source_crs": source,
            "target_crs": target,
            "dataset_name": _safe_component(job.dataset_name, 80),
        }
    )


def _affine_geometry(geometry: Any, job: ConversionJob) -> Any:
    result = geometry
    if job.coordinate_scale != 1.0:
        result = geometry_scale(
            result,
            xfact=job.coordinate_scale,
            yfact=job.coordinate_scale,
            zfact=job.coordinate_scale,
            origin=(0.0, 0.0, 0.0),
        )
    if job.rotation_degrees:
        result = geometry_rotate(result, job.rotation_degrees, origin=(0.0, 0.0))
    if job.offset_x or job.offset_y:
        result = geometry_translate(result, xoff=job.offset_x, yoff=job.offset_y)
    return result


def _transformer_for(job: ConversionJob, report: QualityReport) -> Any | None:
    source = parse_crs(job.source_crs)
    target = parse_crs(job.target_crs)
    if source is None or target is None or source == target:
        report.transform = {
            "source_crs": canonical_crs_text(job.source_crs),
            "target_crs": canonical_crs_text(job.target_crs) or canonical_crs_text(job.source_crs),
            "operation": "assign/preserve",
        }
        if source is None:
            report.add_issue(
                "warning",
                "undefined_crs",
                "Output coordinates are intentionally unreferenced. Assign a verified source CRS "
                "or apply an engineering georeference before spatial overlay.",
            )
        return None
    try:
        transformer = Transformer.from_crs(
            source,
            target,
            always_xy=True,
            allow_ballpark=not job.strict_transform,
            only_best=job.strict_transform,
        )
    except Exception as exc:
        raise ConversionError(f"Could not create CRS transformation: {exc}") from exc
    report.transform = {
        "source_crs": canonical_crs_text(job.source_crs),
        "target_crs": canonical_crs_text(job.target_crs),
        "operation": getattr(transformer, "description", "coordinate transformation"),
        "accuracy_metres": getattr(transformer, "accuracy", None),
        "always_xy": True,
        "strict": job.strict_transform,
        "vertical_note": "Z is passed to PROJ; a 2D CRS operation normally leaves Z unchanged.",
    }
    if getattr(transformer, "accuracy", -1) == -1:
        report.add_issue(
            "warning",
            "unknown_transform_accuracy",
            "PROJ did not state an accuracy for the selected transformation.",
        )
    return transformer


def _apply_crs_transform(geometry: Any, transformer: Any) -> Any:
    def transform_coordinates(x: Any, y: Any, z: Any = None) -> Any:
        if z is None:
            return transformer.transform(x, y, errcheck=True)
        return transformer.transform(x, y, z, errcheck=True)

    return geometry_transform(transform_coordinates, geometry)


def _atomic_geometry_parts(geometry: Any) -> Iterator[Any]:
    if geometry is None or geometry.is_empty:
        return
    if isinstance(geometry, GeometryCollection):
        for child in geometry.geoms:
            yield from _atomic_geometry_parts(child)
    else:
        yield geometry


def _geometry_family(geometry: Any) -> str | None:
    if isinstance(geometry, (Point, MultiPoint)):
        return "point"
    if isinstance(geometry, (LineString, MultiLineString)):
        return "line"
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return "polygon"
    return None


def prepare_output_layers(
    records: list[dict[str, Any]],
    job: ConversionJob,
    report: QualityReport,
    *,
    cancel: CancellationToken,
    progress: ProgressCallback,
    log: LogCallback,
) -> list[OutputLayer]:
    transformer = _transformer_for(job, report)
    processed: list[dict[str, Any]] = []
    total = len(records)
    for index, source_record in enumerate(records):
        cancel.check()
        record = dict(source_record)
        geometry = record.pop("geometry")
        try:
            geometry = _affine_geometry(geometry, job)
            if transformer is not None:
                geometry = _apply_crs_transform(geometry, transformer)
            if not job.include_z:
                geometry = force_2d(geometry)
        except Exception as exc:
            report.statistics["transform_failures"] += 1
            report.add_issue(
                "error",
                "geometry_transform_failed",
                str(exc),
                source_file=record.get("source_file"),
                entity_handle=record.get("entity_handle"),
                entity_type=record.get("entity_type"),
            )
            continue
        valid_before = bool(geometry.is_valid)
        repair_applied = False
        if isinstance(geometry, (Polygon, MultiPolygon)) and not valid_before:
            report.statistics["invalid_polygons_detected"] += 1
            report.add_issue(
                "warning",
                "invalid_polygon",
                "Polygon is invalid in XY topology. It was preserved unless repair was enabled.",
                source_file=record.get("source_file"),
                entity_handle=record.get("entity_handle"),
                entity_type=record.get("entity_type"),
            )
            if job.repair_invalid_polygons:
                geometry = make_valid(geometry)
                repair_applied = True
                report.statistics["invalid_polygons_repaired"] += 1
        parts = list(_atomic_geometry_parts(geometry))
        for part_index, part in enumerate(parts):
            family = _geometry_family(part)
            if family is None or part.is_empty:
                report.statistics["unsupported_result_geometries"] += 1
                report.add_issue(
                    "warning",
                    "unsupported_result_geometry",
                    f"Resulting geometry '{part.geom_type}' could not be written.",
                    source_file=record.get("source_file"),
                    entity_handle=record.get("entity_handle"),
                    entity_type=record.get("entity_type"),
                )
                continue
            out = dict(record)
            out["geometry"] = part
            out["geometry_family"] = family
            out["geometry_part"] = part_index
            out["geometry_valid"] = bool(part.is_valid)
            out["repair_applied"] = repair_applied
            out["has_z"] = bool(has_z(part))
            processed.append(out)
        if index % 250 == 0 or index + 1 == total:
            progress(0.68 + 0.12 * ((index + 1) / max(1, total)), "Applying geometry and CRS operations")

    if not processed:
        raise ConversionError("All extracted geometries failed validation or transformation.")
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in processed:
        source_file = str(record.get("source_file") or "source")
        source_id = str(record.get("source_id") or _safe_component(Path(source_file).stem))
        layer = str(record.get("cad_layer") or "0")
        family = str(record["geometry_family"])
        grouped[(source_id, source_file, layer, family)].append(record)

    target_crs = parse_crs(job.target_crs) or parse_crs(job.source_crs)
    used_names: set[str] = set()
    source_count = len({key[0] for key in grouped})
    result: list[OutputLayer] = []
    for source_id, source_file, layer, family in sorted(
        grouped, key=lambda key: (key[0].casefold(), key[2].casefold(), key[3])
    ):
        cancel.check()
        if source_count > 1:
            source_stem = _safe_component(Path(source_file).stem, 32)
            raw_name = f"{source_stem}_{layer}_{family}"
        else:
            raw_name = f"{layer}_{family}"
        logical_name = _stable_layer_name(raw_name, used_names)
        rows = grouped[(source_id, source_file, layer, family)]
        frame = gpd.GeoDataFrame(rows, geometry="geometry", crs=target_crs)
        result.append(
            OutputLayer(
                logical_name=logical_name,
                source_file=source_file,
                source_id=source_id,
                cad_layer=layer,
                geometry_family=family,
                frame=frame,
            )
        )
        report.name_map.append(
            {
                "source_file": source_file,
                "source_id": source_id,
                "cad_layer": layer,
                "geometry_family": family,
                "output_layer": logical_name,
            }
        )
    report.statistics["features_prepared"] = sum(len(item.frame) for item in result)
    report.statistics["output_layers"] = len(result)
    log(f"Prepared {len(result)} output layers with {report.statistics['features_prepared']} features.")
    return result


_SHP_FIELD_PREFERENCES = {
    "source_file": "SRC_FILE",
    "cad_layer": "CAD_LAYER",
    "entity_type": "ENT_TYPE",
    "entity_handle": "ENT_HNDL",
    "parent_handle": "PAR_HNDL",
    "parent_type": "PAR_TYPE",
    "block_name": "BLK_NAME",
    "block_path": "BLK_PATH",
    "block_id": "BLK_ID",
    "attributes_json": "ATTR_JSON",
    "paper_space": "PAPERSP",
    "source_order": "SRC_ORDER",
    "cad_color": "CAD_COLOR",
    "color_hex": "COLOR_HEX",
    "linetype": "LINETYPE",
    "lineweight": "LINEWGHT",
    "text_value": "TEXT_VAL",
    "text_height": "TEXT_H",
    "text_rotation": "TEXT_ROT",
    "text_style": "TEXT_STYL",
    "geometry_family": "GEOM_FAM",
    "geometry_part": "GEOM_PART",
    "geometry_valid": "GEOM_VALID",
    "repair_applied": "REPAIRED",
    "has_z": "HAS_Z",
}


def _dbf_field_name(name: str, used: set[str]) -> str:
    preferred = _SHP_FIELD_PREFERENCES.get(name)
    base = preferred or _safe_field_token(name).upper()
    base = re.sub(r"[^A-Z0-9_]", "_", base)[:10] or "FIELD"
    candidate = base
    if candidate.casefold() not in used:
        used.add(candidate.casefold())
        return candidate
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:4].upper()
    candidate = f"{base[:5]}_{digest}"[:10]
    index = 2
    while candidate.casefold() in used:
        suffix = str(index)
        candidate = f"{base[:10-len(suffix)]}{suffix}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def _truncate_utf8(text: Any, max_bytes: int = 254) -> tuple[Any, bool]:
    if text is None:
        return None, False
    value = str(text)
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    clipped = encoded[:max_bytes]
    while clipped:
        try:
            return clipped.decode("utf-8"), True
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return "", True


def _prepare_shapefile_frame(
    frame: Any, report: QualityReport
) -> tuple[Any, dict[str, str]]:
    result = frame.copy()
    used: set[str] = set()
    field_map: dict[str, str] = {}
    for column in result.columns:
        if column == result.geometry.name:
            continue
        field_map[column] = _dbf_field_name(str(column), used)
    result = result.rename(columns=field_map)
    truncated_count = 0
    for renamed in field_map.values():
        series = result[renamed]
        if str(series.dtype) in {"bool", "boolean"}:
            result[renamed] = series.astype("Int16")
        elif pd.api.types.is_string_dtype(series.dtype) or str(series.dtype) == "object":
            values = []
            for value in series:
                clipped, truncated = _truncate_utf8(value)
                truncated_count += int(truncated)
                values.append(clipped)
            result[renamed] = values
    if truncated_count:
        report.statistics["shapefile_values_truncated"] += truncated_count
        report.add_issue(
            "warning",
            "shapefile_text_truncated",
            f"{truncated_count} text values exceeded the DBF 254-byte limit and were truncated. "
            "Use GeoPackage for lossless attributes.",
        )
    return result, field_map


@lru_cache(maxsize=32)
def _driver_write_capable(driver: str) -> bool:
    if pyogrio is None:
        return False
    try:
        capability = pyogrio.list_drivers().get(driver, "")
        return "w" in capability
    except Exception:
        return False


def _write_vector_layer(
    frame: Any,
    path: Path,
    *,
    driver: str,
    layer: str | None = None,
    append: bool = False,
    layer_options: dict[str, str] | None = None,
    dataset_options: dict[str, str] | None = None,
) -> None:
    if not _driver_write_capable(driver):
        raise ConversionError(f"GDAL driver '{driver}' is not available for writing.")
    pyogrio.write_dataframe(
        frame,
        str(path),
        layer=layer,
        driver=driver,
        append=append,
        encoding="UTF-8",
        promote_to_multi=None,
        layer_options=layer_options,
        dataset_options=dataset_options,
    )


def _verify_vector(
    path: Path,
    expected_frame: Any,
    expected_crs: Any,
    *,
    layer: str | None = None,
    expected_family: str | None = None,
) -> dict[str, Any]:
    expected_count = len(expected_frame)
    try:
        info = pyogrio.read_info(str(path), layer=layer)
    except Exception as exc:
        raise ConversionError(f"Could not reopen output '{path.name}': {exc}") from exc
    actual_count = int(info.get("features", -1))
    if actual_count != expected_count:
        raise ConversionError(
            f"QA verification failed for '{path.name}'"
            + (f" layer '{layer}'" if layer else "")
            + f": expected {expected_count} features, reopened {actual_count}."
        )
    actual_crs_text = info.get("crs")
    if expected_crs is not None:
        if not actual_crs_text:
            raise ConversionError(f"QA verification found no CRS in '{path.name}'.")
        try:
            actual_crs = CRS.from_user_input(actual_crs_text)
            horizontally_equal = actual_crs.equals(expected_crs)
            if not horizontally_equal:
                with contextlib.suppress(Exception):
                    horizontally_equal = actual_crs.to_2d().equals(expected_crs.to_2d())
            if not horizontally_equal:
                raise ConversionError(
                    f"QA verification found an unexpected CRS in '{path.name}': {actual_crs_text}"
                )
        except ConversionError:
            raise
        except Exception as exc:
            raise ConversionError(f"Could not verify CRS for '{path.name}': {exc}") from exc
    bounds = info.get("total_bounds")
    if expected_count and (
        bounds is None
        or len(bounds) != 4
        or not all(math.isfinite(float(value)) for value in bounds)
    ):
        raise ConversionError(f"QA verification found invalid bounds in '{path.name}'.")

    try:
        reopened = pyogrio.read_dataframe(str(path), layer=layer, columns=[])
    except Exception as exc:
        raise ConversionError(
            f"Could not read output geometry back from '{path.name}': {exc}"
        ) from exc
    geometries = list(reopened.geometry)
    null_count = sum(geometry is None for geometry in geometries)
    empty_count = sum(
        geometry is not None and geometry.is_empty for geometry in geometries
    )
    if null_count or empty_count:
        raise ConversionError(
            f"QA verification found {null_count} null and {empty_count} empty geometries "
            f"in '{path.name}'."
        )
    families = sorted(
        {
            family
            for geometry in geometries
            if (family := _geometry_family(geometry)) is not None
        }
    )
    unknown_count = sum(
        _geometry_family(geometry) is None for geometry in geometries
    )
    if unknown_count or (expected_family and families != [expected_family]):
        raise ConversionError(
            f"QA verification found unexpected geometry types in '{path.name}': "
            f"families={families}, unsupported={unknown_count}."
        )
    expected_z_count = sum(
        geometry is not None and bool(has_z(geometry))
        for geometry in expected_frame.geometry
    )
    actual_z_count = sum(bool(has_z(geometry)) for geometry in geometries)
    if actual_z_count != expected_z_count:
        raise ConversionError(
            f"QA verification found Z loss in '{path.name}': expected {expected_z_count} "
            f"3D geometries, reopened {actual_z_count}."
        )
    expected_fields = {
        str(column).casefold()
        for column in expected_frame.columns
        if column != expected_frame.geometry.name
    }
    info_fields = info.get("fields")
    actual_field_names = [
        str(field) for field in (info_fields if info_fields is not None else [])
    ]
    actual_fields = {field.casefold() for field in actual_field_names}
    missing_fields = sorted(expected_fields - actual_fields)
    if missing_fields:
        raise ConversionError(
            f"QA verification found missing fields in '{path.name}': "
            + ", ".join(missing_fields[:20])
        )
    invalid_count = sum(
        isinstance(geometry, (Polygon, MultiPolygon)) and not geometry.is_valid
        for geometry in geometries
    )
    return {
        "feature_count_verified": actual_count,
        "geometry_type_verified": info.get("geometry_type"),
        "geometry_families_verified": families,
        "null_geometries_verified": null_count,
        "empty_geometries_verified": empty_count,
        "z_geometries_verified": actual_z_count,
        "invalid_polygons_reopened": invalid_count,
        "fields_verified": actual_field_names,
        "crs_verified": actual_crs_text,
        "bounds_verified": _json_safe(list(bounds) if bounds is not None else None),
    }


def _write_csv_wkt(frame: Any, path: Path) -> None:
    attributes = [column for column in frame.columns if column != frame.geometry.name]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=attributes + ["WKT"])
        writer.writeheader()
        for _, row in frame.iterrows():
            record = {column: _json_safe(row[column]) for column in attributes}
            record["WKT"] = row.geometry.wkt if row.geometry is not None else None
            writer.writerow(record)
    if frame.crs is not None:
        path.with_suffix(".prj").write_text(frame.crs.to_wkt(), encoding="utf-8")


def _verify_csv(path: Path, expected_frame: Any, expected_family: str) -> dict[str, Any]:
    expected_count = len(expected_frame)
    families: set[str] = set()
    z_count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if "WKT" not in (reader.fieldnames or []):
            raise ConversionError(f"CSV QA verification found no WKT field in '{path.name}'.")
        rows = list(reader)
    for row_number, row in enumerate(rows, start=2):
        value = row.get("WKT")
        if not value:
            raise ConversionError(
                f"CSV QA verification found empty WKT at row {row_number} in '{path.name}'."
            )
        try:
            geometry = shapely.from_wkt(value, on_invalid="raise")
        except Exception as exc:
            raise ConversionError(
                f"CSV QA verification found invalid WKT at row {row_number} in '{path.name}': {exc}"
            ) from exc
        family = _geometry_family(geometry)
        if family is None or geometry.is_empty:
            raise ConversionError(
                f"CSV QA verification found unsupported geometry at row {row_number} in '{path.name}'."
            )
        families.add(family)
        z_count += int(bool(has_z(geometry)))
    count = len(rows)
    if count != expected_count:
        raise ConversionError(
            f"CSV QA verification failed for '{path.name}': expected {expected_count}, found {count}."
        )
    if families != {expected_family}:
        raise ConversionError(
            f"CSV QA verification found unexpected geometry families in '{path.name}': {sorted(families)}"
        )
    expected_z_count = sum(bool(has_z(geometry)) for geometry in expected_frame.geometry)
    if z_count != expected_z_count:
        raise ConversionError(
            f"CSV QA verification found Z loss in '{path.name}': expected {expected_z_count}, "
            f"reopened {z_count}."
        )
    return {
        "feature_count_verified": count,
        "geometry_type_verified": "WKT",
        "geometry_families_verified": sorted(families),
        "z_geometries_verified": z_count,
        "fields_verified": reader.fieldnames,
    }



KML_NAMESPACE = "http://www.opengis.net/kml/2.2"
ET.register_namespace("", KML_NAMESPACE)


def _kml_tag(name: str) -> str:
    return f"{{{KML_NAMESPACE}}}{name}"


def _kml_coordinate_text(coordinates: Iterable[Any]) -> str:
    values: list[str] = []
    for coordinate in coordinates:
        try:
            x = float(coordinate[0])
            y = float(coordinate[1])
        except (IndexError, TypeError, ValueError):
            continue
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        z: float | None = None
        try:
            candidate = float(coordinate[2])
            if math.isfinite(candidate):
                z = candidate
        except (IndexError, TypeError, ValueError):
            pass
        values.append(f"{x:.12g},{y:.12g}" if z is None else f"{x:.12g},{y:.12g},{z:.12g}")
    return " ".join(values)


def _append_kml_geometry(parent: Any, geometry: Any) -> None:
    """Append one Shapely geometry to a KML Placemark."""
    if isinstance(geometry, Point):
        element = ET.SubElement(parent, _kml_tag("Point"))
        ET.SubElement(element, _kml_tag("coordinates")).text = _kml_coordinate_text([geometry.coords[0]])
        return
    if isinstance(geometry, LineString):
        element = ET.SubElement(parent, _kml_tag("LineString"))
        ET.SubElement(element, _kml_tag("tessellate")).text = "1"
        ET.SubElement(element, _kml_tag("coordinates")).text = _kml_coordinate_text(geometry.coords)
        return
    if isinstance(geometry, Polygon):
        element = ET.SubElement(parent, _kml_tag("Polygon"))
        ET.SubElement(element, _kml_tag("tessellate")).text = "1"
        outer = ET.SubElement(element, _kml_tag("outerBoundaryIs"))
        ring = ET.SubElement(outer, _kml_tag("LinearRing"))
        ET.SubElement(ring, _kml_tag("coordinates")).text = _kml_coordinate_text(geometry.exterior.coords)
        for interior in geometry.interiors:
            inner = ET.SubElement(element, _kml_tag("innerBoundaryIs"))
            inner_ring = ET.SubElement(inner, _kml_tag("LinearRing"))
            ET.SubElement(inner_ring, _kml_tag("coordinates")).text = _kml_coordinate_text(interior.coords)
        return
    if isinstance(geometry, (MultiPoint, MultiLineString, MultiPolygon, GeometryCollection)):
        element = ET.SubElement(parent, _kml_tag("MultiGeometry"))
        for child in geometry.geoms:
            if not child.is_empty:
                _append_kml_geometry(element, child)
        return
    raise ConversionError(f"KML does not support geometry type '{geometry.geom_type}'.")


def _frame_to_wgs84(frame: Any) -> Any:
    if frame.crs is None:
        raise ConversionError("KML output requires a defined source CRS.")
    source_crs = CRS.from_user_input(frame.crs)
    wgs84 = CRS.from_user_input(WGS84_CRS)
    if source_crs.equals(wgs84):
        return frame.copy()
    return frame.to_crs(wgs84)


def _write_kml(frame: Any, path: Path, *, document_name: str) -> None:
    """Write KML 2.2 natively, independent of optional GDAL KML drivers."""
    wgs84_frame = _frame_to_wgs84(frame)
    root = ET.Element(_kml_tag("kml"))
    document = ET.SubElement(root, _kml_tag("Document"))
    ET.SubElement(document, _kml_tag("name")).text = document_name

    attribute_fields = (
        "source_file",
        "cad_layer",
        "entity_type",
        "entity_handle",
        "block_name",
        "text_value",
    )
    for _, row in wgs84_frame.iterrows():
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        placemark = ET.SubElement(document, _kml_tag("Placemark"))
        name = _optional_text(row.get("text_value")) or _optional_text(row.get("block_name"))
        if not name:
            name = _optional_text(row.get("entity_type")) or str(row.get("cad_layer") or "CAD feature")
            handle = _optional_text(row.get("entity_handle"))
            if handle:
                name = f"{name} {handle}"
        ET.SubElement(placemark, _kml_tag("name")).text = name
        ET.SubElement(placemark, _kml_tag("description")).text = (
            f"CAD layer: {row.get('cad_layer')}; source: {row.get('source_file')}"
        )
        extended = ET.SubElement(placemark, _kml_tag("ExtendedData"))
        for field_name in attribute_fields:
            value = _optional_text(row.get(field_name))
            if value is None:
                continue
            data = ET.SubElement(extended, _kml_tag("Data"), {"name": field_name})
            ET.SubElement(data, _kml_tag("value")).text = value
        _append_kml_geometry(placemark, geometry)

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def _verify_kml(path: Path, expected_count: int, expected_family: str) -> dict[str, Any]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise ConversionError(f"Could not reopen KML output '{path.name}': {exc}") from exc
    placemarks = root.findall(f".//{_kml_tag('Placemark')}")
    if len(placemarks) != expected_count:
        raise ConversionError(
            f"KML QA verification failed for '{path.name}': expected {expected_count} features, "
            f"found {len(placemarks)}."
        )
    coordinate_nodes = root.findall(f".//{_kml_tag('coordinates')}")
    if expected_count and not coordinate_nodes:
        raise ConversionError(f"KML QA verification found no coordinates in '{path.name}'.")
    return {
        "feature_count_verified": len(placemarks),
        "geometry_type_verified": "KML",
        "geometry_families_verified": [expected_family],
        "crs_verified": WGS84_CRS,
    }


def _output_layer_stem(dataset: str, item: OutputLayer) -> str:
    return _safe_component(f"{dataset}_{item.logical_name}", 96)

def _managed_dataset_artifacts(output_directory: Path, dataset: str) -> list[Path]:
    """Return existing files owned by the most recent successful dataset manifest."""
    report_candidates = (
        output_directory / f"{dataset}_report.json",
        output_directory / f"{dataset}_conversion_report.json",
    )
    report_path = next((path for path in report_candidates if path.is_file()), None)
    if report_path is None:
        return []

    try:
        manifest = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return []

    if manifest.get("status") != "success":
        return []
    manifest_name = _safe_component(
        str((manifest.get("options") or {}).get("dataset_name") or ""),
        80,
    )
    if manifest_name != dataset:
        return []

    allowed_suffixes = {
        ".gpkg", ".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".fix",
        ".sbn", ".sbx", ".aih", ".ain", ".atx", ".ixs", ".mxs", ".xml",
        ".geojson", ".fgb", ".kml", ".csv", ".json",
    }
    managed: list[Path] = []
    for raw_name in manifest.get("artifacts") or []:
        name = Path(str(raw_name)).name
        if name != str(raw_name):
            continue
        is_dataset_artifact = (
            name == f"{dataset}.gpkg"
            or name.startswith(f"{dataset}_")
            or name.startswith(f"{dataset}__")
        )
        if not is_dataset_artifact or Path(name).suffix.lower() not in allowed_suffixes:
            continue
        candidate = output_directory / name
        if candidate.is_file():
            managed.append(candidate)
    return sorted(set(managed))


_CLEANUP_RETRY_DELAYS = (0.0, 0.02, 0.05, 0.10, 0.20, 0.40, 0.80)


def _unlink_with_retries(path: Path) -> bool:
    """Remove a temporary file after delayed Windows handle release."""
    for delay in _CLEANUP_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            gc.collect()
    return not os.path.lexists(path)


def _rmtree_with_retries(path: Path) -> bool:
    """Remove a temporary tree, retrying transient GDAL/antivirus locks."""
    for delay in _CLEANUP_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            gc.collect()
    return not os.path.lexists(path)


@contextlib.contextmanager
def _dataset_commit_lock(output_directory: Path, dataset: str) -> Iterator[None]:
    """Hold a non-blocking cross-process lock without cluttering output.

    Windows uses a named kernel mutex and therefore creates no lock file. POSIX
    uses ``flock`` in the operating-system temporary directory rather than in
    the user's GIS output directory.
    """
    output_directory = output_directory.resolve()
    identity = os.path.normcase(str(output_directory)) + "\0" + dataset
    digest = hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest()
    with _ACTIVE_COMMIT_LOCKS_GUARD:
        if digest in _ACTIVE_COMMIT_LOCKS:
            raise ConversionError(
                f"Another conversion is committing dataset '{dataset}' in {output_directory}."
            )
        _ACTIVE_COMMIT_LOCKS.add(digest)

    try:
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_mutex = kernel32.CreateMutexW
            create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            create_mutex.restype = ctypes.c_void_p
            wait_for_single = kernel32.WaitForSingleObject
            wait_for_single.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            wait_for_single.restype = ctypes.c_uint32
            release_mutex = kernel32.ReleaseMutex
            release_mutex.argtypes = [ctypes.c_void_p]
            release_mutex.restype = ctypes.c_int
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int

            handle = create_mutex(None, False, f"Local\\CADtoGISCommit_{digest}")
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            acquired = False
            try:
                status = int(wait_for_single(handle, 0))
                if status == 0x00000102:  # WAIT_TIMEOUT
                    raise ConversionError(
                        f"Another conversion is committing dataset '{dataset}' in "
                        f"{output_directory}."
                    )
                if status not in {0x00000000, 0x00000080}:  # OBJECT_0 / ABANDONED
                    raise ctypes.WinError(ctypes.get_last_error())
                acquired = True
                yield
            finally:
                if acquired:
                    release_mutex(handle)
                close_handle(handle)
        else:
            import fcntl

            lock_root = Path(tempfile.gettempdir()) / "cad_to_gis_locks"
            lock_root.mkdir(parents=True, exist_ok=True)
            lock_path = lock_root / f"{digest}.lock"
            with lock_path.open("a+b") as stream:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, PermissionError, OSError) as exc:
                    raise ConversionError(
                        f"Another conversion is committing dataset '{dataset}' in "
                        f"{output_directory}."
                    ) from exc
                try:
                    stream.seek(0)
                    stream.truncate()
                    stream.write(
                        json.dumps({"pid": os.getpid(), "started_utc": _utc_now()}).encode(
                            "utf-8"
                        )
                    )
                    stream.flush()
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        with _ACTIVE_COMMIT_LOCKS_GUARD:
            _ACTIVE_COMMIT_LOCKS.discard(digest)


def _atomic_move_no_replace(
    source: Path,
    target: Path,
    on_target_created: Callable[[], None] | None = None,
) -> None:
    """Move on one filesystem while atomically refusing an existing target."""
    created = on_target_created or (lambda: None)
    if os.name == "nt":
        move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        move_file.restype = ctypes.c_int
        # Omitting REPLACE_EXISTING is the Windows-native no-clobber guarantee
        # and works on FAT/exFAT/SMB. A cross-volume result is handled by the
        # caller using a closed sibling copy in the destination directory.
        if move_file(str(source), str(target), 0x00000008):  # WRITE_THROUGH
            created()
            return
        error_code = ctypes.get_last_error()
        if error_code in {80, 183}:  # ERROR_FILE_EXISTS / ALREADY_EXISTS
            raise FileExistsError(error_code, ctypes.FormatError(error_code), str(target))
        raise OSError(error_code, ctypes.FormatError(error_code), str(target))

    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is not None:
            renameat2.argtypes = [
                ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            result = renameat2(
                -100, os.fsencode(source), -100, os.fsencode(target), 1
            )  # AT_FDCWD, RENAME_NOREPLACE
            if result == 0:
                created()
                return
            error_code = ctypes.get_errno()
            if error_code == errno.EEXIST:
                raise FileExistsError(error_code, os.strerror(error_code), str(target))
            if error_code not in {
                errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP,
            }:
                raise OSError(error_code, os.strerror(error_code), str(target))

    # Portable same-filesystem fallback.  Linking claims the destination name
    # atomically.  Record the official target before removing the staging link
    # so rollback still knows about it if that unlink is interrupted or fails.
    os.link(source, target)
    created()
    source.unlink()


def _is_windows_sharing_violation(exc: BaseException) -> bool:
    """Return True for Windows access/sharing/lock violations.

    ``MoveFileExW`` errors are constructed locally, so depending on the Python
    build the native code may be exposed as either ``winerror`` or ``errno``.
    """
    if os.name != "nt":
        return False
    code = getattr(exc, "winerror", None)
    if code is None:
        code = getattr(exc, "errno", None)
    return code in {5, 32, 33}  # ACCESS_DENIED / SHARING_VIOLATION / LOCK_VIOLATION


def _requires_copy_install(exc: BaseException) -> bool:
    """Return True when a staging file must be copied before final rename."""
    if _is_windows_sharing_violation(exc):
        return True
    code = getattr(exc, "errno", None)
    winerror = getattr(exc, "winerror", None)
    return code == errno.EXDEV or winerror == 17  # ERROR_NOT_SAME_DEVICE


def _copy_then_atomic_install(
    source: Path,
    target: Path,
    on_target_created: Callable[[], None],
) -> None:
    """Install a readable but non-movable staging file transactionally.

    GDAL may keep a short-lived Windows handle after a GeoPackage has been
    reopened for QA.  Such a handle can prevent renaming the staging file even
    though the file is fully readable.  Copying to a closed, sibling temporary
    file and atomically claiming the final name avoids weakening the no-clobber
    guarantee.
    """
    descriptor, incoming_name = tempfile.mkstemp(
        prefix=f".{target.name}.cadgis_incoming_",
        dir=str(target.parent),
    )
    incoming = Path(incoming_name)
    try:
        with source.open("rb") as input_stream, os.fdopen(descriptor, "wb") as output_stream:
            descriptor = -1
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        _atomic_move_no_replace(incoming, target, on_target_created)
        # A lingering GDAL handle may still delay removal.  Stage cleanup will
        # retry implicitly; failure here must not undo an installed output.
        _unlink_with_retries(source)
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        _unlink_with_retries(incoming)


def _commit_staged_files(
    stage: Path,
    output_directory: Path,
    *,
    overwrite: bool,
    retire_targets: Sequence[Path] = (),
    cancel: CancellationToken | None = None,
) -> list[Path]:
    stage = stage.resolve()
    output_directory = output_directory.resolve()
    staged = sorted(
        (path for path in stage.rglob("*") if path.is_file()),
        key=lambda path: str(path.relative_to(stage)),
    )
    targets = [
        (source, output_directory / source.relative_to(stage)) for source in staged
    ]

    def path_exists(path: Path) -> bool:
        # Path.exists() is false for a broken symlink, but replacing one is
        # still a destructive overwrite.
        return os.path.lexists(path)

    def validate_target(path: Path) -> None:
        try:
            path.relative_to(output_directory)
        except ValueError as exc:
            raise ConversionError(f"Refusing to modify a path outside the output folder: {path}") from exc
        if path.is_symlink():
            raise ConversionError(f"Refusing to replace symbolic-link output path: {path}")
        if path_exists(path) and not path.is_file():
            raise ConversionError(f"Refusing to replace non-file output path: {path}")

    moved_new: list[tuple[Path, tuple[int, int] | None]] = []

    def remember_created_target(target: Path) -> None:
        moved_new.append((target, None))
        stat = target.stat(follow_symlinks=False)
        moved_new[-1] = (target, (int(stat.st_dev), int(stat.st_ino)))

    def install_without_replacement(source: Path, target: Path) -> None:
        """Atomically claim an unused target without a check/use race."""
        try:
            _atomic_move_no_replace(
                source, target, lambda: remember_created_target(target)
            )
        except FileExistsError as exc:
            raise ConversionError(
                f"Output appeared while committing; no file was overwritten: {target}"
            ) from exc
        except OSError as exc:
            if _requires_copy_install(exc):
                # Release cyclic Python wrappers first, then use a closed copy
                # if GDAL still owns the staging handle or the system temporary
                # directory is on another volume.
                gc.collect()
                try:
                    _copy_then_atomic_install(
                        source, target, lambda: remember_created_target(target)
                    )
                    return
                except FileExistsError as retry_exc:
                    raise ConversionError(
                        f"Output appeared while committing; no file was overwritten: {target}"
                    ) from retry_exc
                except OSError as retry_exc:
                    if _is_windows_sharing_violation(retry_exc):
                        raise ConversionError(
                            f"Windows has locked '{target.name}'. Close it in QGIS, ArcGIS, "
                            "a file preview, or another application, then retry; alternatively "
                            "use a different dataset name. The existing file was preserved."
                        ) from retry_exc
                    raise ConversionError(
                        f"Could not safely commit '{target.name}': {retry_exc}"
                    ) from retry_exc
            raise ConversionError(
                f"Could not safely commit '{target.name}' without overwriting an existing file: {exc}"
            ) from exc

    for _source, target in targets:
        validate_target(target)
    conflicts = [target for _source, target in targets if path_exists(target)]
    if conflicts and not overwrite:
        preview = "\n".join(str(path) for path in conflicts[:10])
        raise ConversionError(
            "Output already exists and overwrite is disabled:\n" + preview
        )
    backup: Path | None = None
    backed_up: list[tuple[Path, Path]] = []
    try:
        if overwrite:
            to_backup: set[Path] = set()
            for candidate in [*conflicts, *retire_targets]:
                target = Path(candidate)
                validate_target(target)
                if path_exists(target):
                    to_backup.add(target)
            if to_backup:
                backup = Path(
                    tempfile.mkdtemp(prefix=".cadgis_backup_", dir=str(output_directory))
                )
            for target in sorted(to_backup, key=str):
                if cancel is not None:
                    cancel.check()
                assert backup is not None
                backup_path = backup / target.relative_to(output_directory)
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(target, backup_path)
                except OSError as exc:
                    if _is_windows_sharing_violation(exc):
                        raise ConversionError(
                            f"Windows has locked the existing output '{target.name}'. Close it "
                            "in QGIS, ArcGIS, a file preview, or another application, then retry; "
                            "alternatively use a different dataset name. No output was changed."
                        ) from exc
                    raise
                backed_up.append((backup_path, target))
                if cancel is not None:
                    cancel.check()
        for source, target in targets:
            if cancel is not None:
                cancel.check()
            target.parent.mkdir(parents=True, exist_ok=True)
            install_without_replacement(source, target)
            if cancel is not None:
                cancel.check()
        if cancel is not None:
            cancel.check()
    except BaseException as original_error:
        rollback_errors: list[str] = []
        for target, signature in reversed(moved_new):
            try:
                if not path_exists(target):
                    continue
                current = target.stat(follow_symlinks=False)
                if signature is not None and (
                    int(current.st_dev), int(current.st_ino)
                ) != signature:
                    raise OSError("target changed after this conversion created it")
                target.unlink()
            except OSError as exc:
                rollback_errors.append(f"could not remove new '{target}': {exc}")
        for backup_path, target in reversed(backed_up):
            try:
                if path_exists(target):
                    raise OSError("target was recreated during rollback")
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup_path, target)
            except OSError as exc:
                rollback_errors.append(f"could not restore '{target}': {exc}")
        if rollback_errors:
            details = "; ".join(rollback_errors[:8])
            recovery_location = backup if backup is not None else output_directory
            raise ConversionError(
                "Output commit failed and rollback was incomplete. "
                f"Recovery files were preserved in '{recovery_location}'. {details}"
            ) from original_error
        if backup is not None:
            _rmtree_with_retries(backup)
        raise
    if backup is not None:
        _rmtree_with_retries(backup)
    return [target for target, _signature in moved_new]


def write_output_layers(
    layers: list[OutputLayer],
    job: ConversionJob,
    report: QualityReport,
    *,
    cancel: CancellationToken,
    progress: ProgressCallback,
    log: LogCallback,
) -> tuple[list[str], str]:
    output_directory = Path(job.output_directory).resolve()
    dataset = _safe_component(job.dataset_name, 80)
    fmt = job.output_format.lower()
    expected_crs = parse_crs(job.target_crs) or parse_crs(job.source_crs)
    output_directory.mkdir(parents=True, exist_ok=True)

    stage = Path(tempfile.mkdtemp(prefix=f"cadgis_{dataset}_staging_"))
    layer_entries: list[dict[str, Any]] = []
    field_maps: dict[str, dict[str, str]] = {}
    write_formats = ("gpkg", "shp", "kml") if fmt == "gpkg_shp_kml" else (fmt,)
    write_operation_count = max(1, len(layers) * len(write_formats))
    completed_write_operations = 0

    def advance_write_progress(label: str) -> None:
        nonlocal completed_write_operations
        completed_write_operations += 1
        progress(
            0.80 + 0.17 * (completed_write_operations / write_operation_count),
            label,
        )

    try:
        if "gpkg" in write_formats:
            target_name = f"{dataset}.gpkg"
            staged_path = stage / target_name
            for index, item in enumerate(layers):
                cancel.check()
                log(f"Writing GeoPackage layer {item.logical_name}…")
                _write_vector_layer(
                    item.frame,
                    staged_path,
                    driver="GPKG",
                    layer=item.logical_name,
                    append=index > 0,
                    layer_options={"SPATIAL_INDEX": "YES", "FID": "fid"},
                )
                qa = _verify_vector(
                    staged_path,
                    item.frame,
                    expected_crs,
                    layer=item.logical_name,
                    expected_family=item.geometry_family,
                )
                layer_entries.append(
                    {
                        "path": str(output_directory / target_name),
                        "format": "gpkg",
                        "layer": item.logical_name,
                        "source_file": item.source_file,
                        "source_id": item.source_id,
                        "cad_layer": item.cad_layer,
                        "geometry_family": item.geometry_family,
                        "expected_count": len(item.frame),
                        **qa,
                    }
                )
                advance_write_progress(f"Writing GeoPackage layer {item.logical_name}")
            with sqlite3.connect(staged_path) as database:
                integrity = database.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ConversionError(f"GeoPackage integrity check failed: {integrity}")
            report.statistics["gpkg_integrity_ok"] = 1

        extension_driver = {
            "shp": (".shp", "ESRI Shapefile"),
            "geojson": (".geojson", "GeoJSON"),
            "fgb": (".fgb", "FlatGeobuf"),
        }
        for output_format in write_formats:
            if output_format == "gpkg":
                continue
            for item in layers:
                cancel.check()
                stem = _output_layer_stem(dataset, item)
                log(f"Writing {output_format.upper()} layer {item.logical_name}…")

                if output_format == "csv":
                    staged_path = stage / f"{stem}.csv"
                    frame = item.frame
                    _write_csv_wkt(frame, staged_path)
                    qa = _verify_csv(staged_path, frame, item.geometry_family)
                elif output_format == "kml":
                    staged_path = stage / f"{stem}.kml"
                    frame = item.frame
                    _write_kml(frame, staged_path, document_name=item.logical_name)
                    qa = _verify_kml(staged_path, len(frame), item.geometry_family)
                else:
                    extension, driver = extension_driver[output_format]
                    staged_path = stage / f"{stem}{extension}"
                    frame = item.frame
                    layer_options: dict[str, str] = {}
                    if output_format == "shp":
                        frame, field_map = _prepare_shapefile_frame(frame, report)
                        field_maps[item.logical_name] = field_map
                        layer_options = {"SPATIAL_INDEX": "YES", "RESIZE": "YES"}
                    elif output_format == "geojson":
                        layer_options = {"RFC7946": "YES", "COORDINATE_PRECISION": "9"}
                    elif output_format == "fgb":
                        layer_options = {"SPATIAL_INDEX": "YES"}
                    _write_vector_layer(
                        frame,
                        staged_path,
                        driver=driver,
                        layer=item.logical_name,
                        layer_options=layer_options,
                    )
                    if output_format == "shp" and not staged_path.with_suffix(".cpg").exists():
                        staged_path.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")
                    qa = _verify_vector(
                        staged_path,
                        frame,
                        expected_crs,
                        expected_family=item.geometry_family,
                    )

                layer_entries.append(
                    {
                        "path": str(output_directory / staged_path.name),
                        "format": output_format,
                        "layer": item.logical_name,
                        "source_file": item.source_file,
                        "source_id": item.source_id,
                        "cad_layer": item.cad_layer,
                        "geometry_family": item.geometry_family,
                        "expected_count": len(item.frame),
                        **qa,
                    }
                )
                advance_write_progress(f"Writing {output_format.upper()} layer {item.logical_name}")

        if field_maps:
            (stage / f"{dataset}_fields.json").write_text(
                json.dumps(field_maps, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        report.outputs = layer_entries
        report.statistics["features_written"] = sum(entry["expected_count"] for entry in layer_entries)
        report.statistics["files_or_layers_written"] = len(layer_entries)
        report.finish("success")
        report_name = f"{dataset}_report.json"
        report.artifacts = sorted(
            [path.name for path in stage.rglob("*") if path.is_file()] + [report_name]
        )
        (stage / report_name).write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with _dataset_commit_lock(output_directory, dataset):
            managed_artifacts = _managed_dataset_artifacts(output_directory, dataset) if job.overwrite else []
            cancel.check()
            moved = _commit_staged_files(
                stage,
                output_directory,
                overwrite=job.overwrite,
                retire_targets=managed_artifacts,
                cancel=cancel,
            )
        output_paths = sorted({entry["path"] for entry in layer_entries})
        report_path = str(output_directory / report_name)
        log(f"Committed {len(moved)} files to {output_directory}.")
        return output_paths, report_path
    except CancelledError:
        report.finish("cancelled")
        raise
    except Exception:
        report.finish("failed")
        raise
    finally:
        _rmtree_with_retries(stage)


def _geometry_for_preview(geometry: Any, max_vertices: int) -> tuple[Any, int, int, bool]:
    """Return lightweight display geometry without changing exported geometry."""
    max_vertices = max(1, int(max_vertices))
    original_count = int(get_num_coordinates(geometry))
    if original_count <= max_vertices:
        return geometry, original_count, original_count, False

    if isinstance(geometry, MultiPoint):
        points = list(geometry.geoms)
        if max_vertices <= 1:
            display = points[0]
        else:
            indices = {
                round(index * (len(points) - 1) / (max_vertices - 1))
                for index in range(max_vertices)
            }
            display = MultiPoint([points[index] for index in sorted(indices)])
        shown = int(get_num_coordinates(display))
        return display, original_count, shown, True

    # Long single paths can be decimated directly in O(n), avoiding repeated
    # GEOS topology work.  The first and last vertices are always retained.
    if isinstance(geometry, LineString):
        coords = geometry.coords
        count = len(coords)
        if max_vertices < 2:
            display = Point(coords[0])
        else:
            indices = sorted(
                {
                    round(index * (count - 1) / (max_vertices - 1))
                    for index in range(max_vertices)
                }
            )
            display = LineString([coords[index] for index in indices])
        shown = int(get_num_coordinates(display))
        return display, original_count, shown, True

    try:
        bounds = geometry.bounds
        span = max(
            float(bounds[2]) - float(bounds[0]),
            float(bounds[3]) - float(bounds[1]),
        )
    except Exception:
        span = 0.0

    best = None
    if math.isfinite(span) and span > 0:
        low = 0.0
        high = span
        preserve_topology = isinstance(geometry, (Polygon, MultiPolygon))
        # Preview is a visual approximation only. Eight native GEOS iterations
        # are enough for a responsive display and halve the previous worst-case
        # simplification work.
        for _index in range(8):
            tolerance = (low + high) / 2.0
            candidate = geometry.simplify(
                tolerance, preserve_topology=preserve_topology
            )
            count = int(get_num_coordinates(candidate)) if not candidate.is_empty else 0
            if 0 < count <= max_vertices:
                best = candidate
                high = tolerance
            else:
                low = tolerance

    if best is None:
        best = geometry.envelope
        if best.is_empty:
            best = geometry.centroid
    shown_count = int(get_num_coordinates(best))
    return best, original_count, shown_count, True


def prepare_preview_features(
    records: list[dict[str, Any]],
    job: ConversionJob,
    report: QualityReport,
    *,
    cancel: CancellationToken,
    progress: ProgressCallback,
    feature_limit: int,
    vertex_limit: int,
) -> list[dict[str, Any]]:
    """Prepare a bounded, display-only sample without building GeoDataFrames."""
    transformer = _transformer_for(job, report)
    preview: list[dict[str, Any]] = []
    source_vertices = 0
    display_vertices = 0
    simplified_features = 0
    truncated = False
    transform_failures = 0
    total = len(records)

    for index, record in enumerate(records):
        cancel.check()
        if len(preview) >= feature_limit or display_vertices >= vertex_limit:
            truncated = True
            break
        geometry = record.get("geometry")
        if geometry is None or geometry.is_empty:
            continue
        try:
            geometry = _affine_geometry(geometry, job)
            if transformer is not None:
                geometry = _apply_crs_transform(geometry, transformer)
            # The Tk canvas is 2D; dropping Z here avoids unnecessary memory and
            # coordinate conversion while export continues to honour include_z.
            geometry = force_2d(geometry)
        except Exception as exc:
            transform_failures += 1
            if transform_failures <= 10:
                report.add_issue(
                    "warning",
                    "preview_geometry_transform_failed",
                    str(exc),
                    source_file=record.get("source_file"),
                    entity_handle=record.get("entity_handle"),
                    entity_type=record.get("entity_type"),
                )
            continue

        remaining = vertex_limit - display_vertices
        if remaining <= 0:
            truncated = True
            break
        display_geometry, original_count, shown_count, simplified = _geometry_for_preview(
            geometry, max(1, min(1500, remaining))
        )
        if shown_count > remaining:
            truncated = True
            break

        preview.append(
            {
                "geometry": display_geometry,
                "layer": record.get("cad_layer", "0"),
                "source_file": record.get("source_file"),
                "entity_type": record.get("entity_type"),
                "entity_handle": record.get("entity_handle"),
            }
        )
        source_vertices += original_count
        display_vertices += shown_count
        simplified_features += int(simplified)

        if index % 250 == 0 or index + 1 == total:
            progress(
                0.68 + 0.30 * ((index + 1) / max(1, total)),
                "Preparing fast preview",
            )

    if simplified_features:
        report.add_issue(
            "information",
            "preview_geometry_simplified",
            f"{simplified_features} complex feature(s) were simplified for responsive "
            "display; exported geometry remains full precision.",
        )
    source_sampled = bool(report.statistics.get("preview_top_level_skipped", 0))
    if truncated or source_sampled:
        report.add_issue(
            "information",
            "preview_sampled",
            f"Preview is a performance-optimised sample with {len(preview)} displayed "
            f"features and {display_vertices} vertices; export always processes the full drawing.",
        )

    report.statistics["preview_features"] = len(preview)
    report.statistics["preview_source_vertices"] = source_vertices
    report.statistics["preview_vertices"] = display_vertices
    report.statistics["preview_simplified_features"] = simplified_features
    report.statistics["preview_transform_failures"] = transform_failures
    return preview


def convert_job(
    job: ConversionJob,
    *,
    write: bool = True,
    cancel: CancellationToken | None = None,
    progress: ProgressCallback | None = None,
    log: LogCallback | None = None,
    preview_feature_limit: int = DEFAULT_PREVIEW_FEATURE_LIMIT,
    preview_vertex_limit: int = DEFAULT_PREVIEW_VERTEX_LIMIT,
) -> ConversionResult:
    require_runtime()
    token = cancel or CancellationToken()
    update = progress or (lambda _value, _message: None)
    say = log or (lambda _message: None)
    job = validate_job(job, for_writing=write)
    report = QualityReport(job)

    # Preview is deliberately lightweight: the canvas is 2D and does not need
    # full export metadata, topology QA or GeoDataFrame construction.
    working_job = (
        job
        if write
        else replace(job, include_z=False, repair_invalid_polygons=False)
    )
    extractor = DXFExtractor(
        working_job,
        report,
        token,
        update,
        say,
        preview_mode=not write,
        preview_top_level_limit=DEFAULT_PREVIEW_TOP_LEVEL_LIMIT,
        preview_record_limit=max(1, preview_feature_limit),
    )
    try:
        records = extractor.extract_all()
        token.check()

        if not write:
            preview = prepare_preview_features(
                records,
                working_job,
                report,
                cancel=token,
                progress=update,
                feature_limit=max(1, int(preview_feature_limit)),
                vertex_limit=max(100, int(preview_vertex_limit)),
            )
            report.finish("preview")
            update(1.0, "Preview ready")
            return ConversionResult([], None, report.to_dict(), preview)

        layers = prepare_output_layers(
            records, job, report, cancel=token, progress=update, log=say
        )
        outputs, report_path = write_output_layers(
            layers, job, report, cancel=token, progress=update, log=say
        )
        update(1.0, "Conversion complete")
        return ConversionResult(outputs, report_path, report.to_dict())
    except CancelledError:
        report.finish("cancelled")
        raise
    except Exception:
        report.finish("failed")
        raise


# ---------------------------------------------------------------------------
# Tkinter GUI (no Qt, browser engine, JavaScript, tiles or network dependency)
# ---------------------------------------------------------------------------

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception as exc:  # pragma: no cover
    tk = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    filedialog = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    _IMPORT_ERRORS["tkinter"] = str(exc)


class CanvasPreview(ttk.Frame if ttk is not None else object):
    COLORS = (
        "#2F80ED", "#EB5757", "#27AE60", "#9B51E0", "#F2994A",
        "#00A6A6", "#7B61FF", "#C44569", "#607D8B", "#7A9E2F",
    )

    def __init__(self, master: Any):
        super().__init__(master)
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Button(toolbar, text="Fit", command=self.fit).pack(side="left")
        ttk.Label(
            toolbar,
            text="Mouse wheel: zoom   •   Drag: pan   •   Offline coordinate preview",
        ).pack(side="left", padx=12)
        self.summary = ttk.Label(toolbar, text="No preview")
        self.summary.pack(side="right")
        self.canvas = tk.Canvas(
            self,
            background="#000000",
            highlightthickness=1,
            highlightbackground="#C8D2E1",
            cursor="fleur",
        )
        self.canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.features: list[dict[str, Any]] = []
        self.bounds: tuple[float, float, float, float] | None = None
        self.empty_message = "Add CAD files, analyse layers, then choose Preview"
        self.center = (0.0, 0.0)
        self.scale = 1.0
        self._drag_anchor: tuple[int, int] | None = None
        self._redraw_after: str | None = None
        self._color_cache: dict[str, str] = {}
        self._render_transform: tuple[float, float, float] | None = None
        self.canvas.bind("<Configure>", lambda _event: self._schedule_redraw())
        self.canvas.bind("<ButtonPress-1>", self._pan_start)
        self.canvas.bind("<B1-Motion>", self._pan_move)
        self.canvas.bind("<ButtonRelease-1>", lambda _event: setattr(self, "_drag_anchor", None))
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-4>", lambda event: self._zoom_at(event.x, event.y, 1.2))
        self.canvas.bind("<Button-5>", lambda event: self._zoom_at(event.x, event.y, 1 / 1.2))

    def set_features(self, features: list[dict[str, Any]]) -> None:
        self.features = features
        finite_bounds: list[tuple[float, float, float, float]] = []
        for feature in features:
            try:
                bounds = tuple(float(v) for v in feature["geometry"].bounds)
                if len(bounds) == 4 and all(math.isfinite(v) for v in bounds):
                    finite_bounds.append(bounds)  # type: ignore[arg-type]
            except Exception:
                continue
        if finite_bounds:
            self.bounds = (
                min(value[0] for value in finite_bounds),
                min(value[1] for value in finite_bounds),
                max(value[2] for value in finite_bounds),
                max(value[3] for value in finite_bounds),
            )
        else:
            self.bounds = None
        layers = len({item.get("layer") for item in features})
        if features:
            self.summary.configure(text=f"{len(features):,} features • {layers:,} layers")
            self.empty_message = ""
        else:
            self.summary.configure(text="No drawable features")
            self.empty_message = (
                "No drawable geometry was produced. Check the selected layers and the QA report."
            )
        self.after_idle(self.fit)

    def clear(self) -> None:
        self.features = []
        self.bounds = None
        self.canvas.delete("all")
        self.summary.configure(text="No preview")
        self.empty_message = "Add CAD files, analyse layers, then choose Preview"

    def fit(self) -> None:
        if not self.bounds:
            self._redraw()
            return
        min_x, min_y, max_x, max_y = self.bounds
        width = max(100, self.canvas.winfo_width())
        height = max(100, self.canvas.winfo_height())
        span_x = max(max_x - min_x, 1e-12)
        span_y = max(max_y - min_y, 1e-12)
        self.center = ((min_x + max_x) / 2, (min_y + max_y) / 2)
        self.scale = max(1e-12, 0.88 * min(width / span_x, height / span_y))
        self._redraw()

    def _world_to_screen(self, x: float, y: float) -> tuple[float, float]:
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        return (
            width / 2 + (x - self.center[0]) * self.scale,
            height / 2 - (y - self.center[1]) * self.scale,
        )

    def _screen_to_world(self, x: float, y: float) -> tuple[float, float]:
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        return (
            self.center[0] + (x - width / 2) / self.scale,
            self.center[1] - (y - height / 2) / self.scale,
        )

    def _layer_color(self, layer: str) -> str:
        cached = self._color_cache.get(layer)
        if cached is not None:
            return cached
        digest = hashlib.sha1(str(layer).encode("utf-8", "replace")).digest()[0]
        color = self.COLORS[digest % len(self.COLORS)]
        self._color_cache[layer] = color
        return color

    def _coordinates_2d(self, coordinates: Iterable[Any]) -> list[float]:
        flat: list[float] = []
        transform = self._render_transform
        if transform is None:
            width = self.canvas.winfo_width()
            height = self.canvas.winfo_height()
            scale = self.scale
            x0 = width / 2 - self.center[0] * scale
            y0 = height / 2 + self.center[1] * scale
        else:
            x0, y0, scale = transform
        append = flat.extend
        for coordinate in coordinates:
            try:
                x, y = float(coordinate[0]), float(coordinate[1])
                if math.isfinite(x) and math.isfinite(y):
                    append((x0 + x * scale, y0 - y * scale))
            except Exception:
                continue
        return flat

    def _draw_geometry(self, geometry: Any, color: str) -> None:
        if isinstance(geometry, Point):
            transform = self._render_transform
            if transform is None:
                x, y = self._world_to_screen(geometry.x, geometry.y)
            else:
                x0, y0, scale = transform
                x, y = x0 + geometry.x * scale, y0 - geometry.y * scale
            self.canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline=color)
        elif isinstance(geometry, MultiPoint):
            for point in geometry.geoms:
                self._draw_geometry(point, color)
        elif isinstance(geometry, LineString):
            coords = self._coordinates_2d(geometry.coords)
            if len(coords) >= 4:
                self.canvas.create_line(*coords, fill=color, width=1.4)
        elif isinstance(geometry, MultiLineString):
            for line in geometry.geoms:
                self._draw_geometry(line, color)
        elif isinstance(geometry, Polygon):
            exterior = self._coordinates_2d(geometry.exterior.coords)
            if len(exterior) >= 6:
                self.canvas.create_polygon(
                    *exterior, outline=color, fill="", width=1.0
                )
            for ring in geometry.interiors:
                hole = self._coordinates_2d(ring.coords)
                if len(hole) >= 6:
                    self.canvas.create_polygon(
                        *hole, outline=color, fill="", width=1.0
                    )
        elif isinstance(geometry, MultiPolygon):
            for polygon in geometry.geoms:
                self._draw_geometry(polygon, color)
        elif isinstance(geometry, GeometryCollection):
            for child in geometry.geoms:
                self._draw_geometry(child, color)

    def _draw_grid(self) -> None:
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width < 20 or height < 20 or self.scale <= 0:
            return
        desired_world = 100 / self.scale
        if desired_world <= 0 or not math.isfinite(desired_world):
            return
        power = 10 ** math.floor(math.log10(desired_world))
        fraction = desired_world / power
        step = power * (1 if fraction < 2 else 2 if fraction < 5 else 5)
        min_x, max_y = self._screen_to_world(0, 0)
        max_x, min_y = self._screen_to_world(width, height)
        x = math.floor(min_x / step) * step
        while x <= max_x:
            sx, _ = self._world_to_screen(x, 0)
            self.canvas.create_line(sx, 0, sx, height, fill="#333333", width=1)
            x += step
        y = math.floor(min_y / step) * step
        while y <= max_y:
            _, sy = self._world_to_screen(0, y)
            self.canvas.create_line(0, sy, width, sy, fill="#333333", width=1)
            y += step
        self.canvas.create_text(
            8,
            height - 8,
            anchor="sw",
            text=f"Grid ≈ {step:.6g} coordinate units",
            fill="#637083",
            font=("TkDefaultFont", 8),
        )

    def _redraw(self) -> None:
        self._redraw_after = None
        self.canvas.delete("all")
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        scale = self.scale
        self._render_transform = (
            width / 2 - self.center[0] * scale,
            height / 2 + self.center[1] * scale,
            scale,
        )
        self._draw_grid()
        for feature in self.features:
            try:
                self._draw_geometry(
                    feature["geometry"], self._layer_color(str(feature.get("layer", "0")))
                )
            except Exception:
                continue
        self._render_transform = None
        if not self.features:
            self.canvas.create_text(
                self.canvas.winfo_width() / 2,
                self.canvas.winfo_height() / 2,
                text=self.empty_message,
                fill="#637083",
                font=("TkDefaultFont", 11),
                width=max(120, self.canvas.winfo_width() - 60),
            )

    def _schedule_redraw(self) -> None:
        if self._redraw_after is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._redraw_after)
        self._redraw_after = self.after(45, self._redraw)

    def _pan_start(self, event: Any) -> None:
        self._drag_anchor = (event.x, event.y)

    def _pan_move(self, event: Any) -> None:
        if self._drag_anchor is None or self.scale <= 0:
            return
        dx = event.x - self._drag_anchor[0]
        dy = event.y - self._drag_anchor[1]
        self.center = (
            self.center[0] - dx / self.scale,
            self.center[1] + dy / self.scale,
        )
        self._drag_anchor = (event.x, event.y)
        self._schedule_redraw()

    def _wheel(self, event: Any) -> None:
        factor = 1.2 if event.delta > 0 else 1 / 1.2
        self._zoom_at(event.x, event.y, factor)

    def _zoom_at(self, x: int, y: int, factor: float) -> None:
        before = self._screen_to_world(x, y)
        self.scale = min(max(self.scale * factor, 1e-15), 1e15)
        after = self._screen_to_world(x, y)
        self.center = (
            self.center[0] + before[0] - after[0],
            self.center[1] + before[1] - after[1],
        )
        self._schedule_redraw()


def _monitor_work_area(window: Any) -> tuple[int, int, int, int]:
    """Return the usable monitor work area as ``(x, y, width, height)``.

    On Windows this uses the monitor containing the Tk window and excludes the
    taskbar.  The Tk screen size is used as a portable fallback.
    """
    fallback = (0, 0, max(1, int(window.winfo_screenwidth())), max(1, int(window.winfo_screenheight())))
    if os.name != "nt":
        return fallback

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", ctypes.c_ulong),
        ]

    try:
        user32 = ctypes.windll.user32
        monitor_from_window = user32.MonitorFromWindow
        monitor_from_window.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        monitor_from_window.restype = ctypes.c_void_p
        get_monitor_info = user32.GetMonitorInfoW
        get_monitor_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
        get_monitor_info.restype = ctypes.c_int

        # MONITOR_DEFAULTTONEAREST keeps this valid before the root is shown.
        monitor = monitor_from_window(ctypes.c_void_p(int(window.winfo_id())), 2)
        if monitor:
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if get_monitor_info(monitor, ctypes.byref(info)):
                rect = info.rcWork
                width = int(rect.right - rect.left)
                height = int(rect.bottom - rect.top)
                if width > 0 and height > 0:
                    return (int(rect.left), int(rect.top), width, height)
    except Exception:
        pass

    # Primary-monitor fallback that still excludes the Windows taskbar.
    try:
        rect = RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
            if width > 0 and height > 0:
                return (int(rect.left), int(rect.top), width, height)
    except Exception:
        pass
    return fallback


def _fit_toplevel_to_work_area(window: Any, parent: Any, *, margin: int = 10) -> None:
    """Place a Tk toplevel wholly inside its monitor's usable work area."""
    window.update_idletasks()
    work_x, work_y, work_w, work_h = _monitor_work_area(parent)
    req_w = max(1, int(window.winfo_reqwidth()))
    req_h = max(1, int(window.winfo_reqheight()))
    width = min(req_w, max(320, work_w - 2 * margin))
    height = min(req_h, max(240, work_h - 2 * margin))

    parent_x = int(parent.winfo_rootx())
    parent_y = int(parent.winfo_rooty())
    parent_w = max(1, int(parent.winfo_width()))
    parent_h = max(1, int(parent.winfo_height()))
    x = parent_x + max(0, (parent_w - width) // 2)
    y = parent_y + max(0, (parent_h - height) // 2)
    x = max(work_x + margin, min(x, work_x + work_w - width - margin))
    y = max(work_y + margin, min(y, work_y + work_h - height - margin))
    window.geometry(f"{width}x{height}+{x}+{y}")


class CADToGISApp:
    FORMAT_LABELS = {
        "GeoPackage only (.gpkg)": "gpkg",
        "GeoPackage + Shapefile + KML (.gpkg + .shp + .kml)": "gpkg_shp_kml",
        "ESRI Shapefile (.shp)": "shp",
        "GeoJSON RFC 7946 (.geojson)": "geojson",
        "FlatGeobuf (.fgb)": "fgb",
        "KML (.kml)": "kml",
        "CSV with WKT geometry (.csv)": "csv",
    }
    BLOCK_LABELS = {
        "Explode recursively; keep block lineage": "explode",
        "Insertion points only": "points",
        "Both insertion points and exploded geometry": "both",
    }

    def __init__(self, root: Any):
        self.root = root
        self.root.title(f"{APP_NAME} {APP_VERSION}")

        # Use the actual monitor work area (taskbar excluded on Windows) rather
        # than a fixed minimum that can exceed a 1366x768 desktop.
        self.work_area = _monitor_work_area(self.root)
        _work_x, _work_y, work_width, work_height = self.work_area
        self.compact_ui = work_width <= 1400 or work_height <= 800
        min_width = min(1080, max(480, work_width - 32))
        # Leave room for the OS title bar/window frame even when Tk reports the
        # physical work-area height after DPI awareness has been enabled.
        min_height = min(720, max(500, work_height - 80))
        self.root.minsize(min_width, min_height)
        self._configure_style()
        self.input_paths: list[str] = []
        self.layer_state: dict[str, bool] = {}
        self.layer_iids: dict[str, str] = {}
        self.last_scan: dict[str, Any] | None = None
        self.last_report: dict[str, Any] | None = None
        self.worker: threading.Thread | None = None
        self.cancel_token: CancellationToken | None = None
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.active_kind: str | None = None
        self.closing = False
        self._busy_widgets: list[Any] = []
        self._preview_cache_key: Any | None = None
        self._preview_cache_result: ConversionResult | None = None
        self._active_preview_key: Any | None = None

        self.output_var = tk.StringVar()
        self.dataset_var = tk.StringVar(value="cad_to_gis")
        self.format_var = tk.StringVar(value=next(iter(self.FORMAT_LABELS)))
        self.source_crs_var = tk.StringVar(value=DEFAULT_CRS)
        self.target_crs_var = tk.StringVar(value=DEFAULT_CRS)
        self.tolerance_var = tk.StringVar(value="0.01")
        self.scale_var = tk.StringVar(value="1.0")
        self.rotation_var = tk.StringVar(value="0.0")
        self.offset_x_var = tk.StringVar(value="0.0")
        self.offset_y_var = tk.StringVar(value="0.0")
        self.include_z_var = tk.BooleanVar(value=True)
        self.closed_polygon_var = tk.BooleanVar(value=True)
        self.block_var = tk.StringVar(value=next(iter(self.BLOCK_LABELS)))
        self.paperspace_var = tk.BooleanVar(value=False)
        self.audit_var = tk.BooleanVar(value=True)
        self.repair_var = tk.BooleanVar(value=False)
        self.strict_var = tk.BooleanVar(value=True)
        self.overwrite_var = tk.BooleanVar(value=True)
        self.converter_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")
        self.progress_var = tk.DoubleVar(value=0.0)

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._poll_events)
        self._log(f"{APP_NAME} {APP_VERSION}")
        if _IMPORT_ERRORS:
            self._log("Missing dependencies: " + json.dumps(_IMPORT_ERRORS, ensure_ascii=False))
            self.status_var.set("Dependencies missing — see Log")
        else:
            converter = detect_dwg_converter(None)
            self._log(
                "DWG backend: "
                + (f"{converter['engine']} ({converter['path']})" if converter else "not detected; DXF is ready")
            )

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        available = style.theme_names()
        if "vista" in available:
            style.theme_use("vista")
        elif "clam" in available:
            style.theme_use("clam")
        style.configure("Header.TFrame", background="#0B2545")
        title_size = 14 if self.compact_ui else 17
        subtitle_size = 8 if self.compact_ui else 9
        primary_padding = (10, 3) if self.compact_ui else (14, 8)
        tree_row_height = 18 if self.compact_ui else 24
        style.configure(
            "HeaderTitle.TLabel",
            background="#0B2545",
            foreground="#FFFFFF",
            font=("Segoe UI", title_size, "bold"),
        )
        style.configure(
            "HeaderSub.TLabel",
            background="#0B2545",
            foreground="#C9D8EA",
            font=("Segoe UI", subtitle_size),
        )
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), padding=primary_padding)
        style.configure("Treeview", rowheight=tree_row_height)
        style.configure("TLabelframe.Label", font=("Segoe UI", 9, "bold"))

    def _build_ui(self) -> None:
        compact = self.compact_ui
        outer_pad = 4 if compact else 10
        section_gap = 4 if compact else 8
        section_pad = 5 if compact else 8
        row_pad = 1 if compact else 3
        header = ttk.Frame(
            self.root,
            style="Header.TFrame",
            padding=(12, 5) if compact else (18, 12),
        )
        header.pack(fill="x")
        ttk.Label(header, text="CAD → GIS Studio", style="HeaderTitle.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Auditable DXF/DWG conversion • precise CRS handling • offline preview",
            style="HeaderSub.TLabel",
        ).pack(anchor="w")

        paned = ttk.PanedWindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=outer_pad, pady=outer_pad)
        left = ttk.Frame(paned, padding=(0, 0, 8, 0))
        right = ttk.Frame(paned)
        paned.add(left, weight=4)
        paned.add(right, weight=6)

        file_frame = ttk.LabelFrame(left, text="1. CAD input files", padding=section_pad)
        file_frame.pack(fill="x", pady=(0, section_gap))
        file_buttons = ttk.Frame(file_frame)
        file_buttons.pack(fill="x", pady=(0, 3 if compact else 6))
        self.add_button = ttk.Button(file_buttons, text="Add DXF / DWG…", command=self._add_files)
        self.add_button.pack(side="left")
        self.remove_button = ttk.Button(file_buttons, text="Remove", command=self._remove_files)
        self.remove_button.pack(side="left", padx=5)
        self.clear_button = ttk.Button(file_buttons, text="Clear", command=self._clear_files)
        self.clear_button.pack(side="left")
        self.scan_button = ttk.Button(file_buttons, text="Analyse layers", command=self._scan)
        self.scan_button.pack(side="right")
        self.file_tree = ttk.Treeview(
            file_frame,
            columns=("file", "type", "size"),
            show="headings",
            height=2 if compact else 4,
            selectmode="extended",
        )
        self.file_tree.heading("file", text="File")
        self.file_tree.heading("type", text="Type")
        self.file_tree.heading("size", text="Size")
        self.file_tree.column("file", width=280, stretch=True)
        self.file_tree.column("type", width=50, stretch=False, anchor="center")
        self.file_tree.column("size", width=80, stretch=False, anchor="e")
        self.file_tree.pack(fill="x")

        layer_frame = ttk.LabelFrame(left, text="2. CAD layers", padding=section_pad)
        layer_frame.pack(fill="both", expand=True, pady=(0, section_gap))
        layer_buttons = ttk.Frame(layer_frame)
        layer_buttons.pack(fill="x", pady=(0, 3 if compact else 5))
        ttk.Label(layer_buttons, text="Space/double-click toggles selected rows").pack(side="left")
        self.layers_all_button = ttk.Button(
            layer_buttons,
            text="All",
            width=7,
            command=lambda: self._set_all_layers(True),
        )
        self.layers_all_button.pack(side="right")
        self.layers_none_button = ttk.Button(
            layer_buttons,
            text="None",
            width=7,
            command=lambda: self._set_all_layers(False),
        )
        self.layers_none_button.pack(side="right", padx=5)
        self.layer_tree = ttk.Treeview(
            layer_frame,
            columns=("use", "layer", "count", "types", "files"),
            show="headings",
            height=3 if compact else 9,
            selectmode="extended",
        )
        headings = {
            "use": ("Use", 45, "center"),
            "layer": ("Layer", 150, "w"),
            "count": ("Top-level", 75, "e"),
            "types": ("Entity types", 170, "w"),
            "files": ("Files", 55, "center"),
        }
        for column, (label, width, anchor) in headings.items():
            self.layer_tree.heading(column, text=label)
            self.layer_tree.column(column, width=width, anchor=anchor, stretch=column in {"layer", "types"})
        layer_scroll = ttk.Scrollbar(layer_frame, orient="vertical", command=self.layer_tree.yview)
        self.layer_tree.configure(yscrollcommand=layer_scroll.set)
        self.layer_tree.pack(side="left", fill="both", expand=True)
        layer_scroll.pack(side="right", fill="y")
        self.layer_tree.bind("<Double-1>", self._toggle_layer_event)
        self.layer_tree.bind("<space>", self._toggle_layer_event)

        output_frame = ttk.LabelFrame(left, text="3. Output and coordinate systems", padding=section_pad)
        output_frame.pack(fill="x", pady=(0, section_gap))
        output_frame.columnconfigure(1, weight=1)
        ttk.Label(output_frame, text="Format").grid(row=0, column=0, sticky="w", pady=row_pad)
        self.format_combo = ttk.Combobox(
            output_frame,
            textvariable=self.format_var,
            values=list(self.FORMAT_LABELS),
            state="readonly",
            width=34,
        )
        self.format_combo.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=row_pad)
        self.format_combo.bind("<<ComboboxSelected>>", self._format_changed)
        ttk.Label(output_frame, text="Output folder").grid(row=1, column=0, sticky="w", pady=row_pad)
        self.output_entry = ttk.Entry(output_frame, textvariable=self.output_var)
        self.output_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=row_pad)
        self.output_button = ttk.Button(output_frame, text="Browse…", command=self._choose_output)
        self.output_button.grid(row=1, column=2, pady=row_pad)
        ttk.Label(output_frame, text="Dataset name").grid(row=2, column=0, sticky="w", pady=row_pad)
        self.dataset_entry = ttk.Entry(output_frame, textvariable=self.dataset_var)
        self.dataset_entry.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=row_pad)
        ttk.Label(output_frame, text="Source CRS").grid(row=3, column=0, sticky="w", pady=row_pad)
        self.source_crs_entry = ttk.Entry(output_frame, textvariable=self.source_crs_var)
        self.source_crs_entry.grid(row=3, column=1, sticky="ew", padx=8, pady=row_pad)
        ttk.Label(output_frame, text="e.g. EPSG:3763").grid(row=3, column=2, sticky="w", pady=row_pad)
        ttk.Label(output_frame, text="Target CRS").grid(row=4, column=0, sticky="w", pady=row_pad)
        self.target_crs_entry = ttk.Entry(output_frame, textvariable=self.target_crs_var)
        self.target_crs_entry.grid(row=4, column=1, sticky="ew", padx=8, pady=row_pad)
        self.crs_button = ttk.Button(output_frame, text="Validate", command=self._validate_crs_ui)
        self.crs_button.grid(row=4, column=2, sticky="ew", pady=row_pad)
        note = ttk.Label(
            output_frame,
            text=f"Default CRS: {DEFAULT_CRS} — {DEFAULT_CRS_NAME}.",
            foreground="#5E6B7A",
            wraplength=430 if compact else 470,
        )
        note.grid(row=5, column=0, columnspan=3, sticky="w", pady=(1 if compact else 4, 0))

        action_frame = ttk.Frame(left)
        action_frame.pack(fill="x")
        self.advanced_button = ttk.Button(action_frame, text="Advanced…", command=self._show_advanced)
        self.advanced_button.pack(side="left")
        self.preview_button = ttk.Button(action_frame, text="Preview", command=self._preview)
        self.preview_button.pack(side="left", padx=5)
        self.convert_button = ttk.Button(action_frame, text="Convert", style="Primary.TButton", command=self._convert)
        self.convert_button.pack(side="right")
        self.cancel_button = ttk.Button(action_frame, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="right", padx=6)

        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill="both", expand=True)
        self.preview_panel = CanvasPreview(self.notebook)
        self.notebook.add(self.preview_panel, text="Preview")
        self.log_panel = ttk.Frame(self.notebook)
        self.report_panel = ttk.Frame(self.notebook)
        self.notebook.add(self.log_panel, text="Log")
        self.notebook.add(self.report_panel, text="QA report")
        self.log_text = tk.Text(
            self.log_panel,
            wrap="word",
            font=("Cascadia Mono", 9),
            background="#101923",
            foreground="#D8E6F3",
            insertbackground="#FFFFFF",
            relief="flat",
            padx=10,
            pady=10,
        )
        log_scroll = ttk.Scrollbar(self.log_panel, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")
        self.report_text = tk.Text(
            self.report_panel, wrap="none", font=("Cascadia Mono", 9), padx=10, pady=10
        )
        report_y = ttk.Scrollbar(self.report_panel, orient="vertical", command=self.report_text.yview)
        report_x = ttk.Scrollbar(self.report_panel, orient="horizontal", command=self.report_text.xview)
        self.report_text.configure(yscrollcommand=report_y.set, xscrollcommand=report_x.set)
        self.report_text.grid(row=0, column=0, sticky="nsew")
        report_y.grid(row=0, column=1, sticky="ns")
        report_x.grid(row=1, column=0, sticky="ew")
        self.report_panel.rowconfigure(0, weight=1)
        self.report_panel.columnconfigure(0, weight=1)

        status = ttk.Frame(
            self.root, padding=(8, 1, 8, 3) if compact else (10, 2, 10, 8)
        )
        status.pack(fill="x")
        ttk.Label(status, textvariable=self.status_var).pack(side="left")
        self.progress = ttk.Progressbar(
            status,
            variable=self.progress_var,
            maximum=100.0,
            mode="determinate",
            length=220 if compact else 320,
        )
        self.progress.pack(side="right")

        self._busy_widgets.extend(
            [
                self.add_button,
                self.remove_button,
                self.clear_button,
                self.scan_button,
                self.file_tree,
                self.layer_tree,
                self.layers_all_button,
                self.layers_none_button,
                self.format_combo,
                self.output_entry,
                self.output_button,
                self.dataset_entry,
                self.source_crs_entry,
                self.target_crs_entry,
                self.crs_button,
                self.advanced_button,
                self.preview_button,
                self.convert_button,
            ]
        )

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{timestamp}] {message.rstrip()}\n")
        self.log_text.see("end")

    @staticmethod
    def _human_size(size: int) -> str:
        value = float(size)
        for suffix in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or suffix == "TB":
                return f"{value:.0f} {suffix}" if suffix == "B" else f"{value:.1f} {suffix}"
            value /= 1024
        return str(size)

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self.root,
            title="Choose CAD files",
            filetypes=[("CAD files", "*.dxf *.dwg"), ("DXF", "*.dxf"), ("DWG", "*.dwg"), ("All files", "*.*")],
        )
        changed = False
        known = {os.path.normcase(os.path.abspath(path)) for path in self.input_paths}
        for path in paths:
            normalized = os.path.normcase(os.path.abspath(path))
            if normalized not in known:
                self.input_paths.append(os.path.abspath(path))
                known.add(normalized)
                changed = True
        if changed:
            self._refresh_files()
            self._invalidate_analysis()
            if len(self.input_paths) == 1:
                source = Path(self.input_paths[0])
                self.dataset_var.set(_safe_component(source.stem))
                if not self.output_var.get():
                    self.output_var.set(str(source.parent))
            # Display the loaded CAD immediately.  after_idle lets Tk finish
            # updating the file list and form variables before conversion starts.
            self.root.after_idle(self._preview)

    def _remove_files(self) -> None:
        selected = set(self.file_tree.selection())
        if not selected:
            return
        self.input_paths = [path for index, path in enumerate(self.input_paths) if str(index) not in selected]
        self._refresh_files()
        self._invalidate_analysis()

    def _clear_files(self) -> None:
        self.input_paths.clear()
        self._refresh_files()
        self._invalidate_analysis()

    def _refresh_files(self) -> None:
        self.file_tree.delete(*self.file_tree.get_children())
        for index, path in enumerate(self.input_paths):
            source = Path(path)
            try:
                size = self._human_size(source.stat().st_size)
            except OSError:
                size = "missing"
            self.file_tree.insert(
                "", "end", iid=str(index), values=(source.name, source.suffix.upper().lstrip("."), size)
            )

    def _invalidate_analysis(self) -> None:
        self.last_scan = None
        self._preview_cache_key = None
        self._preview_cache_result = None
        self._active_preview_key = None
        self.layer_state.clear()
        self.layer_iids.clear()
        self.layer_tree.delete(*self.layer_tree.get_children())
        self.preview_panel.clear()

    def _choose_output(self) -> None:
        directory = filedialog.askdirectory(
            parent=self.root, title="Choose output folder", initialdir=self.output_var.get() or None
        )
        if directory:
            self.output_var.set(directory)

    def _format_changed(self, _event: Any = None) -> None:
        fmt = self.FORMAT_LABELS.get(self.format_var.get(), DEFAULT_OUTPUT_FORMAT)
        if fmt in WGS84_OUTPUT_FORMATS:
            self.target_crs_var.set(WGS84_CRS)

    def _validate_crs_ui(self) -> None:
        try:
            source = parse_crs(self.source_crs_var.get())
            target = parse_crs(self.target_crs_var.get())
            lines = []
            for label, crs in (("Source", source), ("Target", target)):
                if crs is None:
                    lines.append(f"{label}: undefined/local")
                else:
                    auth = crs.to_authority()
                    identifier = f"{auth[0]}:{auth[1]}" if auth else "custom CRS"
                    lines.append(f"{label}: {identifier} — {crs.name}")
            if target is not None and source is None:
                raise ConversionError("Set the source CRS before setting a target CRS.")
            messagebox.showinfo("Coordinate systems", "\n".join(lines), parent=self.root)
        except Exception as exc:
            messagebox.showerror("Invalid coordinate system", str(exc), parent=self.root)

    def _toggle_layer_event(self, _event: Any = None) -> str:
        selected = list(self.layer_tree.selection())
        if not selected:
            focused = self.layer_tree.focus()
            selected = [focused] if focused else []
        for iid in selected:
            layer = self.layer_iids.get(iid)
            if layer is not None:
                self.layer_state[layer] = not self.layer_state.get(layer, True)
                values = list(self.layer_tree.item(iid, "values"))
                values[0] = "☑" if self.layer_state[layer] else "☐"
                self.layer_tree.item(iid, values=values)
        return "break"

    def _set_all_layers(self, state: bool) -> None:
        for iid, layer in self.layer_iids.items():
            self.layer_state[layer] = state
            values = list(self.layer_tree.item(iid, "values"))
            values[0] = "☑" if state else "☐"
            self.layer_tree.item(iid, values=values)

    def _selected_layers(self) -> frozenset[str] | None:
        if self.last_scan is None:
            return None
        enabled_layers = frozenset(
            layer for layer, enabled in self.layer_state.items() if enabled
        )
        if len(enabled_layers) == len(self.layer_state):
            return None
        return enabled_layers

    def _number(self, variable: Any, label: str) -> float:
        try:
            value = float(variable.get().strip())
        except Exception as exc:
            raise ConversionError(f"{label} must be numeric.") from exc
        if not math.isfinite(value):
            raise ConversionError(f"{label} must be finite.")
        return value

    def _build_job(self, *, write: bool, scan_only: bool = False) -> ConversionJob:
        fmt = "gpkg" if scan_only else self.FORMAT_LABELS[self.format_var.get()]
        target = self.target_crs_var.get().strip() or None
        if scan_only:
            target = None
        elif fmt in WGS84_OUTPUT_FORMATS:
            target = WGS84_CRS
        job = ConversionJob(
            input_paths=tuple(self.input_paths),
            output_directory=self.output_var.get().strip(),
            dataset_name=self.dataset_var.get().strip() or "cad_to_gis",
            output_format=fmt,
            source_crs=self.source_crs_var.get().strip() or None,
            target_crs=target,
            selected_layers=self._selected_layers(),
            curve_tolerance=self._number(self.tolerance_var, "Curve tolerance"),
            coordinate_scale=self._number(self.scale_var, "Coordinate scale"),
            rotation_degrees=self._number(self.rotation_var, "Rotation"),
            offset_x=self._number(self.offset_x_var, "X offset"),
            offset_y=self._number(self.offset_y_var, "Y offset"),
            include_z=self.include_z_var.get(),
            closed_boundaries_as_polygons=self.closed_polygon_var.get(),
            block_mode=self.BLOCK_LABELS[self.block_var.get()],
            include_paperspace=self.paperspace_var.get(),
            audit_and_recover=self.audit_var.get(),
            repair_invalid_polygons=self.repair_var.get(),
            strict_transform=self.strict_var.get(),
            overwrite=self.overwrite_var.get(),
            dwg_converter=self.converter_var.get().strip() or None,
        )
        return validate_job(job, for_writing=write)

    @staticmethod
    def _preview_key(job: ConversionJob) -> tuple[Any, ...]:
        signatures: list[tuple[str, int, int]] = []
        for raw_path in job.input_paths:
            path = Path(raw_path)
            stat = path.stat()
            signatures.append(
                (
                    os.path.normcase(str(path.resolve())),
                    int(stat.st_mtime_ns),
                    int(stat.st_size),
                )
            )
        return (job, tuple(signatures))

    def _scan(self) -> None:
        try:
            job = self._build_job(write=False, scan_only=True)
        except Exception as exc:
            messagebox.showerror("Cannot analyse", str(exc), parent=self.root)
            return
        self._start_worker(
            "scan",
            lambda token, progress, log: scan_cad_files(
                job, cancel=token, progress=progress, log=log
            ),
        )

    def _preview(self) -> None:
        try:
            job = self._build_job(write=False)
            cache_key = self._preview_key(job)
        except Exception as exc:
            messagebox.showerror("Cannot preview", str(exc), parent=self.root)
            return

        self.notebook.select(self.preview_panel)
        if self._preview_cache_key == cache_key and self._preview_cache_result is not None:
            result = self._preview_cache_result
            self.preview_panel.set_features(result.preview_features)
            self._show_report(result.report)
            self.status_var.set(
                f"Preview ready (cached): {len(result.preview_features):,} features"
            )
            return

        self._active_preview_key = cache_key
        self._start_worker(
            "preview",
            lambda token, progress, log: convert_job(
                job, write=False, cancel=token, progress=progress, log=log
            ),
        )

    def _convert(self) -> None:
        try:
            job = self._build_job(write=True)
        except Exception as exc:
            messagebox.showerror("Cannot convert", str(exc), parent=self.root)
            return
        self._start_worker(
            "convert",
            lambda token, progress, log: convert_job(
                job, write=True, cancel=token, progress=progress, log=log
            ),
        )

    def _start_worker(self, kind: str, function: Callable[..., Any]) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.active_kind = kind
        self.cancel_token = CancellationToken()
        self.progress_var.set(0)
        self._set_busy(True)
        self.status_var.set(f"{kind.capitalize()} in progress…")
        self._log(f"Starting {kind}…")

        def progress(value: float, message: str) -> None:
            self.events.put(("progress", (max(0.0, min(1.0, value)), message)))

        def log(message: str) -> None:
            self.events.put(("log", str(message)))

        def target() -> None:
            try:
                result = function(self.cancel_token, progress, log)
                self.events.put(("done", (kind, result)))
            except CancelledError as exc:
                self.events.put(("cancelled", str(exc)))
            except Exception as exc:
                self.events.put(("error", (str(exc), traceback.format_exc())))

        self.worker = threading.Thread(target=target, name=f"cadgis-{kind}", daemon=True)
        self.worker.start()

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for widget in self._busy_widgets:
            try:
                if widget is self.format_combo and not busy:
                    widget.configure(state="readonly")
                else:
                    widget.configure(state=state)
            except Exception:
                pass
        self.cancel_button.configure(state="normal" if busy else "disabled")

    def _poll_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "log":
                    self._log(payload)
                elif event == "progress":
                    value, message = payload
                    self.progress_var.set(value * 100)
                    self.status_var.set(message)
                elif event == "done":
                    kind, result = payload
                    self._handle_done(kind, result)
                elif event == "cancelled":
                    self._log(payload)
                    self.status_var.set("Cancelled")
                    self._finish_worker()
                elif event == "error":
                    message, details = payload
                    self._log(details)
                    self.status_var.set("Failed — see Log")
                    failed_kind = self.active_kind or "conversion"
                    self._finish_worker()
                    if not self.closing:
                        self.notebook.select(self.log_panel)
                        title = {
                            "preview": "Preview failed",
                            "scan": "Layer analysis failed",
                            "convert": "Conversion failed",
                        }.get(failed_kind, "Operation failed")
                        messagebox.showerror(title, message, parent=self.root)
        except queue.Empty:
            pass
        if self.closing and (self.worker is None or not self.worker.is_alive()):
            self.root.destroy()
            return
        self.root.after(80, self._poll_events)

    def _handle_done(self, kind: str, result: Any) -> None:
        if kind == "scan":
            self.last_scan = result
            self.layer_state.clear()
            self.layer_iids.clear()
            self.layer_tree.delete(*self.layer_tree.get_children())
            for index, layer in enumerate(result["layers"]):
                name = layer["name"]
                iid = str(index)
                self.layer_state[name] = True
                self.layer_iids[iid] = name
                type_text = ", ".join(f"{key}:{value}" for key, value in layer["types"].items()) or "—"
                self.layer_tree.insert(
                    "",
                    "end",
                    iid=iid,
                    values=("☑", name, layer["count"], type_text, len(layer["files"])),
                )
            units = ", ".join(
                f"{item['source_file']}: {item['insunits_name']}" for item in result["files"]
            )
            self._log(f"Layer analysis complete. Drawing units — {units}")
            self.status_var.set(f"Found {len(result['layers'])} CAD layers")
        elif kind == "preview":
            assert isinstance(result, ConversionResult)
            if self._active_preview_key is not None:
                self._preview_cache_key = self._active_preview_key
                self._preview_cache_result = result
            self.preview_panel.set_features(result.preview_features)
            self._show_report(result.report)
            self.notebook.select(self.preview_panel)
            self.status_var.set(f"Preview ready: {len(result.preview_features):,} features")
        elif kind == "convert":
            assert isinstance(result, ConversionResult)
            self.last_report = result.report
            self._show_report(result.report)
            self._log("Outputs:\n  " + "\n  ".join(result.outputs))
            self._log(f"QA report: {result.report_path}")
            self.status_var.set("Conversion and QA verification complete")
            if not self.closing:
                messagebox.showinfo(
                    "Conversion complete",
                    f"Created {len(result.outputs)} output dataset(s).\n\nQA report:\n{result.report_path}",
                    parent=self.root,
                )
        self.progress_var.set(100)
        self._finish_worker()

    def _show_report(self, report: dict[str, Any]) -> None:
        self.last_report = report
        self.report_text.delete("1.0", "end")
        self.report_text.insert("1.0", json.dumps(report, ensure_ascii=False, indent=2))

    def _finish_worker(self) -> None:
        self._set_busy(False)
        self.active_kind = None
        self.cancel_token = None
        self._active_preview_key = None

    def _cancel(self) -> None:
        if self.cancel_token:
            self.cancel_token.cancel()
            self.cancel_button.configure(state="disabled")
            self.status_var.set("Cancelling safely…")

    def _use_detected_scale(self) -> None:
        if not self.last_scan:
            messagebox.showinfo("Drawing units", "Analyse the input files first.", parent=self.root)
            return
        factors = {
            item.get("metre_scale_if_local")
            for item in self.last_scan["files"]
            if item.get("metre_scale_if_local") is not None
        }
        if len(factors) != 1:
            messagebox.showwarning(
                "Drawing units",
                "The files have unitless, unknown or inconsistent INSUNITS values. Enter an explicit scale.",
                parent=self.root,
            )
            return
        factor = next(iter(factors))
        self.scale_var.set(f"{factor:.15g}")

    def _show_advanced(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Advanced conversion options")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        compact = self.compact_ui
        body = ttk.Frame(dialog, padding=9 if compact else 14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        row = 0

        def entry(label: str, variable: Any, note: str = "") -> None:
            nonlocal row
            pady = 2 if compact else 4
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=pady)
            ttk.Entry(body, textvariable=variable, width=26).grid(
                row=row, column=1, sticky="ew", padx=8, pady=pady
            )
            ttk.Label(body, text=note, foreground="#5E6B7A").grid(
                row=row, column=2, sticky="w", pady=pady
            )
            row += 1

        entry("Maximum curve chord error", self.tolerance_var, "drawing units; > 0")
        entry("Coordinate scale", self.scale_var, "applied before CRS")
        ttk.Button(body, text="Use detected INSUNITS → metres", command=self._use_detected_scale).grid(
            row=row, column=1, sticky="w", padx=8, pady=(0, 6)
        )
        row += 1
        entry("Rotation", self.rotation_var, "degrees about 0,0")
        entry("X offset", self.offset_x_var, "after scale/rotation")
        entry("Y offset", self.offset_y_var, "after scale/rotation")
        ttk.Label(body, text="Block handling").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Combobox(
            body, textvariable=self.block_var, values=list(self.BLOCK_LABELS), state="readonly", width=43
        ).grid(row=row, column=1, columnspan=2, sticky="ew", padx=8, pady=4)
        row += 1
        checks = (
            ("Preserve Z coordinates", self.include_z_var),
            ("Promote closed CAD boundaries to polygons", self.closed_polygon_var),
            ("Include paper-space layouts", self.paperspace_var),
            ("Audit and recover damaged DXF", self.audit_var),
            ("Repair invalid polygons (recorded in QA report)", self.repair_var),
            ("Require best available CRS transformation", self.strict_var),
            ("Overwrite exact output targets", self.overwrite_var),
        )
        for label, variable in checks:
            ttk.Checkbutton(body, text=label, variable=variable).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=1 if compact else 2
            )
            row += 1
        ttk.Separator(body).grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=6 if compact else 10
        )
        row += 1
        ttk.Label(body, text="DWG converter executable").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.converter_var).grid(row=row, column=1, sticky="ew", padx=8, pady=4)
        ttk.Button(
            body,
            text="Browse…",
            command=lambda: self._choose_converter(dialog),
        ).grid(row=row, column=2, sticky="ew", pady=4)
        row += 1
        ttk.Label(
            body,
            text="Leave blank for automatic detection. ODA File Converter is preferred. "
            "No converter is embedded or downloaded by this application.",
            foreground="#5E6B7A",
            wraplength=520 if compact else 590,
            justify="left",
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(2, 10))
        row += 1
        ttk.Button(body, text="Close", command=dialog.destroy).grid(row=row, column=2, sticky="e")
        _fit_toplevel_to_work_area(dialog, self.root)
        dialog.grab_set()

    def _choose_converter(self, parent: Any) -> None:
        path = filedialog.askopenfilename(
            parent=parent,
            title="Choose ODA File Converter or dwg2dxf",
            filetypes=[("Executables", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self.converter_var.set(path)

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                "Operation in progress",
                "Cancel the current operation and close after temporary files are cleaned up?",
                parent=self.root,
            ):
                return
            self.closing = True
            self._cancel()
            self.status_var.set("Cancelling before close…")
            return
        self.root.destroy()


def _enable_windows_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        with contextlib.suppress(Exception):
            ctypes.windll.user32.SetProcessDPIAware()


def launch_gui() -> int:
    if tk is None:
        print("Tkinter is not available in this Python installation.", file=sys.stderr)
        return 2
    _enable_windows_dpi_awareness()
    root = tk.Tk()
    app = CADToGISApp(root)
    work_x, work_y, work_w, work_h = app.work_area
    try:
        if os.name == "nt":
            # Windows maximization honours the monitor work area and therefore
            # excludes the taskbar while the dynamic minimum keeps all controls
            # visible on compact displays.
            root.state("zoomed")
        else:
            width = min(1400, max(640, work_w - 20))
            height = min(900, max(500, work_h - 20))
            root.geometry(f"{width}x{height}+{work_x + 10}+{work_y + 10}")
    except Exception:
        width = min(1280, max(640, work_w - 20))
        height = min(820, max(500, work_h - 20))
        root.geometry(f"{width}x{height}+{work_x + 10}+{work_y + 10}")
    root.mainloop()
    return 0


# ---------------------------------------------------------------------------
# Command-line interface and packaged-runtime self-test
# ---------------------------------------------------------------------------


def _create_self_test_dxf(path: Path) -> None:
    require_runtime()
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 6
    model = doc.modelspace()
    model.add_line((-90_000, -100_000, 2), (-89_990, -99_990, 3), dxfattribs={"layer": "3D_LINES"})
    model.add_arc((-89_995, -99_995), 4, 350, 10, dxfattribs={"layer": "CURVES"})
    model.add_circle((-89_994, -99_996), 2, dxfattribs={"layer": "CURVES"})
    model.add_lwpolyline(
        [(-90_000, -100_000, 0), (-89_990, -100_000, 0.6), (-89_990, -99_990, 0)],
        format="xyb",
        dxfattribs={"layer": "CURVES"},
    )
    hatch = model.add_hatch(dxfattribs={"layer": "AREAS"})
    hatch.paths.add_polyline_path(
        [(-90_000, -100_000), (-89_980, -100_000), (-89_980, -99_980), (-90_000, -99_980)],
        is_closed=True,
    )
    hatch.paths.add_polyline_path(
        [(-89_996, -99_996), (-89_984, -99_996), (-89_984, -99_984), (-89_996, -99_984)],
        is_closed=True,
    )
    model.add_text("Português — 測試", height=0.5, dxfattribs={"layer": "TEXT"}).set_placement(
        (-89_998, -99_998)
    )
    block = doc.blocks.new("VALVE")
    block.add_line((0, 0), (1, 0), dxfattribs={"layer": "0"})
    block.add_point((0.5, 0), dxfattribs={"layer": "POINTS"})
    block.add_attdef("ID", (0, 0), height=0.2)
    insert = model.add_blockref("VALVE", (-89_990, -99_990), dxfattribs={"layer": "EQUIPMENT"})
    insert.add_auto_attribs({"ID": "V-01"})
    doc.saveas(str(path))


def run_self_test() -> int:
    diagnostics = runtime_diagnostics()
    try:
        require_runtime()
        if not _driver_write_capable("GPKG"):
            raise ConversionError("Missing writable GDAL driver: GPKG")
        with tempfile.TemporaryDirectory(prefix="cadgis_selftest_") as temp:
            root = Path(temp)
            source = root / "self_test.dxf"
            output = root / "output"
            output.mkdir()
            _create_self_test_dxf(source)
            job = ConversionJob(
                input_paths=(str(source),),
                output_directory=str(output),
                dataset_name="self_test",
                output_format=DEFAULT_OUTPUT_FORMAT,
                curve_tolerance=0.01,
                block_mode="both",
                overwrite=True,
            )
            result = convert_job(job, write=True)
            if result.report.get("status") != "success":
                raise ConversionError("Self-test report did not finish successfully.")
            if result.report["statistics"].get("features_written", 0) < 8:
                raise ConversionError("Self-test wrote fewer features than expected.")
            output_paths = [Path(path) for path in result.outputs]
            gpkg_paths = [path for path in output_paths if path.suffix.lower() == ".gpkg"]
            if len(gpkg_paths) != 1:
                raise ConversionError("Self-test did not create exactly one GeoPackage.")
            output_path = gpkg_paths[0]
            if not output_path.is_file():
                raise ConversionError("Self-test GeoPackage is missing.")
            layer_table = pyogrio.list_layers(str(output_path))
            if not layer_table.size:
                raise ConversionError("Self-test GeoPackage contains no layers.")
            first_layer = str(layer_table[0][0])
            output_info = pyogrio.read_info(str(output_path), layer=first_layer)
            output_crs = parse_crs(output_info.get("crs"))
            if output_crs is None or output_crs.to_epsg() != 3763:
                raise ConversionError(
                    f"Self-test GeoPackage CRS is not {DEFAULT_CRS}: {output_info.get('crs')}"
                )
            diagnostics["self_test"] = "passed"
            diagnostics["self_test_features"] = result.report["statistics"]["features_written"]
            diagnostics["self_test_layers"] = result.report["statistics"]["output_layers"]
            diagnostics["self_test_crs"] = DEFAULT_CRS
            diagnostics["self_test_default_format"] = DEFAULT_OUTPUT_FORMAT
        print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        diagnostics["self_test"] = "failed"
        diagnostics["self_test_error"] = str(exc)
        print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="High-fidelity DXF/DWG to GIS converter. Run without arguments for the Tkinter GUI."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    parser.add_argument("--diagnostics", action="store_true", help="print runtime capabilities as JSON")
    parser.add_argument("--self-test", action="store_true", help="run a headless packaged-runtime round-trip test")
    subparsers = parser.add_subparsers(dest="command")

    scan_parser = subparsers.add_parser("scan", help="scan CAD files and print layer information")
    scan_parser.add_argument("inputs", nargs="+", help="DXF/DWG files")
    scan_parser.add_argument("--converter", help="ODA File Converter or dwg2dxf executable")

    convert_parser = subparsers.add_parser("convert", help="convert CAD files without the GUI")
    convert_parser.add_argument("inputs", nargs="+", help="DXF/DWG files")
    convert_parser.add_argument("-o", "--output", required=True, help="output directory")
    convert_parser.add_argument("--name", default="cad_to_gis", help="output dataset name")
    convert_parser.add_argument(
        "--format",
        choices=SUPPORTED_OUTPUT_FORMATS,
        default=DEFAULT_OUTPUT_FORMAT,
        help="output format (default: gpkg)",
    )
    convert_parser.add_argument(
        "--source-crs",
        default=DEFAULT_CRS,
        help=f"source CRS (default: {DEFAULT_CRS})",
    )
    convert_parser.add_argument(
        "--target-crs",
        default=DEFAULT_CRS,
        help=f"target CRS (default: {DEFAULT_CRS}; GeoJSON/KML require {WGS84_CRS})",
    )
    convert_parser.add_argument("--layer", action="append", dest="layers", help="CAD layer to include; repeatable")
    convert_parser.add_argument("--curve-tolerance", type=float, default=0.01)
    convert_parser.add_argument("--scale", type=float, default=1.0)
    convert_parser.add_argument("--rotation", type=float, default=0.0)
    convert_parser.add_argument("--offset-x", type=float, default=0.0)
    convert_parser.add_argument("--offset-y", type=float, default=0.0)
    convert_parser.add_argument("--2d", action="store_true", help="drop Z coordinates")
    convert_parser.set_defaults(closed_as_polygons=True)
    convert_parser.add_argument(
        "--closed-as-polygons",
        dest="closed_as_polygons",
        action="store_true",
        help="promote eligible closed CAD boundaries to polygons",
    )
    convert_parser.add_argument(
        "--keep-closed-as-lines",
        dest="closed_as_polygons",
        action="store_false",
        help="keep eligible closed CAD boundaries as line geometry",
    )
    convert_parser.add_argument("--block-mode", choices=("explode", "points", "both"), default="explode")
    convert_parser.add_argument("--paperspace", action="store_true")
    convert_parser.add_argument("--no-recover", action="store_true")
    convert_parser.add_argument("--repair-invalid", action="store_true")
    convert_parser.add_argument("--allow-ballpark", action="store_true")
    convert_parser.add_argument(
        "--overwrite",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="overwrite exact output targets (default: enabled)",
    )
    convert_parser.add_argument("--converter", help="ODA File Converter or dwg2dxf executable")
    return parser


def _job_from_cli(arguments: Any, *, output: bool) -> ConversionJob:
    return ConversionJob(
        input_paths=tuple(str(Path(path).resolve()) for path in arguments.inputs),
        output_directory=str(Path(arguments.output).resolve()) if output else "",
        dataset_name=getattr(arguments, "name", "cad_to_gis"),
        output_format=getattr(arguments, "format", DEFAULT_OUTPUT_FORMAT),
        source_crs=getattr(arguments, "source_crs", DEFAULT_CRS),
        target_crs=getattr(arguments, "target_crs", DEFAULT_CRS),
        selected_layers=(frozenset(arguments.layers) if getattr(arguments, "layers", None) else None),
        curve_tolerance=getattr(arguments, "curve_tolerance", 0.01),
        coordinate_scale=getattr(arguments, "scale", 1.0),
        rotation_degrees=getattr(arguments, "rotation", 0.0),
        offset_x=getattr(arguments, "offset_x", 0.0),
        offset_y=getattr(arguments, "offset_y", 0.0),
        include_z=not getattr(arguments, "2d", False),
        closed_boundaries_as_polygons=getattr(arguments, "closed_as_polygons", True),
        block_mode=getattr(arguments, "block_mode", "explode"),
        include_paperspace=getattr(arguments, "paperspace", False),
        audit_and_recover=not getattr(arguments, "no_recover", False),
        repair_invalid_polygons=getattr(arguments, "repair_invalid", False),
        strict_transform=not getattr(arguments, "allow_ballpark", False),
        overwrite=getattr(arguments, "overwrite", True),
        dwg_converter=getattr(arguments, "converter", None),
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments_list = list(argv if argv is not None else sys.argv[1:])
    if not arguments_list:
        return launch_gui()
    # A windowed PyInstaller executable has no console streams.  Keep command
    # modes deterministic (and testable) if somebody passes arguments to the
    # optional one-file GUI executable; the one-folder build also supplies a
    # dedicated console companion for interactive CLI use.
    if sys.stdout is None or sys.stderr is None:
        cli_log = os.environ.get("CADGIS_CLI_LOG")
        sink = open(cli_log, "a", encoding="utf-8", buffering=1) if cli_log else open(os.devnull, "w")
        if sys.stdout is None:
            sys.stdout = sink
        if sys.stderr is None:
            sys.stderr = sink
    parser = _build_parser()
    arguments = parser.parse_args(arguments_list)
    if arguments.diagnostics:
        print(json.dumps(runtime_diagnostics(), ensure_ascii=False, indent=2))
        return 0 if not _IMPORT_ERRORS else 1
    if arguments.self_test:
        return run_self_test()
    try:
        if arguments.command == "scan":
            job = _job_from_cli(arguments, output=False)
            result = scan_cad_files(
                job,
                progress=lambda value, message: print(f"[{value:6.1%}] {message}", file=sys.stderr),
                log=lambda message: print(message, file=sys.stderr),
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if arguments.command == "convert":
            job = _job_from_cli(arguments, output=True)
            result = convert_job(
                job,
                write=True,
                progress=lambda value, message: print(f"[{value:6.1%}] {message}", file=sys.stderr),
                log=lambda message: print(message, file=sys.stderr),
            )
            print(json.dumps({"outputs": result.outputs, "report": result.report_path}, ensure_ascii=False, indent=2))
            return 0
        parser.print_help()
        return 2
    except CancelledError as exc:
        print(str(exc), file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
