# Comsci Web Automation

Python-based **web UI automation** project for the Comsci web application, modeled after the
existing Cimpl API automation project's conventions (pytest + dashboard + TestRail publishing),
adapted for browser testing with Playwright.

## What Is In This Repo

- Pytest-based Playwright UI test suites under `tests/` (one folder per page/flow, starting with `login/`)
- Page Object classes under `utils/page_objects/`
- Shared helpers under `utils/` (DB validation, TestRail client, dashboard metadata)
- A lightweight Flask dashboard in `dashboard_server.py`
- JSON + HTML dashboard report generation (`reports/`, gitignored)
- Optional TestRail integration (Comsci project, suite 64174)

## What Should Not Be Committed

- `.env`
- `.venv/`
- `reports/`
- `test-results/`, `playwright-report/`
- local cache files

Only commit `.env.example` as a sanitized template.

## Local Setup

See [PROJECT_SETUP.md](./PROJECT_SETUP.md) for full step-by-step instructions. Quick version:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
```

Then edit `.env` with real values (app URL, login credentials, TestRail keys).

## Run Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests -v
```

Run one feature area:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\login -v
```

## Run Dashboard

```powershell
.\.venv\Scripts\python.exe dashboard_server.py
```

Then open `http://127.0.0.1:5060`.

## Project Documents

- [REQUIREMENTS.md](./REQUIREMENTS.md) - scope, tech stack, and functional requirements
- [PROJECT_SETUP.md](./PROJECT_SETUP.md) - step-by-step environment setup

## Add This Project To GitHub

1. Create a new private repository in GitHub.
2. Open a terminal in this project root.
3. Initialize Git locally:

```powershell
git init
git branch -M main
```

4. Stage and commit:

```powershell
git add .
git commit -m "Initial commit for Comsci web automation project"
```

5. Connect and push:

```powershell
git remote add origin https://github.com/<org-or-user>/<repo-name>.git
git push -u origin main
```
