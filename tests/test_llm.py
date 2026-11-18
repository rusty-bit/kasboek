import json

import httpx
import pytest

from app import llm

CATS = [{"key": "groceries", "label": "Groceries", "kind": "expense"},
        {"key": "eating_out", "label": "Eating out & takeaway", "kind": "expense"},
        {"key": "salary", "label": "Salary", "kind": "income"},
        {"key": "other", "label": "Other spending", "kind": "expense"},
        {"key": "transfer", "label": "Between own accounts", "kind": "transfer"}]
ITEMS = [{"key": ("bakker bart", "out"), "name": "Bakker Bart", "description": "", "typical_amount": 5, "count": 3},
         {"key": ("indurad gmbh", "in"), "name": "indurad GmbH", "description": "Lohn 10/2026",
          "typical_amount": 3500, "count": 2},
         {"key": ("pizza roma", "out"), "name": "Pizza Roma", "description": "", "typical_amount": 25, "count": 1}]


def test_only_local_endpoints_are_allowed():
    for ok in ["http://localhost:11434/v1", "http://127.0.0.1:11434", "http://192.168.178.20:11434/v1",
               "http://10.0.0.5:8080", "http://aibox:11434", "http://aibox.local:11434", "http://172.16.1.2"]:
        assert llm.is_local_url(ok), ok
    for bad in ["https://api.openai.com/v1", "http://8.8.8.8:11434", "https://ollama.example.com", "", "ftp://x"]:
        assert not llm.is_local_url(bad), bad


def test_prompt_contains_only_safe_fields():
    msgs = llm.build_messages(ITEMS, CATS)
    text = json.dumps(msgs)
    assert "groceries" in text and "Bakker Bart" in text and "money out" in text
    assert "IBAN" not in text and "NL0" not in text


def test_parse_answer_keeps_only_valid_and_sensible_keys():
    answer = json.dumps({"1": "groceries", "2": "salary", "3": "not_a_category"})
    got = llm.parse_answer(answer, ITEMS, CATS)
    assert got == {("bakker bart", "out"): "groceries", ("indurad gmbh", "in"): "salary"}
    # an income category for money out (or the other way round) is rejected
    assert llm.parse_answer(json.dumps({"1": "salary", "2": "groceries"}), ITEMS, CATS) == {}
    # a fenced or chatty answer still works; garbage gives nothing
    assert llm.parse_answer('Sure!\n```json\n{"3": "eating_out"}\n```', ITEMS, CATS) == {("pizza roma", "out"): "eating_out"}
    assert llm.parse_answer("no idea", ITEMS, CATS) == {}


def test_suggest_calls_openai_compatible_endpoint():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        body = json.loads(request.content)
        seen["model"] = body["model"]
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"1": "groceries", "3": "eating_out"}'}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    got = llm.suggest(ITEMS, CATS, base_url="http://localhost:11434/v1", model="qwen2.5:7b", client=client)
    assert seen["url"] == "http://localhost:11434/v1/chat/completions" and seen["model"] == "qwen2.5:7b"
    assert got == {("bakker bart", "out"): "groceries", ("pizza roma", "out"): "eating_out"}


def test_suggest_refuses_a_cloud_url():
    with pytest.raises(llm.LLMError):
        llm.suggest(ITEMS, CATS, base_url="https://api.openai.com/v1", model="gpt")
