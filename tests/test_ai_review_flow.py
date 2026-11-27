import json
import ezdxf

import ai_classification as ai
import converter
import review_rules


def test_selection_is_not_approval_and_new_batches_reset_checks():
    proposal={'case_id':'C1','category':'CONTOUR','evidence_ids':['E0'],'explanation':'Verified by the reviewer.'}
    session=ai.ProposalReview()
    session.load([proposal])
    assert session.pending()==[]
    session.toggle('missing')
    assert session.pending()==[]
    session.toggle('C1')
    assert session.pending()==[proposal]
    session.mark_saved(session.pending())
    assert session.pending()==[]
    session.toggle('C1')
    assert session.pending()==[]
    session.load([dict(proposal,category='EXIST. PIT')])
    assert session.pending()==[]
    session.approve_all()
    assert session.pending()[0]['category']=='EXIST. PIT'


def test_export_reports_saved_ai_mappings_and_coverage_change(tmp_path,monkeypatch):
    monkeypatch.setattr(review_rules,'RULE_DIRECTORY',tmp_path/'rules')
    source=tmp_path/'drawing.dxf'
    doc=ezdxf.new()
    line=doc.modelspace().add_line((500000,2700000),(500020,2700010))
    doc.saveas(source)
    original=source.read_bytes()
    options=converter.Options(str(source),source_crs='EPSG:32643',location_confirmed=True)
    review=converter.prepare_semantic_review(options)
    package=ai.build_package(source,review)
    before=converter.classification_metrics(review['summary'])
    assert before['ai_review_mappings_applied']==0
    proposal={'case_id':'C1','category':'CONTOUR','evidence_ids':['E0'],
              'explanation':'Reviewer confirmed the source contour.'}
    ai.apply_reviewed(source,package,[proposal])
    result=converter.convert(options,tmp_path/'reviewed.kmz')
    metrics=result['report']['quality_metrics']
    assert metrics['ai_review_rules_loaded']==1
    assert metrics['ai_review_mappings_applied']==1
    assert metrics['coverage_before_review_rules']==0
    assert metrics['classification_coverage_out_of_100']==100
    assert metrics['independently_verified_accuracy_out_of_100'] is None
    persisted=json.loads(open(result['report_path'],encoding='utf-8').read())
    assert persisted['quality_metrics']['ai_review_mappings_applied']==1
    assert source.read_bytes()==original
