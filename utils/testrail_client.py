import os
from dataclasses import dataclass

import requests


class TestRailError(RuntimeError):
    """Raised when TestRail API requests fail."""


@dataclass
class TestRailConfig:
    url: str
    email: str
    api_key: str
    project_id: int
    suite_id: int
    milestone_id: int


class TestRailClient:
    def __init__(self, config: TestRailConfig, timeout: int = 60):
        self.config = config
        self.timeout = timeout
        self.api_base = f"{config.url.rstrip('/')}/index.php?/api/v2"
        self.session = requests.Session()
        self.session.auth = (config.email, config.api_key)
        self.session.headers.update({"Content-Type": "application/json"})

    @staticmethod
    def _parse_int_env(name: str) -> int:
        value = os.getenv(name, "").strip()
        if not value:
            raise TestRailError(f"Missing required environment variable: {name}")
        try:
            return int(value)
        except ValueError as exc:
            raise TestRailError(f"Environment variable {name} must be an integer") from exc

    @classmethod
    def from_env(cls):
        required = ["TESTRAIL_URL", "TESTRAIL_EMAIL", "TESTRAIL_API_KEY"]
        missing = [name for name in required if not os.getenv(name, "").strip()]
        if missing:
            raise TestRailError(
                "Missing TestRail configuration: " + ", ".join(missing)
            )

        config = TestRailConfig(
            url=os.environ["TESTRAIL_URL"].strip(),
            email=os.environ["TESTRAIL_EMAIL"].strip(),
            api_key=os.environ["TESTRAIL_API_KEY"].strip(),
            project_id=cls._parse_int_env("TESTRAIL_PROJECT_ID"),
            suite_id=cls._parse_int_env("TESTRAIL_SUITE_ID"),
            milestone_id=cls._parse_int_env("TESTRAIL_MILESTONE_ID"),
        )
        return cls(config)

    def _request(self, method: str, path: str, payload: dict | None = None):
        url = f"{self.api_base}/{path}"
        response = self.session.request(
            method=method,
            url=url,
            json=payload,
            timeout=self.timeout,
        )
        if not response.ok:
            text = response.text[:1000]
            raise TestRailError(
                f"TestRail API error {response.status_code} for {path}: {text}"
            )
        if not response.text.strip():
            return {}
        return response.json()

    def get_status_ids(self) -> dict[str, int]:
        fallback = {
            "passed": 1,
            "blocked": 2,
            "untested": 3,
            "retest": 4,
            "failed": 5,
        }
        try:
            statuses = self._request("GET", "get_statuses")
            status_map = {
                status.get("name", "").strip().lower(): int(status.get("id"))
                for status in statuses
                if status.get("name") and status.get("id") is not None
            }
            return {**fallback, **status_map}
        except TestRailError:
            return fallback

    def create_plan(self, name: str, section_case_ids: dict[str, list[int]]):
        entries = []
        for section_name, case_ids in section_case_ids.items():
            entries.append(
                {
                    "suite_id": self.config.suite_id,
                    "name": section_name,
                    "include_all": False,
                    "case_ids": sorted(set(case_ids)),
                }
            )

        payload = {
            "name": name,
            "milestone_id": self.config.milestone_id,
            "entries": entries,
        }
        return self._request(
            "POST", f"add_plan/{self.config.project_id}", payload
        )

    @staticmethod
    def _status_weight(status: str) -> int:
        # Worst status wins when multiple tests map to one TestRail case.
        weights = {"failed": 3, "skipped": 2, "passed": 1}
        return weights.get(status, 0)

    @staticmethod
    def _status_name_for_case(status: str) -> str:
        if status == "failed":
            return "failed"
        if status == "skipped":
            return "retest"
        return "passed"

    def publish_results(self, plan: dict, section_results: dict[str, list[dict]]):
        status_ids = self.get_status_ids()
        published = 0

        for entry in plan.get("entries", []):
            section_name = entry.get("name", "")
            runs = entry.get("runs", [])
            if not runs:
                continue

            run_id = runs[0].get("id")
            if not run_id:
                continue

            results_for_section = section_results.get(section_name, [])
            case_aggregate: dict[int, dict] = {}

            for result in results_for_section:
                case_id = int(result["testrail_case_id"])
                existing = case_aggregate.get(case_id)

                comment = (
                    f"Test Case: {result.get('title', '')}\n"
                    f"Case ID: C{result.get('testrail_case_id', '')}\n"
                    f"Status: {result.get('status', '').upper()}\n"
                    f"UI Flow: {result.get('operation', '')}\n"
                    f"Node: {result.get('nodeid', '')}\n"
                    "----------------------------------------\n"
                    "Execution Details\n"
                    "----------------------------------------\n"
                    f"{result.get('details', '')}"
                )

                if not existing:
                    case_aggregate[case_id] = {
                        "status": result.get("status", "failed"),
                        "comments": [comment],
                    }
                    continue

                if self._status_weight(result.get("status", "")) > self._status_weight(
                    existing["status"]
                ):
                    existing["status"] = result.get("status", "failed")
                existing["comments"].append(comment)

            for case_id, aggregate in case_aggregate.items():
                status_name = self._status_name_for_case(aggregate["status"])
                status_id = status_ids.get(status_name, 5)
                body = {
                    "status_id": status_id,
                    "comment": "\n\n".join(aggregate["comments"]),
                }
                self._request("POST", f"add_result_for_case/{run_id}/{case_id}", body)
                published += 1

        return {"published_results": published}

    def get_case(self, case_id: int):
        return self._request("GET", f"get_case/{int(case_id)}")

    def get_runs(self):
        return self._request("GET", f"get_runs/{self.config.project_id}").get("runs", [])

    def get_plan(self, plan_id: int):
        return self._request("GET", f"get_plan/{int(plan_id)}")

    def get_results_for_run(self, run_id: int):
        return self._request("GET", f"get_results_for_run/{int(run_id)}").get("results", [])

    def get_sections(self):
        return self._request("GET", f"get_sections/{self.config.project_id}&suite_id={self.config.suite_id}").get("sections", [])

    def add_case(self, section_id: int, payload: dict):
        if not payload or not str(payload.get("title", "")).strip():
            raise TestRailError("add_case payload must include a title")
        return self._request("POST", f"add_case/{int(section_id)}", payload)

    def get_cases(self, section_id: int | None = None, limit: int = 250):
        path = f"get_cases/{self.config.project_id}&suite_id={self.config.suite_id}&limit={int(limit)}"
        if section_id is not None:
            path += f"&section_id={int(section_id)}"
        response = self._request("GET", path)
        return response.get("cases", []) if isinstance(response, dict) else []
