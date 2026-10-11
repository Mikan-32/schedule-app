import asyncio
import io
import json
import os
import socket
import sys
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm import runtime as rt
from app.llm.runtime import (
    EmbeddedOllama,
    HttpRangeFile,
    Progress,
    download_runtime,
    import_model,
    manifest_relpath,
    pull_model,
    select_members,
)

FAKE_SERVER = Path(__file__).resolve().parent / "fake_ollama.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------- 設定 ----------
def test_runtime_mode_and_url(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert Settings(llm_runtime="auto").llm_runtime_mode == "embedded"
    assert Settings(llm_runtime="auto", embedded_ollama_port=12345).ollama_url == "http://127.0.0.1:12345"
    s = Settings(llm_runtime="external", ollama_base_url="http://gpu-pc:11434")
    assert s.llm_runtime_mode == "external" and s.ollama_url == "http://gpu-pc:11434"
    monkeypatch.setattr(sys, "platform", "linux")
    assert Settings(llm_runtime="auto").llm_runtime_mode == "external"
    assert Settings(llm_runtime="embedded").llm_runtime_mode == "embedded"


# ---------- 実行ファイル（zip の必要な部分だけ読む） ----------
def make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("lib/ollama/", "")
        z.writestr("lib/ollama/cuda_v12/ggml-cuda.dll", os.urandom(3_000_000), zipfile.ZIP_STORED)
        z.writestr("lib/ollama/cuda_v13/ggml-cuda.dll", os.urandom(2_000_000), zipfile.ZIP_STORED)
        z.writestr("lib/ollama/ggml-base.dll", b"base" * 1000, zipfile.ZIP_DEFLATED)
        z.writestr("lib/ollama/vulkan/ggml-vulkan.dll", b"vk" * 1000, zipfile.ZIP_DEFLATED)
        z.writestr("ollama.exe", b"MZ" + b"x" * 50_000, zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def range_transport(data: bytes, served: list[int]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "github.com":  # 公式の URL は期限付きの URL へリダイレクトされる
            return httpx.Response(302, headers={"Location": "https://objects.example.com/ollama.zip?sig=1"})
        start, end = request.headers["Range"].removeprefix("bytes=").split("-")
        start, end = int(start), min(int(end), len(data) - 1)
        body = data[start : end + 1]
        served.append(len(body))
        return httpx.Response(206, content=body, headers={"Content-Range": f"bytes {start}-{end}/{len(data)}"})

    return httpx.MockTransport(handler)


def test_select_members():
    names = [i.filename for i in zipfile.ZipFile(io.BytesIO(make_zip())).infolist()]
    infos = zipfile.ZipFile(io.BytesIO(make_zip())).infolist()
    cpu = [i.filename for i in select_members(infos, "cpu")]
    assert cpu == ["lib/ollama/ggml-base.dll", "lib/ollama/vulkan/ggml-vulkan.dll", "ollama.exe"]
    nvidia = [i.filename for i in select_members(infos, "nvidia")]
    assert "lib/ollama/cuda_v12/ggml-cuda.dll" in nvidia and len(nvidia) == len(names) - 1  # ディレクトリ以外すべて


def test_download_runtime_fetches_only_needed_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(HttpRangeFile, "BLOCK", 64 * 1024)
    data = make_zip()
    served: list[int] = []
    progress = Progress()
    dest = tmp_path / "bin" / "v1-cpu"
    url = rt.RELEASE_URL.format(version="v1", arch="amd64")
    with httpx.Client(transport=range_transport(data, served)) as client:
        exe = download_runtime(url, dest, "cpu", client, progress)
    assert exe == dest / "ollama.exe" and exe.read_bytes().startswith(b"MZ")
    assert (dest / "lib/ollama/ggml-base.dll").read_bytes() == b"base" * 1000
    assert (dest / "lib/ollama/vulkan/ggml-vulkan.dll").exists()
    assert not (dest / "lib/ollama/cuda_v12").exists()  # GPU 用のライブラリは取り出さない
    assert (dest / rt.COMPLETE_MARK).exists() and not dest.with_name("v1-cpu.part").exists()
    # zip 全体（約 5MB）ではなく、必要な部分だけを読んでいる
    assert sum(served) < 600_000 < len(data)
    assert progress.total > 0 and progress.done == progress.total


def test_download_runtime_with_nvidia_includes_cuda(tmp_path, monkeypatch):
    monkeypatch.setattr(HttpRangeFile, "BLOCK", 64 * 1024)
    data = make_zip()
    dest = tmp_path / "v1-nvidia"
    with httpx.Client(transport=range_transport(data, [])) as client:
        download_runtime("https://github.com/x.zip", dest, "nvidia", client, Progress())
    assert (dest / "lib/ollama/cuda_v12/ggml-cuda.dll").stat().st_size == 3_000_000


# ---------- モデル ----------
def test_manifest_relpath():
    assert str(manifest_relpath("qwen3-vl:8b-instruct")) == "registry.ollama.ai/library/qwen3-vl/8b-instruct"
    assert str(manifest_relpath("gemma3")) == "registry.ollama.ai/library/gemma3/latest"
    assert str(manifest_relpath("someone/model:q4")) == "registry.ollama.ai/someone/model/q4"
    assert str(manifest_relpath("hf.co/org/repo:Q4_K_M")) == "hf.co/org/repo/Q4_K_M"


def make_store(root: Path, name: str, blobs: dict[str, bytes]) -> None:
    digests = list(blobs)
    manifest = {"config": {"digest": digests[0]}, "layers": [{"digest": d} for d in digests[1:]]}
    path = root / "manifests" / manifest_relpath(name)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(manifest))
    (root / "blobs").mkdir(parents=True)
    for d, content in blobs.items():
        (root / "blobs" / d.replace(":", "-")).write_bytes(content)


def test_import_model_links_blobs(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_store(src, "qwen3-vl:8b-instruct", {"sha256:aaa": b"config", "sha256:bbb": b"weights" * 100})
    progress = Progress()
    assert import_model("qwen3-vl:8b-instruct", src, dst, progress)
    blob = dst / "blobs" / "sha256-bbb"
    assert blob.read_bytes() == b"weights" * 100
    assert os.stat(blob).st_ino == os.stat(src / "blobs" / "sha256-bbb").st_ino  # ハードリンク（容量が増えない）
    assert (dst / "manifests" / manifest_relpath("qwen3-vl:8b-instruct")).exists()
    assert progress.done == progress.total == len(b"config") + len(b"weights" * 100)
    # 入っていないモデル・ファイルが欠けているモデルは取り込まない
    assert not import_model("gemma3:12b", src, dst)
    (src / "blobs" / "sha256-bbb").unlink()
    assert not import_model("qwen3-vl:8b-instruct", src, tmp_path / "dst2")
    assert not (tmp_path / "dst2" / "manifests").exists()


def test_import_model_copies_when_link_fails(tmp_path, monkeypatch):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_store(src, "gemma3:4b", {"sha256:c": b"c", "sha256:d": b"d" * 10})

    def no_link(a, b):
        raise OSError("別のドライブ")

    monkeypatch.setattr(rt.os, "link", no_link)
    assert import_model("gemma3:4b", src, dst)
    assert (dst / "blobs" / "sha256-d").read_bytes() == b"d" * 10


async def test_pull_model_progress_and_error():
    def handler(request: httpx.Request) -> httpx.Response:
        name = json.loads(request.content)["model"]
        if name == "missing:latest":
            return httpx.Response(200, text='{"status":"pulling manifest"}\n{"error":"file does not exist"}\n')
        lines = [
            {"status": "pulling manifest"},
            {"status": "pulling a", "digest": "sha256:a", "total": 1000, "completed": 500},
            {"status": "pulling b", "digest": "sha256:b", "total": 3000, "completed": 0},
            {"status": "pulling a", "digest": "sha256:a", "total": 1000, "completed": 1000},
            {"status": "verifying sha256 digest"},
            {"status": "success"},
        ]
        return httpx.Response(200, text="".join(json.dumps(x) + "\n" for x in lines))

    async with httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(handler)) as client:
        progress = Progress()
        await pull_model(client, "qwen3-vl:8b-instruct", progress)
        assert progress.total == 4000 and progress.done == 4000
        with pytest.raises(RuntimeError, match="file does not exist"):
            await pull_model(client, "missing:latest", Progress())


# ---------- 起動・停止・再起動（偽の Ollama で確かめる） ----------
async def wait_for(cond, timeout=20.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("時間内に状態が変わりませんでした")


def embedded_settings(tmp_path: Path, **kw) -> Settings:
    return Settings(
        db_path=tmp_path / "t.db",
        discord_token="",
        llm_runtime="embedded",
        embedded_ollama_dir=tmp_path / "ollama",
        embedded_ollama_port=free_port(),
        embedded_model="qwen3-vl:8b-instruct",
        **kw,
    )


async def test_embedded_lifecycle_pulls_model_and_restarts(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "no-such-store"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")  # 本物の ~/.ollama は見ない
    settings = embedded_settings(tmp_path)
    runtime = EmbeddedOllama(settings, command=[sys.executable, str(FAKE_SERVER)])
    runtime.WATCH_SEC = 0.2
    ready = []

    async def on_ready():
        ready.append(runtime.state)

    runtime.on_ready.append(on_ready)
    runtime.start()
    try:
        await wait_for(lambda: runtime.state in ("running", "error"))
        assert runtime.state == "running", runtime.message
        assert ready == ["running"]
        pulled = (settings.embedded_ollama_dir / "models" / "pulled.txt").read_text().split()
        assert pulled == ["qwen3-vl:8b-instruct"]  # モデルがなかったのでダウンロードした
        status = runtime.status()
        assert status["mode"] == "embedded" and status["url"] == settings.ollama_url

        # Ollama が落ちたら起動し直す
        old = runtime._proc
        old.kill()
        await wait_for(lambda: runtime._proc is not old and len(ready) == 2)
        assert runtime.state == "running"
        assert (settings.embedded_ollama_dir / "models" / "pulled.txt").read_text().split() == pulled  # 2 回目は pull しない
    finally:
        proc = runtime._proc
        await runtime.stop()
    assert runtime.state == "stopped" and proc.poll() is not None


async def test_embedded_imports_model_from_installed_ollama(tmp_path, monkeypatch):
    store = tmp_path / "installed" / "models"
    make_store(store, "qwen3-vl:8b-instruct", {"sha256:aaa": b"config", "sha256:bbb": b"weights"})
    monkeypatch.setenv("OLLAMA_MODELS", str(store))
    settings = embedded_settings(tmp_path)
    runtime = EmbeddedOllama(settings, command=[sys.executable, str(FAKE_SERVER)])
    runtime.start()
    try:
        await wait_for(lambda: runtime.state in ("running", "error"))
        assert runtime.state == "running", runtime.message
        models = settings.embedded_ollama_dir / "models"
        assert (models / "blobs" / "sha256-bbb").read_bytes() == b"weights"
        assert not (models / "pulled.txt").exists()  # 取り込めたのでダウンロードしない
    finally:
        await runtime.stop()


async def test_embedded_reports_error_when_port_taken(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    settings = embedded_settings(tmp_path)
    with socket.socket() as s:  # Ollama ではないものがポートを使っている
        s.bind(("127.0.0.1", settings.embedded_ollama_port))
        s.listen()
        runtime = EmbeddedOllama(settings, command=[sys.executable, str(FAKE_SERVER)])
        runtime.start()
        try:
            await wait_for(lambda: runtime.state == "error")
            assert "EMBEDDED_OLLAMA_PORT" in runtime.message
        finally:
            await runtime.stop()


async def test_embedded_requires_windows_without_command(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    runtime = EmbeddedOllama(embedded_settings(tmp_path))
    runtime.start()
    await wait_for(lambda: runtime.state == "error")
    assert "LLM_RUNTIME=external" in runtime.message
    await runtime.stop()


def test_status_api_reports_runtime(tmp_path):
    from app.main import create_app
    from tests.test_api import FakeLLM

    settings = Settings(db_path=tmp_path / "t.db", discord_token="", llm_runtime="external")
    with TestClient(create_app(settings, llm=FakeLLM(), start_connectors=False)) as c:
        llm = c.get("/api/status").json()["llm"]
        assert llm["runtime"] == {"mode": "external"} and llm["text_model"] == "fake"
        assert c.post("/api/llm/refresh").json()["runtime"]["mode"] == "external"
