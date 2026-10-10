import json
from datetime import datetime

import httpx
import pytest

from app.config import Settings
from app.llm.ollama_client import LLMUnavailable, OllamaClient, rank_models
from app.llm.prompts import build_calendar
from app.models import ImageInput

TAGS = [
    {"name": "nomic-embed-text:latest", "details": {"parameter_size": "137M"}},
    {"name": "qwen3:8b", "details": {"parameter_size": "8.2B"}},
    {"name": "qwen3-vl:latest", "details": {"parameter_size": "8.8B"}},
    {"name": "mystery-model:70b", "details": {"parameter_size": "70B"}},
    {"name": "gemma3:27b-it-qat", "details": {"parameter_size": "27B"}},
]
CAPS = {
    "nomic-embed-text:latest": ["embedding"],
    "qwen3:8b": ["completion", "tools", "thinking"],
    "qwen3-vl:latest": ["completion", "vision", "thinking"],
    "mystery-model:70b": ["completion"],
    "gemma3:27b-it-qat": ["completion", "vision"],
}


def test_rank_models():
    assert rank_models(TAGS) == [
        "gemma3:27b-it-qat",
        "qwen3-vl:latest",
        "qwen3:8b",
        "mystery-model:70b",
        "nomic-embed-text:latest",
    ]


def make_transport(tags, caps, chat_reply, captured):
    def handler(request: httpx.Request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": tags})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": caps[json.loads(request.content)["model"]]})
        if request.url.path == "/api/chat":
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"message": {"role": "assistant", "content": json.dumps(chat_reply)}})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_selection_and_extract_with_image():
    captured = []
    reply = {"events": [{"title": "会議", "date": "2026-10-14", "start_time": "15:00", "end_time": "", "location": "", "description": "", "confidence": 0.9}]}
    tags = [t for t in TAGS if t["name"] != "gemma3:27b-it-qat"]
    client = OllamaClient(Settings(), transport=make_transport(tags, CAPS, reply, captured))
    sel = await client.refresh_selection()
    # qwen3-vl(8b) は qwen3:8b より上位、かつ画像対応
    assert sel.text_model == "qwen3-vl:latest" and sel.vision_model == "qwen3-vl:latest"

    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (3000, 1000), "white").save(buf, format="PNG")
    res = await client.extract("来週水曜15時から会議", datetime(2026, 10, 10, 9, 0), images=[ImageInput(buf.getvalue())])
    assert res.events[0].title == "会議"
    payload = captured[0]
    assert payload["format"]["required"] == ["events"]
    assert payload["think"] is False and payload["options"]["temperature"] == 0
    assert len(payload["messages"][1]["images"]) == 1
    assert "2026-10-14(水) 来週" in payload["messages"][1]["content"]
    await client.aclose()


async def test_text_only_model_drops_images():
    captured = []
    tags = [{"name": "qwen3:8b", "details": {}}]
    client = OllamaClient(Settings(), transport=make_transport(tags, CAPS, {"events": []}, captured))
    await client.extract("明日", datetime(2026, 10, 10), images=[ImageInput(b"\x89PNG")])
    assert "images" not in captured[0]["messages"][1]
    await client.aclose()


async def test_no_models():
    client = OllamaClient(Settings(), transport=make_transport([], {}, {}, []))
    with pytest.raises(LLMUnavailable):
        await client.extract("明日", datetime(2026, 10, 10))
    await client.aclose()


def test_calendar_labels():
    cal = build_calendar(datetime(2026, 10, 10)).splitlines()  # 土曜
    assert cal[0] == "2026-10-10(土) 今日 今週"
    assert cal[1] == "2026-10-11(日) 明日 今週"
    assert cal[2] == "2026-10-12(月) 明後日 来週"
    assert cal[9] == "2026-10-19(月) 再来週"
