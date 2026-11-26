# MinePlan Earth

**Dhruv Menaria**

Version 0.19.0 improves offline classification of exact named tree blocks,
split/abbreviated dump and facility labels, paired section endpoints A–Z and
contour colour shades supported by the plate legend and nearby level labels.
Unlabelled contours require three independently supported lines of that style
in the same plate. Conflicting legend meanings remain unresolved. Generic dump
labels retain a generic meaning; material is not inferred from the word dump.
These rules change categories only, preserving geometry and coordinates.

Export reports show progress against an 80% minimum coverage target and a 90%
goal, including the number of additional objects requiring classification.
These are targets, not a guarantee for every drawing and not verified accuracy.
Unresolved source objects remain available for review; they are not hidden to
raise the score. No API key is required for these improvements.

Version 0.18.0 adds optional Claude-assisted classification with local evidence
preparation, PNG previews, validated proposals and explicit human approval.
The existing converter continues to work without an API key.

## Optional Claude classification

1. Analyse a drawing and choose **Review categories**.
2. Click **AI-assisted classification (Claude)**. This prepares up to 12 review
   cases locally, each with source handles, nearby text/styles, plate legend
   observations and a cropped PNG of the extracted linework.
3. **Save evidence (offline)** works without any API account. The JSON includes
   base64 PNGs, instructions, response schema and an evidence-package identity.
   To import a manually obtained response, retain its `evidence_package_id` and
   supply an `assignments` array following the included schema.
4. Later, configure `ANTHROPIC_API_KEY` in the computer's environment and restart
   the application. Set `MINEPLAN_CLAUDE_MODEL` to a supported Claude model ID,
   or enter that ID in the dialog. Do not put keys in source code, reports or Git.
5. **Ask Claude** sends the displayed batch after confirmation. Only cropped
   linework, labels, styles and legend metadata are submitted; the full DXF is
   not uploaded. This API use can incur charges. Usage tokens appear in the UI.
6. Select proposed mappings, inspect the image and cited evidence, then choose
   **Save selected reviewed mappings**. Re-run review or export to apply them.

The model proposes categories only; geometry, coordinates and CRS stay under
the converter's control. Proposals with unknown case IDs, unsupported categories,
invalid evidence references or conflicting duplicates are rejected. Rules are
bound to the source SHA-256; changed drawings require new evidence. Objects
without a unique plan/handle identity are left for the ordinary review workflow.
The tool handles a bounded batch, not every unresolved object in one request.

AI proposals are not independent verification or a measured accuracy score.
The Claude adapter has been tested with mocked transport; live API performance,
model quality and billing remain untested until an approved key/model is supplied.

Version 0.17.0 fixes premature shared-base consolidation: only known static
features identical in geometry and style across every selected plate can be
shared. Year-specific and unresolved objects remain in their source plates.
It recognises proposed mineral/reject-dump label variants, attaches nearby
same-colour features, pairs section/grid endpoint labels and identifies source
numeric level annotations. Detected drawing frames have a separate folder,
hidden initially. These changes preserve source geometry and do not alter the
coordinate transformation.

Version 0.16.0 improved layer-0 classification with retained hatch patterns and
angles, plate-local anonymous-block legend matching, confirmed block ownership,
matched lease-boundary identities and coordinate annotations. Horizontal Roman
year-colour keys can now group otherwise unidentified linework by source year.
These year groups explicitly retain a feature-role review flag; they do not
claim that every coloured line is a working pit or dump. Human review rules
can resolve that remaining role. Pit containment now requires the full object
to lie inside the pit, avoiding representative-point false matches.

Version 0.15.0 added plate-specific legend wording, index-column and row checks,
pairing confidence and legend coverage reports. Unknown legend terms can be
retained inside evidenced index regions. Ambiguous pairs are reported and do
not teach automatic style rules. Unresolved geometry is preserved under its
original source layer in an `UNRESOLVED - ...` folder. Both full and government
exports carry the source legend and per-feature naming evidence.

Version 0.14.0 added sequential grouped large-DXF export, stricter mining text
classification and GPS label parsing, panel-fit diagnostics, and sampled buffer
distance statistics. The preceding release added mining-specific geometry QA, a clean government-package
preset and cross-tool shape verification. MinePlan Earth now reports lease area,
pit/dump containment, boundary-pillar offsets and approximate 7.5 m, 60 m and
500 m boundary distances. Government KMZ members omit decorative contours,
grids, section lines and unclassified hatches while the normal full export stays
unchanged. Verification can compare equivalent geometry by Hausdorff distance
even when another tool writes a different number of vertices.

