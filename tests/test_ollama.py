import json
from datetime import datetime

import httpx
import pytest

from app.config import Settings
from app.llm.ollama_client import LLMUnavailable, OllamaClient, rank_models
from app.llm.prompts import SYSTEM_PROMPT, build_calendar, build_user_prompt
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


def test_rank_models_prefers_non_thinking_variant():
    tags = [
        {"name": "qwen3-vl:8b", "details": {"parameter_size": "8.8B"}},
        {"name": "qwen3-vl:4b-instruct", "details": {"parameter_size": "4.4B"}},
        {"name": "qwen3-vl:8b-instruct", "details": {"parameter_size": "8.8B"}},
    ]
    # 同じ順位（qwen3-vl:8b 系）の中では思考型を後ろへ。下位のモデルより前には出さない
    assert rank_models(tags, {"qwen3-vl:8b"}) == ["qwen3-vl:8b-instruct", "qwen3-vl:8b", "qwen3-vl:4b-instruct"]
    assert rank_models(tags) == ["qwen3-vl:8b", "qwen3-vl:8b-instruct", "qwen3-vl:4b-instruct"]


async def test_selection_avoids_thinking_model_unless_llm_think():
    tags = [
        {"name": "qwen3-vl:8b", "details": {"parameter_size": "8.8B"}},
        {"name": "qwen3-vl:8b-instruct", "details": {"parameter_size": "8.8B"}},
    ]
    caps = {"qwen3-vl:8b": ["completion", "vision", "tools", "thinking"], "qwen3-vl:8b-instruct": ["completion", "vision", "tools"]}
    client = OllamaClient(Settings(), transport=make_transport(tags, caps, {}, []))
    sel = await client.refresh_selection()
    assert sel.text_model == "qwen3-vl:8b-instruct" and sel.vision_model == "qwen3-vl:8b-instruct"
    await client.aclose()

    settings = Settings()
    settings.llm_think = True
    client = OllamaClient(settings, transport=make_transport(tags, caps, {}, []))
    sel = await client.refresh_selection()
    assert sel.text_model == "qwen3-vl:8b"
    await client.aclose()


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
    # Ollama の既定（4096）では大きな画像が入りきらないので広げる。毎回同じ値（変わるとモデルを読み込み直す）
    assert payload["options"]["num_ctx"] == 8192
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


def test_system_prompt_extracts_invitations():
    # 誘い・提案も日付が分かれば予定として扱う（利用者の返事は Windows 通知では見えないため）
    assert "明日遊ぼう" in SYSTEM_PROMPT
    assert "仮定や検討中の話は抽出しない" not in SYSTEM_PROMPT
    assert "15日と16日どっちがいい？" in SYSTEM_PROMPT  # 候補日が決まっていない話は引き続き抽出しない


async def test_image_requests_wait_longer_and_timeouts_are_not_retried():
    # 画像付きは CPU だけの PC だと数分かかるので、LLM_IMAGE_TIMEOUT まで待つ。時間切れは同じ要求をやり直さない
    waits, chats = [], []

    def handler(request: httpx.Request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3-vl:8b-instruct", "details": {}}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "vision"]})
        waits.append(request.extensions["timeout"]["read"])
        chats.append(1)
        if len(chats) == 2:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"message": {"role": "assistant", "content": '{"events": []}'}})

    client = OllamaClient(Settings(llm_timeout=300, llm_image_timeout=900), transport=httpx.MockTransport(handler))
    await client.extract("明日14時から会議", datetime(2026, 10, 10))
    with pytest.raises(RuntimeError, match="900 秒以内"):
        await client.extract("", datetime(2026, 10, 10), images=[ImageInput(b"\x89PNG")])
    assert waits == [300, 900] and len(chats) == 2
    await client.aclose()


@pytest.mark.parametrize(
    "text, images, expected",
    [
        ("", 1, "添付画像に書かれている内容を本文として読み取る"),
        ("", 0, "【メッセージ本文】\n（本文なし）"),
        ("BBQのお知らせです", 1, "【メッセージ本文】\nBBQのお知らせです"),
    ],
)
def test_prompt_treats_image_as_message_body(text, images, expected):
    prompt = build_user_prompt(text, datetime(2026, 10, 11, 14, 0), image_count=images)
    assert expected in prompt
    assert ("【添付画像】" in prompt) == bool(images)
