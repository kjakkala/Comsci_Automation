import dashboard_server as dashboard


def _prepare_dashboard(monkeypatch):
    monkeypatch.setattr(dashboard, "_load_dashboard_payload", lambda: {"sections": []})
    monkeypatch.setattr(dashboard, "_load_test_catalog", lambda: [])
    monkeypatch.setattr(dashboard, "_load_last_run", lambda: {})
    monkeypatch.setattr(dashboard, "_testrail_connected", lambda: False)


def test_robot_is_rendered_only_for_an_active_run(monkeypatch):
    _prepare_dashboard(monkeypatch)
    monkeypatch.setitem(dashboard._live_run_state, "running", False)
    idle_response = dashboard.app.test_client().get("/sections/authentication")
    assert b'id="qa-rover-overlay"' not in idle_response.data

    monkeypatch.setitem(dashboard._live_run_state, "running", True)
    monkeypatch.setitem(dashboard._live_run_state, "current_section", "Header")
    active_response = dashboard.app.test_client().get("/sections/header")
    assert b'id="qa-rover-overlay"' in active_response.data
    assert b"Running Header" in active_response.data


def test_robot_asset_is_served_locally():
    response = dashboard.app.test_client().get("/qa-rover.svg")
    assert response.status_code == 200
    assert response.mimetype == "image/svg+xml"
    assert b"<svg" in response.data


def test_robot_tracks_section_and_launches_after_completion(monkeypatch):
    _prepare_dashboard(monkeypatch)
    monkeypatch.setitem(dashboard._live_run_state, "running", True)
    monkeypatch.setitem(dashboard._live_run_state, "current_section", "Authentication")
    client = dashboard.app.test_client()

    status = client.get("/api/run-status").get_json()
    page = client.get("/sections/authentication").data

    assert status["current_section"] == "Authentication"
    assert b"state.current_section" in page
    assert b"rover.classList.add('launch')" in page
    assert b"prefers-reduced-motion: reduce" in page
    assert b"pointer-events: none" in page
    assert b"window.location.reload()" in page


def test_current_section_follows_pytest_node_output():
    assert dashboard._current_section_from_output("tests/header/test_search.py::test_search PASSED") == "Header"
    assert dashboard._current_section_from_output("tests/login/test_login.py::test_login PASSED") == "Authentication"