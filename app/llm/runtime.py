"""アプリに同梱する Ollama（ローカルLLM の実行環境）。

Ollama を別にインストールしなくても動くように、アプリが専用の Ollama を data/ollama/ に用意し、
アプリと一緒に起動・停止する。

- 実行ファイル: GitHub の公式リリース（Windows 用の単体版 zip）から必要なファイルだけを取り出す。
  zip は約 1.5GB あるが大半は NVIDIA GPU 用のライブラリで、GPU のない PC では約 40MB のダウンロードで済む
  （HTTP の Range 指定で、zip の必要な部分だけを読む）。
- モデル: data/ollama/models に保存する。別途インストールした Ollama に同じモデルがあれば取り込み
  （同じドライブならハードリンクなので容量は増えない）、なければダウンロードする。
- 別のポート（既定 11435）で起動するので、別途インストールした Ollama（11434）とはぶつからない。
- アプリが異常終了しても Ollama だけが残らないよう、Windows のジョブ オブジェクトに入れて一緒に終わらせる。
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable, Iterable, Optional

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

RELEASE_URL = "https://github.com/ollama/ollama/releases/download/{version}/ollama-windows-{arch}.zip"
# GPU 用のライブラリ（NVIDIA は cuda_v12 / cuda_v13、AMD は別の zip）。GPU のない PC では取り出さない
GPU_DIR_PREFIXES = ("lib/ollama/cuda_", "lib/ollama/rocm")
COMPLETE_MARK = ".complete"


@dataclass
class Progress:
    done: int = 0
    total: int = 0

    def as_dict(self) -> dict:
        return {"done": self.done, "total": self.total}


# ---------- 実行ファイルの用意 ----------
def windows_arch(machine: Optional[str] = None) -> str:
    m = (machine if machine is not None else platform.machine()).lower()
    return "arm64" if m in ("arm64", "aarch64") else "amd64"


def has_nvidia_driver() -> bool:
    if sys.platform != "win32":
        return False
    return (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvcuda.dll").exists()


def wanted_variant(setting: str) -> str:
    """EMBEDDED_GPU（auto / cpu / nvidia）から、取り出すライブラリの種類を決める。"""
    setting = (setting or "auto").strip().lower()
    if setting in ("cpu", "nvidia"):
        return setting
    return "nvidia" if has_nvidia_driver() else "cpu"


def select_members(infos: Iterable[zipfile.ZipInfo], variant: str) -> list[zipfile.ZipInfo]:
    out = []
    for info in infos:
        if info.is_dir():
            continue
        name = info.filename
        if name.startswith(GPU_DIR_PREFIXES) and not (variant == "nvidia" and name.startswith("lib/ollama/cuda_")):
            continue
        out.append(info)
    return out


class HttpRangeFile:
    """HTTP の Range 指定で、ダウンロードせずに必要な部分だけを読めるファイル（zipfile に渡す）。"""

    BLOCK = 4 * 1024 * 1024

    def __init__(self, client: httpx.Client, url: str):
        self._client = client
        self._origin = url
        self._pos = 0
        self._cache_start = 0
        self._cache = b""
        self.fetched = 0
        self.size = 0
        self._resolve()

    def _resolve(self) -> None:
        # リダイレクト先（期限付きの URL）と全体のサイズを調べる
        r = self._client.get(self._origin, headers={"Range": "bytes=0-0"}, follow_redirects=True)
        r.raise_for_status()
        if r.status_code != 206 or "content-range" not in r.headers:
            raise RuntimeError("ダウンロード元が部分的な読み込みに対応していません")
        self._url = str(r.url)
        self.size = int(r.headers["content-range"].rsplit("/", 1)[1])

    def _get(self, start: int, end: int) -> bytes:
        last: Exception | None = None
        for attempt in range(4):
            try:
                r = self._client.get(self._url, headers={"Range": f"bytes={start}-{end}"}, follow_redirects=True)
                if r.status_code in (401, 403, 404) and attempt == 0:
                    self._resolve()  # 期限付き URL が切れた → 取り直す
                    continue
                r.raise_for_status()
                if r.status_code != 206:
                    raise RuntimeError("ダウンロード元が部分的な読み込みに対応していません")
                data = r.content
                self.fetched += len(data)
                return data
            except httpx.TransportError as e:
                last = e
                time.sleep(1 + attempt)
        raise RuntimeError(f"ダウンロードに失敗しました: {last}")

    # --- zipfile が使うファイルの機能 ---
    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        else:
            self._pos = self.size + offset
        return self._pos

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.size - self._pos
        parts = []
        while n > 0 and self._pos < self.size:
            end_cache = self._cache_start + len(self._cache)
            if not (self._cache_start <= self._pos < end_cache):
                start = self._pos
                end = min(self.size, start + max(n, self.BLOCK)) - 1
                self._cache = self._get(start, end)
                self._cache_start = start
                if not self._cache:
                    break
            off = self._pos - self._cache_start
            chunk = self._cache[off : off + n]
            parts.append(chunk)
            self._pos += len(chunk)
            n -= len(chunk)
        return b"".join(parts)

    def close(self) -> None:
        self._cache = b""


def download_runtime(url: str, dest: Path, variant: str, client: httpx.Client, progress: Progress) -> Path:
    """公式の zip から必要なファイルだけを dest に取り出し、ollama.exe のパスを返す。"""
    tmp = dest.with_name(dest.name + ".part")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    root = tmp.resolve()
    rf = HttpRangeFile(client, url)
    with zipfile.ZipFile(rf) as z:  # type: ignore[arg-type]
        members = select_members(z.infolist(), variant)
        progress.total = sum(i.compress_size for i in members)
        progress.done = 0
        base = rf.fetched
        for info in members:
            target = (tmp / info.filename).resolve()
            if root not in target.parents:
                raise RuntimeError(f"zip に不正なパスがあります: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    progress.done = min(progress.total, rf.fetched - base)
    if not (tmp / "ollama.exe").exists():
        raise RuntimeError("ダウンロードした zip に ollama.exe がありません")
    (tmp / COMPLETE_MARK).write_text(url, encoding="utf-8")
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)
    progress.done = progress.total
    return dest / "ollama.exe"


# ---------- モデル ----------
def manifest_relpath(name: str) -> PurePosixPath:
    """'qwen3-vl:8b-instruct' → registry.ollama.ai/library/qwen3-vl/8b-instruct（Ollama のモデル置き場の中の場所）"""
    model, _, tag = name.partition(":")
    parts = model.split("/")
    if len(parts) == 1:
        parts = ["registry.ollama.ai", "library", parts[0]]
    elif len(parts) == 2:
        parts = ["registry.ollama.ai", *parts]
    return PurePosixPath(*parts, tag or "latest")


def full_name(name: str) -> str:
    return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"


def has_any_model(models_dir: Path) -> bool:
    manifests = models_dir / "manifests"
    return manifests.is_dir() and any(p.is_file() for p in manifests.rglob("*"))


def find_model_sources(models_dir: Path) -> list[Path]:
    """別途インストールした Ollama のモデル置き場の候補。"""
    candidates = []
    if os.environ.get("OLLAMA_MODELS"):
        candidates.append(Path(os.environ["OLLAMA_MODELS"]))
    candidates.append(Path.home() / ".ollama" / "models")
    out: list[Path] = []
    for c in candidates:
        try:
            if c.is_dir() and c.resolve() != models_dir.resolve() and c not in out:
                out.append(c)
        except OSError:
            pass
    return out


def _copy_file(src: Path, dst: Path, on_bytes: Callable[[int], None]) -> None:
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            chunk = fi.read(8 * 1024 * 1024)
            if not chunk:
                break
            fo.write(chunk)
            on_bytes(len(chunk))


def import_model(name: str, src_dir: Path, dst_dir: Path, progress: Optional[Progress] = None) -> bool:
    """別の Ollama のモデル置き場から 1 モデルを取り込む。ファイルはハードリンク（できなければコピー）。"""
    manifest = src_dir / "manifests" / manifest_relpath(name)
    if not manifest.is_file():
        return False
    data = json.loads(manifest.read_text(encoding="utf-8"))
    digests = [data["config"]["digest"], *(layer["digest"] for layer in data.get("layers", []))]
    blobs = []
    for digest in digests:
        fname = digest.replace(":", "-")
        src = src_dir / "blobs" / fname
        if not src.is_file():
            return False  # 途中までしかないモデルは取り込まない
        blobs.append((src, dst_dir / "blobs" / fname))

    progress = progress or Progress()
    progress.total = sum(s.stat().st_size for s, _ in blobs)
    progress.done = 0
    (dst_dir / "blobs").mkdir(parents=True, exist_ok=True)
    for src, dst in blobs:
        size = src.stat().st_size
        start = progress.done
        if not (dst.exists() and dst.stat().st_size == size):
            tmp = dst.with_name(dst.name + ".part")
            if tmp.exists():
                tmp.unlink()
            try:
                os.link(src, tmp)
            except OSError:  # 別のドライブなど → コピー
                def add(n: int) -> None:
                    progress.done += n

                _copy_file(src, tmp, add)
            os.replace(tmp, dst)
        progress.done = start + size
    dst_manifest = dst_dir / "manifests" / manifest_relpath(name)
    dst_manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(manifest, dst_manifest)
    return True


async def pull_model(client: httpx.AsyncClient, name: str, progress: Progress) -> None:
    """Ollama の /api/pull でモデルをダウンロードし、進み具合を progress に入れる。"""
    layers: dict[str, tuple[int, int]] = {}
    timeout = httpx.Timeout(None, connect=10.0)
    async with client.stream("POST", "/api/pull", json={"model": name, "name": name, "stream": True}, timeout=timeout) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            data = json.loads(line)
            if data.get("error"):
                raise RuntimeError(f"{name} をダウンロードできません: {data['error']}")
            if data.get("digest") and data.get("total"):
                layers[data["digest"]] = (int(data.get("completed") or 0), int(data["total"]))
                progress.done = sum(c for c, _ in layers.values())
                progress.total = sum(t for _, t in layers.values())
            if data.get("status") == "success":
                progress.done = progress.total
                return
    raise RuntimeError(f"{name} のダウンロードが途中で終わりました")


# ---------- Windows のジョブ オブジェクト（アプリが終わったら Ollama も終わらせる） ----------
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (n, ctypes.c_ulonglong)
        for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WinJob:
    def __init__(self) -> None:
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.TerminateJobObject.restype = wintypes.BOOL
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.CloseHandle.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k = k
        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(
            self.handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info)
        ):
            err = ctypes.get_last_error()  # type: ignore[attr-defined]
            self.close()
            raise ctypes.WinError(err)  # type: ignore[attr-defined]

    def assign(self, pid: int) -> None:
        h = self._k.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not h:
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
        try:
            if not self._k.AssignProcessToJobObject(self.handle, h):
                raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
        finally:
            self._k.CloseHandle(h)

    def terminate(self) -> None:
        if self.handle:
            self._k.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            self._k.CloseHandle(self.handle)
            self.handle = None


# ---------- 本体 ----------
def _friendly(e: BaseException) -> str:
    if isinstance(e, httpx.ConnectError):
        return "ダウンロードできません。インターネット接続を確認してから「再試行」を押してください"
    return str(e) or type(e).__name__


def _tail(path: Path, lines: int = 3) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 2000))
            text = f.read().decode("utf-8", errors="replace")
        return " / ".join(line.strip() for line in text.strip().splitlines()[-lines:])
    except OSError:
        return ""


class EmbeddedOllama:
    """同梱の Ollama を用意・起動・監視・停止する。状態は status() で Web UI に出す。

    state: preparing → downloading（初回のみ）→ importing（既存のモデルがあれば）→ starting
           → pulling（モデルがなければ）→ running。失敗すると error。
    """

    READY_TIMEOUT = 90.0
    WATCH_SEC = 5.0
    MAX_RESTARTS = 3  # RESTART_WINDOW 秒の間に、落ちた Ollama を何回まで自動で起動し直すか
    RESTART_WINDOW = 600.0

    def __init__(
        self,
        settings: Settings,
        transport: Optional[httpx.BaseTransport] = None,
        command: Optional[list[str]] = None,
    ):
        self.settings = settings
        self.root = settings.embedded_ollama_dir
        self.models_dir = self.root / "models"
        self.log_path = self.root / "server.log"
        self.version = settings.embedded_ollama_version
        self.variant = wanted_variant(settings.embedded_gpu)
        self.url = settings.ollama_url
        self.port = settings.embedded_ollama_port
        self._transport = transport
        self._command = command  # テスト用: 実行ファイルを用意せず、このコマンドで起動する
        self.state = "stopped"
        self.message = ""
        self.progress: Optional[Progress] = None
        self.on_ready: list[Callable[[], Awaitable[object]]] = []
        self._task: Optional[asyncio.Task] = None
        self._proc: Optional[subprocess.Popen] = None
        self._job: Optional[_WinJob] = None
        self._log_file = None
        self._adopted = False
        self._restarts: list[float] = []

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin" / f"{self.version}-{self.variant}"

    def status(self) -> dict:
        return {
            "mode": "embedded",
            "state": self.state,
            "message": self.message,
            "progress": self.progress.as_dict() if self.progress else None,
            "version": self.version,
            "gpu": self.variant,
            "url": self.url,
        }

    def _set(self, state: str, message: str = "", progress: Optional[Progress] = None) -> None:
        self.state, self.message, self.progress = state, message, progress

    # ---------- 起動・停止 ----------
    def start(self) -> None:
        """準備〜起動をバックグラウンドで始める（既に動いていれば何もしない）。"""
        if self._task and not self._task.done():
            return
        self._restarts.clear()
        self._set("preparing", "準備しています")
        self._task = asyncio.create_task(self._run(), name="embedded-ollama")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await asyncio.to_thread(self._kill)
        if self._job:
            self._job.close()
            self._job = None
        self._set("stopped")

    async def _run(self) -> None:
        try:
            if self._command is not None:
                command = list(self._command)
            else:
                if sys.platform != "win32":
                    raise RuntimeError(
                        "同梱の Ollama は Windows のみ対応です。.env で LLM_RUNTIME=external にして、"
                        "別途インストールした Ollama を使ってください"
                    )
                command = [str(await self._prepare_runtime()), "serve"]
            await self._import_models()
            await self._start_process(command)
            await self._ensure_models()
            self._set("running")
            log.info("同梱の Ollama を起動しました（%s）", self.url)
            await self._notify_ready()
            await self._watch(command)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("同梱の Ollama を準備できませんでした")
            await asyncio.to_thread(self._kill)
            self._set("error", _friendly(e))

    async def _prepare_runtime(self) -> Path:
        exe = self.bin_dir / "ollama.exe"
        if (self.bin_dir / COMPLETE_MARK).exists() and exe.exists():
            return exe
        url = RELEASE_URL.format(version=self.version, arch=windows_arch())
        progress = Progress()
        self._set("downloading", f"Ollama {self.version} をダウンロードしています", progress)
        log.info("Ollama %s（%s）を取り出します: %s", self.version, self.variant, url)

        def work() -> Path:
            timeout = httpx.Timeout(120.0, connect=15.0)
            with httpx.Client(transport=self._transport, timeout=timeout) as client:
                return download_runtime(url, self.bin_dir, self.variant, client, progress)

        exe = await asyncio.to_thread(work)
        log.info("Ollama を用意しました（%.1fMB）: %s", progress.total / 1e6, exe)
        for old in (self.root / "bin").iterdir():  # 古いバージョンを片付ける
            if old != self.bin_dir:
                shutil.rmtree(old, ignore_errors=True)
        return exe

    def _required_models(self) -> list[str]:
        names = [self.settings.embedded_model]
        for n in (self.settings.ollama_model, self.settings.ollama_vision_model):
            if n and n != "auto" and n not in names:
                names.append(n)
        return [n for n in names if n]

    async def _import_models(self) -> None:
        """モデル置き場が空なら、別途インストールした Ollama からモデルを取り込む。"""
        if not self.settings.embedded_import_models or has_any_model(self.models_dir):
            return
        sources = find_model_sources(self.models_dir)
        for name in self._required_models():
            for src in sources:
                progress = Progress()
                self._set("importing", f"{name} をインストール済みの Ollama から取り込んでいます", progress)
                try:
                    ok = await asyncio.to_thread(import_model, name, src, self.models_dir, progress)
                except OSError as e:
                    log.warning("%s を %s から取り込めませんでした: %s", name, src, e)
                    ok = False
                if ok:
                    log.info("%s を %s から取り込みました", name, src)
                    break

    def _api(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.url, timeout=httpx.Timeout(10.0, connect=3.0))

    async def _ping(self) -> bool:
        try:
            async with self._api() as api:
                r = await api.get("/api/version")
                return r.status_code == 200 and "version" in r.json()
        except Exception:
            return False

    def _port_in_use(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    async def _start_process(self, command: list[str]) -> None:
        self._set("starting", "Ollama を起動しています")
        self._adopted = False
        if await self._ping():
            # 前回のアプリが残した Ollama など。止められないが、そのまま使う
            log.warning("ポート %s で既に Ollama が動いているため、それを使います", self.port)
            self._adopted = True
            return
        if self._port_in_use():
            raise RuntimeError(
                f"ポート {self.port} が別のアプリに使われています。.env の EMBEDDED_OLLAMA_PORT を変えてください"
            )
        self._spawn(command)
        deadline = time.monotonic() + self.READY_TIMEOUT
        while time.monotonic() < deadline:
            code = self._proc.poll() if self._proc else None
            if code is not None:
                raise RuntimeError(f"Ollama が起動直後に終了しました（終了コード {code}）: {_tail(self.log_path)}")
            if await self._ping():
                return
            await asyncio.sleep(0.5)
        raise RuntimeError(f"Ollama の起動を待ちきれませんでした: {_tail(self.log_path)}")

    def _spawn(self, command: list[str]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > 5_000_000:
                os.replace(self.log_path, self.log_path.with_name("server.log.1"))
        except OSError:
            pass
        env = os.environ.copy()
        env["OLLAMA_HOST"] = f"127.0.0.1:{self.port}"
        env["OLLAMA_MODELS"] = str(self.models_dir)
        self._log_file = open(self.log_path, "ab")
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        self._proc = subprocess.Popen(
            command,
            cwd=str(self.root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
        if sys.platform == "win32":
            try:
                if self._job is None:
                    self._job = _WinJob()
                self._job.assign(self._proc.pid)
            except OSError as e:
                log.warning("Ollama をジョブ オブジェクトに入れられませんでした（アプリが異常終了すると残ることがあります）: %s", e)

    def _kill(self) -> None:
        if self._job:
            self._job.terminate()  # Ollama と、Ollama が起動したモデル実行用のプロセスをまとめて終わらせる
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
        if self._log_file:
            self._log_file.close()
            self._log_file = None

    async def _ensure_models(self) -> None:
        """使えるモデルがなければダウンロードする。"""
        async with self._api() as api:
            r = await api.get("/api/tags")
            r.raise_for_status()
            installed = {full_name(m["name"]) for m in r.json().get("models", [])}
            needed = []
            for i, name in enumerate(self._required_models()):
                explicit = i > 0
                if full_name(name) in installed:
                    continue
                if explicit or not installed:
                    needed.append(name)
            for name in needed:
                progress = Progress()
                self._set("pulling", f"{name} をダウンロードしています（初回のみ）", progress)
                log.info("モデル %s をダウンロードします", name)
                await pull_model(api, name, progress)
                log.info("モデル %s をダウンロードしました（%.1fGB）", name, progress.total / 1e9)

    async def _notify_ready(self) -> None:
        for cb in list(self.on_ready):
            try:
                await cb()
            except Exception:
                log.exception("同梱の Ollama の起動後の処理に失敗しました")

    async def _watch(self, command: list[str]) -> None:
        """Ollama が落ちたら起動し直す（短時間に何度も落ちるならあきらめて error にする）。"""
        while True:
            await asyncio.sleep(self.WATCH_SEC)
            if self._adopted:
                if await self._ping():
                    continue
                log.warning("使っていた Ollama が止まったため、アプリで起動します")
            else:
                code = self._proc.poll() if self._proc else 0
                if code is None:
                    continue
                log.warning("Ollama が終了しました（終了コード %s）: %s", code, _tail(self.log_path))
            now = time.monotonic()
            self._restarts = [t for t in self._restarts if now - t < self.RESTART_WINDOW]
            if len(self._restarts) >= self.MAX_RESTARTS:
                raise RuntimeError(f"Ollama が繰り返し終了するため、自動での起動をやめました: {_tail(self.log_path)}")
            self._restarts.append(now)
            await asyncio.to_thread(self._kill)
            await self._start_process(command)
            self._set("running")
            await self._notify_ready()
