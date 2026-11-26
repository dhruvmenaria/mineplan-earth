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
                "input":{"assignments":[proposal()]}}],
                "usage":{"input_tokens":100,"output_tokens":20}}).encode()
    def transport(request,**kwargs):
        assert request.full_url == "https://api.anthropic.com/v1/messages"
        body = json.loads(request.data)
        assert body["model"] == "configured-model"
        assert body["tools"][0]["name"] == "submit_classifications"
        assert "test-key" not in json.dumps(body)
        return Response()
    monkeypatch.setattr(ai.urllib.request,"urlopen",transport)
    response = ai.request_claude(package)
    assert response["assignments"][0]["entity_handle"] == "A1"
    assert response["usage"]["input_tokens"] == 100
