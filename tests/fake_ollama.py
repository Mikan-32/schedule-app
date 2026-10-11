"""テスト用の偽の Ollama（/api/version・/api/tags・/api/pull だけ）。

EmbeddedOllama が渡す OLLAMA_HOST / OLLAMA_MODELS で動き、pull したモデルの名前を
<OLLAMA_MODELS>/pulled.txt に書き残す。
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST, PORT = os.environ["OLLAMA_HOST"].rsplit(":", 1)
MODELS = Path(os.environ["OLLAMA_MODELS"])
LIBRARY = MODELS / "manifests" / "registry.ollama.ai" / "library"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body: bytes, code: int = 200, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/version":
            return self._send(json.dumps({"version": "0.0.0-fake"}).encode())
        if self.path == "/api/tags":
            names = []
            if LIBRARY.is_dir():
                names = [f"{m.name}:{t.name}" for m in LIBRARY.iterdir() for t in m.iterdir()]
            return self._send(json.dumps({"models": [{"name": n} for n in names]}).encode())
        self._send(b'{"error":"not found"}', 404)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path != "/api/pull":
            return self._send(b'{"error":"not found"}', 404)
        name = req.get("model") or req["name"]
        model, _, tag = name.partition(":")
        manifest = LIBRARY / model / (tag or "latest")
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("{}")
        with open(MODELS / "pulled.txt", "a", encoding="utf-8") as f:
            f.write(name + "\n")
        lines = [
            {"status": "pulling manifest"},
            {"status": "pulling abc", "digest": "sha256:abc", "total": 100, "completed": 40},
            {"status": "pulling abc", "digest": "sha256:abc", "total": 100, "completed": 100},
            {"status": "success"},
        ]
        self._send("".join(json.dumps(x) + "\n" for x in lines).encode(), ctype="application/x-ndjson")


ThreadingHTTPServer((HOST, int(PORT)), Handler).serve_forever()
