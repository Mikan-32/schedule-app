import asyncio
import gzip
import hashlib
import io
import json
import os
import socket
import sys
import tarfile
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
    download_archive,
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
    # Mac・Linux も同梱の Ollama を使う。それ以外の OS は自分でインストールした Ollama
    for name in ("darwin", "linux"):
        monkeypatch.setattr(sys, "platform", name)
        assert Settings(llm_runtime="auto").llm_runtime_mode == "embedded"
    monkeypatch.setattr(sys, "platform", "freebsd14")
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
    monkeypatch.setattr(rt, "SYSTEM_MODEL_DIRS", ())  # Linux のサービスとして入れた Ollama も見ない
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


async def test_embedded_unsupported_os_without_command(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "freebsd14")  # 公式の配布ファイルがない OS
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


# ---------- Mac・Linux の配布ファイル（tar.gz / tar.zst を順に読み、必要なファイルだけ書き出す） ----------
needs_symlinks = pytest.mark.skipif(sys.platform == "win32", reason="Windows ではシンボリックリンクを作れないことがある（Mac・Linux 用の処理）")


def zstd_compress(data: bytes) -> bytes:
    try:
        from compression import zstd  # Python 3.14 以降

        return zstd.compress(data)
    except ImportError:
        zstandard = pytest.importorskip("zstandard")
        return zstandard.ZstdCompressor().compress(data)


def make_tar(entries, kind: str) -> bytes:
    """entries: (名前, 中身) のリスト。中身は bytes（ファイル）/ None（ディレクトリ）/ ("link", 先)（シンボリックリンク）。"""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for name, content, mode in entries:
            info = tarfile.TarInfo(name)
            info.mode = mode
            if content is None:
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            elif isinstance(content, tuple):
                info.type = tarfile.SYMTYPE
                info.linkname = content[1]
                tf.addfile(info)
            else:
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
    data = raw.getvalue()
    return gzip.compress(data) if kind == "tgz" else zstd_compress(data)


# Linux の配布ファイルと同じ形（bin/ollama と lib/ollama/。GPU 用は cuda_v12 などのフォルダ）
LINUX_ENTRIES = [
    ("./", None, 0o755),
    ("./bin/ollama", b"\x7fELF" + b"o" * 1000, 0o755),
    ("lib/ollama/cuda_v12", None, 0o755),
    ("lib/ollama/cuda_v12/libggml-cuda.so", os.urandom(200_000), 0o755),
    ("lib/ollama/cuda_v12/libcublas.so.12", ("link", "libcublas.so.12.8.5.5"), 0o777),
    ("lib/ollama/libggml-base.so", ("link", "libggml-base.so.0"), 0o777),
    ("lib/ollama/libggml-base.so.0", ("link", "libggml-base.so.0.25.3"), 0o777),
    ("lib/ollama/libggml-base.so.0.25.3", b"base" * 100, 0o755),
    ("lib/ollama/libggml-cpu-haswell.so", b"cpu" * 100, 0o644),
    ("lib/ollama/vulkan/libggml-vulkan.so", b"vk" * 100, 0o755),
]
# macOS の配布ファイルと同じ形（すべて最上位。Apple シリコン用の mlx_metal_* と、付随ファイルの ._*）
MAC_ENTRIES = [
    ("ollama", b"\xcf\xfa\xed\xfe" + b"m" * 1000, 0o755),
    ("libggml-base.0.25.3.dylib", b"base", 0o755),
    ("libggml-base.0.dylib", ("link", "libggml-base.0.25.3.dylib"), 0o755),
    ("mlx_metal_v4", None, 0o755),
    ("mlx_metal_v4/mlx.metallib", b"metal" * 100, 0o644),
    ("mlx_metal_v4/._mlx.metallib", b"appledouble", 0o644),
]


