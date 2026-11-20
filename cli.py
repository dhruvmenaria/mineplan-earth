"""Optional terminal interface for repeatable local conversions."""
import argparse
import json
from pathlib import Path
import converter


def main():
    parser = argparse.ArgumentParser(description="MinePlan Earth: local DXF/DWG to KML/KMZ")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Generate a synthetic drawing, not a real survey")
    demo.add_argument("--directory", default="demo")
    scan = commands.add_parser("scan", help="List drawing layers")
    scan.add_argument("input")
    scan.add_argument("--dwg-converter", default="")
    plans = commands.add_parser("plans", help="Detect side-by-side plan sheets and survey controls")
    plans.add_argument("input")
    plans.add_argument("--plan-margin", type=float, default=25)
    export = commands.add_parser("convert", help="Export selected drawing layers")
    export.add_argument("input")
    export.add_argument("output")
    export.add_argument("--source-crs", required=True)
    export.add_argument("--confirm-crs", action="store_true", help="Confirm survey CRS and units")
    export.add_argument("--layer", action="append", help="Repeat for each selected layer")
    export.add_argument("--altitude", choices=["clampToGround", "absolute", "relativeToGround"], default="clampToGround")
    export.add_argument("--confirm-heights", action="store_true")
    export.add_argument("--fill", action="store_true")
    export.add_argument("--no-labels", action="store_true")
    export.add_argument("--scale", type=float, default=1)
    export.add_argument("--rotation", type=float, default=0)
    export.add_argument("--offset-x", type=float, default=0)
    export.add_argument("--offset-y", type=float, default=0)
    export.add_argument("--curve-tolerance", type=float, default=0.1)
    export.add_argument("--dwg-converter", default="")
    export.add_argument("--overwrite", action="store_true")
    export.add_argument("--plan", action="append", help="Detected plan id, e.g. plan-03; repeat as needed")
    export.add_argument("--plan-margin", type=float, default=25)
    args = parser.parse_args()
    try:
        if args.command == "demo":
            print(converter.create_demo(args.directory).resolve())
        elif args.command == "scan":
            result = converter.scan(converter.Options(args.input, dwg_converter=args.dwg_converter))
            print(json.dumps(result, indent=2, default=str))
        elif args.command == "plans":
            result = converter.analyze_plan_sheets(
                converter.Options(args.input, plan_margin=args.plan_margin))
            print(json.dumps(result, indent=2, default=str))
        else:
            if Path(args.output).exists() and not args.overwrite:
                parser.error("Output exists. Choose another path or pass --overwrite.")
            options = converter.Options(args.input, source_crs=args.source_crs,
                layers=None if args.layer is None else tuple(args.layer),
                altitude_mode=args.altitude, location_confirmed=args.confirm_crs,
                heights_confirmed=args.confirm_heights, filled_polygons=args.fill,
                include_labels=not args.no_labels, coordinate_scale=args.scale,
                rotation_degrees=args.rotation, offset_x=args.offset_x, offset_y=args.offset_y,
                curve_tolerance=args.curve_tolerance, dwg_converter=args.dwg_converter,
                plan_ids=None if args.plan is None else tuple(args.plan),
                plan_margin=args.plan_margin)
            result = converter.convert(options, args.output, log=print)
            print(json.dumps({"output": result["output"], "report": result["report_path"],
                              "summary": result["report"]["mineplan_earth"]}, indent=2))
    except Exception as exc:
        parser.exit(1, f"Conversion failed: {exc}\n")


if __name__ == "__main__":
    main()
