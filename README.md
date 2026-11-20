# MinePlan Earth

**Dhruv Menaria**

Version 0.9.2 includes a refreshed desktop interface with a clearer five-step
workflow, stronger export actions, improved layer selection, and persistent
status/progress feedback. It also recognises common two-line AutoCAD DMS
latitude/longitude labels even when N/E prefixes are omitted or the degree
symbol is encoding-corrupted. The exact `OUARRY` CAD-layer typo is treated as
`QUARRY`, safely grouping those outlines under `EXIST. PIT`.

Local Windows desktop converter for DXF/DWG drawing outlines to Google Earth KML/KMZ.
Built on the MIT-licensed CAD extraction engine from [CAD to GIS Studio](https://github.com/ricbencar/cad-to-gis-convert).
See `THIRD_PARTY_NOTICES.md` and the preserved upstream license in `vendor/`.

## Start

Double-click **Start MinePlan Earth.cmd**. Python 3.11 or newer is required (this installation uses Python 3.13).
The local `.venv` has been installed on this computer. For a fresh copy, the launcher installs the dependencies on its first run.
Run **Setup or Repair.cmd** if dependencies need reinstalling. Python's Tkinter component must be available.
Conversion and the drawing preview run locally. No AI API, map API key, or file upload is used.
The initial dependency installation requires internet access. Some specialized coordinate transformations may require separately supplied PROJ grid files.

## Try the demo

1. Click **Try demo drawing**. This creates a clearly labeled synthetic drawing near Udaipur, not an actual mining lease.
2. It uses **EPSG:32643 (WGS 84 / UTM zone 43N)**, in metres.
3. Click **Preview selected layers** to view the source drawing.
4. For this synthetic file, tick **I verified the source CRS and drawing units**.
5. Keep **Follow ground (2D overlay)** and click **Export KML / KMZ**.
6. Save the file, then click **Open last export**. If Windows has no KML/KMZ association, open Google Earth Pro and use File > Open.
7. In Google Earth's Places panel, toggle the separate layer folders. The first lease vertex is 73.7 longitude, 24.58 latitude.

## Your own drawing

1. Browse to a DXF and analyse its layers.
2. Include/exclude layers with a click. Remove title blocks, legends, and other sheet furniture as appropriate.
3. If the drawing contains repeated side-by-side plans, the app enables **Clip and align detected plan maps**. Open **Choose detected plans / map margin** to select individual years or plan types.
4. Preview the selected content. Normal preview uses original drawing coordinates; Plan mode previews the clipped maps after alignment.
5. Enter the **verified source CRS**, supplied by the survey team. Presets are conveniences, not suggestions for your drawing.
6. Check drawing units. Advanced scale applies to X, Y, and Z; rotation is around the origin, followed by XY offsets and CRS transformation.
7. Select ground mode or an appropriate elevation mode. Detected Plan mode is intentionally limited to a 2D ground overlay.
8. Export and review the adjacent JSON report, including selected plans, control-fit residual, unsupported objects, and warnings.
9. Open the result in Google Earth and compare independent known survey points before using it in a deliverable.

An EPSG code identifies what the numbers in your drawing already mean. Choosing the location you want is not enough.

When a plan contains latitude/longitude boundary labels, MinePlan Earth calculates the lease centre and displays its confidence and coordinate count. Use **Google Maps** or **Earth Web** to inspect that location, or copy the coordinates. These buttons use ordinary web links: no Google Maps API key, account, or billing is required.

## Large DXF Mode

DXF files larger than 250 MB are indexed with a low-memory streaming reader instead of being fully opened. MinePlan Earth displays object and legacy-vertex totals by layer, lets you select the useful planning layers, and writes a lightweight working DXF without modifying the original. The working file is then analysed and exported normally. Avoid selecting layers with millions of vertices unless they are essential.

## Government Submission Mode

When repeated plan sheets are detected, **Government submission folder** creates a uniquely named directory such as `03-2020 PLAN - FINAL KMZ`, containing separate, officially named KMZ files for Surface, Surface Geological, combined yearly working, Mine Layout, Progressive Closure, Financial Assurance, Conceptual and Environment plans. A matching `03-2020 PLAN - FINAL KMZ QA` directory contains per-file reports, a submission manifest and a mandatory manual-review checklist. The package is never represented as government-approved automatically; survey and regulatory review remain required.
Both full KMZ exports and Government Submission exports read the legends embedded
in the current DXF and use only that file's recognised legend vocabulary for
semantic Google Earth folders such as `LEASE BOUNDARY`, `CONTOUR`, `EXIST. PIT`,
and `PROP. HAULAGE ROAD`. Learned legend rules apply only to that source drawing
and are never reused for another file. Geometry that cannot be matched by an
explicit CAD layer or an unambiguous legend style remains under
`UNCLASSIFIED - REVIEW`; proximity to nearby text is never used to guess.
For local/non-georeferenced sheet coordinates, obtain a verified engineering transformation before exporting. Plan mode activates only when it can match repeated lease geometry to either explicit DXF survey POINT controls or a complete set of embedded latitude/longitude boundary labels.

## Features

- Native DXF extraction; optional DWG bridge through an installed ODA File Converter or LibreDWG `dwg2dxf`.
- Layer selection, local zoom/pan preview, cancellable background processing.
- One KML/KMZ with CAD layers as folders; entity colors, text labels at insertion points, optional transparent polygon fills.
- Repeated-plan detection, per-plan selection, map-area clipping, translation onto DXF survey controls, and one Google Earth folder per selected plan.
- Lines, points, closed boundaries, curves approximated with a configurable tolerance, block expansion, hatch boundaries, and other geometry supported by the upstream engine.
- Ground, absolute, and relative altitude modes, with validation of finite coordinates and longitude/latitude ranges.
- Exported geometry validity checks, XML roundtrip validation, KMZ archive verification, per-export JSON report, and atomic replacement of each file.
- Invalid closed polygons are repaired with a topology-preserving geometry operation and recorded in the JSON QA report.
- Failed non-HATCH extraction/transformation errors block export. Malformed HATCH fills are omitted and reported as warnings because the line overlay remains usable.
- A damaged/recovered DXF may produce warnings: review them and compare with the original drawing.

## DWG

Use the team's existing DWG-to-DXF workflow, or install ODA File Converter / LibreDWG separately.
The app can detect supported installations, or you can choose the executable in Advanced settings.
External converter software and licenses are not included. DWG conversion has not been verified on a company drawing.

## Important limits

- Plan mode has been exercised on the supplied `03-2020 PLAN .dxf`, where it detected 11 panels. This is software validation, not field-survey or government-submission certification.
- Plan alignment uses either a survey-control translation or a least-squares similarity fit to embedded GPS boundary labels. It refuses to activate if adequate evidence is absent; independently distorted panels still need a surveyor-approved transformation.
- Map margin is a geometric crop around the lease boundary. Review every plan for annotations or features that should be included/excluded and adjust the margin as needed.
- The app cannot infer mineral deposits, mining depth, government approval, or survey accuracy from drawing outlines.
- A 2D CRS transformation does not convert vertical datums. Absolute-height interpretation in Google Earth must match the verified source height reference.
- CAD line dashes, hatch patterns, fonts, text rotation/scale, external references, and print layouts are not reproduced exactly. Blocks/entities not supported by the reader appear in the report.
- Geographic range checks will catch many bad coordinate settings, but a wrong CRS can still yield plausible coordinates. Use survey control points.
- Preview sampling affects only the preview. Export processes the full selected geometry.
- Coordinates in the report are boundary extents, not an accuracy certification.
- KML and its JSON report are each written atomically, but the pair is not a single filesystem transaction.

## Optional command line

```powershell
.\.venv\Scripts\python.exe cli.py demo
.\.venv\Scripts\python.exe cli.py scan demo\DEMO_ONLY_Udaipur_UTM43N.dxf
.\.venv\Scripts\python.exe cli.py plans "C:\path\to\plan.dxf"
.\.venv\Scripts\python.exe cli.py convert demo\DEMO_ONLY_Udaipur_UTM43N.dxf output\demo.kmz --source-crs EPSG:32643 --confirm-crs
```

Add `--layer "Year 1"` to limit export, `--fill` for transparent polygons, or `--no-labels` to omit text. Use repeated `--plan plan-03` arguments after reviewing `cli.py plans` output to export detected plans from the terminal.

## Development checks

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe app.py --smoke-ui
```

The tests exercise real synthetic DXF extraction, known coordinate conversion, layer selection, elevation handling,
KML/KMZ structure, cancellation, label filtering, and invalid-setting/geometry rejection. These do not establish field accuracy.
