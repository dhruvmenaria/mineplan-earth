# CAD to GIS Studio

**CAD to GIS Studio** is a Windows-oriented Python application for converting engineering CAD drawings in **DXF** or **DWG** format into GIS vector datasets. It combines a Tkinter graphical interface with a high-fidelity conversion engine based on `ezdxf`, `Shapely`, `GeoPandas`, `pyproj` and `pyogrio`.

The program is intended for engineering, surveying and GIS workflows where CAD drawings need to be transferred into QGIS or other GIS software without losing the practical information required to understand where each feature came from. In addition to geometry, the conversion retains useful CAD metadata such as source file, CAD layer, entity type, entity handle, block information, colours, linetypes and other attributes whenever they are available.

The default output is a **GeoPackage (`.gpkg`)** for the coordinate reference system **ETRS89 / Portugal TM06 (`EPSG:3763`)**. These defaults are appropriate for many Portuguese engineering projects, but the user must always confirm that the CAD coordinates actually use the selected source CRS.

The application runs locally and does not require an internet connection for conversion or preview.

---

## Main features

CAD to GIS Studio reads **DXF directly**. **DWG** files are supported through an external converter installed on the computer, preferably **ODA File Converter**. LibreDWG `dwg2dxf` can also be used as a fallback.

The converter supports common CAD geometry such as lines, polylines, arcs, circles, ellipses, splines, hatches, polygons, points, text, faces, meshes and block references. Curved CAD entities are converted to GIS-compatible coordinate geometry using a configurable curve tolerance.

Closed CAD boundaries are promoted to polygons by default. This is useful for drawings containing buildings, parcels, paved areas, equipment footprints or other closed contours that should become GIS polygon features instead of simple lines.

Block references can be recursively expanded while preserving their lineage and attributes. The application can alternatively export block insertion points only, or both insertion points and exploded block geometry.

The conversion engine also supports source and target CRS definitions, coordinate scaling, rotation and X/Y offsets. These options are useful when a CAD drawing is in a local engineering coordinate system or when units need to be converted before the GIS CRS is assigned.

Z coordinates are preserved by default where the source geometry and output format permit them.

For production work, the application does more than simply write files. It reopens written datasets and checks feature counts, CRS information, geometry validity and other basic properties. GeoPackage outputs also receive an SQLite integrity check. A JSON QA report is written with every successful production conversion.

---

## Output formats

The application can create the following GIS outputs:

- **GeoPackage (`.gpkg`)**;
- **ESRI Shapefile (`.shp`)**;
- **GeoJSON (`.geojson`)**;
- **FlatGeobuf (`.fgb`)**;
- **KML (`.kml`)**;
- **CSV with WKT geometry (`.csv`)**;
- a combined **GeoPackage + Shapefile + KML** delivery.

GeoPackage is normally the best option because a single file can contain multiple GIS layers, long field names and UTF-8 attributes without the main limitations of the Shapefile format.

Shapefile remains available for compatibility with older GIS workflows, but its DBF attribute table imposes short field names and limited text lengths. Where possible, use GeoPackage as the master dataset.

GeoJSON and KML are written in **WGS 84 (`EPSG:4326`)**, as expected for these interchange formats.

---

## Coordinate reference systems

The default source and target CRS is:

```text
EPSG:3763 — ETRS89 / Portugal TM06
```

The source CRS describes the coordinates already contained in the CAD drawing after any explicit scale, rotation or offset operations. The target CRS is the CRS to be written to the GIS dataset.

If the source and target CRS are the same, the geometry is not unnecessarily reprojected. If they are different, the program uses `pyproj` / PROJ to transform the coordinates.

A CRS should never be selected only to remove an "unknown CRS" warning. Assigning the wrong CRS makes the coordinates geographically wrong even though the GIS file may appear formally referenced.

For local engineering drawings, the advanced options allow the following transformation sequence:

```text
CAD geometry
    -> coordinate scale
    -> rotation
    -> X/Y offset
    -> CRS transformation
    -> GIS output
```

The program can also inspect the DXF `$INSUNITS` value and suggest a conversion factor to metres when the drawing units are known.

---

## Graphical interface

Run the script without command-line arguments to open the Tkinter GUI:

```bat
.venv\Scripts\python.exe script.py
```

The GUI is designed for routine interactive conversion and automatically adapts to smaller Windows displays, including approximately **1366 x 768** screens.

The main window is divided into a control area on the left and Preview, Log and QA Report tabs on the right.

