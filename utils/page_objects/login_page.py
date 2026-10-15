import os

from playwright.sync_api import Page, expect


class LoginPage:
    """Page Object for the Comsci login page.

    Locators are verified against the QA login page.
    """

    LOCATOR_CATALOG = (
        ("Username", 'get_by_role("textbox", name="Username")'),
        ("Password", 'get_by_role("textbox", name="Password")'),
        ("Sign In", 'get_by_role("button", name="Sign In")'),
        (
            "Authenticated header logo",
            'locator("#header-container").get_by_role("img", name="Wyoming Enterprise Technology")',
        ),
        ("User profile dropdown", 'locator("#profile_menu a").filter(has_text="kailash test")'),
        ("Logout", 'locator("#profile_menu a.logout")'),
    )

    def __init__(self, page: Page):
        self.page = page
        self.username_input = page.get_by_role("textbox", name="Username")
        self.password_input = page.get_by_role("textbox", name="Password")
        self.login_button = page.get_by_role("button", name="Sign In")
        self.error_message = page.get_by_text("We could not sign you in.")
        self.user_profile_dropdown = page.locator("#profile_menu a").filter(has_text="kailash test")
        self.logout_link = page.locator("#profile_menu a.logout")

    def goto(self):
        self.page.goto(os.environ["BASE_URL"])
        return self

    def login(self, username: str, password: str):
        self.username_input.fill(username)
        self.password_input.fill(password)
        self.login_button.click()
        return self

    def expect_login_success(self):
        expect(
            self.page.locator("#header-container").get_by_role(
                "img", name="Wyoming Enterprise Technology"
            )
        ).to_be_visible()

    def expect_error_message(self, text: str | None = None):
        expect(self.error_message).to_be_visible()
        if text:
            expect(self.error_message).to_contain_text(text)

    def expect_sign_in_rejected(self):
        self.expect_error_message("We could not sign you in.")
        expect(self.page).to_have_url(os.environ["BASE_URL"])

    def open_user_profile_dropdown(self):
        self.user_profile_dropdown.click()
        expect(self.logout_link).to_be_visible()
        return self

    def logout(self):
        self.logout_link.click()
        self.page.wait_for_url("**/automation/logoutsso.php")
        return self

    def expect_logged_out(self):
        expect(self.page).to_have_url("https://qa.itcostcontrol.com/automation/logoutsso.php")
