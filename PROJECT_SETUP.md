# Project Setup Steps - Comsci Web Automation

Follow these steps in order to get the project running on a new machine.

## 1. Prerequisites
- Windows with PowerShell
- Python 3.11 or newer installed and on `PATH`
- Git (optional, for source control)
- Access to the Comsci QA environment (URL + a test login account)

## 2. Open the Project Folder
```powershell
cd "C:\Users\kjakkala\OneDrive - Upland Software Inc\Documents\AI\Comsci_Automation_V2"
```

## 3. Create and Activate a Virtual Environment
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```
You should see `(.venv)` in front of your prompt once activated.

## 4. Install Python Dependencies
```powershell
pip install -r requirements.txt
```

## 5. Install Playwright Browsers
```powershell
playwright install chromium
```
(Run without `chromium` to install all supported browsers if you plan to test Firefox/WebKit too.)

## 6. Configure Environment Variables
```powershell
copy .env.example .env
```
Then open `.env` and fill in:
- `BASE_URL` - Comsci QA app URL (already pre-filled: `https://qa.itcostcontrol.com/automation/`)
- `LOGIN_USERNAME` / `LOGIN_PASSWORD` - test account credentials
- `TESTRAIL_EMAIL` / `TESTRAIL_API_KEY` - your TestRail credentials
- `TESTRAIL_PROJECT_ID` - Comsci project ID (confirm value; suite ID `64174` is already filled in)
- `TESTRAIL_MILESTONE_ID` - milestone to publish results against
- DB variables - leave blank until SQL Server access is granted

Never commit the populated `.env` file.

## 7. Verify the Setup
```powershell
python -m pytest tests -v
```
The `login` tests are marked `skip` until real TestRail case IDs and verified selectors are
in place, so this should run with no failures (skipped tests only) on a fresh checkout.

## 8. View the Dashboard
```powershell
python dashboard_server.py
```
Open `http://127.0.0.1:5060` in a browser. Use "Run Full Suite" to trigger a pytest run and
"View Latest Dashboard Report" to see grouped pass/fail results.

## 9. (Optional) Initialize Git
```powershell
git init
git branch -M main
git add .
git commit -m "Initial commit for Comsci web automation project"
```
Push to a private GitHub repo when ready (see README.md for remote steps).

## 10. Next Steps
1. Confirm the real login page selectors against the running app and update
   `utils/page_objects/login_page.py`.
2. Create the Login test cases in TestRail (Comsci project, Suite 64174) and update the
   placeholder case IDs in `tests/login/test_login.py`.
3. Remove the `@pytest.mark.skip(...)` line once selectors and case IDs are confirmed.
4. Run the suite and validate against real app behavior.