### 1. Add CAD files

Use **Add DXF / DWG…** to select one or more input files.

When the first file is added, the program proposes its filename as the dataset name and normally uses the same folder as the initial output directory.

DXF files are read directly. For DWG files, the program looks for an installed ODA File Converter or LibreDWG converter.

### 2. Analyse layers

The **Analyse layers** button scans the drawing and lists the CAD layers, their entity counts and the types of entities found on each layer.

This step is especially useful when only part of a large drawing needs to be exported. Layers can be enabled or disabled before previewing or converting.

The layer scan also reports drawing-unit information obtained from `$INSUNITS`.

### 3. Choose the output

Select the required GIS format, output folder and dataset name.

For normal QGIS use, leave the output format as:

```text
GeoPackage only (.gpkg)
```

No `_GIS` suffix is automatically added to the output filenames.

### 4. Confirm the CRS

Check the **Source CRS** and **Target CRS** fields.

The default is `EPSG:3763` for both. Use the **Validate** button to confirm how `pyproj` interprets the CRS definitions.

If the drawing is in a local coordinate system, use the Advanced options to apply the required scale, rotation and offsets before assigning or transforming the CRS.

### 5. Preview

Use **Preview** to inspect the drawing before conversion.

The preview is intentionally lighter than the final export. Large drawings are sampled and complex geometry may be simplified so the interface remains responsive. This does **not** change the production geometry written to the final GIS dataset.

The preview supports:

- mouse-wheel zoom;
- mouse drag to pan;
- **Fit** to return to the full drawing extent.

The preview is a local Tkinter Canvas and does not load web maps, tiles or external services.

### 6. Convert

Press **Convert** to run the complete production conversion.

The application extracts CAD entities, applies the selected engineering and CRS transformations, groups the features into GIS layers, writes the selected format and then reopens the result for QA checks.

A successful conversion produces the GIS output together with:

```text
<dataset>_report.json
```

The QA report can also be reviewed directly in the GUI.

---

## Advanced options

The **Advanced…** dialog contains the settings normally left at their defaults but required for more specialised drawings.

### Curve tolerance

The maximum curve chord error controls how accurately CAD curves are represented by GIS vertices.

The default is:

```text
0.01 drawing units
```

A smaller value produces more vertices and closer curve approximation, but increases processing time and file size.

### Coordinate scale

The default scale is `1.0`.

Typical examples are:

```text
millimetres -> metres : 0.001
centimetres -> metres : 0.01
feet -> metres        : 0.3048
metres -> metres      : 1.0
```

The scale is applied before CRS transformation.

### Rotation and offsets

A drawing can be rotated about `(0, 0)` and translated using X and Y offsets. These options are useful for local engineering grids where a known transformation is required before GIS referencing.

### Block handling

Three modes are available:

- **Explode recursively; keep block lineage** — default;
- **Insertion points only**;
- **Both insertion points and exploded geometry**.

The default is appropriate when the actual geometry inside CAD blocks is required in GIS.

### Preserve Z coordinates

Enabled by default. Disable it when a strictly 2D GIS dataset is required.

### Promote closed boundaries to polygons

Enabled by default. Disable it when closed CAD contours must remain line features.

### Include paper-space layouts

Disabled by default. Normal conversion uses model space only.

### Audit and recover damaged DXF

Enabled by default. The application can use `ezdxf` recovery tools when a DXF cannot be read normally.

### Repair invalid polygons

Disabled by default. Invalid polygon geometry is reported rather than silently modified. Enable repair only when automatic topology correction is appropriate for the project.

### Best available CRS transformation

Enabled by default. This requests a proper PROJ transformation rather than silently accepting a low-quality ballpark transformation.

---

## QA and output safety

The converter is designed so that a successful file write is not automatically treated as a successful conversion.

Vector outputs are reopened and checked for expected feature count, CRS, usable bounds, geometry type, missing or empty geometry, Z preservation and expected attribute fields.

GeoPackage output receives an additional SQLite:

```sql
PRAGMA integrity_check;
```

The production writer first creates files in a temporary staging location. Only after the requested outputs have been written and verified are they committed to the selected output folder.

This reduces the risk of leaving a partially replaced production dataset when a conversion fails.

Temporary files are cleaned automatically. On Windows, the code includes retry logic for delayed file handles that can be held temporarily by GDAL, SQLite, antivirus software or other processes.

---

## Installing Python on Windows

A **64-bit Python 3.12** installation from python.org is recommended.

