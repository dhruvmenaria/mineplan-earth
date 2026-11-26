"""Optional Claude evidence and proposal review UI."""
import base64
import json
import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import ai_classification as ai
import converter


def show(app, options, review):
    def ready(package):
        _window(app, options, package)
    app._run(lambda token, log, progress: ai.build_package(options.input_path, review), ready)


def _window(app, options, package):
    dialog = tk.Toplevel(app)
    dialog.title("AI-assisted classification — review proposals")
    dialog.geometry("1050x760")
    dialog.transient(app)
    dialog.grab_set()
    body = ttk.Frame(dialog, padding=16)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text="AI-assisted classification", style="Title.TLabel").pack(anchor="w")
    status = tk.StringVar(value=f"Prepared {len(package['cases'])} cases locally out of {package['total_review_objects']} review objects. Nothing sent.")
    ttk.Label(body, textvariable=status, wraplength=1000).pack(anchor="w", pady=8)
    ttk.Label(body, text="Claude model ID:").pack(anchor="w")
    model = tk.StringVar(value=os.environ.get("MINEPLAN_CLAUDE_MODEL", ""))
    ttk.Entry(body, textvariable=model).pack(fill="x")
    ttk.Label(body, text="Key comes from ANTHROPIC_API_KEY on this computer. No key is saved in the drawing or report.", wraplength=950).pack(anchor="w", pady=6)
    actions = ttk.Frame(body)
    actions.pack(fill="x", pady=8)
    proposals = []
    tree = ttk.Treeview(body, columns=("case", "plan", "handle", "category"), show="headings", height=9, selectmode="extended")
    for key, title in [("case","Case"),("plan","Plan"),("handle","Object handle"),("category","Suggested category")]:
        tree.heading(key, text=title)
        tree.column(key,width=90 if key in {"case","handle"} else 300)
    tree.pack(fill="x")
    preview = tk.Label(body, bg="#181d22")
    preview.pack(side="left", pady=10)
    detail = tk.Text(body, width=46, height=18, wrap="word")
    detail.pack(side="left",fill="both",expand=True,padx=(12,0),pady=10)

    def selected(_=None):
        ids = tree.selection()
        if not ids:
            return
        proposal = proposals[int(ids[0])]
        case = next(c for c in package["cases"] if c["case_id"] == proposal["case_id"])
        picture = tk.PhotoImage(data=case["image_base64"]).subsample(2)
        preview.configure(image=picture)
        preview.image = picture
        refs = [e for e in case["evidence"] if e["evidence_id"] in proposal["evidence_ids"]]
        detail.delete("1.0","end")
        detail.insert("end", proposal["explanation"] + "\n\nCited source evidence:\n" + json.dumps(refs,indent=2))
    tree.bind("<<TreeviewSelect>>", selected)

    def display(result):
        proposals.clear(); proposals.extend(result.get("assignments",[]))
        tree.delete(*tree.get_children())
        for index,p in enumerate(proposals):
            tree.insert("","end",iid=str(index),values=(p["case_id"],p["plan_id"],p["entity_handle"],p["category"]))
        status.set(f"{len(proposals)} proposals require your review; {len(result.get('rejected',[]))} rejected. Select rows and inspect their evidence before saving. Usage: {result.get('usage',{})}")

    def export():
        path = filedialog.asksaveasfilename(parent=dialog,title="Save offline AI evidence",defaultextension=".json",initialfile="ai-evidence.json",filetypes=[("JSON","*.json")])
        if path:
            payload = {"evidence_package_id":ai.package_id(package), "package":package,
                       "response_format":{"evidence_package_id":ai.package_id(package), "assignments":[]},
                       "instructions":ai.SYSTEM, "response_schema":ai.SCHEMA}
            converter._atomic_bytes(path,json.dumps(payload,indent=2).encode())
            status.set("Evidence saved locally. Includes source labels and cropped images; share only with your approved provider.")

    def import_reply():
        path = filedialog.askopenfilename(parent=dialog,filetypes=[("JSON","*.json")])
        if not path:
            return
        try:
            if Path(path).stat().st_size > 4*1024**2:
                raise ValueError("Response file is too large.")
            reply = json.loads(Path(path).read_text(encoding="utf-8"))
            if reply.get("evidence_package_id") != ai.package_id(package):
                raise ValueError("Response does not match this evidence package.")
            valid, rejected = ai.validate_proposals(package,reply)
            display({"assignments":valid,"rejected":rejected})
        except (ValueError,OSError) as exc:
            messagebox.showerror("Import proposals",str(exc),parent=dialog)

    def request():
        if not model.get().strip() or not os.environ.get("ANTHROPIC_API_KEY", "").strip():
            messagebox.showinfo("Claude setup", "Prepare/export evidence now. Later set ANTHROPIC_API_KEY locally and enter a supported Claude model ID.",parent=dialog)
            return
        if app.busy:
            return
        if not messagebox.askyesno("Send evidence to Claude?",f"Send {len(package['cases'])} cropped drawing previews, nearby labels and legend metadata to Anthropic? API usage will be billed. The complete DXF is not uploaded.",parent=dialog):
            return
        chosen = model.get().strip()
        app._run(lambda token, log, progress: ai.request_claude(package, chosen),display)

    def apply():
        picked = [proposals[int(i)] for i in tree.selection()]
        if not picked:
            messagebox.showinfo("Review proposals","Select the proposals you have checked first.",parent=dialog)
            return
        try:
            path = ai.apply_reviewed(options.input_path,package,picked)
            status.set(f"Saved {len(picked)} reviewed object mappings. Re-run Review Categories or export. Rules: {path}")
        except (ValueError,OSError) as exc:
            messagebox.showerror("Save reviewed mappings",str(exc),parent=dialog)

    ttk.Button(actions,text="Save evidence (offline)",command=export).pack(side="left")
    ttk.Button(actions,text="Import proposals",command=import_reply).pack(side="left",padx=6)
    ttk.Button(actions,text="Ask Claude",command=request).pack(side="left",padx=6)
    ttk.Button(actions,text="Save selected reviewed mappings",command=apply).pack(side="right")
