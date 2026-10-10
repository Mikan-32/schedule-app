"""ローカルLLM (Ollama) クライアント。

インストール済みモデルの中から「最も精度が出そうなもの」を自動で選び、
Ollama の REST API (構造化出力) で予定を抽出する。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import time as _time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx

from ..config import Settings
from ..models import EXTRACTION_SCHEMA, ExtractionResult, ImageInput
from .prompts import SYSTEM_PROMPT, build_user_prompt

log = logging.getLogger(__name__)

# 日本語の読解 + 画像内文字認識(OCR) + JSON出力の精度が高いと見込まれる順（2026年10月時点）。
# 上から順に、インストール済みのものを採用する。リストにないモデルはパラメータ数の大きい順。
MODEL_PREFERENCE: list[str] = [
    "qwen3.8:27b",
    "qwen3.6:35b",
    "qwen3.6:27b",
    "qwen3-vl:32b",
    "qwen3-vl:30b",
    "gemma3:27b",
    "qwen3-vl:8b",
    "gemma3:12b",
    "qwen3:14b",
    "qwen3-vl:4b",
    "qwen3:8b",
    "gemma3:4b",
    "qwen3-vl:2b",
]

# ":latest" タグが実際にどのサイズを指しているか
LATEST_ALIASES: dict[str, str] = {
    "qwen3.8:latest": "qwen3.8:27b",
    "qwen3.6:latest": "qwen3.6:35b",
    "qwen3-vl:latest": "qwen3-vl:8b",
    "gemma3:latest": "gemma3:4b",
    "qwen3:latest": "qwen3:8b",
}


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class ModelSelection:
    text_model: Optional[str] = None
    vision_model: Optional[str] = None
    installed: list[str] = field(default_factory=list)
    error: str = ""
    selected_at: float = 0.0

    @property
    def available(self) -> bool:
        return self.text_model is not None


def _canonical(name: str) -> str:
    if ":" not in name:
        name = f"{name}:latest"
    return LATEST_ALIASES.get(name, name)


def _preference_rank(name: str) -> Optional[int]:
    canon = _canonical(name)
    for i, pref in enumerate(MODEL_PREFERENCE):
        # 量子化違い (例: qwen3-vl:8b-instruct-q8_0) も同じモデルとして扱う
        if canon == pref or canon.startswith(pref + "-"):
            return i
    return None


def _param_size(details: dict) -> float:
    raw = str(details.get("parameter_size", ""))
    m = re.match(r"([\d.]+)\s*([BbMm])", raw)
    if not m:
        return 0.0
    value = float(m.group(1))
    return value if m.group(2).lower() == "b" else value / 1000


def rank_models(tags: list[dict]) -> list[str]:
    """インストール済みモデルを精度が高いと見込まれる順に並べる。"""

    def key(m: dict):
        rank = _preference_rank(m["name"])
        if rank is not None:
            return (0, rank, 0.0)
        return (1, 0, -_param_size(m.get("details", {})))

    return [m["name"] for m in sorted(tags, key=key)]


def prepare_image(img: ImageInput, max_side: int) -> str:
    """画像を縮小して base64 文字列にする（大きすぎる画像は推論が遅くなるため）。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(img.data)) as im:
            im.load()
            fmt = "PNG" if (im.format or "").upper() in {"PNG", "GIF", "WEBP", "BMP"} else "JPEG"
            if max(im.size) > max_side:
                im.thumbnail((max_side, max_side))
            if fmt == "JPEG" and im.mode != "RGB":
                im = im.convert("RGB")
            elif fmt == "PNG" and im.mode not in ("RGB", "RGBA", "L", "LA"):
                im = im.convert("RGBA")
            buf = io.BytesIO()
            if fmt == "JPEG":
                im.save(buf, format=fmt, quality=90)
            else:
                im.save(buf, format=fmt)
            return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:  # Pillow が読めない形式でもそのまま送る
        log.warning("画像の前処理に失敗したため元データを送信します: %s", e)
        return base64.b64encode(img.data).decode()


def _parse_json(content: str) -> dict:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            return json.loads(m.group(0))
        raise


