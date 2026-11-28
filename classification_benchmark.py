"""Local, independently labelled classification samples; never calls an AI API."""
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import random

import ai_classification as ai
import review_rules
import semantic_layers
from spatial_index import RecordIndex


def record_key(row):
    geometry = row.get('geometry')
    payload = [str(row.get('plan_id') or ''),str(row.get('entity_handle') or ''),
               str(row.get('entity_type') or ''),str(row.get('text_value') or ''),
               geometry.wkb_hex if geometry is not None else '']
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def prediction(row):
    value = row.get('semantic_category')
    return value if value and value != 'UNCLASSIFIED - REVIEW' and not row.get('feature_role_review_required') else None


def create_sample(source, review, output, *, count=100, seed=17):
    source, output = Path(source).resolve(), Path(output).resolve()
    assets = output.with_suffix('.assets')
    labels = output.with_suffix('.labels.csv')
    if output.exists() or assets.exists() or labels.exists():
        raise ValueError('Choose a new sample filename; existing reference labels/assets will not be replaced.')
    if not 1 <= count <= 1000:
        raise ValueError('Sample count must be between 1 and 1000.')
    source_hash = review_rules.source_digest(source)
    if review.get('source_sha256') and review['source_sha256'] != source_hash:
        raise ValueError('The drawing changed after review. Prepare a fresh sample.')
    keys = Counter(record_key(r) for r in review['records'])
    eligible = sorted((r for r in review['records'] if keys[record_key(r)]==1
                       and r.get('geometry') is not None and not r['geometry'].is_empty),key=record_key)
    selected = random.Random(seed).sample(eligible,min(count,len(eligible)))
    selected.sort(key=record_key)
    output.parent.mkdir(parents=True,exist_ok=True)
    assets.mkdir()
    index = RecordIndex(review['records'])
    samples = []
    for i,row in enumerate(selected):
        g = row['geometry']
        xmin,ymin,xmax,ymax = g.bounds
        margin = max(10,float(row.get('text_height') or 1)*10,max(xmax-xmin,ymax-ymin)*0.2)
        bounds = [xmin-margin,ymin-margin,xmax+margin,ymax+margin]
        context = index.near(row.get('plan_id'),g,margin)
        context.sort(key=lambda r:(not bool(r.get('text_value')),r['geometry'].distance(g)))
        context = [row]+[r for r in context if r is not row][:29]
        image = assets/f'{i+1:04d}.png'
        image.write_bytes(ai._png(context,bounds,target_geometry=g))
        samples.append({'sample_id':f'S{i+1:04d}','record_key':record_key(row),
                        'plan':row.get('plan_title'),'entity_handle':row.get('entity_handle'),
                        'entity_type':row.get('entity_type'),'source_label':row.get('text_value'),
                        'preview':str(image),'prediction_at_sampling':prediction(row),
                        'nearby_labels':[r['text_value'] for r in context if r.get('text_value')],
                        'reference_category':None,'verified_by':'','verification_note':''})
    with labels.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['sample_id','plan','entity_handle','preview',
                                               'reference_category','verified_by','verification_note'])
        writer.writeheader()
        writer.writerows({k:row.get(k) or '' for k in writer.fieldnames} for row in samples)
    document={'schema_version':1,'source_path':str(source),'source_sha256':source_hash,'labels_csv':str(labels),
              'seed':seed,'requested_count':count,'eligible_records':len(eligible),
              'ambiguous_record_identities_excluded':sum(v for v in keys.values() if v>1),
              'source_has_saved_reviews':bool(review['summary'].get('manual_rules_loaded')),
              'category_choices':sorted(set(semantic_layers.available_categories()) | {prediction(r) for r in review['records'] if prediction(r)}),
              'instructions':'Independent reviewer: inspect each preview AND source drawing/legend. Fill reference_category and verified_by in the labels CSV using canonical category_choices where appropriate. Never copy an AI suggestion as verified truth. Files are private/local and are not uploaded.',
              'samples':samples}
    import converter
    converter._atomic_bytes(output,json.dumps(document,ensure_ascii=False,indent=2).encode('utf-8'))
    return document


