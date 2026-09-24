import os
from dataclasses import dataclass

import pytest

from utils.page_objects.login_page import LoginPage
from utils.test_metadata import ui_case


@dataclass(frozen=True)
class LoginCase:
    case_id: str
    title: str
    label: str  # Positive / Validation / Negative


# TODO: Replace placeholder case_id values ("TBD-1", "TBD-2", ...) with real TestRail
# case IDs once cases are created under the Comsci "Login" section (suite 64174).
CASES = [
    LoginCase(case_id="TBD-1", title="Valid credentials log in successfully", label="Positive"),
    LoginCase(case_id="TBD-2", title="Invalid password shows an error", label="Negative"),
    LoginCase(case_id="TBD-3", title="Empty username/password shows validation errors", label="Validation"),
]

CASE_PARAMS = [
    pytest.param(
        case,
        id=f"C{case.case_id}",
        marks=[
            pytest.mark.page_section("Login"),
            pytest.mark.ui_flow("Login"),
            pytest.mark.testrail_case(case.case_id),
            pytest.mark.dashboard_title(case.title),
        ],
    )
    for case in CASES
]


@pytest.mark.skip(reason="Awaiting real TestRail case IDs and confirmed login selectors before enabling.")
@pytest.mark.parametrize("case", CASE_PARAMS)
def test_login(page, case: LoginCase):
    login_page = LoginPage(page).goto()

    if case.label == "Positive":
        login_page.login(os.environ["LOGIN_USERNAME"], os.environ["LOGIN_PASSWORD"])
        login_page.expect_login_success()
    elif case.label == "Negative":
        login_page.login(os.environ["LOGIN_USERNAME"], "wrong-password")
        login_page.expect_error_message()
    elif case.label == "Validation":
        login_page.login("", "")
        login_page.expect_error_message()
