import base64
import json
import pytest
from shapely.geometry import LineString, Point

import ai_classification as ai
import review_rules


def fixture(tmp_path):
    source = tmp_path / "drawing.dxf"
    source.write_text("unchanged source",encoding="utf-8")
    line = {"entity_type":"LINE","entity_handle":"A1","plan_id":"p1", "cad_layer":"0",
            "geometry":LineString([(0,0),(10,10)]), "semantic_category":"UNCLASSIFIED - REVIEW", "color_hex":"#00FFFF"}
    label = {"entity_type":"TEXT","entity_handle":"A2","plan_id":"p1", "cad_layer":"0",
             "geometry":Point(5,5), "text_value":"PROP. REJECT DUMP", "color_hex":"#00FFFF"}
    review = {"records":[line,label],"unclassified":[line],
              "summary":{"allowed_legend_categories":["PROP. MINERAL REJECT DUMP"]}}
    package = ai.build_package(source,review)
    return source,package


def proposal():
    return {"case_id":"C1","category":"PROP. MINERAL REJECT DUMP","evidence_ids":["E1"],
            "explanation":"The adjacent source label identifies a proposed reject dump."}


def test_offline_package_renders_png_and_keeps_target_identity(tmp_path):
    source,package = fixture(tmp_path)
    case = package["cases"][0]
    assert case["entity_handle"] == "A1"
    assert case["evidence"][0]["entity_handle"] == "A1"
    assert base64.b64decode(case["image_base64"]).startswith(b'\x89PNG\r\n\x1a\n')
    assert str(source) not in json.dumps(package)


def test_invalid_and_conflicting_proposals_are_rejected(tmp_path):
    _,package = fixture(tmp_path)
    for change in ({"category":"INVENTED"},{"case_id":"C999"},{"evidence_ids":["E999"]},{"coordinates":[1,2]}):
        accepted,rejected = ai.validate_proposals(package,{"assignments":[dict(proposal(),**change)]})
        assert not accepted and rejected
    accepted,rejected = ai.validate_proposals(package,{"assignments":[proposal(),proposal()]})
    assert not accepted and rejected


def test_only_reviewed_rules_are_saved_and_changed_source_is_rejected(tmp_path):
    source,package = fixture(tmp_path)
    rules_dir = tmp_path / "rules"
    ai.apply_reviewed(source,package,[proposal()],rules_dir)
    saved = review_rules.load_rules(source,rules_dir)
    assert saved[0]["scope"] == "entity"
    assert saved[0]["entity_handle"] == "A1"
    source.write_text("changed source",encoding="utf-8")
    with pytest.raises(ValueError,match="changed"):
        ai.apply_reviewed(source,package,[proposal()],rules_dir)


