"""Face embeddings for the robot's greeting, on a GPU box (DGX f2) or any CPU: POST a JPEG to
/embed and get every face's box and 128-d SFace feature. Matching against enrolled people
happens in the bridge, so this service keeps no faces and no names.

Models (OpenCV Zoo): YuNet detector (MIT) and SFace recognizer (Apache-2.0), both run fine on
CPU. Run: FACE_TOKEN=... python face_server.py  (port FACE_PORT, default 7880).
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import pathlib
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

log = logging.getLogger("face")
ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"
MODELS = {
    "yunet.onnx": f"{ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "sface.onnx": f"{ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}
HERE = pathlib.Path(os.environ.get("FACE_MODEL_DIR", pathlib.Path(__file__).parent / "models"))
TOKEN = os.environ.get("FACE_TOKEN", "")
MIN_SCORE = float(os.environ.get("FACE_MIN_SCORE", "0.8"))


def model(name: str) -> str:
    path = HERE / name
    if not path.exists():
        HERE.mkdir(parents=True, exist_ok=True)
        log.info("downloading %s", MODELS[name])
        urllib.request.urlretrieve(MODELS[name], path)
    return str(path)


class Faces:
    def __init__(self):
        self.detector = cv2.FaceDetectorYN.create(model("yunet.onnx"), "", (320, 320), MIN_SCORE, 0.3, 20)
        self.recognizer = cv2.FaceRecognizerSF.create(model("sface.onnx"), "")
        self.lock = threading.Lock()  # the OpenCV DNN objects are not thread-safe

    def embed(self, jpeg: bytes) -> list[dict]:
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("not an image")
        h, w = img.shape[:2]
        with self.lock:
            self.detector.setInputSize((w, h))
            _, found = self.detector.detect(img)
            out = []
            for f in found if found is not None else []:
                feature = self.recognizer.feature(self.recognizer.alignCrop(img, f)).flatten()
                feature /= np.linalg.norm(feature) or 1.0  # unit length: cosine = dot product
                x, y, bw, bh = (float(v) for v in f[:4])
                out.append({"box": [x / w, y / h, bw / w, bh / h], "score": round(float(f[14]), 3),
                            "embedding": [round(float(v), 5) for v in feature]})
        return sorted(out, key=lambda o: -o["box"][2] * o["box"][3])  # largest (nearest) first


faces: Faces | None = None


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "models": list(MODELS)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/embed":
            return self._send(404, {"error": "not found"})
        if TOKEN and not hmac.compare_digest(self.headers.get("X-Token", ""), TOKEN):
            return self._send(401, {"error": "bad token"})
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 < n <= 4_000_000:
            return self._send(413, {"error": "send one JPEG up to 4 MB"})
        t0 = time.monotonic()
        try:
            found = faces.embed(self.rfile.read(n))
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        self._send(200, {"faces": found, "ms": round((time.monotonic() - t0) * 1000)})

    def log_message(self, fmt, *args):  # no per-request lines: frames arrive every few seconds
        pass


def main() -> None:
    global faces
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    faces = Faces()
    port = int(os.environ.get("FACE_PORT", "7880"))
    log.info("face server on :%d (token %s)", port, "on" if TOKEN else "OFF")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
