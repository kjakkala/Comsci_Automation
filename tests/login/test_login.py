import os

import pytest

from utils.page_objects.login_page import LoginPage
from utils.test_metadata import ui_case
# Tests for login functionality

@ui_case(
    section="Authentication",
    flow="Login",
    testrail_id="17882807",
    title="Login successful",
    labels=("Functionality", "Positive", "UI", "Automated"),
)
def test_login_successful(page):
    login_page = LoginPage(page).goto()
    login_page.login(
        os.environ["LOGIN_USERNAME"],
        os.environ["LOGIN_PASSWORD"],
    )
    login_page.expect_login_success()


@ui_case(
    section="Authentication",
    flow="Logout",
    testrail_id="17882841",
    title="Logout successful",
    labels=("Functionality", "Positive", "UI", "Automated"),
)
def test_logout_successful(authenticated_login_page, capture_screenshot):
    authenticated_login_page.open_user_profile_dropdown()
    capture_screenshot("Dropdown opened")

    authenticated_login_page.logout_link.hover()
    capture_screenshot("Logout selected")

    authenticated_login_page.logout()
    authenticated_login_page.expect_logged_out()
    capture_screenshot("Logged out")


@ui_case(
    section="Authentication",
    flow="Login",
    testrail_id="17882842",
    title="Invalid username",
    labels=("Functionality", "Negative", "UI", "Automated"),
)
def test_invalid_username(page):
    login_page = LoginPage(page).goto()
    login_page.login("invalid.automation.user@cimpl.com", os.environ["LOGIN_PASSWORD"])
    login_page.expect_sign_in_rejected()


@ui_case(
    section="Authentication",
    flow="Login",
    testrail_id="17882843",
    title="Invalid password",
    labels=("Functionality", "Negative", "UI", "Automated"),
)
def test_invalid_password(page):
    login_page = LoginPage(page).goto()
    login_page.login(os.environ["LOGIN_USERNAME"], "InvalidPassword!123")
    login_page.expect_sign_in_rejected()
