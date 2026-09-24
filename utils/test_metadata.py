import pytest


def ui_case(*, section: str, flow: str, testrail_id: str, title: str):
    """Attach page-section and TestRail metadata to a pytest UI test case."""

    def decorator(func):
        marks = [
            pytest.mark.page_section(section),
            pytest.mark.ui_flow(flow),
            pytest.mark.testrail_case(testrail_id),
            pytest.mark.dashboard_title(title),
        ]
        for mark in reversed(marks):
            func = mark(func)
        return func

    return decorator
