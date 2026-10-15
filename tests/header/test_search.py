from utils.page_objects.header_search_page import HeaderSearchPage
from utils.test_metadata import ui_case


@ui_case(
    section="Header",
    flow="Search",
    testrail_id="17882846",
    title="Search and open Billing Inventory",
    labels=("Functionality", "Positive", "UI", "Automated"),
)
def test_search_and_open_billing_inventory(authenticated_login_page, capture_screenshot):
    search_page = HeaderSearchPage(authenticated_login_page.page)
    search_page.search_for("Billing inventory")
    capture_screenshot("Search results")

    search_page.open_billing_inventory()
    search_page.expect_billing_inventory_open()
    capture_screenshot("Billing Inventory opened")