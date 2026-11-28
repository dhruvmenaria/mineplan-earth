import copy
import pytest
from shapely.geometry import LineString
import classification_benchmark as benchmark


def fixture(tmp_path):
    source=tmp_path/'source.dxf'; source.write_text('drawing')
    rows=[{'plan_id':'p1','entity_handle':str(i),'entity_type':'LINE',
           'geometry':LineString([(i*20,0),(i*20+5,0)]),'semantic_category':category,
           'color_hex':'#FFFFFF'} for i,category in enumerate(['CONTOUR','EXIST. PIT','UNCLASSIFIED - REVIEW'])]
    review={'records':rows,'summary':{'manual_rules_loaded':0}}
    document=benchmark.create_sample(source,review,tmp_path/'sample.json',count=100)
    return source,review,document


def test_reference_labels_start_blank_and_approvals_are_not_gold(tmp_path):
    source,review,document=fixture(tmp_path)
    assert all(r['reference_category'] is None and not r['verified_by'] for r in document['samples'])
    report=benchmark.evaluate(source,document,review)
    assert report['accuracy_percent'] is None
    assert report['status']=='awaiting_independent_reference_labels'
    assert report['holdout_status']=='training_inventory_not_declared'
    assert len(list((tmp_path/'sample.assets').glob('*.png')))==3


def test_measured_precision_accuracy_and_abstention_are_separate(tmp_path):
    source,review,document=fixture(tmp_path)
    for row in document['samples']:
        row.update(reference_category='CONTOUR',verified_by='Independent mining reviewer')
    report=benchmark.evaluate(source,document,review)
    assert report['accuracy_percent']==33.33
    assert report['precision_among_assignments_percent']==50
    assert report['classification_coverage_percent']==66.67
    assert report['abstentions']==1
    assert report['per_category']['EXIST. PIT']['false_positive']==1
    assert report['per_category']['CONTOUR']['false_negative']==2


def test_holdout_is_by_whole_source_and_saved_reviews_are_flagged(tmp_path):
    source,review,document=fixture(tmp_path)
    assert benchmark.evaluate(source,document,review,training_sources=[source])['holdout_status']=='training_drawing_overlap'
    other=tmp_path/'train.dxf'; other.write_text('different drawing')
    assert benchmark.evaluate(source,document,review,training_sources=[other])['holdout_status']=='eligible_declared_drawing_holdout'
    review['summary']['manual_rules_loaded']=1
    assert benchmark.evaluate(source,document,review,training_sources=[other])['holdout_status']=='human_assisted_source'


def test_stale_or_duplicate_references_never_produce_a_score(tmp_path):
    source,review,document=fixture(tmp_path)
    duplicate=copy.deepcopy(document); duplicate['samples'].append(duplicate['samples'][0])
    with pytest.raises(ValueError,match='Duplicate'):
        benchmark.evaluate(source,duplicate,review)
    source.write_text('changed')
    with pytest.raises(ValueError,match='exact source'):
        benchmark.evaluate(source,document,review)


def test_reference_csv_can_be_filled_without_editing_json(tmp_path):
    import csv
    source,review,document=fixture(tmp_path)
    path=document['labels_csv']
    with open(path,encoding='utf-8-sig',newline='') as stream:
        rows=list(csv.DictReader(stream))
    rows[0].update(reference_category='CONTOUR',verified_by='Independent reviewer')
    with open(path,'w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    report=benchmark.evaluate(source,document,review)
    assert report['verified_reference_records']==1
    assert report['status']=='partial_reference_sample'
    document['samples'][0]['reference_category']='EXIST. PIT'
    with pytest.raises(ValueError,match='conflict'):
        benchmark.evaluate(source,document,review)
