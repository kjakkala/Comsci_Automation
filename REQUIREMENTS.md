# Requirements Document - Comsci Web Automation

## 1. Purpose

Build an automated UI test framework for the Comsci web application, mirroring the structure
and reporting model already proven in the Cimpl API automation project (pytest + dashboard +
TestRail + DB validation), so both projects are maintained the same way.

## 2. Scope

- **In scope (Phase 1):** Login flow automation, dashboard reporting, TestRail case mapping.
- **Planned (Phase 2+):** Additional flows/pages as they are identified together; SQL Server
  DB validation once DB access is granted; expanding TestRail publishing to full suite runs.
- **Out of scope for now:** Automatic data cleanup (any cleanup tooling must be manual,
  explicit-input-only, same rule as the API project).

## 3. Target Application

| Item | Value |
|---|---|
| Application | Comsci (new product) |
| Base URL (QA) | `https://qa.itcostcontrol.com/automation/` |
| Auth | Username/password login form |

## 4. Tech Stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Test runner | pytest |
| Browser automation | Playwright (sync API) + pytest-playwright, Chromium by default |
| Config | python-dotenv (`.env` / `.env.example`) |
| DB validation | pyodbc against SQL Server (added once access is granted) |
| Local dashboard | Flask |
| Reporting | JSON + HTML dashboard, screenshots on failure |
| Test management | TestRail (Comsci project, Suite ID `64174`) |

## 5. Functional Requirements

### 5.1 Test Execution
- FR1: Tests are organized one folder per feature area under `tests/` (e.g. `tests/login/`).
- FR2: Each test file covers one flow (e.g. `test_login.py` covers the login flow only).
- FR3: Test cases are declared as `@dataclass(frozen=True)` case objects with `case_id`,
  `title`, and `label` (`Positive` / `Negative` / `Validation`, extendable).
- FR4: Every test case is tagged with `page_section`, `ui_flow`, `testrail_case`, and
  `dashboard_title` pytest markers for dashboard grouping and TestRail mapping.

### 5.2 Reporting / Dashboard
- FR5: Every pytest run produces a JSON dashboard payload and an HTML dashboard report under
  `reports/`.
- FR6: Failures capture a Playwright screenshot, linked from the dashboard.
- FR7: Results are rolled up two ways — by execution (every pytest run) and by unique
  TestRail case ID (a case may be exercised by multiple parametrized scenarios).
- FR8: `dashboard_server.py` provides a local web UI to trigger runs and view the latest
  report, without needing the command line.

### 5.3 TestRail Integration
- FR9: TestRail publishing is optional and controlled by `TESTRAIL_ENABLE_ON_RUN`.
- FR10: Cases must exist in TestRail (Comsci project) before automation is written against
  them — never invent case IDs.
- FR11: Config values (`TESTRAIL_PROJECT_ID`, `TESTRAIL_SUITE_ID`, `TESTRAIL_MILESTONE_ID`,
  credentials) come from `.env`, never hardcoded.

### 5.4 DB Validation (Phase 2)
- FR12: Once DB access is available, flows that create/update/delete data via the UI get
  pre-action and post-action DB assertions (row count deltas, field value checks), using
  `utils/db_helper.py`.

## 6. Non-Functional Requirements
- NFR1: Secrets (`.env`, credentials, API keys) are never committed to source control.
- NFR2: Tests must be runnable headless (CI-friendly) and headed (for debugging), toggled via
  `HEADLESS` in `.env`.
- NFR3: Test data cleanup tools, if added, require explicit manual input — no bulk auto-cleanup.
- NFR4: Selectors used in Page Objects must be verified against the real running application,
  not assumed from a requirements doc.

## 7. Open Items / Needs Confirmation
- [ ] Real TestRail case IDs for the Login section (currently placeholders `TBD-1`..`TBD-3`
  in `tests/login/test_login.py`).
- [ ] Confirm `TESTRAIL_PROJECT_ID` for the Comsci project (suite ID `64174` confirmed).
- [ ] SQL Server connection details for Comsci once DB access is granted.
- [ ] Confirm actual login page selectors (username/password field labels, submit button,
  error message region) against the real app.
- [ ] Test account credentials for QA environment.
