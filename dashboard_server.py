import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template_string, request, url_for

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent
REPORTS_DIR = ROOT_DIR / "reports"
DASHBOARD_JSON = REPORTS_DIR / "test_dashboard.json"
LAST_RUN_JSON = REPORTS_DIR / "last_run.json"

app = Flask(__name__)
_run_lock = threading.Lock()


class _PrefixMiddleware:
    """Makes url_for() emit APP_BASE_PATH when reverse-proxied under a base path by IIS."""

    def __init__(self, wsgi_app, base_path: str):
        self.wsgi_app = wsgi_app
        self.base_path = base_path.rstrip("/")

    def __call__(self, environ, start_response):
        if self.base_path:
            environ["SCRIPT_NAME"] = self.base_path
            path_info = environ.get("PATH_INFO", "")
            if path_info.startswith(self.base_path):
                environ["PATH_INFO"] = path_info[len(self.base_path):]
        return self.wsgi_app(environ, start_response)


_app_base_path = os.environ.get("APP_BASE_PATH", "").strip()
if _app_base_path:
    app.wsgi_app = _PrefixMiddleware(app.wsgi_app, _app_base_path)  # type: ignore[assignment]


@app.after_request
def _disable_caching(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _load_dashboard_payload() -> dict:
    if not DASHBOARD_JSON.exists():
        return {"summary": {}, "sections": []}
    return json.loads(DASHBOARD_JSON.read_text(encoding="utf-8"))


def _run_pytest_async(target: str | None):
    def _worker():
        args = [sys.executable, "-m", "pytest"]
        if target:
            args.append(target)
        started_at = datetime.now().isoformat()
        result = subprocess.run(args, cwd=str(ROOT_DIR), capture_output=True, text=True)
        LAST_RUN_JSON.write_text(
            json.dumps(
                {
                    "started_at": started_at,
                    "finished_at": datetime.now().isoformat(),
                    "target": target or "all tests",
                    "returncode": result.returncode,
                    "stdout_tail": result.stdout[-4000:],
                    "stderr_tail": result.stderr[-4000:],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


INDEX_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Comsci Web Automation Dashboard</title>
  <style>
    body { font-family: Segoe UI, Arial, sans-serif; background: #f5f1e8; color: #1d2733; margin: 0; }
    header { padding: 24px 32px; border-bottom: 1px solid #d8cfc1; background: #fffdf8; }
    main { padding: 24px 32px; }
    .card { background: #fffdf8; border: 1px solid #d8cfc1; border-radius: 12px; padding: 16px; margin-bottom: 16px; }
    button, a.button { display: inline-block; background: #0f5c78; color: white; border: none; padding: 10px 18px;
      border-radius: 8px; cursor: pointer; text-decoration: none; font-size: 0.95rem; }
    a.secondary { color: #0f5c78; margin-left: 12px; }
  </style>
</head>
<body>
  <header><h1>Comsci Web Automation Dashboard</h1></header>
  <main>
    <div class="card">
      <form method="post" action="{{ run_url }}">
        <button type="submit">Run Full Suite</button>
      </form>
      <a class="secondary" href="{{ view_url }}">View Latest Dashboard Report</a>
    </div>
    <div class="card">
      <p>Summary: {{ summary }}</p>
    </div>
  </main>
</body>
</html>
"""


@app.route("/")
def index():
    payload = _load_dashboard_payload()
    return render_template_string(
        INDEX_TEMPLATE,
        run_url=url_for("run_suite"),
        view_url=url_for("view_report"),
        summary=payload.get("summary", {}),
    )


@app.route("/run", methods=["POST"])
def run_suite():
    target = request.form.get("target") or None
    with _run_lock:
        _run_pytest_async(target)
    return redirect(url_for("index"))


@app.route("/report")
def view_report():
    html_path = REPORTS_DIR / "test_dashboard.html"
    if not html_path.exists():
        return "No dashboard report yet. Run the suite first.", 404
    return html_path.read_text(encoding="utf-8")


@app.route("/api/dashboard")
def api_dashboard():
    return jsonify(_load_dashboard_payload())


@app.route("/api/last_run")
def api_last_run():
    if not LAST_RUN_JSON.exists():
        return jsonify({})
    return jsonify(json.loads(LAST_RUN_JSON.read_text(encoding="utf-8")))


if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", "5060"))
    app.run(host="0.0.0.0", port=port, debug=False)
