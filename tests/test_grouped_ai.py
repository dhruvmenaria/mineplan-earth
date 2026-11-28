import base64
import json
import pytest
from shapely.geometry import LineString, Point
import ai_groups
import ai_classification as ai
import semantic_layers as sem
import review_rules
from spatial_index import RecordIndex


def record(i, x=None, **changes):
    x=i if x is None else x
    row={'entity_type':'LINE','entity_handle':f'H{i}','plan_id':'p1','plan_title':'Surface Plan',
         'cad_layer':'Surface Plan / 0','original_cad_layer':'0','color_hex':'#12ABCD','linetype':'CONTINUOUS',
         'geometry':LineString([(x,0),(x+1,0)]),'semantic_category':'UNCLASSIFIED - REVIEW'}
    row.update(changes)
    return row


def review(rows):
    return {'records':rows,'unclassified':rows,'summary':{'allowed_legend_categories':['EXIST. PIT']}}


def test_connected_groups_are_bounded_and_never_claim_a_whole_style():
    rows=[record(i) for i in range(85)]+[record(100,x=1000)]
    groups=ai_groups.group_targets(review(rows))
    assert [len(g) for g in groups]==[40,40,5,1]
    assert sorted(r['entity_handle'] for g in groups for r in g)==sorted(r['entity_handle'] for r in rows)


def test_groups_do_not_cross_plan_layer_style_year_or_ambiguous_legend():
    rows=[record(0),record(1),record(2,plan_id='p2'),record(3,source_year='II'),
          record(4,original_cad_layer='OTHER'),record(5,color_hex='#FF0000')]
    assert sorted(map(len,ai_groups.group_targets(review(rows))))==[1,1,1,1,2]
    data=review(rows)
    data['summary']['source_legend']={'observations':[
        {'plan_id':'p1','signature':sem.style_signature(rows[0]),'category':category}
        for category in ['EXIST. PIT','EXIST. ROAD']]}
    assert all(len(g)==1 for g in ai_groups.group_targets(data))


def test_group_approval_saves_only_explicit_members_and_preserves_geometry(tmp_path):
    source=tmp_path/'source.dxf'; source.write_text('source')
    rows=[record(i) for i in range(10)]
    original=[r['geometry'].wkb for r in rows]
    package=ai.build_package(source,review(rows),limit=1)
    case=package['cases'][0]
    assert case['member_count']==10 and package['objects_in_batch']==10
    assert '_members' not in json.dumps(ai.public_package(package))
    proposal={'case_id':'C1','category':'EXIST. PIT','evidence_ids':['E0'],'explanation':'Reviewer checked every connected member.'}
    ai.apply_reviewed(source,package,[proposal],tmp_path/'rules')
    rules=review_rules.load_rules(source,tmp_path/'rules')
    assert rules[0]['scope']=='entity_group'
    extra=record(10)
    output,summary=sem.classify_records(rows+[extra],manual_rules=rules)
    assert summary['ai_review_mappings_applied']==10
    assert all(r['semantic_category']=='EXIST. PIT' for r in output[:10])
    assert output[-1]['semantic_category']=='UNCLASSIFIED - REVIEW'
    assert [r['geometry'].wkb for r in rows]==original


def test_reclassifying_one_member_keeps_the_rest_of_a_reviewed_group(tmp_path):
    source=tmp_path/'source.dxf'; source.write_text('source')
    rows=[record(i) for i in range(3)]
    package=ai.build_package(source,review(rows))
    p={'case_id':'C1','category':'EXIST. PIT','evidence_ids':['E0'],'explanation':'Checked.'}
    ai.apply_reviewed(source,package,[p],tmp_path/'rules')
    single=ai.build_package(source,review([rows[1]]),grouped=False)
    single['cases'][0]['allowed_categories'].append('EXIST. ROAD')
    ai.apply_reviewed(source,single,[dict(p,category='EXIST. ROAD')],tmp_path/'rules')
    output,_=sem.classify_records(rows,manual_rules=review_rules.load_rules(source,tmp_path/'rules'))
    assert [r['semantic_category'] for r in output]==['EXIST. PIT','EXIST. ROAD','EXIST. PIT']


def test_spatial_lookup_matches_brute_force_with_stable_order():
    rows=[record(i,x=i*7,plan_id='p1' if i%2 else 'p2') for i in range(50)]
    index=RecordIndex(rows)
    target=Point(82,0)
    expected=[r for r in rows if r['plan_id']=='p1' and r['geometry'].distance(target)<=20]
    assert index.near('p1',target,20)==expected
    assert index.near('absent',target,100)==[]


