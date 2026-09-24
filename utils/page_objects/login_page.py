from playwright.sync_api import Page, expect


class LoginPage:
    """Page Object for the Comsci login page.

    NOTE: Selectors below are placeholders. Verify against the real app
    (https://qa.itcostcontrol.com/automation/) before relying on these tests,
    then update the locators to match actual field names/roles.
    """

    def __init__(self, page: Page):
        self.page = page
        self.username_input = page.get_by_label("Username")
        self.password_input = page.get_by_label("Password")
        self.login_button = page.get_by_role("button", name="Log In")
        self.error_message = page.get_by_role("alert")

    def goto(self):
        self.page.goto("/")
        return self

    def login(self, username: str, password: str):
        self.username_input.fill(username)
        self.password_input.fill(password)
        self.login_button.click()
        return self

    def expect_login_success(self):
        expect(self.page).not_to_have_url(lambda url: "login" in url.lower())

    def expect_error_message(self, text: str | None = None):
        expect(self.error_message).to_be_visible()
        if text:
            expect(self.error_message).to_contain_text(text)
