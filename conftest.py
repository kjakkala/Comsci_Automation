import os
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv()


def pytest_addoption(parser):
    parser.addoption(
        "--dashboard-dir",
        action="store",
        default="reports",
        help="Directory to write dashboard JSON/HTML output to.",
    )


# --- Playwright defaults sourced from .env, so pytest-playwright doesn't need CLI flags ---
@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    return {**browser_type_launch_args, "headless": os.getenv("HEADLESS", "true").strip().lower() != "false"}


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "base_url": os.getenv("BASE_URL", "").rstrip("/") or None}


def _marker_value(item, name: str, default: str = "") -> str:
    marker = item.get_closest_marker(name)
    if not marker:
        return default
    if marker.args:
        return str(marker.args[0])
    if "value" in marker.kwargs:
        return str(marker.kwargs["value"])
    return default


def _build_testrail_link(case_id: str) -> str:
    template = os.getenv(
        "TESTRAIL_CASE_URL_TEMPLATE",
        "https://upland.testrail.com/index.php?/cases/view/{case_id}",
    ).strip()
    if not template or not case_id:
        return ""
    return template.format(case_id=case_id)


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Stash the report for each phase (setup/call/teardown) on the test item."""
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


@pytest.fixture(autouse=True)
def _capture_failure_screenshot(request):
    """On failure, capture a screenshot from the `page` fixture (if used) for the dashboard."""
    yield
    rep_call = getattr(request.node, "rep_call", None)
    if rep_call is None or not rep_call.failed:
        return
    page = request.node.funcargs.get("page")
    if page is None:
        return
    dashboard_dir = Path(request.config.getoption("--dashboard-dir")).resolve()
    screenshots_dir = dashboard_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    safe_name = request.node.nodeid.replace("::", "__").replace("/", "_").replace("\\", "_")
    screenshot_path = screenshots_dir / f"{safe_name}.png"
    try:
        page.screenshot(path=str(screenshot_path))
        request.node._dashboard_screenshot = str(screenshot_path.relative_to(dashboard_dir))
    except Exception:
        pass


def pytest_configure(config):
    config._dashboard_results = []
    config._dashboard_started_at = datetime.now(timezone.utc)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    outcome = yield
    setup_rep = getattr(item, "rep_setup", None)
    call_rep = getattr(item, "rep_call", None)
    relevant = call_rep if call_rep is not None else setup_rep
    if relevant is None:
        return outcome

    if relevant.when == "setup" and relevant.outcome == "passed":
        return outcome

    status = "skipped" if relevant.outcome == "skipped" else ("failed" if relevant.outcome == "failed" else "passed")

    section = _marker_value(item, "page_section", "Uncategorized")
    flow = _marker_value(item, "ui_flow", "")
    testrail_case_id = _marker_value(item, "testrail_case", "")
    title = _marker_value(item, "dashboard_title", item.name)

    details = ""
    if call_rep is not None and call_rep.longrepr:
        details = str(call_rep.longrepr)
    elif setup_rep is not None and setup_rep.longrepr:
        details = str(setup_rep.longrepr)

    duration = (call_rep.duration if call_rep is not None else 0.0) + (setup_rep.duration if setup_rep is not None else 0.0)

    item.config._dashboard_results.append(
        {
            "nodeid": item.nodeid,
            "section": section,
            "operation": flow,
            "title": title,
            "testrail_case_id": testrail_case_id,
            "testrail_case_url": _build_testrail_link(testrail_case_id),
            "status": status,
            "duration": round(duration, 3),
            "details": details,
            "screenshot": getattr(item, "_dashboard_screenshot", ""),
        }
    )
    return outcome


def _rollup_case_stats(results: list[dict]) -> dict:
    """Roll up execution-level results into per-TestRail-case and per-execution counters."""
    counts = Counter(result["status"] for result in results)
    case_statuses: dict[str, str] = {}
    case_execution_counts: Counter = Counter()
    unmapped_executions = 0
    for result in results:
        case_id = (result.get("testrail_case_id") or "").strip()
        if not case_id:
            unmapped_executions += 1
            continue
        case_execution_counts[case_id] += 1
        current = case_statuses.get(case_id)
        if result["status"] == "failed":
            case_statuses[case_id] = "failed"
        elif result["status"] == "skipped" and current != "failed":
            case_statuses[case_id] = "skipped"
        elif current is None:
            case_statuses[case_id] = "passed"
    case_status_counts = Counter(case_statuses.values())
    cases_with_multiple_executions = sum(1 for n in case_execution_counts.values() if n > 1)

    return {
        "total_executions": len(results),
        "passed_executions": counts.get("passed", 0),
        "failed_executions": counts.get("failed", 0),
        "skipped_executions": counts.get("skipped", 0),
        "unmapped_executions": unmapped_executions,
        "total_cases": len(case_statuses),
        "cases_passed": case_status_counts.get("passed", 0),
        "cases_failed": case_status_counts.get("failed", 0),
        "cases_skipped": case_status_counts.get("skipped", 0),
        "cases_with_multiple_executions": cases_with_multiple_executions,
    }


def _write_dashboard(config) -> None:
    output_dir = Path(config.getoption("--dashboard-dir")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    results = list(getattr(config, "_dashboard_results", []))
    started_at = getattr(config, "_dashboard_started_at", datetime.now(timezone.utc))
    finished_at = datetime.now(timezone.utc)
    counts = Counter(result["status"] for result in results)
    grouped_results: dict[str, list[dict]] = defaultdict(list)
    for result in results:
        grouped_results[result["section"]].append(result)

    overall_rollup = _rollup_case_stats(results)
    by_section = {
        section: _rollup_case_stats(section_results)
        for section, section_results in sorted(grouped_results.items())
    }

    payload = {
        "generated_at": finished_at.isoformat(),
        "started_at": started_at.isoformat(),
        "summary": {
            "total": len(results),
            "passed": counts.get("passed", 0),
            "failed": counts.get("failed", 0),
            "skipped": counts.get("skipped", 0),
            **overall_rollup,
            "by_section": by_section,
        },
        "sections": [
            {"name": section, "results": section_results}
            for section, section_results in sorted(grouped_results.items())
        ],
    }

    json_path = output_dir / "test_dashboard.json"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    section_html: list[str] = []
    for section, section_results in sorted(grouped_results.items()):
        rows: list[str] = []
        for result in section_results:
            case_cell = escape(result["testrail_case_id"] or "Not mapped")
            if result["testrail_case_url"]:
                case_cell = (
                    f'<a href="{escape(result["testrail_case_url"])}" target="_blank" rel="noreferrer">'
                    f"{escape(result['testrail_case_id'])}</a>"
                )
            details = escape(result["details"] or "No details captured.")
            screenshot_cell = ""
            if result.get("screenshot"):
                screenshot_cell = (
                    f'<a href="{escape(result["screenshot"])}" target="_blank" rel="noreferrer">screenshot</a>'
                )
            rows.append(
                "<tr>"
                f'<td><span class="status {escape(result["status"])}">{escape(result["status"].upper())}</span></td>'
                f"<td>{escape(result['title'])}<div class=\"nodeid\">{escape(result['nodeid'])}</div></td>"
                f"<td>{escape(result['operation'])}</td>"
                f'<td>{case_cell}</td>'
                f'<td><details><summary>View output</summary><pre>{details}</pre></details>{screenshot_cell}</td>'
                "</tr>"
            )
        section_html.append(
            "<section class=\"section\">"
            f"<h2>{escape(section)}</h2>"
            "<table>"
            "<thead><tr><th>Status</th><th>Test Case</th><th>UI Flow</th><th>TestRail</th><th>Output</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody>"
            "</table>"
            "</section>"
        )

    html_content = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <title>Web UI Automation Dashboard - Comsci</title>
  <style>
    body {{ font-family: Segoe UI, Arial, sans-serif; background: #f5f1e8; color: #1d2733; margin: 0; }}
    header {{ padding: 24px 32px; border-bottom: 1px solid #d8cfc1; background: #fffdf8; }}
    main {{ padding: 24px 32px; }}
    .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 24px; }}
    .card {{ background: #fffdf8; border: 1px solid #d8cfc1; border-radius: 12px; padding: 14px 16px; }}
    .card .label {{ display: block; font-size: 0.75rem; text-transform: uppercase; color: #5e6b78; }}
    .card .value {{ display: block; margin-top: 6px; font-size: 1.6rem; font-weight: 700; }}
    .section {{ margin-top: 20px; background: #fffdf8; border: 1px solid #d8cfc1; border-radius: 14px; overflow: hidden; }}
    .section h2 {{ margin: 0; padding: 14px 16px; border-bottom: 1px solid #d8cfc1; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 10px 14px; border-bottom: 1px solid #d8cfc1; text-align: left; vertical-align: top; }}
    .status {{ padding: 4px 10px; border-radius: 999px; font-size: 0.75rem; font-weight: 700; }}
    .status.passed {{ background: rgba(43,122,75,0.15); color: #2b7a4b; }}
    .status.failed {{ background: rgba(179,58,58,0.15); color: #b33a3a; }}
    .status.skipped {{ background: rgba(156,107,31,0.15); color: #9c6b1f; }}
    .nodeid {{ font-size: 0.8rem; color: #5e6b78; margin-top: 4px; }}
    pre {{ white-space: pre-wrap; background: #f7f4ee; border: 1px solid #d8cfc1; border-radius: 8px; padding: 10px; }}
  </style>
</head>
<body>
  <header><h1>Web UI Automation Dashboard - Comsci</h1></header>
  <main>
    <section class=\"summary\">
      <div class=\"card\"><span class=\"label\">Generated</span><span class=\"value\">{escape(finished_at.strftime('%Y-%m-%d %H:%M:%S UTC'))}</span></div>
      <div class=\"card\"><span class=\"label\">Total Cases</span><span class=\"value\">{payload['summary']['total_cases']}</span></div>
      <div class=\"card\"><span class=\"label\">Cases Passed</span><span class=\"value\">{payload['summary']['cases_passed']}</span></div>
      <div class=\"card\"><span class=\"label\">Cases Failed</span><span class=\"value\">{payload['summary']['cases_failed']}</span></div>
      <div class=\"card\"><span class=\"label\">Cases Skipped</span><span class=\"value\">{payload['summary']['cases_skipped']}</span></div>
    </section>
    {''.join(section_html) if section_html else '<p>No test results were collected.</p>'}
  </main>
</body>
</html>
"""
    (output_dir / "test_dashboard.html").write_text(html_content, encoding="utf-8")


def pytest_sessionfinish(session, exitstatus):
    _write_dashboard(session.config)
