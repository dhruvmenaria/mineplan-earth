"""Guided AI review: ask, approve, then save and export from one window."""
import copy
import json
import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import ai_classification as ai
import converter


def show(app, options, review):
    app._run(lambda token, log, progress: ai.build_package(options.input_path, review),
             lambda package: _window(app, options, package, review))


def _window(app, options, package, review):
    dialog = tk.Toplevel(app)
    dialog.title('Improve categories — review and export')
    dialog.geometry(f"1120x{min(850, dialog.winfo_screenheight()-90)}")
    dialog.minsize(850, 620)
    dialog.transient(app)
    dialog.grab_set()
    body = ttk.Frame(dialog, padding=14)
    body.pack(fill='both', expand=True)
    body.columnconfigure(0, weight=1)
    body.rowconfigure(4, weight=1)
    body.rowconfigure(5, weight=2)
    state = ai.ProposalReview()
    provenance = {}
    seen = set()
    has_result = False
    poll_job = None
    provider = tk.StringVar(value='Claude' if os.environ.get('ANTHROPIC_API_KEY') else 'Gemini')
    model = tk.StringVar(value=(os.environ.get('MINEPLAN_GEMINI_MODEL', 'gemini-2.5-flash')
                               if provider.get() == 'Gemini' else os.environ.get('MINEPLAN_CLAUDE_MODEL', '')))
    key = tk.StringVar(value='')
    redact = tk.BooleanVar(value=package.get('redaction_enabled', True))
    grouped = tk.BooleanVar(value=package.get('grouping_enabled', True))
    advanced = tk.BooleanVar(value=False)
    status = tk.StringVar()
    usage = tk.StringVar(value='No API request made. Only a bounded batch is sent each time.')
    selection_title = tk.StringVar(value='Select a suggestion to see its object and explanation.')
    ttk.Label(body, text='1. Ask AI   →   2. Review suggestions   →   3. Save & export',
              style='Title.TLabel').grid(row=0, column=0, sticky='w')
    ttk.Label(body, textvariable=status, wraplength=1050).grid(row=1, column=0, sticky='ew', pady=(5, 8))

    setup = ttk.Frame(body)
    setup.grid(row=2, column=0, sticky='ew')
    setup.columnconfigure(3, weight=1)
    ttk.Label(setup, text='Provider').grid(row=0, column=0, sticky='w')
    providers = ttk.Combobox(setup, textvariable=provider, values=['Gemini', 'Claude'], state='readonly', width=12)
    providers.grid(row=0, column=1, padx=(8, 16), sticky='w')
    ttk.Label(setup, text='Model').grid(row=0, column=2, sticky='w')
    ttk.Entry(setup, textvariable=model).grid(row=0, column=3, sticky='ew', padx=(8, 0))
    ttk.Label(setup, text='API key').grid(row=1, column=0, sticky='w', pady=6)
    ttk.Entry(setup, textvariable=key, show='•').grid(row=1, column=1, columnspan=3, sticky='ew', padx=(8, 0))
    ttk.Label(setup, text='Key stays in memory for this window only. It is cleared when you close or change provider.',
              style='Muted.TLabel').grid(row=2, column=0, columnspan=4, sticky='w')

    actions = ttk.Frame(body)
    actions.grid(row=3, column=0, sticky='ew', pady=10)
    ask_button = ttk.Button(actions, text='1. Ask AI', command=lambda: request())
    ask_button.pack(side='left')
    mark_button = ttk.Button(actions, text='Mark all reviewed', command=lambda: mark_all())
    mark_button.pack(side='left', padx=6)
    clear_button = ttk.Button(actions, text='Clear checks', command=lambda: clear_checks())
    clear_button.pack(side='left')
    save_button = ttk.Button(actions, text='3. Save reviewed & export', style='Accent.TButton',
                             command=lambda: save('export'))
    save_button.pack(side='right')

    listing = ttk.Frame(body)
    listing.grid(row=4, column=0, sticky='nsew')
    listing.columnconfigure(0, weight=1)
    listing.rowconfigure(0, weight=1)
    tree = ttk.Treeview(listing, columns=('use', 'case', 'category', 'plan'), show='headings',
                        height=6, selectmode='browse')
    for name, title, width in [('use', 'Reviewed', 75), ('case', 'Object', 75),
                                ('category', 'AI suggestion — check before saving', 330), ('plan', 'Plan', 320)]:
        tree.heading(name, text=title)
        tree.column(name, width=width, stretch=name in {'category', 'plan'})
    tree.grid(row=0, column=0, sticky='nsew')
    scrollbar = ttk.Scrollbar(listing, orient='vertical', command=tree.yview)
    scrollbar.grid(row=0, column=1, sticky='ns')
    tree.configure(yscrollcommand=scrollbar.set)

    preview_panel = ttk.LabelFrame(body, text='2. Inspect the selected object', padding=8)
    preview_panel.grid(row=5, column=0, sticky='nsew', pady=(8, 4))
    preview_panel.columnconfigure(1, weight=1)
    preview_panel.rowconfigure(1, weight=1)
    ttk.Label(preview_panel, textvariable=selection_title, wraplength=1000).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 5))
    canvas = tk.Canvas(preview_panel, width=256, height=256, bg='#181d22', highlightthickness=0)
    canvas.grid(row=1, column=0, sticky='nw')
    detail = ScrolledText(preview_panel, wrap='word', width=55, height=9, state='disabled')
    detail.grid(row=1, column=1, sticky='nsew', padx=(10, 0))
    ttk.Label(preview_panel, text='Orange box = target object (local preview only). Clicking a row does not approve it.',
              style='Muted.TLabel').grid(row=2, column=0, columnspan=2, sticky='w', pady=(5, 0))

    more = ttk.Frame(body)
    more.grid(row=7, column=0, sticky='ew', pady=3)
    ttk.Label(more, textvariable=usage, wraplength=1000).pack(anchor='w')
    ttk.Checkbutton(body, text='More options / manual categories', variable=advanced,
                     command=lambda: extras.grid() if advanced.get() else extras.grid_remove()).grid(row=6, column=0, sticky='w')
    extras = ttk.Frame(body)
    extras.grid(row=8, column=0, sticky='ew')
    extras.grid_remove()
    privacy_tools=ttk.Frame(extras); privacy_tools.pack(fill='x')
    data_tools=ttk.Frame(extras); data_tools.pack(fill='x',pady=3)
    save_tools=ttk.Frame(extras); save_tools.pack(fill='x')
    ttk.Checkbutton(privacy_tools, text='Redact name/location labels', variable=redact, command=lambda: rebuild()).pack(side='left')
    ttk.Checkbutton(privacy_tools, text='Group connected objects (max 40)', variable=grouped, command=lambda: rebuild()).pack(side='left')
    ttk.Button(data_tools, text='Inspect outbound', command=lambda: inspect()).pack(side='left', padx=3)
    ttk.Button(data_tools, text='Save evidence', command=lambda: export_evidence()).pack(side='left', padx=3)
    ttk.Button(data_tools, text='Import reply', command=lambda: import_reply()).pack(side='left', padx=3)
    ttk.Button(data_tools, text='Manual categories', command=lambda: manual()).pack(side='left', padx=3)
    ttk.Button(data_tools, text='Create accuracy sample', command=lambda: benchmark_sample()).pack(side='left',padx=3)
    ttk.Button(data_tools, text='Score verified sample', command=lambda: benchmark_score()).pack(side='left',padx=3)
    save_only_button = ttk.Button(save_tools, text='Save only', command=lambda: save('only'))
    save_only_button.pack(side='left', padx=3)
    next_button = ttk.Button(save_tools, text='Save & next batch', command=lambda: save('next'))
    next_button.pack(side='left', padx=3)
    ttk.Button(save_tools, text='Skip batch', command=lambda: skip_batch()).pack(side='left', padx=3)

    def prepared_status():
        coverage = converter.classification_metrics(review['summary']).get('classification_coverage_out_of_100')
        score = f'{coverage:.2f}%' if coverage is not None else 'not assessed'
        applied = converter.classification_metrics(review['summary'])['ai_review_mappings_applied']
        status.set(f'Current coverage: {score}. {applied} AI-reviewed mappings applied. {len(review["unclassified"]):,} objects unresolved. '
                   f'{len(package["cases"])} cases covering {package.get("objects_in_batch",len(package["cases"]))} objects prepared; nothing sent.')
        if not has_result:
            titles = {str(r.get('plan_id') or ''): r.get('plan_title') or 'Drawing' for r in review['records']}
            tree.delete(*tree.get_children())
            for case in package['cases']:
                tree.insert('', 'end', iid=case['case_id'], values=('—', f"{case['case_id']} ×{case.get('member_count',1)}", 'Not sent to AI yet', titles.get(case['plan_id'], 'Drawing')))
            if package['cases']:
                tree.selection_set(package['cases'][0]['case_id']); select()

    def refresh_buttons():
        nonlocal poll_job
        if not dialog.winfo_exists():
            return
        busy = app.busy
        ask_button.configure(state='disabled' if busy or has_result or not package['cases'] else 'normal')
        for button in (save_button, save_only_button, next_button):
            button.configure(state='normal' if not busy and state.pending() else 'disabled')
        for button in (mark_button, clear_button):
            button.configure(state='normal' if not busy and state.proposals else 'disabled')
        poll_job = dialog.after(150, refresh_buttons)

    def render_checks():
        for proposal in state.proposals:
            cid = proposal['case_id']
            tree.set(cid, 'use', 'Saved' if cid in state.saved else '☑' if cid in state.approved else '☐')
        if has_result:
            status.set(f'{len(state.proposals)} suggestions returned. {len(state.pending())} marked reviewed. '
                       'Only checked suggestions will be saved; nothing is applied automatically.')

    def select(_=None):
        selected = tree.selection()
        if not selected:
            return
        proposal = next((p for p in state.proposals if p['case_id'] == selected[0]), None)
        case = next((c for c in package['cases'] if c['case_id'] == selected[0]), None)
        if case is None:
            return
        canvas.delete('all')
        canvas.image = tk.PhotoImage(data=case['image_base64']).subsample(2)
        canvas.create_image(0, 0, anchor='nw', image=canvas.image)
        xmin, ymin, xmax, ymax = case['context_bounds']
        scale = 492 / max(xmax-xmin, ymax-ymin, 1e-9)
        a, b, c, d = case['target_bounds']
        x1, y1 = (10+(a-xmin)*scale)/2, (502-(d-ymin)*scale)/2
        x2, y2 = (10+(c-xmin)*scale)/2, (502-(b-ymin)*scale)/2
        canvas.create_rectangle(x1-4, y1-4, x2+4, y2+4, outline='#ff9d42', width=2)
        label = ai.category_label(package, proposal['category']) if proposal else 'prepared locally — nothing sent'
        selection_title.set(f"{case['case_id']}: {label}")
        refs = [e for e in case['evidence'] if not proposal or e['evidence_id'] in proposal['evidence_ids']]
        explanation = f"This decision covers {case.get('member_count',1)} explicit object(s) in this plate. Mixed meanings must be skipped or reviewed individually.\n\n"
        explanation += (proposal['explanation'] if proposal else 'AI has not reviewed this object. It is still unresolved.')
        if proposal:
            explanation += f"\nModel confidence: {proposal.get('confidence','not supplied')} (not verified accuracy)."
        explanation += '\n\nSupporting evidence:\n'
        for row in refs:
            explanation += f"• {row['evidence_id']}: {row.get('text_value') or row.get('block_name') or row.get('entity_type', 'drawing object')}"
            if row.get('color_hex'):
                explanation += f"; colour {row['color_hex']}"
            explanation += '\n'
        detail.configure(state='normal'); detail.delete('1.0', 'end')
        detail.insert('end', explanation); detail.configure(state='disabled')

    def toggle(event=None):
        if app.busy:
            return
        cid = tree.identify_row(event.y) if event is not None and hasattr(event, 'y') else next(iter(tree.selection()), '')
        if cid:
            state.toggle(cid); render_checks()

    def click(event):
        if tree.identify_column(event.x) == '#1':
            toggle(event)
    tree.bind('<<TreeviewSelect>>', select)
    tree.bind('<ButtonRelease-1>', click)
    tree.bind('<space>', lambda event: toggle())

    def mark_all():
        if not app.busy:
            state.approve_all(); render_checks()

    def clear_checks():
        if not app.busy:
            state.approved.clear(); render_checks()

    def display(result):
        nonlocal has_result
        if not dialog.winfo_exists():
            return
        state.load(result.get('assignments', []))
        provenance.clear()
        provenance.update(provider=result.get('provider','Imported AI'), model=result.get('reported_model') or result.get('model',''))
        has_result = True
        seen.update((c['plan_id'], h) for c in package['cases'] for h in c.get('_members',[c['entity_handle']]))
        tree.delete(*tree.get_children())
        titles = {str(r.get('plan_id') or ''): r.get('plan_title') or 'Drawing' for r in review['records']}
        for proposal in state.proposals:
            cid = proposal['case_id']
            case = next(c for c in package['cases'] if c['case_id']==cid)
            tree.insert('', 'end', iid=cid, values=('☐', f"{cid} ×{case.get('member_count',1)}", ai.category_label(package, proposal['category']),
                                                    titles.get(proposal['plan_id'], 'Drawing')))
        render_checks()
        usage.set(f"{result.get('provider', 'Imported AI')} / {result.get('reported_model') or result.get('model', '')}. "
                  f"Usage: {result.get('usage', {})}. Abstained: {len(result.get('abstentions',[]))}; omitted: {len(result.get('omitted_case_ids',[]))}; rejected: {len(result.get('rejected', []))}.")
        if state.proposals:
            first = state.proposals[0]['case_id']
            tree.selection_set(first); tree.focus(first); select()
        else:
            status.set('No supported suggestions returned. No mappings were changed.')

    def change_provider(_=None):
        if app.busy:
            return
        key.set('')
        model.set(os.environ.get('MINEPLAN_GEMINI_MODEL', 'gemini-2.5-flash')
                  if provider.get() == 'Gemini' else os.environ.get('MINEPLAN_CLAUDE_MODEL', ''))
    providers.bind('<<ComboboxSelected>>', change_provider)

    def request():
        if app.busy or has_result:
            return
        chosen_provider = provider.get()
        secret = key.get().strip() or os.environ.get('GEMINI_API_KEY' if chosen_provider == 'Gemini' else 'ANTHROPIC_API_KEY', '').strip()
        chosen_model = model.get().strip()
        if not secret or not chosen_model:
            messagebox.showinfo('AI setup', 'Enter a key and model ID first. The key is kept only for this window.', parent=dialog)
            return
        privacy = ('Name/GPS labels are redacted. Shapes can still identify a site.' if package.get('redaction_enabled')
                   else 'Redaction is OFF: names, labels and location information may be shared.')
        if not messagebox.askyesno('Send this batch?',
            f"Send {len(package['cases'])} cropped review cases to {chosen_provider}?\n\n{privacy}\nAPI charges may apply. Suggestions will wait for your review.", parent=dialog):
            return
        approved = copy.deepcopy(package)
        send = ai.request_gemini if chosen_provider == 'Gemini' else ai.request_claude
        status.set('AI is reviewing this batch. No mappings have been changed.')
        app._run(lambda token, log, progress: send(approved, chosen_model, consent=True, api_key=secret), display)

    def close(notify=True):
        nonlocal poll_job
        if app.busy:
            return
        if poll_job is not None:
            dialog.after_cancel(poll_job)
            poll_job = None
        key.set('')
        if notify and state.proposals and not state.saved:
            app.status.set('AI suggestions were not saved. The drawing mappings are unchanged.')
        dialog.grab_release(); dialog.destroy()
    dialog.protocol('WM_DELETE_WINDOW', close)

    def prepare_next(fresh):
        nonlocal has_result
        review.clear(); review.update(fresh)
        eligible = [r for r in fresh['unclassified'] if (str(r.get('plan_id') or ''), str(r.get('entity_handle') or '')) not in seen]
        package.clear(); package.update(ai.build_package(options.input_path, dict(fresh, unclassified=eligible), redact=redact.get(),grouped=grouped.get()))
        has_result = False; state.load([]); tree.delete(*tree.get_children()); canvas.delete('all')
        detail.configure(state='normal'); detail.delete('1.0', 'end'); detail.configure(state='disabled')
        prepared_status()

    def skip_batch():
        if app.busy:
            return
        seen.update((c['plan_id'], h) for c in package['cases'] for h in c.get('_members',[c['entity_handle']]))
        prepare_next(dict(review))

    def save(action):
        if app.busy:
            return
        picked = state.pending()
        if not picked:
            messagebox.showinfo('Review suggestions', 'Tick Reviewed beside the suggestions you have checked first.', parent=dialog)
            return
        try:
            ai.apply_reviewed(options.input_path, package, picked,provenance=provenance)
        except (ValueError, OSError) as exc:
            messagebox.showerror('Save mappings', str(exc), parent=dialog)
            return
        state.mark_saved(picked); render_checks()
        count = sum(c.get('member_count',1) for c in package['cases'] if c['case_id'] in {p['case_id'] for p in picked})
        status.set(f'Saved {len(picked)} reviewed decisions covering {count} objects. Refreshing coverage…')
        def refreshed(fresh):
            metrics = converter.classification_metrics(fresh['summary'])
            coverage = metrics.get('classification_coverage_out_of_100')
            summary = (f"AI-reviewed mappings applied: {metrics['ai_review_mappings_applied']}\n"
                       f"Classification coverage: {coverage:.2f}/100\n"
                       f"Unresolved objects: {len(fresh['unclassified'])}\n\n"
                       'These are saved classifications, not independently verified accuracy.')
            app._set_text(app.report_text, summary); app.tabs.select(1)
            app.status.set(f'Saved {len(picked)} reviewed mappings. Coverage is now {coverage:.2f}%.')
            if action == 'next':
                prepare_next(fresh)
            else:
                close(notify=False)
                if action == 'export':
                    app.after_idle(app._export)
        app._run(lambda token, log, progress: converter.prepare_semantic_review(
            options, cancel=token, log=log, progress=progress), refreshed)

    def rebuild():
        if app.busy:
            redact.set(package.get('redaction_enabled', True))
            return
        if state.pending() and not messagebox.askyesno('Rebuild evidence?', 'Discard unsaved checks and rebuild with this redaction setting?', parent=dialog):
            redact.set(package.get('redaction_enabled', True)); return
        seen.clear()
        app._run(lambda token, log, progress: converter.prepare_semantic_review(
            options, cancel=token, log=log, progress=progress), prepare_next)

    def inspect():
        public = ai.public_package(package)
        window = tk.Toplevel(dialog); window.title('Outbound evidence — nothing sent'); window.geometry('850x600')
        text = ScrolledText(window, wrap='word'); text.pack(fill='both', expand=True)
        for case in public['cases']:
            case['image_base64'] = '[cropped PNG; see the object preview]'
        text.insert('end', json.dumps(public, indent=2, ensure_ascii=False)); text.configure(state='disabled')

    def export_evidence():
        path = filedialog.asksaveasfilename(parent=dialog, defaultextension='.json', initialfile='ai-evidence.json')
        if path:
            payload = {'evidence_package_id':ai.package_id(package), 'package':ai.public_package(package),
                       'response_format':{'evidence_package_id':ai.package_id(package), 'assignments':[]},
                       'instructions':ai.SYSTEM, 'response_schema':ai.proposal_schema(package)}
            converter._atomic_bytes(path, json.dumps(payload, indent=2).encode())

    def import_reply():
        path = filedialog.askopenfilename(parent=dialog, filetypes=[('JSON', '*.json')])
        if not path:
            return
        try:
            if Path(path).stat().st_size > 4*1024**2:
                raise ValueError('Response file is too large.')
            result = json.loads(Path(path).read_text(encoding='utf-8'))
            if result.get('evidence_package_id') != ai.package_id(package):
                raise ValueError('Response does not match this evidence package.')
            valid, rejected = ai.validate_proposals(package, result)
            display({'assignments':valid, 'rejected':rejected})
        except (ValueError, OSError) as exc:
            messagebox.showerror('Import proposals', str(exc), parent=dialog)

    def manual():
        if app.busy:
            return
        close(notify=False)
        app._open_review_dialog(review, options)

    def benchmark_sample():
        if app.busy:
            return
        path=filedialog.asksaveasfilename(parent=dialog,title='Create local accuracy reference sample',
                 initialfile=Path(options.input_path).stem+'-accuracy-sample.json',defaultextension='.json',
                 filetypes=[('JSON reference manifest','*.json')])
        if not path:
            return
        import classification_benchmark as benchmark
        def ready(document):
            status.set(f"Created {len(document['samples'])} local cases. Ask an independent reviewer to fill {document['labels_csv']}. No accuracy is claimed yet.")
        app._run(lambda token,log,progress: benchmark.create_sample(options.input_path,review,path),ready)

    def benchmark_score():
        if app.busy:
            return
        sample=filedialog.askopenfilename(parent=dialog,title='Choose verified sample JSON (labels CSV is read alongside it)',
                                         filetypes=[('JSON reference manifest','*.json')])
        if not sample:
            return
        output=filedialog.asksaveasfilename(parent=dialog,title='Save accuracy report',defaultextension='.json',
                                            initialfile='classification-accuracy-report.json')
        if not output:
            return
        if Path(output).exists():
            messagebox.showerror('Accuracy report','Choose a new report filename.',parent=dialog); return
        def work(token,log,progress):
            import classification_benchmark as benchmark
            document=json.loads(Path(sample).read_text(encoding='utf-8'))
            fresh=converter.prepare_semantic_review(options,cancel=token,log=log,progress=progress)
            result=benchmark.evaluate(options.input_path,document,fresh)
            converter._atomic_bytes(output,json.dumps(result,indent=2).encode())
            return result
        def ready(result):
            accuracy = 'not measured — independent labels needed' if result['accuracy_percent'] is None else f"{result['accuracy_percent']}%"
            status.set(f"Verified labels: {result['verified_reference_records']}/{result['sample_records']}. "
                       f"Accuracy on those labels: {accuracy}. Holdout: {result['holdout_status']}. Report: {output}")
        app._run(work,ready)

    # Local UI smoke checks can exercise display/review without a paid request.
    dialog._mineplan_ai = {'display':display, 'state':state, 'tree':tree, 'canvas':canvas,
                          'save_button':save_button, 'save':save, 'mark_all':mark_all,
                          'details':detail, 'package':package, 'skip':skip_batch}
    prepared_status(); refresh_buttons()
    return dialog