def test_mining_terms_survive_redaction_while_names_and_coordinates_do_not():
    for label in ['CRUSHER PLANT','WEIGHBRIDGE','EXPLOSIVE MAGAZINE','CHECK DAM','TOE WALL','RL 340','KHASRA','SUMP']:
        assert ai._safe_label(label)==label
    for label in ['JOHN SHARMA CRUSHER PLANT','राम सिंह','N26°44\'56.2 E75°13\'37.99','27.12345, 73.23456','500000,2700000','owner@example.com']:
        assert ai._safe_label(label)=='[redacted label]'


def test_schema_has_enums_and_abstentions_are_counted_not_saved(tmp_path):
    source=tmp_path/'source.dxf'; source.write_text('source')
    package=ai.build_package(source,review([record(0),record(2,x=20)]))
    schema=ai.proposal_schema(package)['properties']['assignments']['items']
    assert schema['properties']['category']['enum']==['ABSTAIN','EXIST. PIT']
    abstain={'case_id':'C1','category':'ABSTAIN','decision':'abstain','confidence':'LOW',
             'evidence_ids':[],'explanation':'Conflicting meanings in this crop.'}
    raw={'assignments':[abstain]}
    accepted,rejected=ai.validate_proposals(package,raw)
    assert accepted==[] and rejected==[]
    result=ai.decision_summary(package,raw,accepted)
    assert result['abstentions'][0]['case_id']=='C1'
    assert result['omitted_case_ids']==['C2']
    with pytest.raises(ValueError):
        ai.apply_reviewed(source,package,[abstain],tmp_path/'rules')


@pytest.mark.parametrize('change',[{'case_id':[]},{'confidence':[]},{'confidence':0.99}])
def test_bad_abstention_shapes_are_rejected_safely(tmp_path,change):
    source=tmp_path/'source.dxf'; source.write_text('source')
    package=ai.build_package(source,review([record(0)]))
    p={'case_id':'C1','category':'ABSTAIN','decision':'abstain','confidence':'LOW','evidence_ids':[],
       'explanation':'Unclear.'}; p.update(change)
    accepted,rejected=ai.validate_proposals(package,{'assignments':[p]})
    assert not accepted and rejected


def test_source_change_between_analysis_and_package_is_rejected(tmp_path):
    source=tmp_path/'source.dxf'; source.write_text('old')
    data=review([record(0)]); data['source_sha256']=review_rules.source_digest(source)
    source.write_text('new')
    with pytest.raises(ValueError,match='changed after analysis'):
        ai.build_package(source,data)


def test_provider_assignments_need_explicit_tiers_and_target_citation(tmp_path):
    source=tmp_path/'source.dxf'; source.write_text('source')
    package=ai.build_package(source,review([record(0)]))
    p={'case_id':'C1','category':'EXIST. PIT','decision':'assign','confidence':'HIGH',
       'evidence_ids':['E0'],'explanation':'Target and source evidence agree.'}
    assert ai.validate_proposals(package,{'assignments':[p]},require_decisions=True)[0]
    for change in [{'evidence_ids':[]},{'confidence':'not_supplied'},{'confidence':0.99}]:
        accepted,rejected=ai.validate_proposals(package,{'assignments':[dict(p,**change)]},require_decisions=True)
        assert not accepted and rejected


def test_group_rule_does_not_follow_changed_year_evidence(tmp_path):
    source=tmp_path/'source.dxf'; source.write_text('source')
    rows=[record(0),record(1)]
    package=ai.build_package(source,review(rows))
    ai.apply_reviewed(source,package,[{'case_id':'C1','category':'EXIST. PIT','evidence_ids':['E0'],
                                     'explanation':'Reviewed explicit members.'}],tmp_path/'rules')
    changed=[dict(r,source_year='II') for r in rows]
    output,_=sem.classify_records(changed,manual_rules=review_rules.load_rules(source,tmp_path/'rules'))
    assert all(r['semantic_category']=='UNCLASSIFIED - REVIEW' for r in output)


def test_network_image_cap_is_enforced_before_transmission(tmp_path,monkeypatch):
    source=tmp_path/'source.dxf'; source.write_text('source')
    package=ai.build_package(source,review([record(0)]))
    package['cases']=package['cases']*21
    monkeypatch.setattr(ai.urllib.request,'urlopen',lambda *a,**kw:pytest.fail('Unexpected network request'))
    with pytest.raises(ValueError,match='20 cases'):
        ai.request_claude(package,consent=True,api_key='test')
    with pytest.raises(ValueError,match='20 cases'):
        ai.request_gemini(package,consent=True,api_key='test')
