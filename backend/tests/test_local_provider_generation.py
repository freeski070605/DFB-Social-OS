import json
from datetime import datetime, timezone

import pytest

from app.ai import providers
from app.core.errors import ProviderError
from app.schemas.domain import AIConfig, GeneratedContent


def test_ollama_request_serializes_parent_and_accepts_fenced_structured_output(monkeypatch):
    seen = {}
    content = {
        "topic": "7 things to do tonight to make tomorrow easier", "pillar": "routines & weekly resets",
        "format": "carousel", "hook": "Tomorrow gets easier when you stop leaving everything for tomorrow.",
        "slides": [{"title": "Tonight", "body": "Prepare one useful thing.", "kind": "cover", "items": []}],
        "caption": "Try a short reset tonight.", "knowledge_refs": [216],
    }

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "```json\n" + json.dumps(content) + "\n```"}}

    class Client:
        def __init__(self, **kwargs):
            seen["timeout"] = kwargs["timeout"]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, json):
            seen["url"], seen["payload"] = url, json
            return Response()

    monkeypatch.setattr(providers.httpx, "Client", Client)
    parent = {"id": 4, "created_at": datetime(2026, 9, 29, tzinfo=timezone.utc)}
    result = providers.LocalAIProvider(AIConfig()).generate("Create carousel", {
        "parent": parent, "request": {"format": "carousel"}}, GeneratedContent)
    assert result.slides[0].title == "Tonight" and result.knowledge_refs == [216]
    assert seen["url"].endswith("/api/chat") and seen["payload"]["model"] == "qwen2.5:7b"
    assert seen["payload"]["stream"] is False and isinstance(seen["payload"]["format"], dict)
    assert seen["payload"]["format"]["properties"]["slides"]["minItems"] == 1
    assert "slides" in seen["payload"]["format"]["required"]
    assert json.loads(seen["payload"]["messages"][1]["content"])["parent"]["created_at"].startswith("2026-09-29")


def test_invalid_model_output_gives_parse_error_without_persistence(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "```json\n{not json}\n```"}}

    class Client:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(providers.httpx, "Client", Client)
    with pytest.raises(ProviderError, match="invalid structured output"):
        providers.LocalAIProvider(AIConfig()).generate("Create carousel", {}, GeneratedContent)


def test_generated_carousel_without_slides_is_invalid():
    with pytest.raises(ValueError, match="at least one slide"):
        GeneratedContent.model_validate({
            "topic": "7 things to do tonight to make tomorrow easier", "pillar": "routines & weekly resets",
            "format": "carousel", "hook": "Tomorrow gets easier", "caption": "A short reset helps.",
            "slides": [], "knowledge_refs": [216],
        })
