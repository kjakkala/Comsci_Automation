import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template_string, request, send_file, send_from_directory, url_for
from utils.scheduler import ScheduleStore, SchedulerWorker

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent
REPORTS_DIR = ROOT_DIR / "reports"
DASHBOARD_JSON = REPORTS_DIR / "test_dashboard.json"
LAST_RUN_JSON = REPORTS_DIR / "last_run.json"
LAST_CODEGEN_JSON = REPORTS_DIR / "last_codegen.json"


def _configured_path(variable: str, default: Path) -> Path:
    configured = Path(os.getenv(variable, str(default)))
    return configured if configured.is_absolute() else ROOT_DIR / configured


SECTION_RUN_CONFIG = {
    "Authentication": {"target": "tests/login", "button": "Run Auth"},
    "Header": {"target": "tests/header", "button": "Run Search"},
}

app = Flask(__name__)
_run_lock = threading.Lock()
_testrail_status_lock = threading.Lock()
_testrail_status = {"connected": False, "checked_at": None}
_testrail_cases_lock = threading.Lock()
_testrail_cases_cache = {"cases": [], "sections": [], "loaded_at": None}
_TESTRAIL_CASES_CACHE_TTL = timedelta(minutes=5)
_live_run_lock = threading.Lock()
_live_run_state = {
    "running": False,
    "mode": None,
    "section": None,
    "current_section": None,
    "output": "",
    "returncode": None,
    "testrail": {},
    "started_at": None,
    "finished_at": None,
}
SCHEDULER_DB_PATH = _configured_path("SCHEDULER_DB_PATH", REPORTS_DIR / "scheduler.sqlite3")
SCHEDULER_LOG_DIR = _configured_path("SCHEDULER_LOG_DIR", REPORTS_DIR / "scheduler_logs")
_scheduler_store_lock = threading.Lock()
_scheduler_store = None
_scheduler_worker = None


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


CATALOG_JSON = REPORTS_DIR / "test_catalog.json"