Local Windows desktop converter for DXF/DWG drawing outlines to Google Earth KML/KMZ.
Built on the MIT-licensed CAD extraction engine from [CAD to GIS Studio](https://github.com/ricbencar/cad-to-gis-convert).
See `THIRD_PARTY_NOTICES.md` and the preserved upstream license in `vendor/`.

## Start

Double-click **Start MinePlan Earth.cmd**. Python 3.11 or newer is required (this installation uses Python 3.13).
The local `.venv` has been installed on this computer. For a fresh copy, the launcher installs the dependencies on its first run.
Run **Setup or Repair.cmd** if dependencies need reinstalling. Python's Tkinter component must be available.
Conversion and drawing preview run locally without a map or AI API key. Optional
Claude-assisted classification sends selected evidence only when explicitly requested.
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
5. Enter the **verified source CRS**, supplied by the survey team, or open **Coordinate Control Wizard** and fit matched CAD/survey controls.
6. In the wizard, keep one point as an independent verification point whenever four or more controls are available. Review every residual before applying.
7. Check drawing units. Advanced scale applies to X, Y, and Z; rotation is around the origin, followed by XY offsets and CRS transformation.
8. Select ground mode or an appropriate elevation mode. Detected Plan mode is intentionally limited to a 2D ground overlay.
9. Open **Review categories**. Inspect any uncertain objects and save only the decisions you can confirm from the source drawing or survey documentation.
10. Export and review the adjacent JSON report, including selected plans, coordinate controls, unsupported objects, and warnings.
11. Open the result in Google Earth and compare independent known survey points before using it in a deliverable.

An EPSG code identifies what the numbers in your drawing already mean. Choosing the location you want is not enough.

When a plan contains latitude/longitude boundary labels, MinePlan Earth calculates the lease centre and displays its confidence and coordinate count. Use **Google Maps** or **Earth Web** to inspect that location, or copy the coordinates. These buttons use ordinary web links: no Google Maps API key, account, or billing is required.

## Coordinate Control Wizard

Use authoritative controls from the survey report or the coordinate labels in
the CAD drawing. The accepted CSV columns are:

```text
name,source_x,source_y,latitude,longitude
A,1234.50,5678.25,26.74894444,75.22721944
B,1320.10,5681.80,N26°44'57.10",E75°13'39.20"
```

The wizard converts WGS 84 latitude/longitude into a verified projected CRS,
then calculates a 2D similarity/Helmert transformation containing uniform
scale, rotation and translation. It reports calculated and expected coordinates,
east/north residuals, RMS error, maximum error, coordinate-label precision,
control-polygon area difference and possible outliers. With four or more points,
the last point can be withheld from fitting as an independent check.

The built-in decision limits are PASS at no more than 0.5 m RMS and 1.0 m
maximum, REVIEW at no more than 2.0 m RMS and 5.0 m maximum, and FAIL above
those values. A FAIL cannot be applied. REVIEW requires explicit confirmation.
These software limits do not replace the accuracy standard required by the
surveyor or government authority. Plan Mode avoids double transformation by
applying only the calculated correction after its existing panel alignment.
The full control table and transformation are embedded in every export report.

Google Maps and satellite imagery are visual references only. They are not
survey controls and no Google Maps API key is used to calculate coordinates.

## Mining geometry QA

When a drawing legend identifies a closed lease boundary, the export report
contains a `mining_geometry_audit` section. It records calculated lease area in
hectares and conservatively checks classified pits, dumps, topsoil, backfill and
reclaimed areas against the lease. Boundary pillars farther than 2 m from the
lease and statutory/environment boundaries whose sampled median distance does
not resemble 7.5 m, 60 m or 500 m are flagged for review. These are software QA
checks, not survey or regulatory certification.

The government submission folder enables a clean-review preset. It removes
classified contour, grid and section-line furniture and unclassified decorative
hatches from those KMZ members only. The source drawing and ordinary full KMZ
export are not altered. Confirm the required contents with the responsible
qualified person because scrutiny requirements can vary by authority and case.

## Cross-tool verification

The verifier first checks corresponding WGS 84 vertices. If component structure
or vertex density differs, it projects both features into the local UTM zone and
uses Hausdorff distance; polygon comparisons also report overlap percentage.
This permits comparison with geometrically equivalent KML from other tools while
still flagging actual boundary displacement.

## Large DXF Mode

**Grouped large export** processes the core together and then converts known
contour/topography layers in bounded temporary DXF parts. All parts reuse the
core's plan alignment, CRS and legend rules. The output includes individual
KMZ parts, a self-contained `COMPLETE PLAN.kmz`, and a manifest with the source
SHA-256 and reconciled DXF record counts. The complete file contains all parts
and may still use substantial memory in Google Earth; open individual parts
for lighter internal review. Choose the government purpose in the grouped-export
dialog to skip contour/topography bulk before conversion and produce the existing
self-contained plate package from the shared core. The manifest records those
bulk records as deliberately excluded.

Partitioning preserves POLYLINE/VERTEX/SEQEND and INSERT/ATTRIB groups. Mixed
layers (including layer 0) remain in the core. A core larger than 64 MB, shared
BLOCKS/header data above 32 MB, or an individual entity exceeding the 32 MB part
budget causes an explicit stop rather than unsafe splitting. This first version
does not partition arbitrary mixed layer-0 geometry or prune shared BLOCKS.
Repeated plan layouts require selecting the core's plan sheets first. Verify
the source CRS using survey controls before exporting.

DXF files larger than 250 MB are indexed with a low-memory streaming reader instead of being fully opened. MinePlan Earth displays object and legacy-vertex totals by layer, lets you select the useful planning layers, and writes a lightweight working DXF without modifying the original. The working file is then analysed and exported normally. Avoid selecting layers with millions of vertices unless they are essential.

## Government Submission Mode

When repeated plan sheets are detected, **Government submission folder** creates a uniquely named directory such as `03-2020 PLAN - FINAL KMZ`, containing separate, officially named KMZ files for Surface, Surface Geological, combined yearly working, Mine Layout, Progressive Closure, Financial Assurance, Conceptual and Environment plans. A matching `03-2020 PLAN - FINAL KMZ QA` directory contains per-file reports, a submission manifest and a mandatory manual-review checklist. The package is never represented as government-approved automatically; survey and regulatory review remain required.
Both full KMZ exports and Government Submission exports read the legends embedded
in the current DXF and use only that file's recognised legend vocabulary for
semantic Google Earth folders such as `LEASE BOUNDARY`, `CONTOUR`, `EXIST. PIT`,
and `PROP. HAULAGE ROAD`. Learned legend rules apply only to that source drawing
and are never reused for another file. Legend matching is scoped to each plan
plate, so a reused AutoCAD colour can mean different things on different
plates. Numeric contour labels and geometry contained by an explicit pit
boundary are attached only through constrained spatial rules. Everything else
that cannot be matched remains under `UNCLASSIFIED - REVIEW`.
The **Review categories** window lets a human resolve those uncertain objects.
An individual rule targets one DXF entity handle; a matching-style rule is
restricted to the same detected plan, original CAD layer, geometry family,
colour and linetype. Rules are stored locally under `review_rules/`, never
uploaded, and are bound to a SHA-256 digest of the source drawing. If the DXF
changes, its old decisions are ignored and the drawing must be reviewed again.
Before manual review, MinePlan also uses named AutoCAD block names/attributes,
closed areas containing explicit labels, multi-part symbols connected to one
agreeing label, conservative same-colour line labels, repeated aligned geometry
across plan plates, and strongly dominant contour legend evidence. A nearby
label is never sufficient when multiple nearby categories disagree. The review
window explains whether an item remains uncertain because it is on generic
layer `0`, its legend style has several meanings, its block name is unknown, or
its text is only an annotation/value.
Environment plates tolerate millimetre-level copied-boundary drift and the
common `ENVIROMENT PLAN` spelling, and export the full 60 m/500 m study area
without the surrounding AutoCAD paper frame.
For local/non-georeferenced sheet coordinates, obtain a verified engineering transformation before exporting. Plan mode activates only when it can match repeated lease geometry to either explicit DXF survey POINT controls or a complete set of embedded latitude/longitude boundary labels.

## Features

- Native DXF extraction; optional DWG bridge through an installed ODA File Converter or LibreDWG `dwg2dxf`.
- Layer selection, local zoom/pan preview, cancellable background processing.
- One KML/KMZ with CAD layers as folders; entity colors, text labels at insertion points, optional transparent polygon fills.
- Repeated-plan detection, per-plan selection, map-area clipping, translation onto DXF survey controls, and one Google Earth folder per selected plan.
- Lease-shaped cropping removes detected legends, title blocks, scale bars, certification tables, and other sheet furniture outside the mapped lease area.
- Exact repeated geometry is consolidated into one `Shared base map` folder. Combined exports open with one plan visible so overlapping year plans do not obscure the map.
- Lines, points, closed boundaries, curves approximated with a configurable tolerance, block expansion, hatch boundaries, and other geometry supported by the upstream engine.
- Ground, absolute, and relative altitude modes, with validation of finite coordinates and longitude/latitude ranges.
- Exported geometry validity checks, XML roundtrip validation, KMZ archive verification, per-export JSON report, and atomic replacement of each file.
- Repeated side-by-side paper plans are blocked from ordinary export and must use reviewed Plan mode, preventing plausible but geographically scattered output.
- Polygon placemarks include geodesic area (square metres and hectares) and perimeter metadata; reports aggregate the same measurements by folder.
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
