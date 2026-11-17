"""Pratham Media Downloader — local web app.

Run:  ./run.sh   (or: .venv/bin/python app.py)  then open http://127.0.0.1:5173
"""

import io
import os
import subprocess
import threading
import time
import zipfile
from collections import defaultdict, deque
from urllib.parse import quote

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

import engine

app = Flask(__name__, static_folder="public", static_url_path="")

# Hosted behind a proxy (Render, Railway, …): trust its X-Forwarded-* headers so
# links come out as https:// and rate limiting sees the visitor's real IP.
BEHIND_PROXY = os.environ.get("BEHIND_PROXY") == "1"
if BEHIND_PROXY:
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Simple per-visitor limit so a public instance isn't hammered (0 = off).
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MIN", "0") or 0)
_hits: dict[str, deque] = defaultdict(deque)
_hits_lock = threading.Lock()


def _rate_limited(ip: str) -> bool:
    if RATE_LIMIT <= 0:
        return False
    now = time.time()
    with _hits_lock:
        q = _hits[ip]
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return True
        q.append(now)
        return False


def _is_local_request() -> bool:
    return not BEHIND_PROXY and request.remote_addr in ("127.0.0.1", "::1")

# If the UI is hosted elsewhere (e.g. Netlify), list its origin here, comma-separated.
ALLOWED_ORIGINS = {o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()}


@app.after_request
def cors(resp):
    origin = request.headers.get("Origin")
    if origin and origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Vary"] = "Origin"
    return resp


@app.route("/api/<path:_any>", methods=["OPTIONS"])
def preflight(_any):
    return ("", 204)


@app.get("/healthz")
def healthz():
    return jsonify(ok=True)


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.post("/api/fetch")
def api_fetch():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    quality = data.get("quality", "best")
    if not url:
        return jsonify(error="Paste an Instagram link first."), 400
    if _rate_limited(request.remote_addr or "?"):
        return jsonify(error="Too many downloads in a minute. Take a short breather and try again."), 429
    try:
        result = engine.fetch(url, quality)
        # Absolute links so the UI works even when it's hosted on another domain (e.g. Netlify).
        base = request.host_url.rstrip("/")
        for it in result["items"]:
            path = "/".join(quote(p) for p in it["relpath"].split("/"))
            it["url"] = f"{base}/media/{path}"
            it["download"] = f"{base}/media/{path}?dl=1"
        folder = os.path.basename(result["folder"])
        result["zip"] = f"{base}/zip/{quote(folder)}" if len(result["items"]) > 1 else None
        result["can_reveal"] = _is_local_request()
        if not result["can_reveal"]:
            result["folder"] = folder  # don't expose server paths
        return jsonify(result)
    except engine.DownloadError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:  # unexpected
        return jsonify(error=engine._friendly(e)), 500


def _safe_path(relpath: str):
    root = engine.DOWNLOAD_DIR.resolve()
    p = (root / relpath).resolve()
    if root not in p.parents or not p.is_file():
        abort(404)
    return p


@app.get("/media/<path:relpath>")
def media(relpath):
    p = _safe_path(relpath)
    return send_file(p, as_attachment=request.args.get("dl") == "1", download_name=p.name)


@app.get("/zip/<folder>")
def zip_folder(folder):
    root = engine.DOWNLOAD_DIR.resolve()
    d = (root / folder).resolve()
    if d.parent != root or not d.is_dir():
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for f in sorted(d.iterdir()):
            if f.is_file():
                z.write(f, f.name)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{folder}.zip", mimetype="application/zip")


@app.post("/api/reveal")
def reveal():
    """Open the saved folder in Finder (local convenience)."""
    if not _is_local_request():
        abort(403)
    folder = (request.get_json(silent=True) or {}).get("folder", "")
    root = engine.DOWNLOAD_DIR.resolve()
    d = (root / os.path.basename(folder)).resolve()
    if d.parent == root and d.is_dir():
        subprocess.Popen(["open", str(d)])
        return jsonify(ok=True)
    return jsonify(ok=False), 404


if __name__ == "__main__":
    engine.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("PORT", 5173))
    print(f"\n  Pratham Media Downloader → http://127.0.0.1:{port}\n")
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=port, debug=False)