def evaluate(source, document, review, *, training_sources=None):
    if document.get('schema_version')!=1 or review_rules.source_digest(source)!=document.get('source_sha256'):
        raise ValueError('The reference sample does not match this exact source drawing. Generate and verify a new sample.')
    rows={}
    for row in review['records']:
        key=record_key(row)
        if key in rows:
            rows[key]=None
        else:
            rows[key]=row
    seen=set(); results=[]
    csv_labels={}
    labels_path=Path(document['labels_csv']) if document.get('labels_csv') else None
    if labels_path and labels_path.is_file():
        known={s['sample_id'] for s in document.get('samples',[])}
        with labels_path.open(encoding='utf-8-sig',newline='') as stream:
            for label in csv.DictReader(stream):
                sid=label.get('sample_id')
                if sid not in known or sid in csv_labels:
                    raise ValueError('Unknown or duplicate sample identity in reference CSV.')
                csv_labels[sid]=label
    for sample in document.get('samples',[]):
        key=sample.get('record_key')
        if key in seen:
            raise ValueError('Duplicate reference sample identity.')
        seen.add(key)
        if key not in rows or rows[key] is None:
            raise ValueError('A sampled object cannot be uniquely matched; do not guess its reference label.')
        csv_label=csv_labels.get(sample['sample_id'],{})
        expected=sample.get('reference_category')
        if str(csv_label.get('reference_category') or '').strip():
            if expected and expected.strip()!=csv_label['reference_category'].strip():
                raise ValueError('CSV and JSON reference labels conflict; resolve them before scoring.')
            expected=csv_label['reference_category']
        verifier=csv_label.get('verified_by') or sample.get('verified_by')
        if not isinstance(expected,str) or not expected.strip() or not str(verifier or '').strip():
            continue
        expected=expected.strip()
        if expected in {'ABSTAIN','UNCLASSIFIED - REVIEW'}:
            raise ValueError('A gold reference must identify the correct category; unresolved reference labels are not ground truth.')
        actual=prediction(rows[key])
        results.append({'sample_id':sample['sample_id'],'expected':expected,'predicted':actual,'correct':actual==expected})
    n=len(results); assigned=sum(r['predicted'] is not None for r in results); correct=sum(r['correct'] for r in results)
    per_category={}
    for category in sorted({r['expected'] for r in results}|{r['predicted'] for r in results if r['predicted']}):
        tp=sum(r['correct'] and r['expected']==category for r in results)
        fp=sum(r['predicted']==category and r['expected']!=category for r in results)
        fn=sum(r['expected']==category and r['predicted']!=category for r in results)
        per_category[category]={'correct':tp,'false_positive':fp,'false_negative':fn,
                                'precision':tp/(tp+fp) if tp+fp else None,
                                'recall':tp/(tp+fn) if tp+fn else None}
    hashes={review_rules.source_digest(p) for p in (training_sources or [])}
    assisted=bool(document.get('source_has_saved_reviews') or review['summary'].get('manual_rules_loaded'))
    holdout=('human_assisted_source' if assisted else 'training_drawing_overlap' if document['source_sha256'] in hashes
             else 'eligible_declared_drawing_holdout' if hashes else 'training_inventory_not_declared')
    return {'status':('awaiting_independent_reference_labels' if not n else
                      'partial_reference_sample' if n<len(document.get('samples',[])) else 'measured_on_verified_rows'),
            'source_sha256':document['source_sha256'],'sample_records':len(document.get('samples',[])),
            'verified_reference_records':n,'unverified_reference_records':len(document.get('samples',[]))-n,
            'classification_coverage_percent':round(100*assigned/n,2) if n else None,
            'accuracy_percent':round(100*correct/n,2) if n else None,
            'precision_among_assignments_percent':round(100*correct/assigned,2) if assigned else None,
            'abstentions':n-assigned,'per_category':per_category,'results':results,
            'holdout_status':holdout,'reference_labels_are_human_supplied':True,
            'scope':'Classification of sampled extracted records only; does not verify coordinates, omitted source objects or regulatory compliance.'}
