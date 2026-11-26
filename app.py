"""MinePlan Earth desktop application. Start with python app.py."""
from __future__ import annotations

import argparse
import json
import os
import queue
import threading
import traceback
import tkinter as tk
import webbrowser
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

import converter
import coordinate_controls
import large_dxf
import review_rules
import semantic_layers
from verify_dialog import VerifyPanel
from vendor import cad_to_gis as cad

HERE = Path(__file__).resolve().parent
COLORS = {
    "canvas": "#e9f0f3",
    "surface": "#ffffff",
    "surface_alt": "#f7fafb",
    "header": "#0b3438",
    "header_deep": "#072a2e",
    "accent": "#0b8f82",
    "accent_dark": "#08746b",
    "accent_soft": "#dff4f0",
    "text": "#18333b",
    "muted": "#667b83",
    "border": "#d4e0e5",
    "warning": "#b45309",
    "danger": "#b42318",
}
ALTITUDES = {
    "Follow ground (2D overlay)": "clampToGround",
    "Survey elevations (absolute)": "absolute",
    "Height above ground (relative)": "relativeToGround",
}
PRESETS = {
    "Choose a verified source CRS...": "",
    "WGS 84 / UTM 42N - EPSG:32642": "EPSG:32642",
    "WGS 84 / UTM 43N - EPSG:32643": "EPSG:32643",
    "WGS 84 / UTM 44N - EPSG:32644": "EPSG:32644",
    "WGS 84 / UTM 45N - EPSG:32645": "EPSG:32645",
    "WGS 84 / UTM 46N - EPSG:32646": "EPSG:32646",
    "WGS 84 / UTM 47N - EPSG:32647": "EPSG:32647",
    "WGS 84 longitude / latitude - EPSG:4326": "EPSG:4326",
    "Custom EPSG code or WKT": "",
}


