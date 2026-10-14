"""OpenAI-compatible text embeddings (POST /v1/embeddings) for the assistant's memory, with
BGE-M3 (MIT, 100+ languages including Thai) by default. Runs on the DGX next to the face
server; any other OpenAI-compatible embeddings server can replace it (--embed-url).

    EMBED_TOKEN=... python embed_server.py      # port EMBED_PORT (7890), model EMBED_MODEL
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("embed")
MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")
TOKEN = os.environ.get("EMBED_TOKEN", "")
model = None
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/health", "/v1/models"):
            return self._send(200, {"ok": True, "data": [{"id": MODEL, "object": "model"}]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/embeddings":
            return self._send(404, {"error": "not found"})
        if TOKEN and not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {TOKEN}"):
            return self._send(401, {"error": "bad token"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
            if not texts or len(texts) > 64 or not all(isinstance(t, str) for t in texts):
                raise ValueError("input: 1-64 strings")
        except (ValueError, KeyError) as e:
            return self._send(400, {"error": str(e)})
        t0 = time.monotonic()
        with lock:
            vectors = model.encode(texts, normalize_embeddings=True, batch_size=16).tolist()
        log.info("%d texts in %.0f ms", len(texts), (time.monotonic() - t0) * 1000)
        self._send(200, {"object": "list", "model": MODEL,
                         "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)]})

    def log_message(self, fmt, *args):
        pass


def main() -> None:
    global model
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    from sentence_transformers import SentenceTransformer
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = SentenceTransformer(MODEL, device=device)
    port = int(os.environ.get("EMBED_PORT", "7890"))
    log.info("%s on %s, port %d (token %s)", MODEL, device, port, "on" if TOKEN else "OFF")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