class OllamaClient:
    REFRESH_SEC = 300

    def __init__(self, settings: Settings, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.ollama_base_url,
            timeout=httpx.Timeout(settings.llm_timeout, connect=5.0),
            transport=transport,
        )
        self._caps: dict[str, list[str]] = {}
        self.selection = ModelSelection()
        self._lock = asyncio.Lock()
        self._busy = asyncio.Semaphore(1)  # ローカルGPUを奪い合わないよう直列実行

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------- モデル選択 ----------
    async def capabilities(self, model: str) -> list[str]:
        if model not in self._caps:
            r = await self._client.post("/api/show", json={"model": model})
            r.raise_for_status()
            data = r.json()
            caps = data.get("capabilities")
            if caps is None:  # 古い Ollama 向けの推定
                caps = ["completion"]
                if data.get("projector_info") or "clip" in json.dumps(data.get("details", {})):
                    caps.append("vision")
            self._caps[model] = caps
        return self._caps[model]

    async def refresh_selection(self) -> ModelSelection:
        async with self._lock:
            sel = ModelSelection(selected_at=_time.time())
            try:
                r = await self._client.get("/api/tags")
                r.raise_for_status()
                tags = r.json().get("models", [])
            except Exception as e:
                sel.error = f"Ollama に接続できません ({self.settings.ollama_base_url}): {e}"
                self.selection = sel
                return sel

            sel.installed = [m["name"] for m in tags]
            ranked = rank_models(tags)

            if self.settings.ollama_model != "auto":
                sel.text_model = self.settings.ollama_model
            if self.settings.ollama_vision_model != "auto":
                sel.vision_model = self.settings.ollama_vision_model

            for name in ranked:
                if sel.text_model and sel.vision_model:
                    break
                try:
                    caps = await self.capabilities(name)
                except Exception as e:
                    log.warning("モデル情報の取得に失敗: %s (%s)", name, e)
                    continue
                if "completion" not in caps:
                    continue  # 埋め込み専用モデルなど
                if sel.text_model is None:
                    sel.text_model = name
                if sel.vision_model is None and "vision" in caps:
                    sel.vision_model = name

            if not sel.text_model:
                sel.error = "利用できるモデルがありません。`ollama pull qwen3-vl:8b` などでモデルを取得してください。"
            log.info("LLM選択: text=%s vision=%s", sel.text_model, sel.vision_model)
            self.selection = sel
            return sel

    async def ensure_selection(self) -> ModelSelection:
        sel = self.selection
        if not sel.available or _time.time() - sel.selected_at > self.REFRESH_SEC:
            sel = await self.refresh_selection()
        return sel

    # ---------- 抽出 ----------
    async def extract(
        self,
        text: str,
        sent_at: datetime,
        author: str = "",
        channel: str = "",
        images: Optional[list[ImageInput]] = None,
    ) -> ExtractionResult:
        sel = await self.ensure_selection()
        if not sel.available:
            raise LLMUnavailable(sel.error)

        images = images or []
        model = sel.text_model
        if images:
            if sel.vision_model:
                model = sel.vision_model
            else:
                log.warning("画像対応モデルがないため、画像を無視してテキストのみ精査します")
                images = []

        user_msg: dict = {
            "role": "user",
            "content": build_user_prompt(text, sent_at, author, channel, len(images)),
        }
        if images:
            user_msg["images"] = [prepare_image(img, self.settings.max_image_side) for img in images]

        payload: dict = {
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, user_msg],
            "stream": False,
            "format": EXTRACTION_SCHEMA,
            "options": {"temperature": 0},
            "keep_alive": "15m",
        }
        try:
            if "thinking" in await self.capabilities(model):
                payload["think"] = self.settings.llm_think
        except Exception:
            pass

        last_error: Exception | None = None
        async with self._busy:
            for attempt in range(2):
                try:
                    r = await self._client.post("/api/chat", json=payload)
                    r.raise_for_status()
                    content = r.json()["message"]["content"]
                    return ExtractionResult.model_validate(_parse_json(content))
                except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                    self.selection = ModelSelection(error=str(e))
                    raise LLMUnavailable(f"Ollama に接続できません: {e}") from e
                except Exception as e:
                    last_error = e
                    log.warning("LLM応答の解析に失敗 (試行%d): %s", attempt + 1, e)
        raise RuntimeError(f"LLMからの抽出に失敗しました: {last_error}")

    def status(self) -> dict:
        s = self.selection
        return {
            "base_url": self.settings.ollama_base_url,
            "available": s.available,
            "text_model": s.text_model,
            "vision_model": s.vision_model,
            "installed": s.installed,
            "error": s.error,
        }