def test_unconfigured_claude_never_makes_a_network_request(tmp_path,monkeypatch):
    _,package = fixture(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY",raising=False)
    monkeypatch.delenv("MINEPLAN_CLAUDE_MODEL",raising=False)
    monkeypatch.setattr(ai.urllib.request,"urlopen",lambda *a,**kw: pytest.fail("Unexpected network call"))
    with pytest.raises(ValueError,match="not configured"):
        ai.request_claude(package)


def test_mock_claude_response_is_validated_without_executing_tools(tmp_path,monkeypatch):
    _,package = fixture(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY","test-key")
    monkeypatch.setenv("MINEPLAN_CLAUDE_MODEL","configured-model")
    class Response:
        def __enter__(self):
            return self
        def __exit__(self,*args):
            pass
        def read(self,*args):
            return json.dumps({"stop_reason":"tool_use","content":[{
                "type":"tool_use","name":"submit_classifications",
                "input":{"assignments":[dict(proposal(),decision='assign',confidence='HIGH',evidence_ids=['E0','E1'])]}}],
                "usage":{"input_tokens":100,"output_tokens":20}}).encode()
    def transport(request,**kwargs):
        assert request.full_url == "https://api.anthropic.com/v1/messages"
        body = json.loads(request.data)
        assert body["model"] == "configured-model"
        assert body["tools"][0]["name"] == "submit_classifications"
        assert body['tools'][0]['strict'] is True
        assert 'ABSTAIN' in body['tools'][0]['input_schema']['properties']['assignments']['items']['properties']['category']['enum']
        assert "test-key" not in json.dumps(body)
        assert 'entity_handle' not in json.dumps(body['messages'])
        assert 'source_sha256' not in json.dumps(body['messages'])
        assert '_category_labels' not in json.dumps(body['messages'])
        return Response()
    monkeypatch.setattr(ai.urllib.request,"urlopen",transport)
    response = ai.request_claude(package, consent=True)
    assert response["assignments"][0]["entity_handle"] == "A1"
    assert response["usage"]["input_tokens"] == 100


def test_configured_api_cannot_send_without_job_consent(tmp_path, monkeypatch):
    _, package = fixture(tmp_path)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
    monkeypatch.setenv('MINEPLAN_CLAUDE_MODEL', 'test-model')
    monkeypatch.setattr(ai.urllib.request, 'urlopen', lambda *a, **kw: pytest.fail('Unexpected network call'))
    with pytest.raises(ValueError, match='consent'):
        ai.request_claude(package)


def test_compact_redaction_removes_names_gps_world_bounds_and_private_mappings(tmp_path):
    source, _ = fixture(tmp_path)
    line = {'entity_type':'LINE', 'entity_handle':'A1', 'plan_id':'PRIVATE_MINE_NAME',
            'plan_title':'PRIVATE_MINE_NAME', 'geometry':LineString([(500000.123456,2700000.654321),(500010,2700010)]),
            'semantic_category':'UNCLASSIFIED - REVIEW', 'color_hex':'#00FFFF'}
    labels = [dict(line, entity_type='TEXT', entity_handle=f'L{i}', geometry=Point(500005,2700005),
                   text_value=text) for i, text in enumerate([
                       'N26°44\'56.2" E75°13\'37.99"', 'PRIVATE_MINE_NAME',
                       '27.123456, 73.234567', 'PROP. REJECT DUMP'] * 30)]
    legend = [{'plan_id':'PRIVATE_MINE_NAME', 'category':'PRIVATE_MINE_NAME',
               'label':'PRIVATE_MINE_NAME','label_position':[500000,2700000],
               'signature':['LINE','#00FFFF','PRIVATE_MINE_NAME'], 'pairing_confidence':'HIGH'}]
    review = {'records':[line,*labels], 'unclassified':[line],
              'summary':{'source_legend':{'observations':legend}}}
    package = ai.build_package(source, review)
    public = ai.public_package(package)
    serialized = json.dumps(public, ensure_ascii=False)
    for value in ['PRIVATE_MINE_NAME', '26°', '27.123456', '500000', '2700000', '_category_labels', 'source_sha256']:
        assert value not in serialized
    case = public['cases'][0]
    assert len(case['evidence']) == 30
    assert case['context_truncated'] is True
    assert all('hatch_pattern' not in row for row in case['evidence'])
    assert case['target_bounds'][0:2] == [0,0]
    assert 'plan_title' not in case and 'entity_handle' not in case
    alias = case['allowed_categories'][0]
    reply = dict(proposal(), category=alias, evidence_ids=['E0'])
    ai.apply_reviewed(source, package, [reply], tmp_path/'rules')
    assert review_rules.load_rules(source,tmp_path/'rules')[0]['category'] == 'PRIVATE_MINE_NAME'
    unredacted = json.dumps(ai.public_package(ai.build_package(source,review,redact=False)), ensure_ascii=False)
    assert 'PRIVATE_MINE_NAME' in unredacted and '26°' in unredacted


def gemini_reply(assignments=None, finish='STOP'):
    return {'modelVersion':'gemini-2.5-flash','candidates':[{'finishReason':finish,
            'content':{'parts':[{'functionCall':{'name':'submit_classifications',
                        'args':{'assignments':[dict(proposal(),decision='assign',confidence='HIGH',evidence_ids=['E0','E1'])] if assignments is None else assignments}}}]}}],
            'usageMetadata':{'promptTokenCount':123,'candidatesTokenCount':45,'totalTokenCount':168}}


def test_gemini_forces_one_function_and_excludes_private_source_metadata(tmp_path,monkeypatch):
    _,package=fixture(tmp_path)
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,*args): return json.dumps(gemini_reply()).encode()
    def transport(request,**kwargs):
        assert request.full_url=='https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent'
        assert request.get_header('X-goog-api-key')=='temporary-test-key'
        body=json.loads(request.data)
        serialized=json.dumps(body)
        for private in ['temporary-test-key','source_sha256','entity_handle','_category_labels']:
            assert private not in serialized
        assert body['toolConfig']['functionCallingConfig']['allowedFunctionNames']==['submit_classifications']
        assert body['toolConfig']['functionCallingConfig']['mode']=='ANY'
        assert body['generationConfig']['thinkingConfig']['thinkingBudget']==0
        assert any('inlineData' in p for p in body['contents'][0]['parts'])
        return Response()
    monkeypatch.setattr(ai.urllib.request,'urlopen',transport)
    result=ai.request_gemini(package,consent=True,api_key='temporary-test-key')
    assert result['assignments'][0]['entity_handle']=='A1'
    assert result['reported_model']=='gemini-2.5-flash'
    assert result['usage']['input_tokens']==123
    assert result['usage']['output_tokens']==45


def test_gemini_never_sends_without_consent_or_valid_model(tmp_path,monkeypatch):
    _,package=fixture(tmp_path)
    monkeypatch.setattr(ai.urllib.request,'urlopen',lambda *a,**kw:pytest.fail('Unexpected request'))
    with pytest.raises(ValueError,match='consent'):
        ai.request_gemini(package,api_key='test')
    with pytest.raises(ValueError,match='model ID'):
        ai.request_gemini(package,model='https://elsewhere.example',consent=True,api_key='test')


@pytest.mark.parametrize('finish',['MAX_TOKENS','SAFETY','OTHER'])
def test_gemini_truncated_or_blocked_response_never_becomes_a_proposal(tmp_path,finish):
    _,package=fixture(tmp_path)
    with pytest.raises(ValueError,match='complete classification'):
        ai.parse_gemini_response(package,gemini_reply(finish=finish),'gemini-2.5-flash')


def test_gemini_geometry_edits_and_duplicates_are_rejected(tmp_path):
    _,package=fixture(tmp_path)
    response=gemini_reply([dict(proposal(),coordinates=[1,2])])
    result=ai.parse_gemini_response(package,response,'gemini-2.5-flash')
    assert not result['assignments'] and result['rejected']
    result=ai.parse_gemini_response(package,gemini_reply([proposal(),proposal()]),'gemini-2.5-flash')
    assert not result['assignments']


def test_gemini_http_failure_never_echoes_credentials(tmp_path,monkeypatch):
    import io
    _,package=fixture(tmp_path)
    def fail(*a,**kw):
        raise ai.urllib.error.HTTPError('https://generativelanguage.googleapis.com',403,
                                        'sensitive-test-key',{},io.BytesIO(b'sensitive-test-key'))
    monkeypatch.setattr(ai.urllib.request,'urlopen',fail)
    with pytest.raises(ValueError) as error:
        ai.request_gemini(package,consent=True,api_key='sensitive-test-key')
    assert 'sensitive-test-key' not in str(error.value)
    assert '403' in str(error.value)