def archive_transport(files: dict[str, bytes], requested: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if request.url.host == "github.com":  # 公式の URL は期限付きの URL へリダイレクトされる
            return httpx.Response(302, headers={"Location": f"https://objects.example.com/{name}?sig=1"})
        requested.append(name)
        if name in files:
            return httpx.Response(200, content=files[name])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_runtime_asset_per_os():
    assert rt.runtime_asset("win32", "AMD64") == rt.RuntimeAsset("ollama-windows-amd64.zip", "zip", "ollama.exe")
    assert rt.runtime_asset("win32", "ARM64").name == "ollama-windows-arm64.zip"
    mac = rt.runtime_asset("darwin", "arm64")
    assert (mac.name, mac.kind, mac.exe) == ("ollama-darwin.tgz", "tgz", "ollama")
    assert rt.runtime_asset("darwin", "x86_64") == mac  # Intel の Mac も同じ配布ファイル
    linux = rt.runtime_asset("linux", "x86_64")
    assert (linux.name, linux.kind, linux.exe) == ("ollama-linux-amd64.tar.zst", "tzst", "bin/ollama")
    assert rt.runtime_asset("linux", "aarch64").name == "ollama-linux-arm64.tar.zst"
    assert linux.url("v0.40.2") == "https://github.com/ollama/ollama/releases/download/v0.40.2/ollama-linux-amd64.tar.zst"
    for os_name, machine in (("linux", "riscv64"), ("freebsd14", "amd64")):
        with pytest.raises(RuntimeError, match="LLM_RUNTIME=external"):
            rt.runtime_asset(os_name, machine)


@needs_symlinks
def test_download_archive_linux_skips_gpu_and_keeps_links(tmp_path):
    data = make_tar(LINUX_ENTRIES, "tzst")
    progress = Progress()
    dest = tmp_path / "bin" / "v1-cpu"
    url = rt.runtime_asset("linux", "x86_64").url("v1")
    with httpx.Client(transport=archive_transport({"ollama-linux-amd64.tar.zst": data}, [])) as client:
        exe = download_archive(url, dest, "cpu", client, progress, "tzst", "bin/ollama", hashlib.sha256(data).hexdigest())
    assert exe == dest / "bin" / "ollama" and exe.read_bytes().startswith(b"\x7fELF") and os.access(exe, os.X_OK)
    lib = dest / "lib" / "ollama"
    assert os.readlink(lib / "libggml-base.so") == "libggml-base.so.0"
    assert (lib / "libggml-base.so").read_bytes() == b"base" * 100  # リンクをたどって本体に届く
    assert (lib / "libggml-cpu-haswell.so").exists() and not os.access(lib / "libggml-cpu-haswell.so", os.X_OK)
    assert (lib / "vulkan" / "libggml-vulkan.so").exists()
    assert not (lib / "cuda_v12").exists()  # NVIDIA のドライバーがない PC では GPU 用のライブラリを書き出さない
    assert (dest / rt.COMPLETE_MARK).exists() and not dest.with_name("v1-cpu.part").exists()
    assert progress.total == progress.done == len(data)


@needs_symlinks
def test_download_archive_nvidia_includes_cuda(tmp_path):
    data = make_tar(LINUX_ENTRIES, "tzst")
    with httpx.Client(transport=archive_transport({"ollama-linux-amd64.tar.zst": data}, [])) as client:
        download_archive(rt.runtime_asset("linux", "x86_64").url("v1"), tmp_path / "v1-nvidia", "nvidia", client,
                         Progress(), "tzst", "bin/ollama")
    assert (tmp_path / "v1-nvidia/lib/ollama/cuda_v12/libggml-cuda.so").stat().st_size == 200_000


@needs_symlinks
def test_download_archive_macos(tmp_path):
    data = make_tar(MAC_ENTRIES, "tgz")
    dest = tmp_path / "v1-metal"
    with httpx.Client(transport=archive_transport({"ollama-darwin.tgz": data}, [])) as client:
        exe = download_archive(rt.runtime_asset("darwin", "arm64").url("v1"), dest, "metal", client, Progress(), "tgz", "ollama")
    assert exe == dest / "ollama" and os.access(exe, os.X_OK)
    assert os.readlink(dest / "libggml-base.0.dylib") == "libggml-base.0.25.3.dylib"
    assert (dest / "mlx_metal_v4" / "mlx.metallib").exists()
    assert not (dest / "mlx_metal_v4" / "._mlx.metallib").exists()  # macOS の付随ファイルは書き出さない


def test_download_archive_rejects_broken_download(tmp_path):
    data = make_tar(MAC_ENTRIES[:2], "tgz")
    with httpx.Client(transport=archive_transport({"ollama-darwin.tgz": data}, [])) as client:
        with pytest.raises(RuntimeError, match="sha256"):
            download_archive(rt.runtime_asset("darwin", "arm64").url("v1"), tmp_path / "v1", "metal", client,
                             Progress(), "tgz", "ollama", "0" * 64)
    assert not (tmp_path / "v1").exists() and not (tmp_path / "v1.part").exists()


@pytest.mark.parametrize(
    "bad",
    [
        ("../evil", b"x", 0o644),
        ("/tmp/evil-absolute", b"x", 0o644),
        ("lib/ollama/link", ("link", "../../../evil"), 0o777),
        ("lib/ollama/link", ("link", "/etc/passwd"), 0o777),
    ],
)
def test_download_archive_rejects_unsafe_paths(tmp_path, bad):
    data = make_tar([("ollama", b"x", 0o755), bad], "tgz")
    with httpx.Client(transport=archive_transport({"ollama-darwin.tgz": data}, [])) as client:
        with pytest.raises(RuntimeError, match="不正な"):
            download_archive(rt.runtime_asset("darwin", "arm64").url("v1"), tmp_path / "out" / "v1", "metal", client,
                             Progress(), "tgz", "ollama")
    assert not (tmp_path / "evil").exists() and not (tmp_path / "out" / "v1").exists()


@needs_symlinks
def test_download_archive_rejects_chained_links(tmp_path):
    # リンクを重ねて外に出ようとする配布ファイル（s は取り出す場所自体を指し、s/t がその 3 つ上を指す）
    entries = [
        ("ollama", b"x", 0o755),
        ("d1/d2/d3/s", ("link", "../../.."), 0o777),
        ("d1/d2/d3/s/t", ("link", "../../.."), 0o777),
        ("t/PWNED.txt", b"pwned", 0o644),
    ]
    data = make_tar(entries, "tgz")
    out = tmp_path / "a" / "b" / "c"
    with httpx.Client(transport=archive_transport({"ollama-darwin.tgz": data}, [])) as client:
        with pytest.raises(RuntimeError, match="不正な"):
            download_archive(rt.runtime_asset("darwin", "arm64").url("v1"), out / "v1", "metal", client, Progress(), "tgz", "ollama")
    assert not list(tmp_path.rglob("PWNED.txt")) and not (out / "v1").exists() and not (out / "v1.part").exists()


def test_download_archive_can_be_cancelled(tmp_path):
    import threading

    data = make_tar(MAC_ENTRIES[:2], "tgz")
    stop = threading.Event()
    stop.set()  # アプリの終了でやめる
    with httpx.Client(transport=archive_transport({"ollama-darwin.tgz": data}, [])) as client:
        with pytest.raises(rt.DownloadCancelled):
            download_archive(rt.runtime_asset("darwin", "arm64").url("v1"), tmp_path / "v1", "metal", client,
                             Progress(), "tgz", "ollama", None, stop)
    assert not (tmp_path / "v1").exists() and not (tmp_path / "v1.part").exists()


def test_fetch_sha256():
    sums = b"abc123  ./ollama-darwin.tgz\nDEF456  ./ollama-linux-amd64.tar.zst\n"
    with httpx.Client(transport=archive_transport({"sha256sum.txt": sums}, [])) as client:
        assert rt.fetch_sha256(client, "v1", "ollama-linux-amd64.tar.zst") == "def456"
        assert rt.fetch_sha256(client, "v1", "ollama-windows-amd64.zip") is None
    with httpx.Client(transport=archive_transport({}, [])) as client:
        assert rt.fetch_sha256(client, "v1", "ollama-darwin.tgz") is None  # 取れなくても止めない


@needs_symlinks
async def test_prepare_runtime_on_linux(tmp_path, monkeypatch):
    data = make_tar(LINUX_ENTRIES, "tzst")
    sums = f"{hashlib.sha256(data).hexdigest()}  ./ollama-linux-amd64.tar.zst\n".encode()
    requested: list[str] = []
    transport = archive_transport({"ollama-linux-amd64.tar.zst": data, "sha256sum.txt": sums}, requested)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(rt.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(rt, "has_nvidia_driver", lambda: False)
    settings = embedded_settings(tmp_path, embedded_ollama_version="v9")
    runtime = EmbeddedOllama(settings, transport=transport)
    exe = await runtime._prepare_runtime()
    assert exe == settings.embedded_ollama_dir / "bin" / "v9-cpu" / "bin" / "ollama"
    assert requested == ["sha256sum.txt", "ollama-linux-amd64.tar.zst"]
    requested.clear()
    assert await runtime._prepare_runtime() == exe and requested == []  # 2 回目はダウンロードしない


def test_variant_per_os(tmp_path, monkeypatch):
    # Mac は配布ファイルが 1 種類。名前だけ Apple シリコン=metal / Intel=cpu（EMBEDDED_GPU は関係ない）
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(rt.platform, "machine", lambda: "arm64")
    assert rt.wanted_variant("auto") == rt.wanted_variant("nvidia") == "metal"
    monkeypatch.setattr(rt.platform, "machine", lambda: "x86_64")
    assert rt.wanted_variant("auto") == "cpu"
    # Linux は NVIDIA のドライバーがあれば GPU 用も
    import ctypes.util

    monkeypatch.setattr(sys, "platform", "linux")
    marker = tmp_path / "nvidia-version"
    monkeypatch.setattr(rt, "NVIDIA_LINUX_MARKERS", (str(marker),))
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: None)
    assert not rt.has_nvidia_driver() and rt.wanted_variant("auto") == "cpu"
    marker.write_text("NVRM version: 580")
    assert rt.has_nvidia_driver() and rt.wanted_variant("auto") == "nvidia"
    assert rt.wanted_variant("cpu") == "cpu"


def test_linux_model_sources_include_service_install(tmp_path, monkeypatch):
    service_store = tmp_path / "usr-share-ollama"
    service_store.mkdir()
    monkeypatch.setattr(rt, "SYSTEM_MODEL_DIRS", (service_store,))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert rt.find_model_sources(tmp_path / "app-models") == [service_store]
    monkeypatch.setattr(sys, "platform", "darwin")
    assert rt.find_model_sources(tmp_path / "app-models") == []


def test_parent_death_signal_wrapper(monkeypatch):
    monkeypatch.setattr(rt.shutil, "which", lambda name: None)
    assert rt.with_parent_death_signal(["ollama", "serve"]) == ["ollama", "serve"]
    monkeypatch.setattr(rt.shutil, "which", lambda name: "/usr/bin/setpriv")
    monkeypatch.setattr(rt, "_setpriv_has_pdeathsig", lambda path: True)
    assert rt.with_parent_death_signal(["ollama", "serve"]) == ["/usr/bin/setpriv", "--pdeathsig", "TERM", "--", "ollama", "serve"]
    monkeypatch.setattr(rt, "_setpriv_has_pdeathsig", lambda path: False)  # 古い setpriv
    assert rt.with_parent_death_signal(["ollama", "serve"]) == ["ollama", "serve"]