Tkinter is included with the normal Windows Python installer and does not normally need to be installed with `pip`.

Open **Command Prompt** in the folder containing `script.py`.

Create a virtual environment:

```bat
py -m venv .venv
```

Activate it:

```bat
.venv\Scripts\activate
```

Upgrade `pip`:

```bat
python -m pip install --upgrade pip
```

On Windows Command Prompt, do **not** use:

```text
source .venv/Scripts/activate
```

That syntax belongs to Unix-style shells. In Windows Command Prompt use:

```bat
.venv\Scripts\activate
```

Activation is optional. You can always call the environment explicitly with:

```bat
.venv\Scripts\python.exe
```

---

## Python dependencies

Install the runtime packages with:

```bat
.venv\Scripts\python.exe -m pip install ^
    "ezdxf>=1.4.4,<1.5" ^
    "Shapely>=2.1.2,<2.2" ^
    "pyproj>=3.7.2,<3.8" ^
    "pandas>=2.2,<3.1" ^
    "GeoPandas>=1.1,<1.2" ^
    "pyogrio>=0.11.1,<0.14"
```

The packages have the following main roles:

- `ezdxf` reads DXF entities, layers, blocks, paths and CAD metadata;
- `Shapely` handles GIS geometry and topology;
- `pyproj` handles CRS definitions and coordinate transformations;
- `pandas` and `GeoPandas` organise attributes and GIS feature tables;
- `pyogrio` provides fast GDAL-based GIS reading and writing.

After installation, the application can be started with:

```bat
.venv\Scripts\python.exe script.py
```

---

## DWG support

DXF support is native. DWG is converted to temporary DXF before the normal extraction process.

For DWG input, install one of the following:

1. **ODA File Converter** — preferred;
2. **LibreDWG `dwg2dxf`** — fallback.

The program attempts to detect the converter automatically. A specific executable can also be selected in **Advanced…**.

The DWG converter is not bundled with the Python program and is not downloaded automatically.

If DWG support is not required, no external CAD converter is needed.

---

## Diagnostics and self-test

Before packaging the application, or after installing it on a new computer, run:

```bat
.venv\Scripts\python.exe script.py --diagnostics
```

This prints information about the Python environment, imported package versions, GDAL driver availability and detected DWG converter.

A complete internal conversion test is also available:

```bat
.venv\Scripts\python.exe script.py --self-test
```

The self-test creates a temporary DXF, converts it to GeoPackage, reopens it and verifies the main runtime components. It does not modify the user's project files.

---

## Checking the Python script

Before building an executable, check the script syntax:

```bat
.venv\Scripts\python.exe -m py_compile script.py
```

`py_compile` checks Python syntax and creates bytecode in `__pycache__`.

It does **not** create a standalone executable.

A distributable Windows `.exe` is created with **PyInstaller**.

---

## Building a standalone Windows executable

Install the build tools inside the same virtual environment:

```bat
.venv\Scripts\python.exe -m pip install ^
    "PyInstaller>=6.15,<7" ^
    "pyinstaller-hooks-contrib>=2025.8"
```

### Recommended build with `script.spec`

If the project includes a tested `script.spec` file, use it:

```bat
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean script.spec
```

The executable will normally be created in:

```text
dist\
```

For example:

```text
dist\script.exe
```

Using a project-specific `.spec` file is preferred because the GIS Python stack contains compiled libraries and data files associated with GDAL, PROJ, Shapely and pyogrio.

### One-file fallback

If no `script.spec` is available, a standalone one-file GUI executable can be attempted with:

```bat
.venv\Scripts\python.exe -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --name script ^
    --collect-all pyogrio ^
    --collect-all pyproj ^
    --collect-all shapely ^
    --collect-all geopandas ^
    --collect-all ezdxf ^
    script.py
```

The `--collect-all` options help PyInstaller include the non-Python resources required by the geospatial libraries.

A one-file executable is convenient for distribution, but it can start more slowly because its bundled files are extracted to a temporary location when launched.

For debugging a packaging problem, a console or one-folder build is generally easier to diagnose.

---

## Recommended Windows build sequence

A clean setup can be performed as follows:

```bat
py -m venv .venv

.venv\Scripts\activate

python -m pip install --upgrade pip

python -m pip install ^
    "ezdxf>=1.4.4,<1.5" ^
    "Shapely>=2.1.2,<2.2" ^
    "pyproj>=3.7.2,<3.8" ^
    "pandas>=2.2,<3.1" ^
    "GeoPandas>=1.1,<1.2" ^
    "pyogrio>=0.11.1,<0.14"

python -m py_compile script.py

python script.py --diagnostics

python script.py --self-test

python script.py
```

