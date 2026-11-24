"""Verification controls for the existing MinePlan Earth workspace."""
from __future__ import annotations

import math
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import converter
import verifier


FULL_EXPORT = "Full export (selected layers and plans)"


class VerifyPanel(ttk.Frame):
    def __init__(self, notebook, app):
        super().__init__(notebook, padding=16)
        self.app = app
        self.candidate = tk.StringVar(self)
        self.scope = tk.StringVar(self, value=FULL_EXPORT)
        self.tolerance = tk.StringVar(self, value="0.5")
        self.result = None
        self.report_name = ""

        ttk.Label(self, text="Verify KML / KMZ against DXF", style="Title.TLabel").pack(anchor="w")
        ttk.Label(self, text="Load the source DXF at left. Use the same coordinate system, layers and plan settings as the export.",
                  wraplength=530, style="Muted.TLabel").pack(anchor="w", pady=(4, 12))
        ttk.Label(self, text="Google Earth file to check").pack(anchor="w")
        row = ttk.Frame(self)
        row.pack(fill="x", pady=(3, 8))
        self.file_entry = ttk.Entry(row, textvariable=self.candidate)
        self.file_entry.pack(side="left", fill="x", expand=True)
        self.browse_button = ttk.Button(row, text="Browse…", command=self.browse)
        self.browse_button.pack(side="left", padx=(8, 0))

        settings = ttk.Frame(self)
        settings.pack(fill="x", pady=(0, 9))
        settings.columnconfigure(0, weight=1)
        ttk.Label(settings, text="Expected export").grid(row=0, column=0, sticky="w")
        ttk.Label(settings, text="Tolerance (metres)").grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.scope_box = ttk.Combobox(settings, state="readonly", textvariable=self.scope,
                                      values=(FULL_EXPORT,), postcommand=self.refresh_groups)
        self.scope_box.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        self.tolerance_entry = ttk.Entry(settings, textvariable=self.tolerance, width=13)
        self.tolerance_entry.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=(3, 0))
        ttk.Label(self, text="For a government package, choose the individual plan file and its matching group.",
                  wraplength=530, style="Muted.TLabel").pack(anchor="w")
        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=(9, 10))
        self.verify_button = ttk.Button(actions, text="Verify export", command=self.run, style="Accent.TButton")
        self.verify_button.pack(side="left")
        self.save_button = ttk.Button(actions, text="Save report…", command=self.save, state="disabled")
        self.save_button.pack(side="left", padx=8)
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.summary = app._text_box(body)
        for field in (self.candidate, self.scope, self.tolerance):
            field.trace_add("write", self._clear_result)
        self._clear_result()

    def _clear_result(self, *_):
        self.result = None
        self.save_button.configure(state="disabled")
        self.app._set_text(self.summary, "Verification results will appear here.")

    def refresh_groups(self):
        groups = []
        if self.app.plan_mode.get() and self.app.plan_info.get("available"):
            groups = [g["filename"] for g in converter.submission_groups(
                self.app.plan_info, self.app.selected_plans)]
        self.scope_box.configure(values=[FULL_EXPORT, *groups])
        return groups

    def set_candidate(self, path, *, guess_group=False):
        self.candidate.set(str(path))
        scope = FULL_EXPORT
        if guess_group:
            stem = Path(path).stem.casefold()
            scope = next((name for name in self.refresh_groups()
                          if Path(name).stem.casefold() == stem), FULL_EXPORT)
        self.scope.set(scope)

    def browse(self):
        path = filedialog.askopenfilename(
            parent=self, title="Choose an existing KML or KMZ to verify",
            filetypes=[("Google Earth files", "*.kml *.kmz"), ("KML", "*.kml"), ("KMZ", "*.kmz")])
        if path:
            self.set_candidate(path, guess_group=True)

    def set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for widget in (self.verify_button, self.browse_button, self.file_entry, self.tolerance_entry):
            widget.configure(state=state)
        self.scope_box.configure(state="disabled" if busy else "readonly")

    def run(self):
        if self.app.busy:
            return
        try:
            options = self.app._collect_options()
            converter.job_for(options, export=True)
            if Path(options.input_path).suffix.casefold() != ".dxf":
                raise ValueError("Load the source DXF to run this comparison.")
            path = Path(self.candidate.get().strip()).expanduser()
            if path.suffix.lower() not in {".kml", ".kmz"} or not path.is_file():
                raise ValueError("Choose an existing KML or KMZ file to compare.")
            try:
                tolerance = float(self.tolerance.get())
            except ValueError:
                raise ValueError("Enter a numeric horizontal tolerance in metres.") from None
            if not math.isfinite(tolerance) or tolerance < 0:
                raise ValueError("Enter a finite horizontal tolerance of zero metres or more.")
            group = self.scope.get() if self.scope.get() != FULL_EXPORT else None
            if group and group not in self.refresh_groups():
                raise ValueError("Choose an expected export available for the currently selected plans.")
        except (OSError, ValueError) as exc:
            messagebox.showerror("Before verifying", str(exc), parent=self)
            return

        self._clear_result()
        self.app._set_text(self.summary, f"Comparing {path.name} with {Path(options.input_path).name}…")

        def done(result):
            self.result = result
            self.report_name = path.stem + ".verification.json"
            self.app._set_text(self.summary, verifier.format_report(result))
            self.save_button.configure(state="normal")
            self.app.tabs.select(self)
            self.app.status.set("Verification complete — " + result["status"].capitalize())

        self.app._run(lambda token, log, progress: verifier.verify_export(
            options, path, tolerance_m=tolerance, submission_group=group,
            cancel=token, log=log, progress=progress), done)

    def save(self):
        if self.result is None:
            return
        path = filedialog.asksaveasfilename(parent=self, title="Save verification report",
                    initialfile=self.report_name, defaultextension=".json",
                    filetypes=[("JSON report", "*.json")])
        if path:
            try:
                if Path(path).suffix.casefold() != ".json":
                    raise ValueError("Choose a .json filename for the verification report.")
                verifier.save_report(self.result, Path(path))
                self.app.status.set(f"Saved verification report: {path}")
            except (OSError, ValueError) as exc:
                messagebox.showerror("Save report", str(exc), parent=self)
