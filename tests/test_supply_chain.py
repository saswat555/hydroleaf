# tests/test_supply_chain.py

import pytest
from fastapi import HTTPException
from app.services.supply_chain_service import extract_json_from_response

def test_extract_json_happy_path():
    s = 'ignore { "a": 1, "b": 2 } trailing'
    out = extract_json_from_response(s)
    assert out == {"a": 1, "b": 2}

def test_extract_json_multiple_json_blocks():
    s = 'prefix {"a":1} middle {"b":2} suffix'
    # should pick up the first JSON object only
    out = extract_json_from_response(s)
    assert out == {"a": 1}

def test_extract_json_nested_objects():
    s = 'foo {"outer":{"inner":123,"list":[1,2,3]}} bar'
    out = extract_json_from_response(s)
    assert out == {"outer": {"inner": 123, "list": [1, 2, 3]}}

def test_extract_json_unicode_and_escapes():
    s = 'pre { "text": "new\\nline", "emoji": "😃" } post'
    out = extract_json_from_response(s)
    # ensure escaped newline is unescaped and unicode preserved
    assert out["text"] == "new\nline"
    assert out["emoji"] == "😃"

def test_extract_json_throws_on_no_json():
    with pytest.raises(HTTPException):
        extract_json_from_response("no JSON here")

def test_extract_json_raises_on_malformed_json_block():
    # unmatched brace / unterminated JSON should error
    with pytest.raises(HTTPException):
        extract_json_from_response("oops { 'a': 1 ")


