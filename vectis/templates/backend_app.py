
import html
import os
import socket
import sys
import time

from flask import Flask, jsonify, make_response, request, send_from_directory

app = Flask(__name__)
BACKEND_ID = os.environ.get("BACKEND_ID", "A")
PORT = int(os.environ.get("PORT", "3001"))
SITE_DIR = os.environ.get("SITE_DIR") or None
START = time.time()
HOSTNAME = socket.gethostname()


def _client_ip():
    return request.headers.get("X-Real-IP") or request.remote_addr


@app.before_request
def _stamp():
    request.environ["vectis_t0"] = time.perf_counter()


@app.after_request
def _finish(resp):
    resp.headers["X-Backend"] = BACKEND_ID
    resp.headers.pop("Date", None)
    ms = (time.perf_counter() - request.environ.get("vectis_t0", time.perf_counter())) * 1000
    print(
        f"req rid={request.headers.get('X-Request-ID', '-')} backend={BACKEND_ID} "
        f"client={_client_ip()} host={request.host} method={request.method} "
        f"uri={request.full_path.rstrip('?')} status={resp.status_code} ms={ms:.1f}",
        flush=True,
    )
    return resp


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backend {bid}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; background: #0f172a; color: #e2e8f0; }}
  main {{ max-width: 640px; margin: 8vh auto; padding: 0 20px; }}
  .badge {{ display: inline-block; font-size: 72px; font-weight: 700; padding: 8px 36px;
            border-radius: 16px; background: {color}; color: #fff; }}
  table {{ border-collapse: collapse; margin-top: 28px; width: 100%; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #334155; font-family: ui-monospace, Menlo, monospace; font-size: 14px; }}
  td:first-child {{ color: #94a3b8; width: 38%; }}
  a {{ color: #7dd3fc; }}
</style></head>
<body><main>
  <div class="badge">Backend {bid}</div>
  <p>This page was served by <b>Backend {bid}</b>. Reload: the load balancer
  may answer from the other backend.</p>
  <table>
    <tr><td>served by</td><td>Backend {bid} ({hostname}, port {port})</td></tr>
    <tr><td>you reached it as</td><td>{host}</td></tr>
    <tr><td>your IP (as the LB saw it)</td><td>{client}</td></tr>
    <tr><td>request id</td><td>{rid}</td></tr>
    <tr><td>forwarded by</td><td>{fwd}</td></tr>
    <tr><td>served at</td><td>{now}</td></tr>
    <tr><td>uptime</td><td>{uptime}s</td></tr>
  </table>
  <p><a href="/api/status">/api/status</a> &middot; <a href="/healthz">/healthz</a></p>
</main></body></html>
"""


def _builtin_page():
    esc = lambda v: html.escape(str(v))  # noqa: E731
    body = PAGE.format(
        bid=esc(BACKEND_ID), color="#2563eb" if BACKEND_ID == "A" else "#ea580c",
        hostname=esc(HOSTNAME), port=PORT, host=esc(request.host), client=esc(_client_ip()),
        rid=esc(request.headers.get("X-Request-ID", "- (not via the load balancer)")),
        fwd=esc(request.headers.get("X-Forwarded-For", "- (direct)")),
        now=time.strftime("%Y-%m-%d %H:%M:%S"), uptime=int(time.time() - START),
    )
    resp = make_response(body)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _serve_site_file(path):
    """Static files as-is; .html files get three tokens filled in so a
    prebuilt page can say which backend served it (a script on the page
    could not: its follow-up requests may be balanced to the OTHER backend)."""
    resp = send_from_directory(SITE_DIR, path)
    if path.lower().endswith(".html") and resp.status_code == 200:
        resp.direct_passthrough = False
        text = resp.get_data(as_text=True)
        if "__VECTIS_" in text:
            e = lambda v: html.escape(str(v))  # noqa: E731
            text = (text.replace("__VECTIS_BACKEND__", e(BACKEND_ID))
                    .replace("__VECTIS_REQUEST_ID__", e(request.headers.get("X-Request-ID", "-")))
                    .replace("__VECTIS_CLIENT__", e(_client_ip()))
                    .replace("__VECTIS_HOST__", e(request.host)))
            resp = make_response(text)
            resp.headers["Content-Type"] = "text/html; charset=utf-8"
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
def root():
    if SITE_DIR:
        return _serve_site_file("index.html")
    if request.accept_mimetypes.best_match(["text/html", "application/json"]) == "text/html":
        return _builtin_page()
    resp = jsonify(service="vectis-backend", backend=BACKEND_ID, up_since=START)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/status")
def status():
    resp = make_response(jsonify(backend=BACKEND_ID, status="ok"))
    resp.headers["Cache-Control"] = "max-age=60"
    return resp


@app.get("/healthz")
def healthz():
    resp = make_response(jsonify(backend=BACKEND_ID, status="ok", uptime=int(time.time() - START)))
    resp.headers["Cache-Control"] = "no-store"
    return resp


if SITE_DIR:
    @app.get("/<path:path>")
    def site_file(path):
        return _serve_site_file(path)


if __name__ == "__main__":
    if SITE_DIR and not os.path.isfile(os.path.join(SITE_DIR, "index.html")):
        print(f"fatal: SITE_DIR {SITE_DIR} has no index.html", file=sys.stderr, flush=True)
        sys.exit(2)
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
