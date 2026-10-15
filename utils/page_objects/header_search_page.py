from playwright.sync_api import Page, expect


class HeaderSearchPage:
    """Header search controls and Billing Inventory navigation assertions."""

    LOCATOR_CATALOG = (
        ("Search trigger", 'locator(".dynamic-search__label")'),
        ("Search input", 'get_by_role("textbox", name="Search..")'),
        ("Billing Inventory result", 'get_by_role("link", name="Billing Inventory", exact=True)'),
        ("Billing Inventory side menu", 'get_by_role("link", name="Billing Inventory", exact=True)'),
        ("Submit header item", 'get_by_role("listitem").filter(has_text="Submit")'),
    )

    def __init__(self, page: Page):
        self.page = page
        self.search_trigger = page.locator(".dynamic-search__label")
        self.search_input = page.get_by_role("textbox", name="Search..")
        self.billing_inventory_result = page.get_by_role(
            "link", name="Billing Inventory", exact=True
        )
        self.billing_inventory_side_menu = page.get_by_role(
            "link", name="Billing Inventory", exact=True
        )
        self.submit_header_item = page.get_by_role("listitem").filter(has_text="Submit")

    def search_for(self, query: str):
        self.search_trigger.wait_for(state="visible")
        self.search_trigger.click()
        self.search_input.wait_for(state="visible")
        self.search_input.press_sequentially(query)
        return self

    def open_billing_inventory(self):
        self.billing_inventory_result.click()
        self.page.wait_for_url("**/automation/billing-inventory/search")
        return self

    def expect_billing_inventory_open(self):
        expect(self.page).to_have_url(
            "https://qa.itcostcontrol.com/automation/billing-inventory/search"
        )
        expect(self.billing_inventory_side_menu).to_be_visible()
        expect(self.submit_header_item).to_be_visible()