For the executable:

```bat
python -m pip install ^
    "PyInstaller>=6.15,<7" ^
    "pyinstaller-hooks-contrib>=2025.8"

python -m PyInstaller --noconfirm --clean script.spec
```

If no spec file is available, use the one-file fallback command shown above.

---

## Command-line use

Although the GUI is the main interface, the same script can also run without the GUI.

Show the available options:

```bat
.venv\Scripts\python.exe script.py --help
```

Scan CAD layers:

```bat
.venv\Scripts\python.exe script.py scan plan.dxf
```

Run a normal GeoPackage conversion:

```bat
.venv\Scripts\python.exe script.py convert plan.dxf ^
    -o C:\GIS\output ^
    --name plan
```

The command-line interface is useful for automated conversions, testing and diagnostics, while the Tkinter GUI is normally more convenient for interactive engineering work.

---

## Using the output in QGIS

For normal QGIS work, use **GeoPackage**.

A typical workflow is:

```text
1. Add the DXF or DWG.
2. Analyse CAD layers if required.
3. Confirm drawing units.
4. Confirm the source CRS.
5. Check any scale, rotation or offsets.
6. Preview the drawing.
7. Convert to GeoPackage.
8. Review the QA report.
9. Open the .gpkg in QGIS.
10. Compare representative geometry with the CAD source or trusted control data.
```

A GeoPackage can contain several output layers because the program separates source CAD layers and geometry families where required.

For example, one CAD layer can produce separate point, line and polygon GIS layers if all three geometry families occur in the source.

---

## Important limitations

CAD and GIS use different data models, so conversion is not equivalent to reproducing AutoCAD inside GIS.

Some points should always be kept in mind:

- general CAD curves are converted to segmented GIS geometry;
- text is represented by its anchor point and attributes, not by glyph outlines;
- exploded blocks are GIS features, not native CAD blocks;
- meshes are exported as polygon faces, not volumetric solids;
- infinite CAD construction geometry such as `RAY` and `XLINE` has no direct finite GIS equivalent;
- Shapefile has unavoidable field-name and text-length restrictions;
- GeoJSON and KML use WGS 84;
- a horizontal CRS transformation does not automatically perform a vertical datum transformation;
- preview geometry may be simplified for speed, while production export retains the full conversion geometry;
- DWG conversion depends on an external converter.

The most important responsibility remains the interpretation of the source coordinates. A correct EPSG code must come from project information, survey control or other authoritative georeferencing information.

---

## Recommended production workflow

For engineering deliverables, the following sequence is recommended:

```text
CAD source
    -> verify drawing units
    -> verify engineering transformation if required
    -> verify source CRS
    -> select CAD layers
    -> preview
    -> convert to GeoPackage
    -> review QA report
    -> open in QGIS
    -> compare with trusted control data
```

The JSON QA report should be retained with the converted GIS dataset whenever traceability is important.

---

## Default settings

The principal defaults in the current script are:

| Setting | Default |
| --- | --- |
| Application | CAD to GIS Studio |
| Version | 1.0.0 |
| Output format | GeoPackage |
| Source CRS | EPSG:3763 |
| Target CRS | EPSG:3763 |
| Curve tolerance | 0.01 |
| Coordinate scale | 1.0 |
| Rotation | 0 degrees |
| X / Y offset | 0 |
| Preserve Z | enabled |
| Closed boundaries as polygons | enabled |
| Block handling | recursive explode |
| Paper space | disabled |
| DXF audit/recovery | enabled |
| Invalid-polygon repair | disabled |
| Strict CRS transformation | enabled |
| Overwrite managed outputs | enabled |

---

## Summary

CAD to GIS Studio provides a practical engineering workflow for converting DXF and DWG drawings into modern GIS datasets through a Windows-friendly Tkinter interface.

Its main emphasis is on preserving useful CAD provenance, handling coordinates explicitly, producing a responsive preview, writing GeoPackage-first GIS output and checking the result before it is committed as a completed conversion.

For most users, the normal workflow is simply:

```text
Add CAD -> confirm CRS -> Preview -> Convert -> review QA -> open GeoPackage in QGIS
```

For deployment, use a Python virtual environment, verify the installation with `--diagnostics` and `--self-test`, check the source with `py_compile`, and use PyInstaller to create the Windows executable.