def _load_test_catalog() -> list[dict]:
    if not CATALOG_JSON.exists():
        return []
    try:
        return json.loads(CATALOG_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _merge_section_rows(catalog: list[dict], results: list[dict], section: str) -> list[dict]:
    """Shows every known case for a section, overlaying the latest execution result when available."""
    result_by_nodeid = {result["nodeid"]: result for result in results if result.get("section") == section}
    rows = []
    seen = set()
    for entry in catalog:
        if entry.get("section") != section:
            continue
        seen.add(entry["nodeid"])
        result = result_by_nodeid.get(entry["nodeid"])
        if result:
            rows.append(result)
        else:
            rows.append({**entry, "status": "not_run", "details": "", "screenshot": "", "screenshots": []})
    for nodeid, result in result_by_nodeid.items():
        if nodeid not in seen:
            rows.append(result)
    return rows


def _rollup_rows(rows: list[dict]) -> dict:
    counts = Counter(row["status"] for row in rows)
    return {
        "total_cases": len(rows),
        "cases_passed": counts.get("passed", 0),
        "cases_failed": counts.get("failed", 0),
        "cases_skipped": counts.get("skipped", 0),
        "cases_not_run": counts.get("not_run", 0),
    }


def _testrail_connected() -> bool:
    """Check TestRail read access at most once per minute for the dashboard header."""
    with _testrail_status_lock:
        checked_at = _testrail_status["checked_at"]
        if checked_at and datetime.now() - checked_at < timedelta(minutes=1):
            return bool(_testrail_status["connected"])

        try:
            from utils.testrail_client import TestRailClient

            TestRailClient.from_env().get_sections()
            _testrail_status["connected"] = True
        except Exception:
            _testrail_status["connected"] = False
        _testrail_status["checked_at"] = datetime.now()
        return bool(_testrail_status["connected"])


def _load_testrail_case_data() -> tuple[list[dict], list[dict]]:
    with _testrail_cases_lock:
        loaded_at = _testrail_cases_cache["loaded_at"]
        if loaded_at and datetime.now() - loaded_at < _TESTRAIL_CASES_CACHE_TTL:
            return _testrail_cases_cache["cases"], _testrail_cases_cache["sections"]

        from utils.testrail_client import TestRailClient

        client = TestRailClient.from_env()
        cases = client.get_all_cases()
        sections = client.get_sections()
        _testrail_cases_cache.update(cases=cases, sections=sections, loaded_at=datetime.now())
        return cases, sections


def _testrail_section_names(sections: list[dict]) -> dict[int, str]:
    by_id = {int(section["id"]): section for section in sections if section.get("id") is not None}
    names = {}
    for section_id, section in by_id.items():
        path = []
        current = section
        visited = set()
        while current and int(current["id"]) not in visited:
            visited.add(int(current["id"]))
            path.append(str(current.get("name", "")))
            current = by_id.get(int(current.get("parent_id") or 0))
        names[section_id] = " / ".join(reversed([name for name in path if name]))
    return names


def _load_last_run() -> dict:
    if not LAST_RUN_JSON.exists():
        return {}
    return json.loads(LAST_RUN_JSON.read_text(encoding="utf-8"))


def _publish_section_results(section: str, results: list[dict]) -> dict:
    if os.getenv("TESTRAIL_ENABLE_ON_RUN", "1").strip().lower() in {"0", "false", "no"}:
        return {"enabled": False}

    mapped_results = [result for result in results if result.get("testrail_case_id")]
    if not mapped_results:
        return {"enabled": True, "error": "No mapped TestRail cases were executed."}

    from utils.testrail_client import TestRailClient

    client = TestRailClient.from_env()
    case_ids = [int(result["testrail_case_id"]) for result in mapped_results]
    plan_name = f"Comsci Automation - {section} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    plan = client.create_plan(plan_name, {section: case_ids})
    published = client.publish_results(plan, {section: mapped_results})
    return {
        "enabled": True,
        "plan_id": plan.get("id"),
        "plan_url": f"{client.config.url.rstrip('/')}/index.php?/plans/view/{plan.get('id')}",
        **published,
    }


def _current_section_from_output(line: str) -> str | None:
    normalized_line = line.replace("\\", "/")
    return next(
        (
            name
            for name, config in SECTION_RUN_CONFIG.items()
            if config["target"].replace("\\", "/").rstrip("/") in normalized_line
        ),
        None,
    )


def _server_timezone() -> str:
    return datetime.now().astimezone().tzname() or str(datetime.now().astimezone().tzinfo)


def _scheduler_environments() -> dict[str, str]:
    configured = os.getenv("SCHEDULE_ENVIRONMENTS", "QA=BASE_URL")
    environments = {}
    for entry in configured.split(","):
        label, separator, variable = entry.strip().partition("=")
        if separator and label.strip() and variable.strip():
            url = os.getenv(variable.strip(), "").strip()
            if url:
                environments[label.strip()] = url
    return environments


def _redact_scheduler_output(text: str) -> str:
    redacted = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)
    secret_values = {
        value
        for name, value in os.environ.items()
        if re.search(r"PASSWORD|TOKEN|SECRET|API.?KEY|USERNAME|(?:^|_)USER$|EMAIL", name, re.IGNORECASE) and value
    }
    secret_values.update(_scheduler_environments().values())
    for secret in sorted(secret_values, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _get_scheduler_store() -> ScheduleStore:
    global _scheduler_store
    with _scheduler_store_lock:
        if _scheduler_store is None:
            _scheduler_store = ScheduleStore(SCHEDULER_DB_PATH, SCHEDULER_LOG_DIR)
        return _scheduler_store


def _launch_scheduled_run(schedule: dict, due_at: datetime) -> bool:
    store = _get_scheduler_store()
    started_at = datetime.now()
    environment_url = _scheduler_environments().get(schedule["environment"])
    run_id = store.mark_started(schedule, started_at)
    if not environment_url:
        finished_at = datetime.now()
        store.append_log(
            run_id,
            "Configured environment URL is unavailable.\n"
            f"\nFinished: {finished_at.isoformat(timespec='seconds')} {_server_timezone()}\n"
            "Outcome: failed\n",
        )
        store.finish_run(run_id, finished_at, "failed")
        store.advance_after_start(schedule, started_at)
        return True

    sections = schedule["sections"]
    targets = [SECTION_RUN_CONFIG[name]["target"] for name in sections]
    section_label = sections[0] if len(sections) == 1 else "All sections"

    def _write_output(line: str) -> None:
        store.append_log(run_id, _redact_scheduler_output(line))

    def _finish_run(result: dict) -> None:
        outcome = "passed" if result["returncode"] == 0 else "failed"
        finished_at = datetime.fromisoformat(result["finished_at"])
        completion_log = (
            f"\nFinished: {result['finished_at']} {_server_timezone()}\n"
            f"Outcome: {outcome}\n"
            f"TestRail: {json.dumps(result['testrail'], ensure_ascii=True)}\n"
        )
        store.append_log(run_id, _redact_scheduler_output(completion_log))
        store.finish_run(run_id, finished_at, outcome)

    accepted = _start_section_run(
        targets,
        section_label,
        "scheduled",
        environment_url=environment_url,
        publish_sections=sections,
        on_output=_write_output,
        on_complete=_finish_run,
    )
    if not accepted:
        store.cancel_unstarted_run(run_id)
        return False
    store.advance_after_start(schedule, started_at)
    return True


def start_scheduler_service() -> None:
    global _scheduler_worker
    if _scheduler_worker is None:
        store = _get_scheduler_store()
        store.recover_interrupted_runs(datetime.now())
        _scheduler_worker = SchedulerWorker(store, _launch_scheduled_run)
    _scheduler_worker.start()


def _start_section_run(
    pytest_args: list[str],
    section: str | None,
    mode: str,
    environment_url: str | None = None,
    publish_sections: list[str] | None = None,
    on_output=None,
    on_complete=None,
) -> bool:
    """Starts a tracked pytest run (mode: "live" or "background"). Returns False if a run is already active."""
    with _live_run_lock:
        if _live_run_state["running"]:
            return False
        started_at = datetime.now().isoformat()
        _live_run_state.update(
            {
                "running": True,
                "mode": mode,
                "section": section,
                "current_section": section or "All tests",
                "output": "",
                "returncode": None,
                "testrail": {},
                "started_at": started_at,
                "finished_at": None,
            }
        )

    def _worker():
        args = [sys.executable, "-m", "pytest", *pytest_args]
        environment = os.environ.copy()
        if environment_url:
            environment["BASE_URL"] = environment_url
        try:
            process = subprocess.Popen(
                args,
                cwd=str(ROOT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=environment,
            )
            assert process.stdout is not None
            for line in process.stdout:
                current_section = _current_section_from_output(line)
                with _live_run_lock:
                    _live_run_state["output"] = (_live_run_state["output"] + line)[-8000:]
                    if current_section:
                        _live_run_state["current_section"] = current_section
                if on_output:
                    try:
                        on_output(line)
                    except OSError:
                        pass
            returncode = process.wait()
        except Exception as exc:
            failure_output = f"Could not start test process: {exc}\n"
            with _live_run_lock:
                _live_run_state["output"] = (_live_run_state["output"] + failure_output)[-8000:]
            if on_output:
                try:
                    on_output(failure_output)
                except OSError:
                    pass
            returncode = 1

        testrail = {}
        sections_to_publish = publish_sections or ([section] if section in SECTION_RUN_CONFIG else [])
        if sections_to_publish:
            try:
                payload = _load_dashboard_payload()
                published_sections = {}
                for section_name in sections_to_publish:
                    section_results = next(
                        (item.get("results", []) for item in payload.get("sections", []) if item.get("name") == section_name),
                        [],
                    )
                    published_sections[section_name] = _publish_section_results(section_name, section_results)
                testrail = (
                    next(iter(published_sections.values()))
                    if len(published_sections) == 1
                    else {"enabled": True, "sections": published_sections}
                )
            except Exception as exc:
                testrail = {"enabled": True, "error": str(exc)}

        finished_at = datetime.now().isoformat()
        with _live_run_lock:
            _live_run_state["running"] = False
            _live_run_state["returncode"] = returncode
            _live_run_state["testrail"] = testrail
            _live_run_state["finished_at"] = finished_at
            output_tail = _live_run_state["output"]

        LAST_RUN_JSON.write_text(
            json.dumps(
                {
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "target": " ".join(pytest_args) or "all tests",
                    "returncode": returncode,
                    "section": section,
                    "mode": mode,
                    "testrail": testrail,
                    "stdout_tail": output_tail[-4000:],
                    "stderr_tail": "",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        if on_complete:
            try:
                on_complete(
                    {
                        "returncode": returncode,
                        "finished_at": finished_at,
                        "testrail": testrail,
                    }
                )
            except Exception:
                app.logger.exception("Scheduled run completion callback failed")

    threading.Thread(target=_worker, daemon=True).start()
    return True


def _launch_codegen() -> None:
    base_url = os.getenv("BASE_URL", "").strip()
    if not base_url:
        raise ValueError("BASE_URL must be configured before launching Playwright Codegen.")

    command = [sys.executable, "-m", "playwright", "codegen", base_url]
    try:
        subprocess.Popen(command, cwd=str(ROOT_DIR))
    except OSError as exc:
        LAST_CODEGEN_JSON.write_text(
            json.dumps({"status": "error", "message": str(exc)}, indent=2),
            encoding="utf-8",
        )
        raise

    LAST_CODEGEN_JSON.write_text(
        json.dumps(
            {
                "status": "started",
                "started_at": datetime.now().isoformat(),
                "base_url": base_url,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


INDEX_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
    <title>Comsci Web Automation Dashboard</title>
  <style>
        :root { --blue: #3478d3; --ink: #17365d; --line: #d9e2ef; --surface: #ffffff; --ground: #f2f6fb; --muted: #6681a1; }
        * { box-sizing: border-box; }
        body { margin: 0; font-family: "Segoe UI", Tahoma, sans-serif; color: var(--ink); background: var(--ground); font-size: 14px; }
        header { position: fixed; top: 0; left: 0; right: 0; height: 72px; display: flex; align-items: center; gap: 14px; padding: 0 18px; background: var(--blue); color: white; z-index: 30; }
        .menu { width: 32px; height: 32px; border: 0; border-radius: 4px; color: var(--blue); background: white; font-size: 18px; }
        .brand { font-size: 18px; font-weight: 700; white-space: nowrap; }
        .header-actions { margin-left: auto; display: flex; gap: 8px; align-items: center; }
        .header-actions form { margin: 0; }
        .testrail-status { display: inline-flex; align-items: center; gap: 6px; padding: 6px 9px; border: 1px solid rgba(255,255,255,.5); border-radius: 4px; color: white; font-size: 11px; font-weight: 700; }
        .testrail-status i { width: 9px; height: 9px; border-radius: 50%; background: #ff8c8c; }
        .testrail-status.connected i { background: #6de5a0; }
        button, .link-button { border: 1px solid #c8d8ee; border-radius: 4px; padding: 9px 13px; background: white; color: var(--ink); font-weight: 600; cursor: pointer; text-decoration: none; }
        button.primary { border-color: white; color: var(--blue); }
        .shell { margin-top: 72px; min-height: calc(100vh - 72px); display: grid; grid-template-columns: 190px minmax(0, 1fr); align-items: start; }
        aside { position: fixed; top: 72px; bottom: 0; left: 0; width: 190px; padding: 18px 12px; background: white; border-right: 1px solid var(--line); overflow-y: auto; }
        .nav-title { margin: 0 0 9px; color: #7c91aa; font-size: 11px; font-weight: 700; text-transform: uppercase; }
        .nav-item { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 10px 12px; border-left: 3px solid transparent; color: var(--ink); font-weight: 700; text-decoration: none; }
        .nav-item.active { border-left-color: var(--blue); background: #edf5ff; }
        .nav-group { display: block; padding: 10px 12px 4px; color: var(--ink); font-weight: 700; }
        .nav-sub { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 2px 0 6px 12px; padding: 6px 10px; border-left: 1px solid var(--line); color: #557492; font-size: 12px; text-decoration: none; }
        .nav-sub.active { color: var(--blue); font-weight: 700; }
        .nav-count { padding: 1px 5px; border-radius: 8px; color: #356795; background: #edf5ff; font-size: 10px; font-weight: 800; white-space: nowrap; }
        main { grid-column: 2; padding: 24px; max-width: 1440px; width: 100%; margin: 0 auto; }
        .run-card, .module-card { margin-bottom: 14px; border: 1px solid var(--line); border-radius: 7px; background: var(--surface); box-shadow: 0 1px 2px rgba(25, 60, 100, .04); }
        .run-card { display: flex; justify-content: space-between; gap: 20px; padding: 18px 20px; }
        h1, h2 { margin: 0; }
        h1 { font-size: 20px; }
        h2 { font-size: 22px; }
        .subtle { margin: 5px 0 0; color: var(--muted); font-size: 12px; }
        .badge { align-self: start; border-radius: 14px; padding: 5px 10px; color: #197141; background: #d8f5e4; font-size: 11px; font-weight: 800; }
        .module-head { display: flex; gap: 16px; align-items: center; padding: 17px 20px; border-bottom: 1px solid var(--line); }
        .subsection-head { margin: 0 20px; padding: 12px 0; border-bottom: 1px solid var(--line); color: var(--ink); font-size: 15px; }
        .module-actions { margin-left: auto; display: flex; gap: 8px; }
        .module-actions form { margin: 0; }
        .case-browser { padding: 14px 20px; border-bottom: 1px solid var(--line); background: #f8fbff; }
        .case-browser-toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
        .case-browser-toolbar input { width: min(100%, 320px); margin-left: auto; padding: 8px 10px; border: 1px solid var(--line); border-radius: 4px; }
        .case-browser-table-wrap { max-height: 420px; overflow: auto; border: 1px solid var(--line); background: white; }
        .case-browser table { min-width: 620px; }
        .case-browser td { padding: 9px 12px; }
        .case-state { color: var(--muted); font-size: 12px; }
        .case-state.runnable { color: #197141; font-weight: 700; }
        .case-browser .empty { padding: 16px; }
        .counts { display: grid; grid-template-columns: repeat(5, minmax(110px, 1fr)); gap: 10px; padding: 14px 20px; }
        .count { padding: 12px; border: 1px solid var(--line); border-radius: 5px; }
        .count b { display: block; font-size: 20px; }
        .count span { color: var(--muted); font-size: 12px; font-weight: 600; }
        table { width: 100%; border-collapse: collapse; }
        th { padding: 10px 16px; color: var(--muted); font-size: 11px; text-align: left; text-transform: uppercase; }
        td { padding: 13px 16px; border-top: 1px solid var(--line); vertical-align: top; }
        .status { display: inline-grid; width: 20px; height: 20px; place-items: center; border-radius: 50%; font-weight: 800; }
        .passed { color: #197141; background: #d8f5e4; }
        .failed { color: #b42318; background: #fee4e2; }
        .skipped { color: #946200; background: #fff1c2; }
        .not_run { color: var(--muted); background: #eef2f7; }
        .case-title { display: block; font-weight: 700; }
        .node { margin-top: 3px; color: var(--muted); font-size: 11px; }
        .labels { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }
        .label { padding: 3px 7px; border-radius: 10px; background: #edf5ff; color: #356795; font-size: 11px; font-weight: 700; }
        a { color: #1769d2; font-weight: 600; }
        summary { color: #1769d2; cursor: pointer; font-weight: 600; }
        pre { max-width: 540px; max-height: 190px; overflow: auto; white-space: pre-wrap; color: #34495e; }
        .evidence { margin-top: 8px; display: block; }
        .empty { padding: 24px 20px; color: var(--muted); }
        .locator-catalog { margin: 0 20px 20px; border: 1px solid var(--line); border-radius: 5px; overflow: hidden; }
        .locator-catalog summary { padding: 12px; background: #f8fbff; }
        .locator-row { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 12px; padding: 10px 12px; border-top: 1px solid var(--line); }
        .locator-name { font-weight: 700; }
        code { overflow-wrap: anywhere; color: #334e68; font-family: Consolas, monospace; font-size: 12px; }
        .live-panel { margin: 0 20px 16px; border: 1px solid var(--line); border-radius: 5px; background: #0f1b2d; }
        .live-panel .live-output { margin: 0; padding: 12px; max-height: 220px; overflow: auto; color: #d7e6f5; background: transparent; border: 0; font-family: Consolas, monospace; font-size: 12px; }
        .select-col { width: 32px; }
        .bg-note { color: var(--muted); font-size: 12px; align-self: center; }
        .qa-rover-overlay { position: fixed; z-index: 60; right: 0; bottom: 18px; left: 0; height: 112px; overflow: visible; pointer-events: none; }
        .qa-rover { position: absolute; bottom: 0; left: 0; display: grid; justify-items: center; gap: 2px; width: 116px; animation: rover-crossing 12s linear infinite alternate; }
        .qa-rover img { display: block; width: 94px; height: 76px; overflow: visible; }
        .qa-rover-label { max-width: 180px; padding: 3px 8px; border: 1px solid #b9d3eb; border-radius: 3px; color: #17365d; background: rgba(255,255,255,.94); font-size: 11px; font-weight: 700; white-space: nowrap; }
        .qa-rover.launch { animation: rover-launch 1.15s ease-in forwards; }
        .qa-rover.launch::after { position: absolute; z-index: -1; bottom: 57px; left: 43px; width: 30px; height: 36px; background: linear-gradient(#ffd05c, #f05b35); clip-path: polygon(50% 100%, 0 0, 100% 0); content: ''; }
        @keyframes rover-crossing { from { transform: translateX(-8px); } to { transform: translateX(calc(100vw - 116px)); } }
        @keyframes rover-launch { to { transform: translate(100vw, -100vh) rotate(24deg); opacity: 0; } }
        @media (prefers-reduced-motion: reduce) { .qa-rover { animation: none; left: calc(50% - 58px); } .qa-rover.launch { animation: none; visibility: hidden; } }
        @media (max-width: 780px) { header { height: 112px; flex-wrap: wrap; align-content: center; padding: 10px 12px; } .brand { font-size: 16px; } .header-actions { width: calc(100% - 44px); margin-left: 44px; justify-content: flex-end; gap: 6px; } .header-actions button { padding: 7px 9px; } .shell { margin-top: 112px; grid-template-columns: minmax(0, 1fr); } aside { display: none; } main { grid-column: 1; min-width: 0; padding: 12px; } .module-card, .module-head, .module-actions { min-width: 0; } .module-head { flex-wrap: wrap; } .module-actions { width: 100%; margin-left: 0; flex-wrap: wrap; } .case-browser { padding: 12px; } .case-browser-toolbar { flex-wrap: wrap; } .case-browser-toolbar input { margin-left: 0; } .counts { grid-template-columns: repeat(2, 1fr); } .run-card { display: block; } .badge { display: inline-block; margin-top: 12px; } table { display: block; overflow-x: auto; } }
  </style>
</head>
<body>
    {% if is_running %}<div class="qa-rover-overlay" id="qa-rover-overlay" role="status" aria-live="polite"><div class="qa-rover"><img src="{{ rover_url }}" alt="" /><span class="qa-rover-label">Running {{ current_section }}</span></div></div>{% endif %}
    <header>
        <button class="menu" aria-label="Menu">☰</button>
        <div class="brand">Comsci Web Automation Dashboard</div>
        <div class="header-actions">
            <span class="testrail-status {% if testrail_connected %}connected{% endif %}"><i></i>TestRail</span>
            <form method="post" action="{{ codegen_url }}"><button type="submit">Open Playwright Codegen</button></form>
            <form method="post" action="{{ run_url }}"><button class="primary" type="submit">▶ Run All</button></form>
        </div>
    </header>
    <div class="shell">
        <aside>
            <p class="nav-title">Workspace</p>
            <a class="nav-item {% if active_section == 'Authentication' %}active{% endif %}" href="{{ authentication_url }}"><span>Authentication</span><span class="nav-count">{{ auth_summary.cases_passed or 0 }}/{{ auth_summary.total_cases or 0 }}</span></a>
            <span class="nav-group">Header</span>
            <a class="nav-sub {% if active_section == 'Header' %}active{% endif %}" href="{{ header_url }}"><span>Search</span><span class="nav-count">{{ header_summary.cases_passed or 0 }}/{{ header_summary.total_cases or 0 }}</span></a>
            <a class="nav-item" href="{{ scheduler_url }}">Scheduler</a>
        </aside>
        <main>
            <section class="run-card">
                {% set run_testrail = last_run.get("testrail", {}) %}
                <div><h1>Latest run</h1><p class="subtle">{{ active_section }} · {{ generated_at or 'No test run has been recorded yet.' }}</p>{% if last_run.section == active_section and run_testrail.plan_url %}<p class="subtle">TestRail: <a href="{{ run_testrail.plan_url }}" target="_blank" rel="noreferrer">Plan #{{ run_testrail.plan_id }}</a> · {{ run_testrail.published_results }} results published</p>{% elif last_run.section == active_section and run_testrail.error %}<p class="subtle">TestRail publish: {{ run_testrail.error }}</p>{% endif %}</div>
                {% if generated_at %}<span class="badge">COMPLETED</span>{% endif %}
            </section>
            {% if active_section == 'Authentication' %}
            <section id="authentication" class="module-card">
                <div class="module-head"><div><h2>Authentication</h2><p class="subtle">Executed UI authentication cases</p></div><div class="module-actions"><span class="bg-note" id="authentication-bg-note"></span><button type="button" id="authentication-show-cases-btn" onclick="toggleCaseBrowser('Authentication','authentication-cases-table','authentication-case-browser','authentication-show-cases-btn','authentication-case-filter','authentication-case-note')">Show Cases</button><button type="button" id="authentication-run-bg-btn" onclick="runSection('background','Authentication','authentication-cases-table',null,'authentication-bg-note','authentication-run-bg-btn','/api/run-section-background')">Run Background</button><button class="primary" type="button" id="authentication-run-live-btn" onclick="runSection('live','Authentication','authentication-cases-table','authentication-live','authentication-bg-note','authentication-run-live-btn','/api/run-section-live')">▶ Run Live</button></div></div>
                <div class="case-browser" id="authentication-case-browser" hidden><div class="case-browser-toolbar"><span class="case-state" id="authentication-case-note">TestRail cases load when shown.</span><input id="authentication-case-filter" type="search" placeholder="Filter cases" aria-label="Filter Authentication cases" oninput="filterCases('authentication-cases-table',this.value)" /></div><div class="case-browser-table-wrap"><table id="authentication-cases-table" data-loaded="false"><thead><tr><th class="select-col"></th><th>Case</th><th>Section</th><th>Automation</th></tr></thead><tbody></tbody></table></div></div>
                <div class="live-panel" id="authentication-live" hidden><pre class="live-output"></pre></div>
                <div class="counts">
                    <div class="count"><b>{{ auth_summary.total_cases or 0 }}</b><span>Mapped cases</span></div>
                    <div class="count"><b>{{ auth_summary.cases_passed or 0 }}</b><span>Passed</span></div>
                    <div class="count"><b>{{ auth_summary.cases_failed or 0 }}</b><span>Failed</span></div>
                    <div class="count"><b>{{ auth_summary.cases_skipped or 0 }}</b><span>Skipped</span></div>
                    <div class="count"><b>{{ auth_summary.cases_not_run or 0 }}</b><span>Not run</span></div>
                </div>
                {% if auth_results %}
                <table id="authentication-results-table"><thead><tr><th>Status</th><th>Test case</th><th>TestRail</th><th>Output</th></tr></thead><tbody>
                {% for result in auth_results %}<tr>
                    <td><span class="status {{ result.status }}">{% if result.status == 'passed' %}✓{% elif result.status == 'failed' %}!{% elif result.status == 'skipped' %}−{% else %}•{% endif %}</span></td>
                    <td><span class="case-title">{{ result.title }}</span><div class="labels">{% for label in result.labels %}<span class="label">{{ label }}</span>{% endfor %}</div><div class="node">{{ result.nodeid }}</div></td>
                    <td>{% if result.testrail_case_url %}<a href="{{ result.testrail_case_url }}" target="_blank" rel="noreferrer">{{ result.testrail_case_id }}</a>{% else %}Not mapped{% endif %}</td>
                    <td><details><summary>Expand output</summary><pre>{{ result.details or ('Not executed yet.' if result.status == 'not_run' else 'Completed successfully.') }}</pre></details>{% if result.screenshots %}{% for evidence in result.screenshots %}<a class="evidence" href="{{ report_prefix }}/{{ evidence.path }}" target="_blank">{{ evidence.label }}</a>{% endfor %}{% elif result.screenshot %}<a class="evidence" href="{{ report_prefix }}/{{ result.screenshot }}" target="_blank">View screenshot</a>{% endif %}</td>
                </tr>{% endfor %}
                </tbody></table>
                {% else %}<p class="empty">No Authentication cases have been executed yet.</p>{% endif %}
                <details class="locator-catalog"><summary>Configured locators</summary>{% for locator in locator_catalog %}<div class="locator-row"><span class="locator-name">{{ locator[0] }}</span><code>{{ locator[1] }}</code></div>{% endfor %}</details>
            </section>
            {% endif %}
            {% if active_section == 'Header' %}
            <section id="header" class="module-card">
                <div class="module-head"><div><h2>Header</h2><p class="subtle">Executed header interaction cases</p></div><div class="module-actions"><span class="bg-note" id="header-bg-note"></span><button type="button" id="header-show-cases-btn" onclick="toggleCaseBrowser('Header','header-cases-table','header-case-browser','header-show-cases-btn','header-case-filter','header-case-note')">Show Cases</button><button type="button" id="header-run-bg-btn" onclick="runSection('background','Header','header-cases-table',null,'header-bg-note','header-run-bg-btn','/api/run-section-background')">Run Background</button><button class="primary" type="button" id="header-run-live-btn" onclick="runSection('live','Header','header-cases-table','header-live','header-bg-note','header-run-live-btn','/api/run-section-live')">▶ Run Live</button></div></div>
                <div class="case-browser" id="header-case-browser" hidden><div class="case-browser-toolbar"><span class="case-state" id="header-case-note">TestRail cases load when shown.</span><input id="header-case-filter" type="search" placeholder="Filter cases" aria-label="Filter Header cases" oninput="filterCases('header-cases-table',this.value)" /></div><div class="case-browser-table-wrap"><table id="header-cases-table" data-loaded="false"><thead><tr><th class="select-col"></th><th>Case</th><th>Section</th><th>Automation</th></tr></thead><tbody></tbody></table></div></div>
                <div class="counts">
                    <div class="count"><b>{{ header_summary.total_cases or 0 }}</b><span>Mapped cases</span></div>
                    <div class="count"><b>{{ header_summary.cases_passed or 0 }}</b><span>Passed</span></div>
                    <div class="count"><b>{{ header_summary.cases_failed or 0 }}</b><span>Failed</span></div>
                    <div class="count"><b>{{ header_summary.cases_skipped or 0 }}</b><span>Skipped</span></div>
                    <div class="count"><b>{{ header_summary.cases_not_run or 0 }}</b><span>Not run</span></div>
                </div>
                <div class="module-head"><h3 class="subsection-head">Search</h3></div>
                <div class="live-panel" id="header-live" hidden><pre class="live-output"></pre></div>
                {% if header_results %}
                <table id="header-results-table"><thead><tr><th>Status</th><th>Test case</th><th>TestRail</th><th>Output</th></tr></thead><tbody>
                {% for result in header_results %}<tr>
                    <td><span class="status {{ result.status }}">{% if result.status == 'passed' %}✓{% elif result.status == 'failed' %}!{% elif result.status == 'skipped' %}−{% else %}•{% endif %}</span></td>
                    <td><span class="case-title">{{ result.title }}</span><div class="labels">{% for label in result.labels %}<span class="label">{{ label }}</span>{% endfor %}</div><div class="node">{{ result.nodeid }}</div></td>
                    <td>{% if result.testrail_case_url %}<a href="{{ result.testrail_case_url }}" target="_blank" rel="noreferrer">{{ result.testrail_case_id }}</a>{% else %}Not mapped{% endif %}</td>
                    <td><details><summary>Expand output</summary><pre>{{ result.details or ('Not executed yet.' if result.status == 'not_run' else 'Completed successfully.') }}</pre></details>{% if result.screenshots %}{% for evidence in result.screenshots %}<a class="evidence" href="{{ report_prefix }}/{{ evidence.path }}" target="_blank">{{ evidence.label }}</a>{% endfor %}{% elif result.screenshot %}<a class="evidence" href="{{ report_prefix }}/{{ result.screenshot }}" target="_blank">View screenshot</a>{% endif %}</td>
                </tr>{% endfor %}
                </tbody></table>
                {% else %}<p class="empty">No Header Search cases have been executed yet.</p>{% endif %}
                <details class="locator-catalog"><summary>Configured locators</summary>{% for locator in header_locator_catalog %}<div class="locator-row"><span class="locator-name">{{ locator[0] }}</span><code>{{ locator[1] }}</code></div>{% endfor %}</details>
            </section>
            {% endif %}
        </main>
    </div>
    <script>
        async function toggleCaseBrowser(section, tableId, panelId, buttonId, filterId, noteId) {
            var panel = document.getElementById(panelId);
            var button = document.getElementById(buttonId);
            var table = document.getElementById(tableId);
            var note = document.getElementById(noteId);
            if (!panel.hidden) {
                panel.hidden = true;
                button.textContent = 'Show Cases';
                return;
            }
            panel.hidden = false;
            if (table.dataset.loaded === 'true') {
                button.textContent = 'Hide Cases';
                return;
            }

            button.disabled = true;
            button.textContent = 'Loading...';
            note.textContent = 'Loading TestRail cases...';
            try {
                var response = await fetch('/api/testrail-cases?section=' + encodeURIComponent(section));
                var payload = await response.json();
                if (!response.ok) { throw new Error(payload.error || response.statusText); }
                var body = table.querySelector('tbody');
                body.replaceChildren();
                payload.cases.forEach(function (item) {
                    var row = document.createElement('tr');
                    var selectCell = document.createElement('td');
                    var checkbox = document.createElement('input');
                    checkbox.type = 'checkbox';
                    checkbox.className = 'case-select';
                    checkbox.value = item.runnable_nodeid || '';
                    checkbox.disabled = !item.runnable_nodeid;
                    selectCell.appendChild(checkbox);
                    row.appendChild(selectCell);

                    var titleCell = document.createElement('td');
                    var title = document.createElement(item.url ? 'a' : 'span');
                    title.textContent = 'C' + item.id + ' · ' + item.title;
                    if (item.url) { title.href = item.url; title.target = '_blank'; title.rel = 'noreferrer'; }
                    titleCell.appendChild(title);
                    row.appendChild(titleCell);

                    var sectionCell = document.createElement('td');
                    sectionCell.textContent = item.section || 'Unassigned';
                    row.appendChild(sectionCell);

                    var stateCell = document.createElement('td');
                    stateCell.className = 'case-state' + (item.runnable_nodeid ? ' runnable' : '');
                    stateCell.textContent = item.runnable_nodeid ? 'Runnable here' : (item.automation_section ? 'Runnable in ' + item.automation_section : 'Not automated');
                    row.appendChild(stateCell);
                    row.dataset.search = (item.id + ' ' + item.title + ' ' + item.section).toLowerCase();
                    body.appendChild(row);
                });
                if (!payload.cases.length) {
                    var emptyRow = document.createElement('tr');
                    var emptyCell = document.createElement('td');
                    emptyCell.colSpan = 4;
                    emptyCell.className = 'empty';
                    emptyCell.textContent = 'No TestRail cases were found.';
                    emptyRow.appendChild(emptyCell);
                    body.appendChild(emptyRow);
                }
                table.dataset.loaded = 'true';
                document.getElementById(filterId).value = '';
                var runnableCount = payload.cases.filter(function (item) { return Boolean(item.runnable_nodeid); }).length;
                note.textContent = payload.cases.length + ' cases · ' + runnableCount + ' runnable in ' + section + '.';
                button.textContent = 'Hide Cases';
            } catch (error) {
                note.textContent = 'Could not load cases: ' + error.message;
                button.textContent = 'Retry Cases';
            } finally {
                button.disabled = false;
            }
        }

        function filterCases(tableId, query) {
            var normalized = query.trim().toLowerCase();
            document.querySelectorAll('#' + tableId + ' tbody tr[data-search]').forEach(function (row) {
                row.hidden = !row.dataset.search.includes(normalized);
            });
        }

        async function runSection(mode, section, tableId, panelId, noteId, buttonId, endpoint) {
            var table = document.getElementById(tableId);
            var cases = table ? Array.from(table.querySelectorAll('.case-select:checked')).map(function (cb) { return cb.value; }) : [];
            var caseBrowser = table ? table.closest('.case-browser') : null;
            var button = document.getElementById(buttonId);
            var panel = panelId ? document.getElementById(panelId) : null;
            var output = panel ? panel.querySelector('.live-output') : null;
            var note = noteId ? document.getElementById(noteId) : null;
            if (caseBrowser && !caseBrowser.hidden && table.dataset.loaded !== 'true') {
                if (note) { note.textContent = 'Load the case list before running a selection, or hide it to run the whole section.'; }
                return;
            }
            if (caseBrowser && !caseBrowser.hidden && !cases.length) {
                if (note) { note.textContent = 'Select at least one case, or hide the list to run the whole section.'; }
                return;
            }
            var startMessage = cases.length ? ('Starting run for ' + cases.length + ' selected case(s)...') : 'Starting run for all cases in this section...';
            button.disabled = true;
            if (panel) { panel.hidden = false; }
            if (output) { output.textContent = startMessage + '\\n'; }
            if (note) { note.textContent = startMessage; }
            var response;
            try {
                response = await fetch(endpoint, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ section: section, cases: cases })
                });
            } catch (err) {
                var failMessage = 'Failed to start run: ' + err;
                if (output) { output.textContent = failMessage; }
                if (note) { note.textContent = failMessage; }
                button.disabled = false;
                return;
            }
            if (!response.ok) {
                var errorBody = await response.json().catch(function () { return {}; });
                var message = 'Failed to start run: ' + (errorBody.error || response.statusText);
                if (output) { output.textContent = message; }
                if (note) { note.textContent = message; }
                button.disabled = false;
                return;
            }
            showQaRover(section);
            pollRunStatus(mode, output, note, button);
        }

        function showQaRover(section) {
            var overlay = document.getElementById('qa-rover-overlay');
            if (!overlay) {
                overlay = document.createElement('div');
                overlay.className = 'qa-rover-overlay';
                overlay.id = 'qa-rover-overlay';
                overlay.setAttribute('role', 'status');
                overlay.setAttribute('aria-live', 'polite');
                var rover = document.createElement('div');
                rover.className = 'qa-rover';
                var image = document.createElement('img');
                image.src = '{{ rover_url }}';
                image.alt = '';
                var label = document.createElement('span');
                label.className = 'qa-rover-label';
                rover.append(image, label);
                overlay.appendChild(rover);
                document.body.appendChild(overlay);
            }
            overlay.querySelector('.qa-rover-label').textContent = 'Running ' + section;
        }

        async function pollRunStatus(mode, output, note, button) {
            var response = await fetch('/api/run-status');
            var state = await response.json();
            var rover = document.querySelector('#qa-rover-overlay .qa-rover');
            var roverLabel = document.querySelector('#qa-rover-overlay .qa-rover-label');
            if (roverLabel && state.current_section) { roverLabel.textContent = 'Running ' + state.current_section; }
            if (output) {
                output.textContent = state.output || '';
                output.scrollTop = output.scrollHeight;
            }
            if (state.running) {
                setTimeout(function () { pollRunStatus(mode, output, note, button); }, 1200);
                return;
            }
            if (rover) {
                rover.classList.add('launch');
                setTimeout(function () { window.location.reload(); }, 1200);
            }
            if (button) { button.disabled = false; }
            var summary = '';
            if (state.testrail && state.testrail.plan_url) {
                summary = 'TestRail plan: ' + state.testrail.plan_url + ' (' + (state.testrail.published_results || 0) + ' results published)';
            } else if (state.testrail && state.testrail.error) {
                summary = 'TestRail publish: ' + state.testrail.error;
            } else {
                summary = 'Completed.';
            }
            if (output) { output.textContent += '\\n' + summary; }
            if (note) { note.textContent = summary; }
            if (!rover) { setTimeout(function () { window.location.reload(); }, 1500); }
        }

        {% if is_running %}pollRunStatus('{{ run_mode }}', null, null, null);{% endif %}
    </script>
</body>
</html>
"""


SCHEDULER_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <title>Scheduler · Comsci Web Automation Dashboard</title>
    <style>
        :root { --blue: #3478d3; --ink: #17365d; --line: #d9e2ef; --surface: #fff; --ground: #f2f6fb; --muted: #6681a1; }
        * { box-sizing: border-box; }
        body { margin: 0; color: var(--ink); background: var(--ground); font: 14px "Segoe UI", Tahoma, sans-serif; }
        header { position: fixed; z-index: 30; inset: 0 0 auto; height: 72px; display: flex; align-items: center; padding: 0 20px; color: white; background: var(--blue); }
        .brand { font-size: 18px; font-weight: 700; }
        .shell { display: grid; grid-template-columns: 190px minmax(0, 1fr); min-height: 100vh; padding-top: 72px; }
        aside { position: fixed; top: 72px; bottom: 0; width: 190px; padding: 18px 12px; border-right: 1px solid var(--line); background: white; }
        .nav-title { margin: 0 0 9px; color: #7c91aa; font-size: 11px; font-weight: 700; text-transform: uppercase; }
        .nav-item { display: block; padding: 10px 12px; border-left: 3px solid transparent; color: var(--ink); font-weight: 700; text-decoration: none; }
        .nav-item.active { border-left-color: var(--blue); background: #edf5ff; }
        main { grid-column: 2; width: min(1440px, 100%); margin: 0 auto; padding: 24px; }
        h1, h2, p { margin-top: 0; }
        h1 { margin-bottom: 5px; font-size: 22px; }
        h2 { margin: 0; font-size: 17px; }
        .subtle, .timezone { color: var(--muted); font-size: 12px; }
        .timezone { margin: 0 0 16px; }
        .module-card { margin-bottom: 16px; overflow: hidden; border: 1px solid var(--line); border-radius: 6px; background: var(--surface); box-shadow: 0 1px 2px rgba(25,60,100,.04); }
        .module-head { display: flex; align-items: center; padding: 16px 20px; border-bottom: 1px solid var(--line); }
        .form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px 18px; padding: 18px 20px; }
        label.field { display: grid; gap: 6px; color: var(--ink); font-size: 12px; font-weight: 700; }
        input[type=text], input[type=datetime-local], select { width: 100%; min-height: 38px; padding: 8px 10px; border: 1px solid #c8d8ee; border-radius: 4px; background: white; color: var(--ink); font: inherit; }
        .section-options { display: flex; flex-wrap: wrap; gap: 8px 18px; padding-top: 4px; }
        .section-options label { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 500; }
        .section-field, .form-actions { grid-column: 1 / -1; }
        .form-actions { display: flex; align-items: center; gap: 12px; }
        button { padding: 8px 12px; border: 1px solid #c8d8ee; border-radius: 4px; background: white; color: var(--ink); font-weight: 700; cursor: pointer; }
        button.primary { border-color: var(--blue); color: white; background: var(--blue); }
        .notice { margin: 0 0 14px; padding: 10px 12px; border: 1px solid #f0c6bc; border-radius: 4px; color: #8c3020; background: #fff6f3; }
        .notice.success { border-color: #b8dfc6; color: #17643b; background: #f2fbf5; }
        .table-wrap { overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; }
        th { padding: 10px 12px; color: var(--muted); font-size: 11px; text-align: left; text-transform: uppercase; white-space: nowrap; }
        td { padding: 11px 12px; border-top: 1px solid var(--line); vertical-align: top; }
        .actions { display: flex; gap: 6px; white-space: nowrap; }
        .actions form { margin: 0; }
        .actions button { padding: 6px 8px; font-size: 12px; }
        .state { font-weight: 700; text-transform: capitalize; }
        .state.active, .outcome.passed { color: #197141; }
        .state.paused { color: #946200; }
        .state.completed, .outcome.interrupted { color: var(--muted); }
        .outcome.failed { color: #b42318; }
        a { color: #1769d2; font-weight: 600; }
        .empty { padding: 20px; color: var(--muted); }
        @media (max-width: 780px) { header { height: 60px; } .shell { display: block; padding-top: 60px; } aside { position: static; width: auto; padding: 8px 12px; border-right: 0; border-bottom: 1px solid var(--line); } aside .nav-title { display: none; } .nav-item { display: inline-block; } main { padding: 12px; } .form-grid { grid-template-columns: 1fr; padding: 14px; } .section-field, .form-actions { grid-column: auto; } }
    </style>
</head>
<body>
    <header><div class="brand">Comsci Web Automation Dashboard</div></header>
    <div class="shell">
        <aside><p class="nav-title">Workspace</p><a class="nav-item" href="{{ authentication_url }}">Authentication</a><a class="nav-item" href="{{ header_url }}">Header</a><a class="nav-item active" href="{{ scheduler_url }}">Scheduler</a></aside>
        <main>
            <h1>Scheduler</h1>
            <p class="subtle">Automated test runs</p>
            <p class="timezone">Times are shown in the server's local time zone: <strong>{{ timezone }}</strong></p>
            {% if request.args.get('message') %}<p class="notice {{ request.args.get('category', 'success') }}">{{ request.args.get('message') }}</p>{% endif %}
            <section class="module-card">
                <div class="module-head"><h2>Create schedule</h2></div>
                <form method="post" action="{{ create_url }}" class="form-grid">
                    <label class="field">Schedule name<input type="text" name="name" maxlength="100" required /></label>
                    <label class="field">Environment<select name="environment" required>{% for name in environments %}<option value="{{ name }}">{{ name }}</option>{% endfor %}</select></label>
                    <label class="field">Run date and time<input type="datetime-local" name="first_run" required /></label>
                    <label class="field">Recurrence<select name="recurrence"><option value="once">One-time</option><option value="weekly">Weekly</option><option value="monthly">Monthly</option></select></label>
                    <div class="field section-field"><strong>Test sections</strong><div class="section-options"><label><input type="checkbox" name="sections" value="__all__" />All sections</label>{% for section in sections %}<label><input type="checkbox" name="sections" value="{{ section }}" />{{ section }}</label>{% endfor %}</div></div>
                    <div class="form-actions"><button class="primary" type="submit" {% if not environments %}disabled{% endif %}>Create schedule</button>{% if not environments %}<span class="subtle">Configure SCHEDULE_ENVIRONMENTS before creating schedules.</span>{% endif %}</div>
                </form>
            </section>
            <section class="module-card">
                <div class="module-head"><h2>Schedules</h2></div>
                {% if schedules %}<div class="table-wrap"><table><thead><tr><th>Name</th><th>Environment</th><th>Sections</th><th>Recurrence</th><th>Status</th><th>Next run ({{ timezone }})</th><th>Actions</th></tr></thead><tbody>
                {% for schedule in schedules %}<tr><td>{{ schedule.name }}</td><td>{{ schedule.environment }}</td><td>{{ schedule.sections|join(', ') }}</td><td>{{ schedule.recurrence }}</td><td><span class="state {{ schedule.status }}">{{ schedule.status }}</span></td><td>{{ schedule.next_run|replace('T', ' ') or '—' }}</td><td><div class="actions">{% if schedule.next_run %}<form method="post" action="{{ url_for('scheduler_toggle', schedule_id=schedule.id) }}"><input type="hidden" name="enabled" value="{{ 0 if schedule.enabled else 1 }}" /><button type="submit">{{ 'Pause' if schedule.enabled else 'Resume' }}</button></form>{% endif %}<form method="post" action="{{ url_for('scheduler_delete', schedule_id=schedule.id) }}" onsubmit="return confirm('Delete this schedule? Run history is retained.');"><button type="submit">Delete</button></form></div></td></tr>{% endfor %}
                </tbody></table></div>{% else %}<p class="empty">No schedules yet.</p>{% endif %}
            </section>
            <section class="module-card">
                <div class="module-head"><h2>Recent run history</h2></div>
                {% if history %}<div class="table-wrap"><table><thead><tr><th>Started</th><th>Schedule</th><th>Environment</th><th>Sections</th><th>Outcome</th><th>Log</th></tr></thead><tbody>
                {% for run in history %}<tr><td>{{ run.started_at|replace('T', ' ') }}</td><td>{{ run.schedule_name }}</td><td>{{ run.environment }}</td><td>{{ run.sections|join(', ') }}</td><td><span class="outcome {{ run.outcome }}">{{ run.outcome }}</span></td><td><a href="{{ url_for('scheduler_log', run_id=run.id) }}">Download log</a></td></tr>{% endfor %}
                </tbody></table></div>{% else %}<p class="empty">No scheduled runs have completed yet.</p>{% endif %}
            </section>
        </main>
    </div>
</body>
</html>
"""


@app.route("/")
def index():
    return redirect(url_for("section_page", section_name="authentication"))


@app.route("/sections/<section_name>")
def section_page(section_name: str):
    section_lookup = {"authentication": "Authentication", "header": "Header"}
    active_section = section_lookup.get(section_name.lower())
    if active_section is None:
        return "Unknown dashboard section.", 404
    return _render_section(active_section)


def _render_section(active_section: str):
    from utils.page_objects.header_search_page import HeaderSearchPage
    from utils.page_objects.login_page import LoginPage

    payload = _load_dashboard_payload()
    codegen_status = {}
    if LAST_CODEGEN_JSON.exists():
        codegen_status = json.loads(LAST_CODEGEN_JSON.read_text(encoding="utf-8"))
    authentication = next(
        (section for section in payload.get("sections", []) if section.get("name") == "Authentication"),
        {"results": []},
    )
    header = next(
        (section for section in payload.get("sections", []) if section.get("name") == "Header"),
        {"results": []},
    )
    catalog = _load_test_catalog()
    auth_rows = _merge_section_rows(catalog, authentication.get("results", []), "Authentication")
    header_rows = _merge_section_rows(catalog, header.get("results", []), "Header")
    auth_summary = _rollup_rows(auth_rows)
    header_summary = _rollup_rows(header_rows)
    with _live_run_lock:
        is_running = bool(_live_run_state["running"])
        current_section = _live_run_state["current_section"] or _live_run_state["section"] or "All tests"
        run_mode = _live_run_state["mode"] or "background"
    return render_template_string(
        INDEX_TEMPLATE,
        run_url=url_for("run_suite"),
        view_url=url_for("view_report"),
        codegen_url=url_for("open_codegen"),
        codegen_status=codegen_status.get("status", ""),
        testrail_connected=_testrail_connected(),
        active_section=active_section,
        authentication_url=url_for("section_page", section_name="authentication"),
        header_url=url_for("section_page", section_name="header"),
        scheduler_url=url_for("scheduler_page"),
        generated_at=payload.get("generated_at", ""),
        last_run=_load_last_run(),
        active_results=(auth_rows if active_section == "Authentication" else header_rows),
        auth_results=auth_rows,
        auth_summary=auth_summary,
        locator_catalog=LoginPage.LOCATOR_CATALOG,
        header_results=header_rows,
        header_summary=header_summary,
        header_locator_catalog=HeaderSearchPage.LOCATOR_CATALOG,
        report_prefix=url_for("report_file", filename="").rstrip("/"),
        is_running=is_running,
        current_section=current_section,
        run_mode=run_mode,
        rover_url=url_for("qa_rover"),
    )


@app.route("/scheduler")
def scheduler_page():
    return render_template_string(
        SCHEDULER_TEMPLATE,
        schedules=_get_scheduler_store().list_schedules(),
        history=_get_scheduler_store().list_history(),
        environments=list(_scheduler_environments()),
        sections=list(SECTION_RUN_CONFIG),
        timezone=_server_timezone(),
        authentication_url=url_for("section_page", section_name="authentication"),
        header_url=url_for("section_page", section_name="header"),
        scheduler_url=url_for("scheduler_page"),
        create_url=url_for("scheduler_create"),
    )


@app.route("/scheduler/create", methods=["POST"])
def scheduler_create():
    try:
        name = request.form.get("name", "").strip()
        environment = request.form.get("environment", "")
        recurrence = request.form.get("recurrence", "")
        first_run = datetime.fromisoformat(request.form.get("first_run", ""))
        submitted_sections = request.form.getlist("sections")
        sections = list(SECTION_RUN_CONFIG) if "__all__" in submitted_sections else submitted_sections
        sections = [section for section in SECTION_RUN_CONFIG if section in sections]
        if environment not in _scheduler_environments():
            raise ValueError("Choose a configured target environment.")
        if not sections:
            raise ValueError("Select at least one test section.")
        schedule_id = _get_scheduler_store().create_schedule(
            name,
            environment,
            sections,
            recurrence,
            first_run,
            _server_timezone(),
        )
        message, category = f"Schedule #{schedule_id} created.", "success"
    except (ValueError, TypeError) as exc:
        message, category = str(exc) or "Enter a valid run date and time.", "error"
    return redirect(url_for("scheduler_page", message=message, category=category))


@app.route("/scheduler/<int:schedule_id>/toggle", methods=["POST"])
def scheduler_toggle(schedule_id: int):
    enabled = request.form.get("enabled") == "1"
    if not _get_scheduler_store().set_enabled(schedule_id, enabled):
        message, category = "That schedule is no longer available to change.", "error"
    else:
        message, category = ("Schedule resumed." if enabled else "Schedule paused."), "success"
    return redirect(url_for("scheduler_page", message=message, category=category))


@app.route("/scheduler/<int:schedule_id>/delete", methods=["POST"])
def scheduler_delete(schedule_id: int):
    if not _get_scheduler_store().delete_schedule(schedule_id):
        message, category = "Schedule not found.", "error"
    else:
        message, category = "Schedule deleted. Existing run history was retained.", "success"
    return redirect(url_for("scheduler_page", message=message, category=category))


@app.route("/scheduler/logs/<int:run_id>")
def scheduler_log(run_id: int):
    log_path = _get_scheduler_store().get_log_path(run_id)
    if log_path is None or not log_path.is_file():
        abort(404)
    return send_file(log_path, as_attachment=True, download_name=log_path.name, mimetype="text/plain")


@app.route("/api/testrail-cases")
def api_testrail_cases():
    section = request.args.get("section", "")
    if section not in SECTION_RUN_CONFIG:
        return jsonify({"error": "Unknown dashboard section."}), 400

    try:
        cases, sections = _load_testrail_case_data()
    except Exception:
        app.logger.exception("Could not load TestRail cases for dashboard")
        return jsonify({"error": "TestRail cases could not be loaded. Check the dashboard configuration and connection."}), 502

    section_names = _testrail_section_names(sections)
    mappings = {}
    for entry in _load_test_catalog():
        case_id = str(entry.get("testrail_case_id", "")).strip()
        if case_id:
            mappings.setdefault(case_id, []).append(entry)

    case_url_base = os.getenv("TESTRAIL_URL", "").strip().rstrip("/")
    response_cases = []
    for case in cases:
        case_id = str(case.get("id", ""))
        case_mappings = mappings.get(case_id, [])
        mapping = next((entry for entry in case_mappings if entry.get("section") == section), None)
        other_mapping = mapping or next(iter(case_mappings), None)
        section_id = int(case.get("section_id") or 0)
        response_cases.append(
            {
                "id": case_id,
                "title": str(case.get("title", "Untitled case")),
                "section": section_names.get(section_id, "Unassigned"),
                "runnable_nodeid": mapping.get("nodeid", "") if mapping else "",
                "automation_section": other_mapping.get("section", "") if other_mapping else "",
                "url": f"{case_url_base}/index.php?/cases/view/{case_id}" if case_url_base and case_id else "",
            }
        )

    response_cases.sort(key=lambda item: (item["section"].casefold(), item["title"].casefold(), item["id"]))
    return jsonify({"cases": response_cases, "total": len(response_cases)})


@app.route("/run", methods=["POST"])
def run_suite():
    target = request.form.get("target") or None
    with _run_lock:
        _start_section_run([target] if target else [], None, "background")
    return redirect(url_for("index"))


def _handle_section_run_request(mode: str):
    payload = request.get_json(silent=True) or {}
    section = str(payload.get("section", ""))
    cases = [str(case).strip() for case in payload.get("cases", []) if str(case).strip()]
    config = SECTION_RUN_CONFIG.get(section)
    if config is None:
        return jsonify({"error": "Unknown dashboard section."}), 400
    if cases:
        allowed_cases = {
            entry.get("nodeid")
            for entry in _load_test_catalog()
            if entry.get("section") == section
        }
        if any(case not in allowed_cases for case in cases):
            return jsonify({"error": "Selected cases must be automated cases in this section."}), 400
    pytest_args = cases if cases else [config["target"]]
    if not _start_section_run(pytest_args, section, mode):
        return jsonify({"error": "A test run is already in progress."}), 409
    return jsonify({"status": "started", "section": section, "cases": cases, "mode": mode})


@app.route("/api/run-section-live", methods=["POST"])
def run_section_live():
    return _handle_section_run_request("live")


@app.route("/api/run-section-background", methods=["POST"])
def run_section_background():
    return _handle_section_run_request("background")


@app.route("/api/run-status")
def run_status():
    with _live_run_lock:
        return jsonify(dict(_live_run_state))


@app.route("/codegen", methods=["POST"])
def open_codegen():
    try:
        _launch_codegen()
    except ValueError as exc:
        return str(exc), 400
    return redirect(url_for("index"))


@app.route("/report")
def view_report():
    html_path = REPORTS_DIR / "test_dashboard.html"
    if not html_path.exists():
        return "No dashboard report yet. Run the suite first.", 404
    return html_path.read_text(encoding="utf-8")


@app.route("/reports/<path:filename>")
def report_file(filename: str):
    return send_from_directory(REPORTS_DIR, filename)


@app.route("/qa-rover.svg")
def qa_rover():
    return send_from_directory(ROOT_DIR, "qa-rover.svg", mimetype="image/svg+xml")


@app.route("/api/dashboard")
def api_dashboard():
    return jsonify(_load_dashboard_payload())


@app.route("/api/last_run")
def api_last_run():
    if not LAST_RUN_JSON.exists():
        return jsonify({})
    return jsonify(json.loads(LAST_RUN_JSON.read_text(encoding="utf-8")))


if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", "5062"))
    start_scheduler_service()
    try:
        app.run(host="0.0.0.0", port=port, debug=False)
    finally:
        if _scheduler_worker:
            _scheduler_worker.stop()
