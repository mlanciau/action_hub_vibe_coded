import json
import os
import unittest
from unittest.mock import patch

from app import CATEGORY_OPTIONS, QUERY_ACTION_NAME, create_app


class FakeBigQueryClient:
    def __init__(self, errors=None):
        self.errors = errors or []
        self.inserted_table = None
        self.inserted_rows = None

    def insert_rows_json(self, table_id, rows):
        self.inserted_table = table_id
        self.inserted_rows = rows
        return self.errors


class ActionHubTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeBigQueryClient()
        self.client_project_id: str | None = None

        def client_factory(project_id):
            self.client_project_id = project_id
            return self.client

        self.app = create_app(client_factory)
        self.app.config["TESTING"] = True
        self.http = self.app.test_client()
        self.environment = patch.dict(
            os.environ,
            {
                "ACTION_HUB_BASE_URL": "https://actions.example.test/",
                "ACTION_HUB_TOKEN": "test-token",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.headers = {"Authorization": 'Token token="test-token"'}

    def test_action_metadata_is_cell_scoped_and_tagged(self):
        response = self.http.post("/", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        action = response.get_json()["integrations"][0]
        self.assertEqual(action["supported_action_types"], ["cell"])
        self.assertEqual(action["required_fields"], [{"tag": "bigquery_write"}])
        self.assertEqual(action["form_url"], (
            "https://actions.example.test/actions/bigquery_cell_capture/form"
        ))
        user_id_param = next(
            param for param in action["params"] if param["name"] == "looker_user_id"
        )
        self.assertEqual(user_id_param["user_attribute_name"], "id")
        self.assertTrue(user_id_param["required"])
        query_action = response.get_json()["integrations"][1]
        self.assertEqual(query_action["name"], QUERY_ACTION_NAME)
        self.assertEqual(query_action["supported_action_types"], ["query"])
        self.assertEqual(query_action["supported_formats"], ["json_detail"])
        self.assertEqual(query_action["required_fields"], [])

    def test_form_has_custom_category_and_notes_fields(self):
        response = self.http.post(
            "/actions/bigquery_cell_capture/form",
            json={"form_params": {"category": "follow_up", "notes": "Check this"}},
            headers=self.headers,
        )

        fields = response.get_json()["fields"]
        self.assertEqual(fields[0]["default"], "follow_up")
        self.assertEqual(fields[0]["options"], [
            {"name": name, "label": label} for name, label in CATEGORY_OPTIONS
        ])
        self.assertEqual(fields[1]["default"], "Check this")

    def test_form_handles_non_object_refresh_payload(self):
        response = self.http.post(
            "/actions/bigquery_cell_capture/form",
            data="[]",
            content_type="application/json",
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["fields"][0]["default"], "review")

    def test_execute_inserts_cell_and_form_data(self):
        response = self.http.post(
            "/actions/bigquery_cell_capture/execute",
            json={
                "type": "cell",
                "data": {
                    "value": "customer-42",
                    "project_id": "demo-project",
                    "dataset_id": "feedback",
                    "table_id": "looker_cell_actions",
                    "looker_user_id": "77",
                },
                "form_params": {"category": "review", "notes": "Verify this row"},
            },
            headers={**self.headers, "X-Looker-Instance": "looker.example.test"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["looker"]["success"])
        self.assertEqual(
            self.client.inserted_table,
            "demo-project.feedback.looker_cell_actions",
        )
        self.assertEqual(self.client_project_id, "demo-project")
        assert self.client.inserted_rows is not None
        row = self.client.inserted_rows[0]
        self.assertEqual(row["field_value"], "customer-42")
        self.assertEqual(row["category"], "review")
        self.assertEqual(row["notes"], "Verify this row")
        self.assertEqual(row["looker_instance"], "looker.example.test")
        self.assertEqual(row["looker_user_id"], "77")
        self.assertIsNone(row["generated_sql"])
        self.assertTrue(row["inserted_at"].endswith("+00:00"))

    def test_execute_query_inserts_generated_sql(self):
        sql = "SELECT customer_id FROM customers"
        response = self.http.post(
            f"/actions/{QUERY_ACTION_NAME}/execute",
            json={
                "type": "query",
                "data": {
                    "project_id": "demo-project",
                    "dataset_id": "feedback",
                    "table_id": "looker_cell_actions",
                },
                "attachment": {
                    "mimetype": "application/json",
                    "extension": "json",
                    "data": {"sql": sql},
                },
                "form_params": {"category": "review", "notes": "Explore query"},
            },
            headers={**self.headers, "X-Looker-Instance": "looker.example.test"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["looker"]["success"])
        assert self.client.inserted_rows is not None
        row = self.client.inserted_rows[0]
        self.assertIsNone(row["field_value"])
        self.assertIsNone(row["looker_user_id"])
        self.assertEqual(row["generated_sql"], sql)
        self.assertEqual(row["category"], "review")

    def test_action_request_logs_non_secret_fields_and_redacts_secrets(self):
        sql = "SELECT customer_id FROM customers"
        with self.assertLogs("app", level="INFO") as captured:
            form_response = self.http.post(
                "/actions/bigquery_query_sql_capture/form",
                json={
                    "form_params": {"notes": "Looker form note"},
                    "state": {"access_token": "form-access-secret"},
                },
                headers=self.headers,
            )
            execute_response = self.http.post(
                f"/actions/{QUERY_ACTION_NAME}/execute",
                json={
                    "type": "query",
                    "data": {
                        "project_id": "demo-project",
                        "dataset_id": "feedback",
                        "table_id": "looker_cell_actions",
                        "looker_action_token": "payload-action-secret",
                    },
                    "attachment": {
                        "data": json.dumps(
                            {
                                "sql": sql,
                                "refresh_token": "attachment-refresh-secret",
                            }
                        ),
                    },
                    "form_params": {"category": "review", "notes": "Saved note"},
                },
                headers=self.headers,
            )

        self.assertEqual(form_response.status_code, 200)
        self.assertEqual(execute_response.status_code, 200)
        log_output = "\n".join(captured.output)
        self.assertIn('"endpoint":"form"', log_output)
        self.assertIn('"endpoint":"execute"', log_output)
        self.assertIn(sql, log_output)
        self.assertIn("Looker form note", log_output)
        self.assertIn("Saved note", log_output)
        self.assertIn("[REDACTED]", log_output)
        for secret in (
            "test-token",
            "form-access-secret",
            "payload-action-secret",
            "attachment-refresh-secret",
        ):
            self.assertNotIn(secret, log_output)

    def test_execute_query_accepts_json_string_attachment(self):
        sql = "SELECT order_id FROM orders"
        response = self.http.post(
            f"/actions/{QUERY_ACTION_NAME}/execute",
            json={
                "type": "query",
                "data": {
                    "project_id": "demo-project",
                    "dataset_id": "feedback",
                    "table_id": "looker_cell_actions",
                },
                "attachment": {
                    "mimetype": "application/json",
                    "extension": "json",
                    "data": f'{{"sql":"{sql}"}}',
                },
                "form_params": {"category": "review"},
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        assert self.client.inserted_rows is not None
        self.assertEqual(self.client.inserted_rows[0]["generated_sql"], sql)

    def test_execute_query_rejects_attachment_without_sql(self):
        response = self.http.post(
            f"/actions/{QUERY_ACTION_NAME}/execute",
            json={
                "type": "query",
                "data": {
                    "project_id": "demo-project",
                    "dataset_id": "feedback",
                    "table_id": "looker_cell_actions",
                },
                "attachment": {
                    "mimetype": "application/json",
                    "extension": "json",
                    "data": {"fields": {}, "data": []},
                },
                "form_params": {"category": "review"},
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.client.inserted_rows)

    def test_execute_rejects_invalid_category_without_writing(self):
        response = self.http.post(
            "/actions/bigquery_cell_capture/execute",
            json={
                "type": "cell",
                "data": {
                    "value": "customer-42",
                    "project_id": "demo-project",
                    "dataset_id": "feedback",
                    "table_id": "looker_cell_actions",
                },
                "form_params": {"category": "not-a-category"},
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["looker"]["success"])
        self.assertIsNone(self.client.inserted_rows)

    def test_execute_rejects_invalid_user_id_type(self):
        response = self.http.post(
            "/actions/bigquery_cell_capture/execute",
            json={
                "type": "cell",
                "data": {
                    "value": "customer-42",
                    "project_id": "demo-project",
                    "dataset_id": "feedback",
                    "table_id": "looker_cell_actions",
                    "looker_user_id": {"bad": "type"},
                },
                "form_params": {"category": "review"},
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["looker"]["success"])
        self.assertIsNone(self.client.inserted_rows)

    def test_action_api_rejects_invalid_token(self):
        response = self.http.post("/", headers={"Authorization": "Token token=wrong"})

        self.assertEqual(response.status_code, 401)

    def test_health_endpoint_is_unauthenticated(self):
        response = self.http.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_docs_and_openapi_are_public_with_security_headers_and_no_secrets(self):
        docs_response = self.http.get("/docs")
        self.assertEqual(docs_response.status_code, 200)
        docs_html = docs_response.get_data(as_text=True)
        self.assertIn("Looker BigQuery Action Hub", docs_html)
        self.assertNotIn("test-token", docs_html)
        self.assertEqual(docs_response.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(docs_response.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(docs_response.headers.get("Cache-Control"), "no-store")
        csp = docs_response.headers.get("Content-Security-Policy", "")
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)

        docs_slash = self.http.get("/docs/")
        self.assertEqual(docs_slash.status_code, 200)

        js_response = self.http.get("/static/docs.js")
        self.assertEqual(js_response.status_code, 200)
        js_body = js_response.get_data(as_text=True)
        js_response.close()
        self.assertNotIn("innerHTML", js_body)
        self.assertNotIn("localStorage", js_body)
        self.assertNotIn("sessionStorage", js_body)

        css_response = self.http.get("/static/docs.css")
        self.assertEqual(css_response.status_code, 200)
        css_response.close()

        spec_response = self.http.get("/openapi.json")
        self.assertEqual(spec_response.status_code, 200)
        spec = spec_response.get_json()
        self.assertEqual(spec["openapi"], "3.0.3")
        self.assertIn("/actions/{action_name}/execute", spec["paths"])

    def test_execute_rejects_invalid_bigquery_identifiers(self):
        response = self.http.post(
            f"/actions/{QUERY_ACTION_NAME}/execute",
            json={
                "type": "query",
                "data": {
                    "project_id": "demo-project",
                    "dataset_id": "feedback;DROP",
                    "table_id": "looker_cell_actions",
                },
                "attachment": {"data": {"sql": "SELECT 1"}},
                "form_params": {"category": "review"},
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["looker"]["success"])
        self.assertIsNone(self.client.inserted_rows)

    def test_disallowed_method_and_rate_limit_are_enforced(self):
        trace_response = self.http.open("/", method="TRACE", headers=self.headers)
        self.assertEqual(trace_response.status_code, 405)

        limited_app = create_app(lambda _project_id: self.client, rate_limit_per_minute=2)
        limited_client = limited_app.test_client()
        self.assertEqual(limited_client.get("/health").status_code, 200)
        self.assertEqual(limited_client.get("/health").status_code, 200)
        self.assertEqual(limited_client.get("/health").status_code, 429)


if __name__ == "__main__":
    unittest.main()