class DrawingPreview(cad.CanvasPreview):
    COLORS = ("#5ee6ac", "#79b9ff", "#ffb86b", "#d8a5ff", "#ff788b", "#6ee7f2")

    def _layer_color(self, layer):
        if layer not in self._color_cache:
            self._color_cache[layer] = self.COLORS[len(self._color_cache) % len(self.COLORS)]
        return self._color_cache[layer]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"MinePlan Earth {converter.APP_VERSION} | {converter.APP_AUTHOR}")
        self.geometry("1320x900")
        self.minsize(1040, 700)
        self.configure(bg=COLORS["canvas"])
        self.events = queue.Queue()
        self.busy = False
        self.token = None
        self.scanned_path = None
        self.last_output = None
        self.selected = set()
        self.layer_info = []
        self.plan_info = {"available": False, "plans": []}
        self.large_index = None
        self.large_source = None
        self.selected_plans = set()
        self.file = tk.StringVar()
        self.crs = tk.StringVar()
        self.preset = tk.StringVar(value=next(iter(PRESETS)))
        self.altitude = tk.StringVar(value=next(iter(ALTITUDES)))
        self.location_ok = tk.BooleanVar(value=False)
        self.height_ok = tk.BooleanVar(value=False)
        self.labels = tk.BooleanVar(value=True)
        self.fill = tk.BooleanVar(value=False)
        self.dwg = tk.StringVar()
        self.tolerance = tk.StringVar(value="0.1")
        self.scale = tk.StringVar(value="1")
        self.rotation = tk.StringVar(value="0")
        self.offset_x = tk.StringVar(value="0")
        self.offset_y = tk.StringVar(value="0")
        self.layer_filter = tk.StringVar()
        self.crs_info = tk.StringVar(value="No source CRS selected. Drawing preview works without one.")
        self.layer_summary = tk.StringVar(value="Load a drawing to see its layers.")
        self.plan_mode = tk.BooleanVar(value=False)
        self.plan_margin = tk.StringVar(value="25")
        self.plan_summary = tk.StringVar(value="No repeated plan sheets detected yet.")
        self.detected_location = None
        self.coordinate_control_report = None
        self.location_summary = tk.StringVar(value="No embedded latitude/longitude detected yet.")
        self.status = tk.StringVar(value="Ready. Load your DXF or try the synthetic demo.")
        self._styles()
        self._build()
        for variable in [self.crs, self.scale, self.rotation, self.offset_x, self.offset_y]:
            variable.trace_add("write", self._coordinate_settings_changed)
        self.altitude.trace_add("write", lambda *_: self.height_ok.set(False))
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), foreground=COLORS["text"])
        style.configure("TFrame", background=COLORS["surface"])
        style.configure("Panel.TFrame", background=COLORS["canvas"])
        style.configure("Action.TFrame", background=COLORS["surface_alt"])
        style.configure("Status.TFrame", background=COLORS["header_deep"])
        style.configure("TLabel", background=COLORS["surface"], foreground=COLORS["text"])
        style.configure("Muted.TLabel", foreground=COLORS["muted"], font=("Segoe UI", 9))
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 11), foreground=COLORS["accent_dark"])
        style.configure("Status.TLabel", background=COLORS["header_deep"], foreground="#cfe5e5",
                        font=("Segoe UI", 9))
        style.configure("TButton", padding=(12, 8), background="#edf3f5",
                        foreground=COLORS["text"], borderwidth=0, focusthickness=1,
                        focuscolor=COLORS["accent"])
        style.map("TButton", background=[("active", "#dce8eb"), ("pressed", "#cfdee2"),
                                          ("disabled", "#edf1f2")],
                  foreground=[("disabled", "#9aa9ae")])
        style.configure("Accent.TButton", padding=(16, 9), background=COLORS["accent"],
                        foreground="white", font=("Segoe UI Semibold", 10), borderwidth=0)
        style.map("Accent.TButton", background=[("active", COLORS["accent_dark"]),
                                                 ("pressed", "#065d57"),
                                                 ("disabled", "#a8bfbd")])
        style.configure("Soft.TButton", background=COLORS["accent_soft"],
                        foreground=COLORS["accent_dark"], font=("Segoe UI Semibold", 10))
        style.map("Soft.TButton", background=[("active", "#ccebe5"), ("pressed", "#b9e2db")])
        style.configure("Danger.TButton", foreground=COLORS["danger"], background="#fff1f0")
        style.map("Danger.TButton", background=[("active", "#ffe2df")])
        style.configure("TEntry", padding=(9, 7), fieldbackground="#fbfdfe",
                        bordercolor=COLORS["border"], lightcolor=COLORS["border"],
                        darkcolor=COLORS["border"])
        style.map("TEntry", bordercolor=[("focus", COLORS["accent"])],
                  lightcolor=[("focus", COLORS["accent"])],
                  darkcolor=[("focus", COLORS["accent"])])
        style.configure("TCombobox", padding=(8, 6), fieldbackground="#fbfdfe",
                        bordercolor=COLORS["border"], arrowcolor=COLORS["accent_dark"])
        style.configure("TCheckbutton", background=COLORS["surface"], padding=(0, 3))
        style.map("TCheckbutton", background=[("active", COLORS["surface"])],
                  indicatorcolor=[("selected", COLORS["accent"]), ("!selected", "#ffffff")])
        style.configure("Treeview", rowheight=30, background=COLORS["surface"],
                        fieldbackground=COLORS["surface"], borderwidth=0,
                        foreground=COLORS["text"])
        style.map("Treeview", background=[("selected", COLORS["accent_soft"])],
                  foreground=[("selected", COLORS["accent_dark"])])
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9), padding=(7, 8),
                        background="#eaf1f3", foreground="#3d5961", relief="flat")
        style.map("Treeview.Heading", background=[("active", "#dce8eb")])
        style.configure("TNotebook", background=COLORS["canvas"], borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", padding=(18, 10), background="#dfe8eb",
                        foreground=COLORS["muted"], borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", COLORS["surface"]), ("active", "#edf4f5")],
                  foreground=[("selected", COLORS["accent_dark"])])
        style.configure("Horizontal.TProgressbar", troughcolor="#244c50",
                        background="#4ed6c5", borderwidth=0, lightcolor="#4ed6c5",
                        darkcolor="#4ed6c5")
        style.configure("TPanedwindow", background=COLORS["canvas"], sashwidth=8)

    def _build(self):
        header = tk.Frame(self, bg=COLORS["header"], padx=24, pady=15)
        header.pack(fill="x")
        brand = tk.Frame(header, bg=COLORS["header"])
        brand.pack(side="left")
        tk.Label(brand, text="MINE PLANNING WORKSPACE", bg=COLORS["header"], fg="#69d7ca",
                 font=("Segoe UI Semibold", 8)).pack(anchor="w")
        title_row = tk.Frame(brand, bg=COLORS["header"])
        title_row.pack(anchor="w")
        tk.Label(title_row, text="MinePlan Earth", bg=COLORS["header"], fg="white",
                 font=("Segoe UI Semibold", 23)).pack(side="left")
        tk.Label(title_row, text=f"  v{converter.APP_VERSION}", bg=COLORS["header"], fg="#9fd8d2",
                 font=("Segoe UI Semibold", 9)).pack(side="left", pady=(7, 0))
        tk.Label(header, text="Turn CAD mine plans into structured, review-ready Google Earth files.",
                 bg=COLORS["header"], fg="#b9d9d3", font=("Segoe UI", 10)).pack(side="left", padx=30)
        self.demo_button = ttk.Button(header, text="Try demo drawing", command=self._demo)
        self.demo_button.pack(side="right")
        tk.Label(header, text=converter.APP_AUTHOR,
                 bg=COLORS["header"], fg="#d8ebe7",
                 font=("Segoe UI Semibold", 9)).pack(side="right", padx=(10, 18))
        tk.Label(header, text="LOCAL • PRIVATE • NO UPLOAD", bg="#17494d", fg="#bceae4",
                 padx=10, pady=5, font=("Segoe UI Semibold", 8)).pack(side="right", padx=(0, 8))

        panels = ttk.Panedwindow(self, orient="horizontal")
        panels.pack(fill="both", expand=True, padx=16, pady=(14, 10))
        left_shell = ttk.Frame(panels, width=450, style="Panel.TFrame")
        panels.add(left_shell, weight=0)
        canvas = tk.Canvas(left_shell, bg=COLORS["surface"], highlightthickness=1,
                           highlightbackground=COLORS["border"], width=442)
        scroll = ttk.Scrollbar(left_shell, orient="vertical", command=canvas.yview)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        left = ttk.Frame(canvas, padding=(18, 16, 18, 20))
        window = canvas.create_window((0, 0), window=left, anchor="nw")
        left.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        def scroll_settings(event):
            widget = event.widget
            while widget is not None:
                if widget is left_shell:
                    if event.widget is self.tree:
                        return None
                    canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                    return "break"
                widget = getattr(widget, "master", None)
        self.bind_all("<MouseWheel>", scroll_settings, add="+")

        self._section(left, "1  Drawing")
        row = ttk.Frame(left)
        row.pack(fill="x", pady=(7, 3))
        self.file_entry = ttk.Entry(row, textvariable=self.file)
        self.file_entry.pack(side="left", fill="x", expand=True)
        self.browse_button = ttk.Button(row, text="Browse", command=self._browse)
        self.browse_button.pack(side="right", padx=(6, 0))
        ttk.Label(left, text="DXF reads locally. DWG requires an installed converter.",
                  style="Muted.TLabel", wraplength=365).pack(anchor="w")
        self.scan_button = ttk.Button(left, text="Analyse drawing / reload layers", command=self._scan)
        self.scan_button.pack(fill="x", pady=(8, 12))

        self._section(left, "2  Layers to export")
        search = ttk.Entry(left, textvariable=self.layer_filter)
        search.pack(fill="x", pady=(7, 4))
        self.layer_filter.trace_add("write", lambda *_: self._render_layers())
        ttk.Label(left, text="Filter layer names. Click a row to include or exclude it.",
                  style="Muted.TLabel").pack(anchor="w")
        frame = ttk.Frame(left)
        frame.pack(fill="x", pady=5)
        self.tree = ttk.Treeview(frame, columns=("use", "layer", "count"), show="headings", height=6,
                                 selectmode="browse")
        for key, name, width in [("use", "Use", 42), ("layer", "CAD layer", 242), ("count", "Objects", 62)]:
            self.tree.heading(key, text=name)
            self.tree.column(key, width=width, stretch=key=="layer")
        self.tree.pack(side="left", fill="x", expand=True)
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        bar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.tag_configure("included", background="#effaf7", foreground="#0b6f66")
        self.tree.tag_configure("excluded", background="#ffffff", foreground="#63777e")
        self.tree.bind("<ButtonRelease-1>", self._toggle_layer)
        row = ttk.Frame(left)
        row.pack(fill="x")
        ttk.Button(row, text="All", command=lambda: self._select_layers(True)).pack(side="left")
        ttk.Button(row, text="None", command=lambda: self._select_layers(False)).pack(side="left", padx=5)
        ttk.Label(left, textvariable=self.layer_summary, style="Muted.TLabel").pack(anchor="w", pady=(5, 12))

        self._section(left, "3  Plans arranged side by side")
        ttk.Checkbutton(left, text="Clip and align detected plan maps",
                        variable=self.plan_mode).pack(anchor="w", pady=(7, 3))
        self.plan_button = ttk.Button(left, text="Choose detected plans / map margin",
                                      command=self._configure_plans, state="disabled")
        self.plan_button.pack(fill="x", pady=(2, 4))
        ttk.Label(left, textvariable=self.plan_summary, style="Muted.TLabel",
                  wraplength=365).pack(anchor="w", pady=(1, 12))

        self._section(left, "4  Where is this drawing?")
        combo = ttk.Combobox(left, textvariable=self.preset, values=list(PRESETS), state="readonly")
        combo.pack(fill="x", pady=(7, 5))
        combo.bind("<<ComboboxSelected>>", self._preset)
        combo.bind("<MouseWheel>", scroll_settings)
        row = ttk.Frame(left)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.crs).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Check CRS", command=self._check_crs).pack(side="right", padx=(5, 0))
        ttk.Label(left, textvariable=self.crs_info, wraplength=365,
                  style="Muted.TLabel").pack(anchor="w", pady=5)
        ttk.Checkbutton(left, text="I verified the source CRS and drawing units",
                        variable=self.location_ok).pack(anchor="w", pady=(1, 12))

        self.control_button = ttk.Button(
            left, text="Coordinate Control Wizard",
            command=self._coordinate_control_wizard, state="disabled", style="Soft.TButton")
        self.control_button.pack(fill="x", pady=(0, 7))
        ttk.Label(
            left,
            text="Fit AutoCAD X/Y to matched survey latitude/longitude controls and inspect per-point error.",
            wraplength=365, style="Muted.TLabel").pack(anchor="w", pady=(0, 10))

        ttk.Label(left, text="Detected map location", style="Title.TLabel").pack(anchor="w", pady=(2, 2))
        ttk.Label(left, textvariable=self.location_summary, wraplength=365,
                  style="Muted.TLabel").pack(anchor="w", pady=(1, 5))
        location_actions = ttk.Frame(left)
        location_actions.pack(fill="x", pady=(0, 12))
        self.maps_button = ttk.Button(location_actions, text="Google Maps",
                                      command=self._open_google_maps, state="disabled")
        self.maps_button.pack(side="left")
        self.earth_button = ttk.Button(location_actions, text="Earth Web",
                                       command=self._open_google_earth, state="disabled")
        self.earth_button.pack(side="left", padx=5)
        self.copy_location_button = ttk.Button(location_actions, text="Copy coordinates",
                                               command=self._copy_coordinates, state="disabled")
        self.copy_location_button.pack(side="left")

        self._section(left, "5  Google Earth appearance")
        altitude_combo = ttk.Combobox(left, textvariable=self.altitude, values=list(ALTITUDES), state="readonly")
        altitude_combo.pack(fill="x", pady=(7, 5))
        altitude_combo.bind("<MouseWheel>", scroll_settings)
        ttk.Label(left, text="Ground mode is for 2D overlays. Elevation modes require heights in metres and a known reference.",
                  wraplength=365, style="Muted.TLabel").pack(anchor="w")
        ttk.Checkbutton(left, text="I verified the height units and reference",
                        variable=self.height_ok).pack(anchor="w", pady=3)
        ttk.Checkbutton(left, text="Include text labels", variable=self.labels).pack(anchor="w")
        ttk.Checkbutton(left, text="Fill closed boundaries with transparent color", variable=self.fill).pack(anchor="w")

        ttk.Button(left, text="Advanced / DWG converter settings", command=self._advanced).pack(fill="x", pady=(14, 7))
        ttk.Label(left, text="Plan mode clips map content around the repeated lease boundary and aligns copies to DXF survey-control POINTs. Always verify in Google Earth.",
                  wraplength=365, style="Muted.TLabel").pack(anchor="w", pady=(2, 5))

        self.tabs = ttk.Notebook(panels)
        panels.add(self.tabs, weight=1)
        preview_tab = ttk.Frame(self.tabs)
        self.tabs.add(preview_tab, text="Drawing preview")
        self.preview_canvas = DrawingPreview(preview_tab)
        self.preview_canvas.pack(fill="both", expand=True)
        ttk.Label(preview_tab, text="Preview colors distinguish layers. Export uses CAD colors. Check map placement in Google Earth.",
                  style="Muted.TLabel", padding=(12, 7)).pack(fill="x")
        report_tab = ttk.Frame(self.tabs)
        self.tabs.add(report_tab, text="Export report")
        self.report_text = self._text_box(report_tab)
        self._set_text(self.report_text, "Your export summary will appear here.\n\nEvery KML/KMZ export includes a JSON report next to the output file.\n\nObjects that cannot be represented are reported. Verify important boundary points against trusted survey coordinates.")
        self.verify_panel = VerifyPanel(self.tabs, self)
        self.tabs.add(self.verify_panel, text="Verify export")
        log_tab = ttk.Frame(self.tabs)
        self.tabs.add(log_tab, text="Activity")
        self.log_text = self._text_box(log_tab)
        bottom = ttk.Frame(self, padding=(18, 11), style="Action.TFrame")
        bottom.pack(fill="x", padx=16, pady=(0, 0))
        self.preview_button = ttk.Button(bottom, text="Preview selected layers", command=self._preview)
        self.preview_button.pack(side="left")
        self.review_button = ttk.Button(
            bottom, text="Review categories", command=self._review_unclassified,
            state="disabled", style="Soft.TButton")
        self.review_button.pack(side="left", padx=(8, 0))
        self.export_button = ttk.Button(bottom, text="Export KML / KMZ", style="Accent.TButton", command=self._export)
        self.export_button.pack(side="left", padx=8)
        self.submission_button = ttk.Button(
            bottom, text="Government submission folder",
            command=self._export_submission, state="disabled", style="Soft.TButton")
        self.submission_button.pack(side="left")
        ttk.Button(bottom, text="Grouped large export", command=self._export_grouped,
                   style="Soft.TButton").pack(side="left", padx=5)
        self.open_button = ttk.Button(bottom, text="Open last export", command=self._open_export, state="disabled")
        self.open_button.pack(side="left", padx=8)
        self.cancel_button = ttk.Button(bottom, text="Cancel", command=self._cancel,
                                        state="disabled", style="Danger.TButton")
        self.cancel_button.pack(side="right")
        footer = ttk.Frame(self, padding=(18, 7), style="Status.TFrame")
        footer.pack(fill="x")
        self.progressbar = ttk.Progressbar(footer, mode="determinate", maximum=1, length=150)
        self.progressbar.pack(side="right")
        ttk.Label(footer, text="●", style="Status.TLabel").pack(side="left", padx=(0, 7))
        ttk.Label(footer, textvariable=self.status, style="Status.TLabel").pack(side="left", fill="x", expand=True)

    def _section(self, parent, text):
        number, _, title = text.partition("  ")
        row = tk.Frame(parent, bg=COLORS["surface"])
        row.pack(fill="x", pady=(4, 0))
        tk.Label(row, text=number, bg=COLORS["accent_soft"], fg=COLORS["accent_dark"],
                 width=2, height=1, font=("Segoe UI Semibold", 9)).pack(side="left")
        tk.Label(row, text=title or number, bg=COLORS["surface"], fg=COLORS["text"],
                 font=("Segoe UI Semibold", 11)).pack(side="left", padx=(8, 0))
        tk.Frame(row, bg=COLORS["border"], height=1).pack(side="left", fill="x",
                                                          expand=True, padx=(10, 0))

    def _text_box(self, parent):
        text = tk.Text(parent, wrap="word", bg="#f8fbfc", fg=COLORS["text"], relief="flat",
                       insertbackground=COLORS["accent"], selectbackground=COLORS["accent_soft"],
                       padx=20, pady=20, font=("Cascadia Mono", 9))
        bar = ttk.Scrollbar(parent, orient="vertical", command=text.yview)
        bar.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        text.configure(yscrollcommand=bar.set, state="disabled")
        return text

    def _set_text(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    def _log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", str(text) + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _collect_options(self, selected=True):
        if selected and self.scanned_path != str(Path(self.file.get()).expanduser().resolve()):
            raise ValueError("Analyse this drawing first so you can confirm which layers to export.")
        return converter.Options(
            input_path=self.file.get(), source_crs=self.crs.get(),
            layers=tuple(sorted(self.selected)) if selected else None,
            altitude_mode=ALTITUDES[self.altitude.get()], filled_polygons=self.fill.get(),
            include_labels=self.labels.get(), curve_tolerance=float(self.tolerance.get()),
            coordinate_scale=float(self.scale.get()), rotation_degrees=float(self.rotation.get()),
            offset_x=float(self.offset_x.get()), offset_y=float(self.offset_y.get()),
            dwg_converter=self.dwg.get(), location_confirmed=self.location_ok.get(),
            heights_confirmed=self.height_ok.get(),
            plan_ids=tuple(sorted(self.selected_plans)) if self.plan_mode.get() else None,
            plan_margin=float(self.plan_margin.get()),
            coordinate_control_json=(
                json.dumps(self.coordinate_control_report, separators=(",", ":"), default=str)
                if self.coordinate_control_report else ""))

    def _browse(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(title="Choose a CAD drawing",
                    filetypes=[("CAD drawings", "*.dxf *.dwg"), ("DXF", "*.dxf"), ("DWG", "*.dwg")])
        if path:
            self._load_path(path)

    def _load_path(self, path):
        self.file.set(str(path))
        self.verify_panel.set_candidate("")
        self.large_index = None
        self.large_source = None
        self.scanned_path = None
        self.selected.clear()
        self.layer_info = []
        self.plan_info = {"available": False, "plans": []}
        self.selected_plans.clear()
        self.plan_mode.set(False)
        self.plan_button.configure(state="disabled")
        self.submission_button.configure(state="disabled")
        self.review_button.configure(state="disabled")
        self.control_button.configure(state="disabled")
        self.coordinate_control_report = None
        self.plan_summary.set("Analysing repeated plan geometry...")
        self._set_detected_location(None)
        self.location_ok.set(False)
        self.height_ok.set(False)
        self._render_layers()
        self.preview_canvas.set_features([])
        self._scan()

    def _scan(self):
        if self.busy:
            return
        try:
            options = self._collect_options(selected=False)
            converter.job_for(options)
        except Exception as exc:
            messagebox.showerror("Drawing", str(exc))
            return
        path = str(Path(options.input_path).expanduser().resolve())
        if self.large_index and self.large_source == path:
            self._configure_large_dxf()
            return
        if large_dxf.is_large(path):
            self._scan_large_dxf(path)
            return
        def done(result):
            scan_result = result["scan"]
            self.scanned_path = path
            self.layer_info = scan_result["layers"]
            # Block children can use layers with no top-level entity count.
            self.selected = {item["name"] for item in self.layer_info}
            self.plan_info = result["plans"]
            self._set_detected_location(self.plan_info.get("location"))
            if self.plan_info.get("available"):
                self.selected_plans = {p["id"] for p in self.plan_info["plans"]}
                self.plan_mode.set(True)
                self.plan_button.configure(state="normal")
                self.submission_button.configure(state="normal")
                suggested = self.plan_info.get("suggested_crs")
                if suggested and not self.crs.get().strip():
                    self.crs.set(suggested)
                    preset_name = next((name for name, value in PRESETS.items()
                                        if value == suggested), None)
                    if preset_name:
                        self.preset.set(preset_name)
                    self._check_crs()
                error = self.plan_info.get("control_match_error", 0)
                self.plan_summary.set(
                    f"{len(self.selected_plans)} plans selected; control fit residual {error:.3f} drawing units.")
            else:
                self.selected_plans.clear()
                self.plan_mode.set(False)
                self.plan_button.configure(state="disabled")
                self.submission_button.configure(state="disabled")
                self.plan_summary.set(self.plan_info.get("reason") or "No repeated plan sheets detected.")
            self._render_layers()
            self.review_button.configure(state="normal")
            self.control_button.configure(state="normal")
            self._log(json.dumps(scan_result["files"], indent=2, default=str))
            self._log(json.dumps(self.plan_info, indent=2, default=str))
            self.status.set("Drawing analysed. Select layers, preview, and confirm the source CRS.")
        def operation(token, log, progress):
            scanned = converter.scan(options, cancel=token, log=log,
                                     progress=lambda p, s: progress(p * .65, s))
            progress(.7, "Detecting repeated plan sheets and survey controls")
            plans = converter.analyze_plan_sheets(options)
            progress(1, "Drawing analysis complete")
            return {"scan": scanned, "plans": plans}
        self._run(operation, done)

    def _scan_large_dxf(self, path):
        """Index a huge ASCII DXF without constructing its geometry."""
        def done(result):
            self.large_index = result
            self.large_source = path
            self.scanned_path = None
            self.layer_info = result["layers"]
            self.selected = {
                item["name"] for item in self.layer_info
                if item.get("vertices", 0) <= 250000 and item.get("count", 0) <= 250000
            }
            self.plan_info = {"available": False, "plans": []}
            self.selected_plans.clear()
            self.plan_mode.set(False)
            self.plan_button.configure(state="disabled")
            self.plan_summary.set("Create a working subset before detecting repeated plan sheets.")
            self._render_layers()
            size_gb = result["size_bytes"] / (1024 ** 3)
            self.layer_summary.set(
                f"Large DXF: {len(self.layer_info)} layers, {result['records']:,} records, {size_gb:.2f} GB")
            self._log(json.dumps(result, indent=2, default=str))
            self.status.set("Large DXF indexed. Choose layers and create a working subset.")
            self._configure_large_dxf()

        self._run(
            lambda token, log, progress: large_dxf.index_layers(
                path, cancel=token, progress=progress),
            done)

    def _configure_large_dxf(self):
        if self.busy or not self.large_index:
            return
        dialog = tk.Toplevel(self)
        dialog.title("Large DXF Mode - choose working layers")
        dialog.geometry("1020x650")
        dialog.transient(self)
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill="both", expand=True)
        size_gb = self.large_index["size_bytes"] / (1024 ** 3)
        ttk.Label(body, text="Create a lightweight working DXF", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text=(
            f"The original is {size_gb:.2f} GB with {self.large_index['records']:,} records. "
            "Select only the lease boundary, yearly plans, coordinate points and necessary labels. "
            "The original file will not be changed."), wraplength=960,
            style="Muted.TLabel").pack(anchor="w", pady=(4, 10))

        controls = ttk.Frame(body)
        controls.pack(side="bottom", fill="x", pady=(10, 0))
        selection_summary = tk.StringVar()
        ttk.Label(controls, textvariable=selection_summary,
                  style="Muted.TLabel").pack(side="bottom", anchor="w", pady=(7, 0))

        table = ttk.Frame(body)
        table.pack(fill="both", expand=True)
        columns = ("use", "layer", "objects", "vertices", "types")
        tree = ttk.Treeview(table, columns=columns, show="headings", height=18,
                            selectmode="browse")
        settings = [
            ("use", "Use", 48), ("layer", "CAD layer", 300),
            ("objects", "Objects", 100), ("vertices", "Legacy vertices", 120),
            ("types", "Main entity types", 350),
        ]
        for key, title, width in settings:
            tree.heading(key, text=title)
            tree.column(key, width=width, stretch=key in {"layer", "types"})
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        layers = self.large_index["layers"]
        # Recalculate this every time the dialog opens so a previous "All"
        # choice cannot silently carry millions of vertices into a new subset.
        selected = {item["name"] for item in layers
                    if item.get("vertices", 0) <= 250000
                    and item.get("count", 0) <= 250000}
        self.selected = set(selected)
        tree.tag_configure("heavy", background="#fff1f2")

        def render():
            tree.delete(*tree.get_children())
            for index, item in enumerate(layers):
                type_text = ", ".join(
                    f"{name} {count:,}" for name, count in list(item.get("types", {}).items())[:4])
                heavy = item.get("vertices", 0) > 250000 or item.get("count", 0) > 250000
                tree.insert("", "end", iid=str(index), values=(
                    "Yes" if item["name"] in selected else "No", item["name"],
                    f"{item.get('count', 0):,}", f"{item.get('vertices', 0):,}", type_text),
                    tags=("heavy",) if heavy else ())
            vertices = sum(item.get("vertices", 0) for item in layers
                           if item["name"] in selected)
            objects = sum(item.get("count", 0) for item in layers
                          if item["name"] in selected)
            selection_summary.set(
                f"Selected: {len(selected)} layers • {objects:,} objects • {vertices:,} legacy vertices. "
                "Pink rows are heavy.")

        def toggle(event):
            iid = tree.identify_row(event.y)
            if iid:
                selected.symmetric_difference_update({layers[int(iid)]["name"]})
                render()

        tree.bind("<ButtonRelease-1>", toggle)
        render()

        def recommend():
            selected.clear()
            selected.update(item["name"] for item in layers
                            if item.get("vertices", 0) <= 250000
                            and item.get("count", 0) <= 250000)
            render()

        ttk.Button(controls, text="Recommended light layers", command=recommend).pack(side="left")
        ttk.Button(controls, text="All", command=lambda: (selected.update(
            item["name"] for item in layers), render())).pack(side="left", padx=5)
        ttk.Button(controls, text="None", command=lambda: (selected.clear(), render())).pack(side="left")

        def create():
            if not selected:
                messagebox.showerror("Large DXF", "Select at least one layer.", parent=dialog)
                return
            selected_vertices = sum(item.get("vertices", 0) for item in layers
                                    if item["name"] in selected)
            if selected_vertices > 1_000_000 and not messagebox.askyesno(
                    "Very large selection",
                    f"The selected layers contain {selected_vertices:,} legacy vertices and may still be slow. "
                    "Create the subset anyway?", parent=dialog):
                return
            folder = HERE / "output" / "working_files"
            folder.mkdir(parents=True, exist_ok=True)
            source = Path(self.large_source)
            output = filedialog.asksaveasfilename(
                parent=dialog, title="Save lightweight working DXF", initialdir=folder,
                initialfile=source.stem + "_working.dxf", defaultextension=".dxf",
                filetypes=[("DXF drawing", "*.dxf")])
            if not output:
                return
            if Path(output).resolve() == source.resolve():
                messagebox.showerror("Large DXF", "Choose a new filename; the original cannot be replaced.",
                                     parent=dialog)
                return
            if Path(output).exists() and not messagebox.askyesno(
                    "Replace working DXF?", "Replace the existing working file?", parent=dialog):
                return
            self.selected = set(selected)
            dialog.destroy()
            self._create_large_subset(source, output, selected)

        ttk.Button(controls, text="Create working DXF", command=create,
                   style="Accent.TButton").pack(side="right")
        dialog.grab_set()

    def _export_grouped(self):
        if self.busy:
            return
        try:
            options = self._collect_options(selected=False)
            converter.job_for(options, export=True)
            if Path(options.input_path).suffix.lower() != ".dxf":
                raise ValueError("Grouped export requires an ASCII DXF.")
        except Exception as exc:
            messagebox.showerror("Grouped export", str(exc), parent=self)
            return
        folder = filedialog.askdirectory(title="Choose a new grouped-export destination")
        if not folder:
            return
        import grouped_export
        government = messagebox.askyesnocancel(
            "Grouped export purpose",
            "Create a government plate package?\n\nYes: exclude contour/topography bulk before conversion.\nNo: include bulk parts for internal review.", parent=self)
        if government is None:
            return
        def done(result):
            self.last_output = result["output"]
            self.open_button.configure(state="normal")
            self._set_text(self.report_text, json.dumps(result["report"], indent=2, default=str))
            self.tabs.select(1)
            self.status.set("Grouped export ready: " + result["output"])
        self._run(lambda token, log, progress: grouped_export.export(
            options, folder, government=government, cancel=token, log=log, progress=progress), done)

    def _create_large_subset(self, source, output, selected):
        def done(result):
            size_mb = result["size_bytes"] / (1024 ** 2)
            self._log(json.dumps(result, indent=2, default=str))
            self.status.set(f"Working DXF created ({size_mb:.1f} MB). Analysing it now...")
            self._load_path(result["output"])

        self._run(
            lambda token, log, progress: large_dxf.write_subset(
                source, output, selected, cancel=token, progress=progress),
            done)

    def _set_detected_location(self, location):
        self.detected_location = location
        state = "normal" if location else "disabled"
        for button_name in ("maps_button", "earth_button", "copy_location_button"):
            button = getattr(self, button_name, None)
            if button is not None:
                button.configure(state=state)
        if not location:
            self.location_summary.set("No embedded latitude/longitude detected yet.")
            return
        lat = float(location["center_latitude"])
        lon = float(location["center_longitude"])
        count = int(location.get("coordinate_count", 0))
        confidence = str(location.get("confidence", "review")).upper()
        hint = " ".join(str(location.get("place_hint", "")).replace("\\P", " ")
                        .replace("\\v", " ").replace("/v", " ").split())
        lines = [f"Centre: {lat:.6f}, {lon:.6f}",
                 f"{count} GPS points • {confidence} confidence"]
        if hint:
            lines.append(hint[:180])
        self.location_summary.set("\n".join(lines))

    def _location_coordinates(self):
        if not self.detected_location:
            return None
        return (float(self.detected_location["center_latitude"]),
                float(self.detected_location["center_longitude"]))

    def _open_google_maps(self):
        coordinates = self._location_coordinates()
        if coordinates:
            lat, lon = coordinates
            webbrowser.open(f"https://www.google.com/maps?q={lat:.8f},{lon:.8f}")
            self.status.set("Opened the detected centre in Google Maps.")

    def _open_google_earth(self):
        coordinates = self._location_coordinates()
        if coordinates:
            lat, lon = coordinates
            webbrowser.open(f"https://earth.google.com/web/@{lat:.8f},{lon:.8f},1000a,1200d,35y,0h,0t,0r")
            self.status.set("Opened the detected centre in Google Earth Web.")

    def _copy_coordinates(self):
        coordinates = self._location_coordinates()
        if coordinates:
            lat, lon = coordinates
            self.clipboard_clear()
            self.clipboard_append(f"{lat:.8f}, {lon:.8f}")
            self.update_idletasks()
            self.status.set("Detected latitude and longitude copied to the clipboard.")

    def _render_layers(self):
        if not hasattr(self, "tree"):
            return
        self.tree.delete(*self.tree.get_children())
        search = self.layer_filter.get().casefold()
        for index, item in enumerate(self.layer_info):
            if search in item["name"].casefold():
                included = item["name"] in self.selected
                self.tree.insert(
                    "", "end", iid=str(index),
                    values=("✓" if included else "—", item["name"], f"{item['count']:,}"),
                    tags=("included" if included else "excluded",))
        self.layer_summary.set(f"{len(self.selected)} of {len(self.layer_info)} layers selected")

    def _toggle_layer(self, event):
        if self.busy:
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            name = self.layer_info[int(iid)]["name"]
            self.selected.symmetric_difference_update({name})
            self._render_layers()

    def _select_layers(self, all_layers):
        if not self.busy:
            self.selected = {v["name"] for v in self.layer_info} if all_layers else set()
            self._render_layers()

    def _configure_plans(self):
        if self.busy or not self.plan_info.get("available"):
            return
        dialog = tk.Toplevel(self)
        dialog.title("Detected plan sheets")
        dialog.geometry("880x560")
        dialog.transient(self)
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Select the maps to place on the same survey boundary",
                  style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text=(
            self.plan_info.get("method", "Detected repeated lease geometry") + ". "
            "The map margin includes nearby plan content but excludes most titles and sections."),
            wraplength=820, style="Muted.TLabel").pack(anchor="w", pady=(4, 10))
        tree = ttk.Treeview(body, columns=("use", "plan", "shift", "handle"),
                            show="headings", height=14, selectmode="browse")
        for key, title, width in [("use", "Use", 50), ("plan", "Detected plan", 450),
                                  ("shift", "Alignment transform", 210),
                                  ("handle", "Boundary", 90)]:
            tree.heading(key, text=title)
            tree.column(key, width=width, stretch=key == "plan")
        tree.pack(fill="both", expand=True)
        selected = set(self.selected_plans)
        plans = self.plan_info["plans"]
        def render():
            tree.delete(*tree.get_children())
            for index, plan in enumerate(plans):
                shift = f"{plan['translate_x']:.3f}, {plan['translate_y']:.3f}"
                tree.insert("", "end", iid=str(index), values=(
                    "Yes" if plan["id"] in selected else "-", plan["title"], shift,
                    plan["boundary_handle"]))
        def toggle(event):
            iid = tree.identify_row(event.y)
            if iid:
                selected.symmetric_difference_update({plans[int(iid)]["id"]})
                render()
        tree.bind("<ButtonRelease-1>", toggle)
        render()
        controls = ttk.Frame(body)
        controls.pack(fill="x", pady=(10, 0))
        ttk.Button(controls, text="All", command=lambda: (selected.update(p["id"] for p in plans), render())).pack(side="left")
        ttk.Button(controls, text="None", command=lambda: (selected.clear(), render())).pack(side="left", padx=5)
        ttk.Label(controls, text="Map margin:").pack(side="left", padx=(25, 5))
        margin = tk.StringVar(value=self.plan_margin.get())
        ttk.Entry(controls, textvariable=margin, width=8).pack(side="left")
        ttk.Label(controls, text="drawing units (25 is a safe starting point)",
                  style="Muted.TLabel").pack(side="left", padx=5)
        def save():
            try:
                value = float(margin.get())
                if not __import__('math').isfinite(value) or not 0 <= value <= 250:
                    raise ValueError("Map margin must be between 0 and 250 drawing units.")
                if not selected:
                    raise ValueError("Select at least one plan.")
                self.plan_margin.set(str(value))
                self.selected_plans = set(selected)
                self.plan_mode.set(True)
                error = self.plan_info.get("control_match_error", 0)
                self.plan_summary.set(
                    f"{len(selected)} plans selected; {value:g}-unit map margin; control residual {error:.3f}.")
                dialog.destroy()
            except ValueError as exc:
                messagebox.showerror("Plan sheets", str(exc), parent=dialog)
        ttk.Button(controls, text="Use selected plans", command=save,
                   style="Accent.TButton").pack(side="right")
        dialog.grab_set()

    def _preset(self, _=None):
        self.crs.set(PRESETS[self.preset.get()])
        if self.crs.get():
            self._check_crs()

    def _check_crs(self):
        try:
            if not self.crs.get().strip():
                raise ValueError("Enter an EPSG code such as EPSG:32643, or a WKT definition supplied with the survey.")
            crs = cad.CRS.from_user_input(self.crs.get().strip())
            units = ", ".join(sorted({a.unit_name for a in crs.axis_info}))
            area = crs.area_of_use
            text = f"{crs.name}\nUnits: {units}"
            if area:
                text += f"\nArea: {area.name[:160]}" + ("..." if len(area.name) > 160 else "")
            self.crs_info.set(text)
        except Exception as exc:
            self.crs_info.set("Source CRS could not be validated.")
            messagebox.showerror("Source coordinate system", str(exc))

    def _coordinate_settings_changed(self, *_):
        self.location_ok.set(False)
        self.coordinate_control_report = None

    def _coordinate_control_wizard(self):
        if self.busy or not self.scanned_path:
            return
        dialog = tk.Toplevel(self)
        dialog.title("Coordinate Control Wizard")
        dialog.geometry("1180x780")
        dialog.minsize(980, 680)
        dialog.transient(self)

        header = tk.Frame(dialog, bg=COLORS["header"], padx=20, pady=14)
        header.pack(fill="x")
        tk.Label(header, text="Coordinate Control Wizard", bg=COLORS["header"], fg="white",
                 font=("Segoe UI Semibold", 17)).pack(anchor="w")
        tk.Label(
            header,
            text="Match CAD coordinates to authoritative survey controls. Google imagery is not used as control data.",
            bg=COLORS["header"], fg="#c6e1de", font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))

        body = ttk.Frame(dialog, padding=14, style="Panel.TFrame")
        body.pack(fill="both", expand=True)
        panes = ttk.Panedwindow(body, orient="horizontal")
        panes.pack(fill="both", expand=True)
        input_panel = ttk.Frame(panes, padding=10)
        result_panel = ttk.Frame(panes, padding=10)
        panes.add(input_panel, weight=1)
        panes.add(result_panel, weight=2)

        ttk.Label(input_panel, text="1. Matched control points", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            input_panel,
            text=("One CSV row per matching pillar: name, CAD X, CAD Y, latitude, longitude. "
                  "Latitude/longitude may be decimal degrees or quoted DMS."),
            wraplength=400, style="Muted.TLabel").pack(anchor="w", pady=(3, 7))
        editor_frame = ttk.Frame(input_panel)
        editor_frame.pack(fill="both", expand=True)
        editor = tk.Text(
            editor_frame, wrap="none", bg="#f8fbfc", fg=COLORS["text"], relief="flat",
            insertbackground=COLORS["accent"], padx=10, pady=10,
            font=("Cascadia Mono", 9), undo=True)
        editor_scroll_y = ttk.Scrollbar(editor_frame, orient="vertical", command=editor.yview)
        editor_scroll_x = ttk.Scrollbar(editor_frame, orient="horizontal", command=editor.xview)
        editor.configure(yscrollcommand=editor_scroll_y.set, xscrollcommand=editor_scroll_x.set)
        editor_scroll_y.pack(side="right", fill="y")
        editor_scroll_x.pack(side="bottom", fill="x")
        editor.pack(fill="both", expand=True)

        detected = coordinate_controls.detected_control_points(self.plan_info)
        if detected:
            editor.insert("1.0", coordinate_controls.controls_to_text(detected))
        else:
            editor.insert("1.0", "name,source_x,source_y,latitude,longitude\n")

        input_actions = ttk.Frame(input_panel)
        input_actions.pack(fill="x", pady=(7, 4))

        def load_detected():
            points = coordinate_controls.detected_control_points(self.plan_info)
            if not points:
                messagebox.showinfo(
                    "Coordinate controls",
                    "No complete GPS-labelled boundary was detected. Import the survey controls or enter them manually.",
                    parent=dialog)
                return
            editor.delete("1.0", "end")
            editor.insert("1.0", coordinate_controls.controls_to_text(points))

        def import_csv():
            path = filedialog.askopenfilename(
                parent=dialog, title="Import matched survey controls",
                filetypes=[("CSV files", "*.csv"), ("Text files", "*.txt"), ("All files", "*.*")])
            if not path:
                return
            try:
                content = Path(path).read_text(encoding="utf-8-sig")
                coordinate_controls.parse_control_text(content)
                editor.delete("1.0", "end")
                editor.insert("1.0", content)
            except Exception as exc:
                messagebox.showerror("Import controls", str(exc), parent=dialog)

        ttk.Button(input_actions, text="Use detected controls", command=load_detected).pack(side="left")
        ttk.Button(input_actions, text="Import CSV", command=import_csv).pack(side="left", padx=6)

        settings = ttk.Frame(input_panel)
        settings.pack(fill="x", pady=(5, 0))
        ttk.Label(settings, text="Projected target CRS:").grid(row=0, column=0, sticky="w")
        initial_crs = self.plan_info.get("suggested_crs") or self.crs.get().strip() or "EPSG:32643"
        target_crs = tk.StringVar(value=initial_crs)
        ttk.Entry(settings, textvariable=target_crs, width=18).grid(
            row=0, column=1, sticky="ew", padx=(8, 0))
        use_holdout = tk.BooleanVar(value=len(detected) >= 4)
        ttk.Checkbutton(
            settings, text="Keep the last point as an independent verification point",
            variable=use_holdout).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        settings.columnconfigure(1, weight=1)

        ttk.Label(result_panel, text="2. Transformation and accuracy", style="Title.TLabel").pack(anchor="w")
        result_summary = tk.StringVar(value="Enter matched controls, then calculate the fit.")
        ttk.Label(result_panel, textvariable=result_summary, wraplength=680,
                  style="Muted.TLabel").pack(fill="x", pady=(3, 8))
        columns = ("name", "role", "expected", "calculated", "dx", "dy", "error")
        tree = ttk.Treeview(result_panel, columns=columns, show="headings", height=15)
        definitions = [
            ("name", "Point", 65), ("role", "Role", 96),
            ("expected", "Survey E / N", 180), ("calculated", "Calculated E / N", 180),
            ("dx", "dE", 70), ("dy", "dN", 70), ("error", "Error", 75),
        ]
        for key, title, width in definitions:
            tree.heading(key, text=title)
            tree.column(key, width=width, stretch=key in {"expected", "calculated"})
        result_scroll = ttk.Scrollbar(result_panel, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=result_scroll.set)
        result_scroll.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        tree.tag_configure("pass", background="#effaf7")
        tree.tag_configure("review", background="#fff8e8")
        tree.tag_configure("fail", background="#fff1f0")
        result_holder = [None]

        def calculate():
            try:
                points = coordinate_controls.parse_control_text(editor.get("1.0", "end"))
                holdout_index = len(points) - 1 if use_holdout.get() else None
                report = coordinate_controls.solve(
                    points, target_crs=target_crs.get().strip(), holdout_index=holdout_index)
            except Exception as exc:
                messagebox.showerror("Coordinate controls", str(exc), parent=dialog)
                return
            result_holder[0] = report
            tree.delete(*tree.get_children())
            for index, row in enumerate(report["controls"]):
                error = float(row["error_metres"])
                tag = "pass" if error <= 1 else "review" if error <= 5 else "fail"
                tree.insert("", "end", iid=str(index), values=(
                    row["name"], "VERIFY" if row["role"] == "independent_verification" else "FIT",
                    f"{row['expected_x']:.3f}, {row['expected_y']:.3f}",
                    f"{row['calculated_x']:.3f}, {row['calculated_y']:.3f}",
                    f"{row['error_x_metres']:+.3f}", f"{row['error_y_metres']:+.3f}",
                    f"{error:.3f} m"), tags=(tag,))
            status = report["status"].upper()
            holdout = report.get("holdout_error_metres")
            precision = report.get("input_coordinate_precision_metres")
            text = (
                f"{status}  •  {report['target_crs']}  •  "
                f"RMS {report['rms_error_metres']:.3f} m  •  "
                f"Maximum fit {report['maximum_fit_error_metres']:.3f} m")
            if holdout is not None:
                text += f"  •  Independent check {holdout:.3f} m"
            text += (f"\nScale {report['scale']:.10f}  •  "
                     f"Rotation {report['rotation_degrees']:.8f}°  •  "
                     f"Offset {report['offset_x']:.3f}, {report['offset_y']:.3f} m")
            if precision:
                text += f"\nWorst estimated coordinate-label precision: approximately {precision:.2f} m."
            if report.get("area_difference_percent") is not None:
                text += f"  Control-polygon area difference: {report['area_difference_percent']:.4f}%."
            for note in report.get("quality_notes", ()):
                text += f"\nReview: {note}"
            outlier = report.get("suspected_outlier")
            if outlier:
                text += (f"\nPossible outlier: {outlier['name']} predicts "
                         f"{outlier['predicted_error_metres']:.2f} m error. Review it; it was not excluded.")
            result_summary.set(text)

        def apply_result():
            report = dict(result_holder[0] or {})
            if not report:
                messagebox.showinfo("Coordinate controls", "Calculate the fit first.", parent=dialog)
                return
            if report["status"] == "fail":
                messagebox.showerror(
                    "Coordinate controls",
                    "This fit failed the accuracy limits and cannot be applied. Correct the point identities, values, datum, or CRS.",
                    parent=dialog)
                return
            if report["status"] == "review" and not messagebox.askyesno(
                    "Apply review-level calibration?",
                    "This fit does not meet the strict pass limits. Apply it for survey-professional review?",
                    parent=dialog):
                return
            matching_preset = next(
                (name for name, value in PRESETS.items()
                 if value and cad.CRS.from_user_input(value).equals(
                     cad.CRS.from_user_input(report["target_crs"]))),
                "Custom EPSG code or WKT")
            applied = {
                "scale": report["scale"],
                "rotation_degrees": report["rotation_degrees"],
                "offset_x": report["offset_x"],
                "offset_y": report["offset_y"],
            }
            plans = list(self.plan_info.get("plans") or ())
            if self.plan_mode.get() and plans and plans[0].get("affine"):
                try:
                    applied = coordinate_controls.correction_after_existing_affine(
                        report, plans[0]["affine"])
                except Exception as exc:
                    messagebox.showerror("Coordinate controls", str(exc), parent=dialog)
                    return
                report["application_mode"] = "post_plan_alignment_correction"
                report["existing_plan_alignment_affine"] = list(plans[0]["affine"])
            else:
                report["application_mode"] = "global_drawing_similarity"
            report["applied_scale"] = applied["scale"]
            report["applied_rotation_degrees"] = applied["rotation_degrees"]
            report["applied_offset_x"] = applied["offset_x"]
            report["applied_offset_y"] = applied["offset_y"]
            self.preset.set(matching_preset)
            self.crs.set(report["target_crs"])
            self.scale.set(f"{applied['scale']:.15g}")
            self.rotation.set(f"{applied['rotation_degrees']:.15g}")
            self.offset_x.set(f"{applied['offset_x']:.15g}")
            self.offset_y.set(f"{applied['offset_y']:.15g}")
            # Variable traces deliberately clear stale calibration; assign the
            # exact report only after every calculated setting has been applied.
            self.coordinate_control_report = report
            self.location_ok.set(False)
            self._check_crs()
            self.status.set(
                f"Coordinate calibration applied ({report['status'].upper()}, RMS "
                f"{report['rms_error_metres']:.3f} m). Verify the controls, then confirm the CRS.")
            dialog.destroy()

        footer = ttk.Frame(body, padding=(0, 10, 0, 0), style="Panel.TFrame")
        footer.pack(fill="x")
        ttk.Label(
            footer,
            text="PASS ≤0.5 m RMS and ≤1 m maximum. REVIEW ≤2 m RMS and ≤5 m maximum.",
            style="Muted.TLabel").pack(side="left")
        ttk.Button(footer, text="Close", command=dialog.destroy).pack(side="right")
        ttk.Button(footer, text="Apply transformation", command=apply_result,
                   style="Accent.TButton").pack(side="right", padx=7)
        ttk.Button(footer, text="Calculate fit", command=calculate,
                   style="Soft.TButton").pack(side="right")
        dialog.grab_set()

    def _preview(self):
        if self.busy:
            return
        try:
            options = self._collect_options()
            converter.job_for(options)
        except Exception as exc:
            messagebox.showerror("Preview", str(exc))
            return
        def done(result):
            self.preview_canvas.set_features(result.preview_features)
            self.tabs.select(0)
            self.status.set("Drawing preview ready. It uses source coordinates, not geographic placement.")
        self._run(lambda token, log, progress: converter.preview(options, cancel=token, log=log, progress=progress), done)

    def _review_unclassified(self):
        if self.busy:
            return
        try:
            options = self._collect_options()
            converter.job_for(options)
        except Exception as exc:
            messagebox.showerror("Classification review", str(exc))
            return

        def done(result):
            self.status.set(
                f"Classification review ready: {len(result['unclassified']):,} item(s) need a decision.")
            self._open_review_dialog(result, options)

        self._run(
            lambda token, log, progress: converter.prepare_semantic_review(
                options, cancel=token, log=log, progress=progress),
            done)

    def _open_review_dialog(self, result, options):
        unclassified = list(result.get("unclassified", ()))
        if not unclassified:
            messagebox.showinfo(
                "Classification review",
                "All extracted objects are classified. No manual decisions are required.")
            return

        dialog = tk.Toplevel(self)
        dialog.title("Review unclassified drawing objects")
        dialog.geometry("1180x760")
        dialog.minsize(940, 620)
        dialog.transient(self)

        header = tk.Frame(dialog, bg=COLORS["header"], padx=20, pady=14)
        header.pack(fill="x")
        tk.Label(header, text="Classification review", bg=COLORS["header"], fg="white",
                 font=("Segoe UI Semibold", 17)).pack(anchor="w")
        tk.Label(
            header,
            text="Assign uncertain CAD objects to a government-review folder. Decisions stay local and apply only while this exact drawing is unchanged.",
            bg=COLORS["header"], fg="#c6e1de", font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))

        body = ttk.Frame(dialog, padding=14, style="Panel.TFrame")
        body.pack(fill="both", expand=True)
        summary_text = tk.StringVar()
        ttk.Label(body, textvariable=summary_text, style="Title.TLabel").pack(anchor="w", pady=(0, 8))
        def open_ai_review():
            import ai_dialog
            ai_dialog.show(self, options, result)
        ttk.Button(body, text="AI-assisted classification (Claude)",
                   command=open_ai_review).pack(anchor="w", pady=(0, 8))
        panes = ttk.Panedwindow(body, orient="horizontal")
        panes.pack(fill="both", expand=True)

        list_panel = ttk.Frame(panes, padding=8)
        preview_panel = ttk.Frame(panes, padding=8)
        panes.add(list_panel, weight=1)
        panes.add(preview_panel, weight=2)

        columns = ("plan", "type", "layer", "style", "value")
        tree = ttk.Treeview(list_panel, columns=columns, show="headings", selectmode="browse")
        headings = [
            ("plan", "Plan", 130), ("type", "Object", 78),
            ("layer", "Original layer", 145), ("style", "Color / line", 115),
            ("value", "Text / handle", 170),
        ]
        for key, title, width in headings:
            tree.heading(key, text=title)
            tree.column(key, width=width, stretch=key in {"plan", "layer", "value"})
        tree_scroll = ttk.Scrollbar(list_panel, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)

        review_preview = DrawingPreview(preview_panel)
        review_preview.pack(fill="both", expand=True)
        detail = tk.StringVar(value="Select an object to inspect it.")
        ttk.Label(preview_panel, textvariable=detail, wraplength=650,
                  style="Muted.TLabel").pack(fill="x", pady=(4, 8))

        decision = ttk.Frame(preview_panel)
        decision.pack(fill="x")
        ttk.Label(decision, text="Assign folder:").grid(row=0, column=0, sticky="w")
        categories = sorted(set(semantic_layers.available_categories()) |
                            set(result.get("summary", {}).get("allowed_legend_categories", ())))
        category = tk.StringVar(value=categories[0] if categories else "LEASE BOUNDARY")
        category_box = ttk.Combobox(
            decision, textvariable=category, values=categories, state="readonly", width=34)
        category_box.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        scope = tk.StringVar(value="entity")
        ttk.Radiobutton(
            decision, text="Only this object", variable=scope, value="entity").grid(
                row=1, column=1, sticky="w", pady=(7, 0))
        ttk.Radiobutton(
            decision, text="All matching objects in this plan and CAD layer",
            variable=scope, value="style").grid(row=2, column=1, sticky="w")
        decision.columnconfigure(1, weight=1)

        record_by_iid = {}
        all_records = list(result.get("records", ()))
        saved_rule_identities = {
            json.dumps(rule, sort_keys=True, default=str)
            for rule in result.get("rules", ())
        }
        ambiguous_styles = {
            tuple(item.get("signature") or ()): item.get("votes") or {}
            for item in (result.get("summary", {}).get("source_legend") or {}).get(
                "ambiguous_styles", ())
        }

        def label_value(record):
            value = " ".join(str(record.get("text_value") or "").split())
            return (value[:55] if value else str(record.get("entity_handle") or "—"))

        def review_reason(record):
            signature = semantic_layers.style_signature(record)
            votes = ambiguous_styles.get(tuple(signature))
            if votes:
                choices = ", ".join(
                    f"{name} ({count})" for name, count in
                    sorted(votes.items(), key=lambda item: (-item[1], item[0])))
                return "This style has multiple meanings in the drawing legend: " + choices
            layer = str(record.get("original_cad_layer") or record.get("cad_layer") or "0")
            if layer.strip() == "0":
                return "The object is on generic AutoCAD layer 0 and has no unique legend/style match."
            if record.get("block_name"):
                return f"Block '{record['block_name']}' has no recognised semantic name or attribute."
            if record.get("text_value"):
                return "The text is an annotation/value, but it does not uniquely identify a feature category."
            return "No explicit layer, named block, unique legend style, or safe local relationship was found."

        def insert_records():
            tree.delete(*tree.get_children())
            record_by_iid.clear()
            for index, record in enumerate(unclassified):
                iid = str(index)
                record_by_iid[iid] = record
                signature = semantic_layers.style_signature(record)
                tree.insert("", "end", iid=iid, values=(
                    record.get("plan_title") or "Drawing",
                    record.get("entity_type") or "—",
                    record.get("original_cad_layer") or record.get("cad_layer") or "0",
                    f"{signature[1] or 'BYLAYER'} / {signature[2]}",
                    label_value(record)))

        def update_summary():
            classified = int(result.get("summary", {}).get("classified_records", 0))
            summary_text.set(
                f"{len(tree.get_children()):,} uncertain objects remaining  •  "
                f"{classified:,} automatically classified  •  "
                f"{len(saved_rule_identities):,} saved decisions")

        def preview_record(_event=None):
            selected = tree.selection()
            if not selected:
                review_preview.set_features([])
                return
            record = record_by_iid[selected[0]]
            geometry = record.get("geometry")
            features = []
            if geometry is not None and not geometry.is_empty:
                all_bounds = [candidate["geometry"].bounds for candidate in all_records
                              if candidate.get("geometry") is not None
                              and not candidate["geometry"].is_empty]
                if all_bounds:
                    width = max(value[2] for value in all_bounds) - min(value[0] for value in all_bounds)
                    height = max(value[3] for value in all_bounds) - min(value[1] for value in all_bounds)
                    radius = max(width, height) * 0.025
                    region = geometry.envelope.buffer(max(radius, 1e-9))
                    for candidate in all_records:
                        candidate_geometry = candidate.get("geometry")
                        if candidate_geometry is not None and not candidate_geometry.is_empty \
                                and candidate_geometry.intersects(region):
                            features.append({
                                "geometry": candidate_geometry,
                                "layer": "Nearby context",
                                "entity_type": candidate.get("entity_type"),
                                "entity_handle": candidate.get("entity_handle"),
                            })
                            if len(features) >= 250:
                                break
                features.append({
                    "geometry": geometry, "layer": "SELECTED OBJECT",
                    "entity_type": record.get("entity_type"),
                    "entity_handle": record.get("entity_handle"),
                })
            review_preview.set_features(features)
            signature = semantic_layers.style_signature(record)
            detail.set(
                f"Plan: {record.get('plan_title') or 'Drawing'}   •   "
                f"Handle: {record.get('entity_handle') or 'none'}   •   "
                f"Type: {record.get('entity_type') or 'unknown'}\n"
                f"CAD layer: {record.get('original_cad_layer') or record.get('cad_layer') or '0'}   •   "
                f"Style: {signature[0]}, {signature[1] or 'BYLAYER'}, {signature[2]}   •   "
                f"Text: {label_value(record)}\nWhy review: {review_reason(record)}")

        def save_decision():
            selected = tree.selection()
            if not selected:
                messagebox.showinfo("Classification review", "Select an object first.", parent=dialog)
                return
            record = record_by_iid[selected[0]]
            chosen = category.get().strip()
            if not chosen:
                return
            selected_scope = scope.get()
            handle = str(record.get("entity_handle") or "")
            if selected_scope == "entity" and not handle:
                messagebox.showerror(
                    "Classification review",
                    "This object has no stable CAD handle. Use the matching-style option instead.",
                    parent=dialog)
                return
            rule = {
                "scope": selected_scope,
                "plan_id": str(record.get("plan_id") or ""),
                "category": chosen,
            }
            if selected_scope == "entity":
                rule["entity_handle"] = handle
            else:
                rule["signature"] = list(semantic_layers.style_signature(record))
                rule["original_layer"] = str(
                    record.get("original_cad_layer") or record.get("cad_layer") or "0")
            review_rules.add_rule(options.input_path, rule)
            identity_fields = ("scope", "plan_id", "entity_handle", "signature", "original_layer")
            identity = tuple(json.dumps(rule.get(field), sort_keys=True)
                             for field in identity_fields)
            for existing in list(saved_rule_identities):
                try:
                    parsed = json.loads(existing)
                except (TypeError, ValueError):
                    continue
                existing_identity = tuple(json.dumps(parsed.get(field), sort_keys=True)
                                          for field in identity_fields)
                if existing_identity == identity:
                    saved_rule_identities.discard(existing)
            saved_rule_identities.add(json.dumps(rule, sort_keys=True, default=str))

            def matched(candidate):
                if str(candidate.get("plan_id") or "") != rule["plan_id"]:
                    return False
                if selected_scope == "entity":
                    return str(candidate.get("entity_handle") or "") == handle
                return (list(semantic_layers.style_signature(candidate)) == rule["signature"] and
                        str(candidate.get("original_cad_layer") or candidate.get("cad_layer") or "0") ==
                        rule["original_layer"])

            for iid in list(tree.get_children()):
                if matched(record_by_iid[iid]):
                    tree.delete(iid)
            update_summary()
            remaining = tree.get_children()
            if remaining:
                tree.selection_set(remaining[0])
                tree.focus(remaining[0])
                preview_record()
            else:
                review_preview.set_features([])
                detail.set("All uncertain objects in this review have a saved decision. Reanalyse to verify the result.")

        def clear_saved():
            if messagebox.askyesno(
                    "Clear saved decisions?",
                    "Remove all manual category decisions for this drawing?",
                    parent=dialog):
                review_rules.clear_rules(options.input_path)
                dialog.destroy()
                self.after_idle(self._review_unclassified)

        def reanalyse():
            dialog.destroy()
            self.after_idle(self._review_unclassified)

        tree.bind("<<TreeviewSelect>>", preview_record)
        insert_records()
        update_summary()
        actions = ttk.Frame(body, padding=(0, 10, 0, 0), style="Panel.TFrame")
        actions.pack(fill="x")
        ttk.Button(actions, text="Clear saved decisions", command=clear_saved,
                   style="Danger.TButton").pack(side="left")
        ttk.Button(actions, text="Close", command=dialog.destroy).pack(side="right")
        ttk.Button(actions, text="Reanalyse drawing", command=reanalyse).pack(side="right", padx=7)
        ttk.Button(actions, text="Save decision", command=save_decision,
                   style="Accent.TButton").pack(side="right")
        first = tree.get_children()[0]
        tree.selection_set(first)
        tree.focus(first)
        preview_record()
        dialog.grab_set()

    def _export(self):
        if self.busy:
            return
        try:
            options = self._collect_options()
            converter.job_for(options, export=True)
        except Exception as exc:
            messagebox.showerror("Before exporting", str(exc))
            return
        folder = HERE / "output"
        folder.mkdir(exist_ok=True)
        output = filedialog.asksaveasfilename(title="Export Google Earth file", initialdir=folder,
                    initialfile=Path(options.input_path).stem + ".kmz", defaultextension=".kmz",
                    filetypes=[("Google Earth KMZ", "*.kmz"), ("Google Earth KML", "*.kml")])
        if not output:
            return
        if Path(output).exists() and not messagebox.askyesno("Replace export?", "Replace this existing export and its report?"):
            return
        def done(result):
            self.last_output = result["output"]
            self.open_button.configure(state="normal")
            self.verify_panel.set_candidate(result["output"])
            self._set_text(self.report_text, self._summary(result))
            self.tabs.select(1)
            info = result["report"]["mineplan_earth"]
            semantic = result["report"].get("semantic_classification", {})
            review = int(semantic.get("unclassified_records", 0))
            suffix = f" {review} item(s) require legend review." if review else ""
            self.status.set(
                f"Exported {info['features_exported']} features.{suffix} Verify placement in Google Earth.")
        self._run(lambda token, log, progress: converter.convert(options, output, cancel=token, log=log, progress=progress), done)

    def _summary(self, result):
        report = result["report"]
        data = report["mineplan_earth"]
        lines = ["EXPORT READY", "", result["output"], "", f"Features: {data['features_exported']}",
                 f"Altitude mode: {data['altitude_mode']}", "", "LAYERS"]
        metrics = report.get("quality_metrics", {})
        coverage = metrics.get("classification_coverage_out_of_100")
        lines[0:0] = [
            f"Classification coverage: {coverage:.2f}/100" if coverage is not None else "Classification coverage: not assessed",
            "Independently verified accuracy: not measured",
            f"Processing time: {report.get('performance', {}).get('processing_seconds', 0):.3f} seconds",
            "Coverage measures assignments, not their correctness.", "",
        ]
        if coverage is not None:
            remaining = metrics.get("additional_assignments_for_90_percent")
            minimum = metrics.get("additional_assignments_for_80_percent")
            if minimum is not None and remaining is not None:
                lines[1:1] = [
                    f"80% minimum: {'met' if minimum == 0 else str(minimum) + ' more objects need classification'}",
                    f"90% goal: {'met' if remaining == 0 else str(remaining) + ' more objects need classification'}",
                ]
        lines += [f"  {name}: {count}" for name, count in data["layer_counts"].items()]
        controls = report.get("coordinate_control")
        if controls:
            lines += [
                "", "COORDINATE CONTROL",
                f"Status: {str(controls.get('status', 'review')).upper()}",
                f"Projected CRS: {controls.get('target_crs', '')}",
                f"Matched controls: {controls.get('point_count', 0)}",
                f"RMS fit error: {float(controls.get('rms_error_metres', 0)):.3f} m",
                f"Maximum fit error: {float(controls.get('maximum_fit_error_metres', 0)):.3f} m",
            ]
            if controls.get("holdout_error_metres") is not None:
                lines.append(
                    f"Independent verification error: {float(controls['holdout_error_metres']):.3f} m")
            lines.append(
                f"Application: {controls.get('application_mode', 'global_drawing_similarity')}")
        semantic = report.get("semantic_classification")
        if semantic:
            lines += [
                "", "AUTOCAD LEGEND GROUPING",
                f"Recognised legend folders: {len(semantic.get('allowed_legend_categories', []))}",
                f"Matched to legend folders: {semantic.get('classified_records', 0)}",
                f"Unclassified - review required: {semantic.get('unclassified_records', 0)}",
                f"Year known, feature role requires review: {semantic.get('feature_role_review_records', 0)}",
                f"Saved review decisions applied: {semantic.get('manual_rules_loaded', 0)}",
                "Policy: file-local legend, constrained geometry rules, and saved human review.",
                "Uncertain objects remain unclassified until a reviewer decides.",
            ]
            source_legend = semantic.get("source_legend") or report.get("source_legend") or {}
            coverage = semantic.get("legend_coverage", [])
            lines += [
                f"Legend swatch observations: {source_legend.get('legend_labels_found', 0)}",
                f"Swatch pairings requiring review: {len(source_legend.get('pairings_requiring_review', []))}",
                f"Legend entries without matched features: {sum(row.get('status') != 'matched' for row in coverage)}",
                "Export folders retain the plate's original legend wording when the name is unambiguous.",
            ]
        audit = report.get("mining_geometry_audit")
        if audit:
            counts = audit.get("counts", {})
            lines += [
                "", "MINING GEOMETRY QA",
                f"Status: {str(audit.get('status', 'review')).upper()}",
                f"Plans checked: {audit.get('plans_checked', 0)}",
                f"Controlled pits/dumps/areas checked: {audit.get('controlled_features_checked', 0)}",
                f"Errors: {counts.get('error', 0)}    Review items: {counts.get('review', 0)}",
            ]
            for lease in audit.get("lease_areas", ()):
                lines.append(
                    f"Lease area — {lease.get('plan', 'Plan')}: "
                    f"{float(lease.get('area_hectares', 0)):.4f} ha")
            for issue in audit.get("issues", ())[:12]:
                lines.append(f"- {issue.get('severity', 'review').upper()}: {issue.get('message', '')}")
            if len(audit.get("issues", ())) > 12:
                lines.append("- Additional findings are included in the JSON report.")
        preset = report.get("government_clean_preset")
        if preset:
            lines += [
                "", "GOVERNMENT CLEAN PRESET",
                f"Removed decorative records: {preset.get('records_removed', 0)}",
                "Normal/full exports are not changed by this preset.",
            ]
        b = data["wgs84_bounds"]
        lines += ["", f"Longitude: {b[0]:.7f} to {b[2]:.7f}", f"Latitude:  {b[1]:.7f} to {b[3]:.7f}",
                  "", "CHECK BEFORE USING", "Open in Google Earth and compare known survey boundary points.",
                  "Automatic coordinate-range checks cannot establish survey accuracy.", "", "NOTES"]
        lines += ["- " + v for v in data["notes"]]
        if report.get("unsupported_entities"):
            lines += ["", "UNSUPPORTED OBJECTS", json.dumps(report["unsupported_entities"], indent=2)]
        if report.get("issues"):
            lines += ["", "WARNINGS / INFORMATION"]
            lines += [f"- {i['severity']}: {i['message']}" for i in report["issues"][:50]]
        lines += ["", "Full report:", result["report_path"]]
        return "\n".join(lines)

    def _export_submission(self):
        if self.busy:
            return
        try:
            options = self._collect_options()
            converter.job_for(options, export=True)
            if options.plan_ids is None:
                raise ValueError("Enable Plan mode and select the detected plan sheets first.")
        except Exception as exc:
            messagebox.showerror("Government submission", str(exc))
            return
        parent = filedialog.askdirectory(
            title="Choose where the uniquely named submission folders will be created",
            initialdir=HERE / "output")
        if not parent:
            return
        parent_path = Path(parent)
        kmz_name, _ = converter.submission_folder_names(options.input_path)
        target = parent_path if parent_path.name.casefold() == kmz_name.casefold() \
            else parent_path / kmz_name
        existing = list(target.glob("*.kmz")) if target.exists() else []
        if existing and not messagebox.askyesno(
                "Replace existing submission files?",
                f"{len(existing)} KMZ file(s) already exist in:\n\n{target}\n\n"
                "Files with matching official names will be replaced. Continue?"):
            return

        def done(result):
            self.last_output = result["output"]
            self.open_button.configure(state="normal")
            summary = result["report"]
            lines = [
                "GOVERNMENT SUBMISSION PACKAGE READY", "",
                f"KMZ folder: {result['output']}",
                f"QA folder:  {result['qa_folder']}", "",
                f"Files: {len(summary['files'])}",
                f"Total features: {summary['total_features']}", "",
            ]
            lines.extend(
                f"  {item['filename']} - {item['features']} features - "
                f"{item['size_bytes'] / 1024 / 1024:.2f} MB"
                for item in summary["files"])
            if summary["warnings"]:
                lines.extend(["", "WARNINGS"] + ["- " + value for value in summary["warnings"]])
            lines.extend([
                "", "MANUAL REVIEW IS REQUIRED",
                "Open every KMZ and verify survey controls, boundaries and plan contents.",
                "", f"Manifest: {result['report_path']}",
            ])
            self._set_text(self.report_text, "\n".join(lines))
            self.tabs.select(1)
            self.status.set(
                f"Government package created: {len(summary['files'])} KMZ files. Manual review required.")

        self._run(
            lambda token, log, progress: converter.convert_submission(
                options, parent, cancel=token, log=log, progress=progress),
            done)

    def _advanced(self):
        if self.busy:
            return
        dialog = tk.Toplevel(self)
        dialog.title("Advanced settings")
        dialog.geometry("570x460")
        dialog.transient(self)
        body = ttk.Frame(dialog, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Use values supplied by the survey team. No automatic shifts are applied.",
                  wraplength=520).pack(anchor="w", pady=(0, 12))
        variables = [("Curve tolerance (drawing units)", self.tolerance), ("Coordinate scale (includes Z)", self.scale),
                     ("Rotation around origin (degrees)", self.rotation), ("X offset (source CRS units)", self.offset_x),
                     ("Y offset (source CRS units)", self.offset_y), ("ODAFileConverter.exe or dwg2dxf.exe", self.dwg)]
        local = [(label, original, tk.StringVar(value=original.get())) for label, original in variables]
        for label, _, variable in local:
            row = ttk.Frame(body)
            row.pack(fill="x", pady=5)
            ttk.Label(row, text=label).pack(side="left")
            ttk.Entry(row, textvariable=variable, width=22).pack(side="right")
        def browse_converter():
            path = filedialog.askopenfilename(parent=dialog, title="Choose installed DWG converter", filetypes=[("Executable", "*.exe")])
            if path:
                local[-1][2].set(path)
        ttk.Button(body, text="Locate DWG converter", command=browse_converter).pack(anchor="e", pady=8)
        def save():
            try:
                values = [float(row[2].get()) for row in local[:-1]]
                if not all(__import__('math').isfinite(v) for v in values) or values[0] <= 0 or values[1] <= 0:
                    raise ValueError("Use finite numbers; curve tolerance and scale must be positive.")
                for _, original, variable in local:
                    original.set(variable.get())
                dialog.destroy()
            except ValueError as exc:
                messagebox.showerror("Settings", str(exc), parent=dialog)
        ttk.Button(body, text="Save settings", command=save, style="Accent.TButton").pack(anchor="e", pady=8)
        dialog.grab_set()

    def _demo(self):
        if self.busy:
            return
        try:
            path = converter.create_demo(HERE / "demo")
            self.preset.set("WGS 84 / UTM 43N - EPSG:32643")
            self.crs.set("EPSG:32643")
            self.scale.set("1")
            self.rotation.set("0")
            self.offset_x.set("0")
            self.offset_y.set("0")
            self._check_crs()
            self._load_path(path)
            self._log("DEMO ONLY: synthetic drawing near 73.7 E, 24.58 N. CRS EPSG:32643, metres. Check the source-CRS confirmation box before export.")
        except Exception as exc:
            messagebox.showerror("Demo", str(exc))

    def _run(self, operation, done):
        self.busy = True
        self.verify_panel.set_busy(True)
        self.token = cad.CancellationToken()
        self.progressbar["value"] = 0
        for button in [self.browse_button, self.scan_button, self.demo_button,
                       self.preview_button, self.export_button, self.submission_button,
                       self.plan_button, self.review_button, self.control_button]:
            button.configure(state="disabled")
        self.file_entry.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status.set("Working...")
        def worker():
            try:
                result = operation(self.token, lambda s: self.events.put(("log", s)),
                                   lambda p, s: self.events.put(("progress", (p, s))))
                self.events.put(("done", (done, result)))
            except cad.CancelledError:
                self.events.put(("cancelled", None))
            except Exception as exc:
                self.events.put(("error", (str(exc), traceback.format_exc())))
        threading.Thread(target=worker, daemon=True).start()

    def _finish(self):
        self.busy = False
        self.verify_panel.set_busy(False)
        for button in [self.browse_button, self.scan_button, self.demo_button,
                       self.preview_button, self.export_button]:
            button.configure(state="normal")
        self.review_button.configure(
            state="normal" if self.scanned_path else "disabled")
        self.control_button.configure(
            state="normal" if self.scanned_path else "disabled")
        self.plan_button.configure(state="normal" if self.plan_info.get("available") else "disabled")
        self.submission_button.configure(
            state="normal" if self.plan_info.get("available") else "disabled")
        self.file_entry.configure(state="normal")
        self.cancel_button.configure(state="disabled")

    def _poll(self):
        try:
            while True:
                event, data = self.events.get_nowait()
                if event == "log":
                    self._log(data)
                elif event == "progress":
                    self.progressbar["value"] = max(0, min(1, data[0]))
                    self.status.set(data[1])
                elif event == "done":
                    self._finish()
                    data[0](data[1])
                elif event == "cancelled":
                    self._finish()
                    self.status.set("Cancelled.")
                elif event == "error":
                    self._finish()
                    self._log(data[1])
                    self.status.set("Could not complete. See Activity for details.")
                    messagebox.showerror(f"MinePlan Earth {converter.APP_VERSION}", data[0])
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _cancel(self):
        if self.token:
            self.token.cancel()
            self.status.set("Cancelling...")

    def _open_export(self):
        if self.last_output:
            try:
                os.startfile(self.last_output)
            except OSError:
                messagebox.showinfo("Open in Google Earth", "Open Google Earth Pro, then File > Open and select:\n\n" + self.last_output)

    def _close(self):
        if self.busy:
            if not messagebox.askyesno("Conversion in progress", "Cancel the operation and close?"):
                return
            self._cancel()
        self.destroy()


def main():
    parser = argparse.ArgumentParser(description="MinePlan Earth desktop converter")
    parser.add_argument("--smoke-ui", action="store_true", help="Open and close the UI for a startup check")
    args = parser.parse_args()
    cad._enable_windows_dpi_awareness()
    app = App()
    if args.smoke_ui:
        app.after(1500, app.destroy)
    app.mainloop()


if __name__ == "__main__":
    main()
