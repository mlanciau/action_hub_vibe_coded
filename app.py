import hmac
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable

from flask import Flask, jsonify, request
from google.api_core.exceptions import GoogleAPICallError
from google.auth.exceptions import GoogleAuthError
from google.cloud import bigquery


ACTION_NAME = "bigquery_cell_capture"
CATEGORY_OPTIONS = (
    ("review", "Review"),
    ("follow_up", "Follow up"),
    ("ignore", "Ignore"),
)

BigQueryClientFactory = Callable[[str], Any]


def create_app(
    bigquery_client_factory: BigQueryClientFactory | None = None,
) -> Flask:
    app = Flask(__name__)
    client_factory = bigquery_client_factory or (
        lambda project_id: bigquery.Client(project=project_id)
    )

    @app.before_request
    def authenticate_action_api():
        if request.endpoint == "health":
            return None

        token = os.environ.get("ACTION_HUB_TOKEN")
        if not token:
            app.logger.error("ACTION_HUB_TOKEN is not configured.")
            return jsonify({"error": "Action Hub authentication is not configured."}), 503

        expected = f'Token token="{token}"'
        provided = request.headers.get("Authorization", "")
        if not hmac.compare_digest(provided, expected):
            return jsonify({"error": "Unauthorized."}), 401
        return None

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/")
    def list_actions():
        base_url = os.environ.get("ACTION_HUB_BASE_URL", "").rstrip("/")
        if not base_url:
            app.logger.error("ACTION_HUB_BASE_URL is not configured.")
            return jsonify({"error": "Action Hub URL is not configured."}), 503

        action_url = f"{base_url}/actions/{ACTION_NAME}"
        return jsonify(
            {
                "label": os.environ.get("ACTION_HUB_LABEL", "BigQuery Cell Actions"),
                "integrations": [
                    {
                        "name": ACTION_NAME,
                        "label": "Save cell to BigQuery",
                        "description": (
                            "Save the selected Looker cell value and form details "
                            "to a BigQuery table."
                        ),
                        "supported_action_types": ["cell"],
                        "required_fields": [{"tag": "bigquery_write"}],
                        "url": f"{action_url}/execute",
                        "form_url": f"{action_url}/form",
                        "params": [
                            {
                                "name": "project_id",
                                "label": "BigQuery project ID",
                                "description": "Google Cloud project containing the destination table.",
                                "required": True,
                                "sensitive": False,
                                "user_attribute_name": None,
                            },
                            {
                                "name": "dataset_id",
                                "label": "BigQuery dataset ID",
                                "required": True,
                                "sensitive": False,
                                "user_attribute_name": None,
                            },
                            {
                                "name": "table_id",
                                "label": "BigQuery table ID",
                                "required": True,
                                "sensitive": False,
                                "user_attribute_name": None,
                            },
                        ],
                        "supported_download_settings": ["push"],
                        "uses_oauth": False,
                    }
                ],
            }
        )

    @app.post(f"/actions/{ACTION_NAME}/form")
    def action_form():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            payload = {}
        form_params = payload.get("form_params", {})
        if not isinstance(form_params, dict):
            form_params = {}
        return jsonify({"fields": get_form_fields(form_params)})

    @app.post(f"/actions/{ACTION_NAME}/execute")
    def execute_action():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return looker_response(False, "Request body must be a JSON object.", 400)
        if payload.get("type") != "cell":
            return looker_response(False, "This action only supports cell payloads.", 400)

        data = payload.get("data")
        if not isinstance(data, dict):
            return looker_response(False, "The cell data is missing.", 400)
        if "value" not in data or not isinstance(data["value"], str):
            return looker_response(False, "The cell value is missing or invalid.", 400)

        form_params = payload.get("form_params")
        if not isinstance(form_params, dict):
            return looker_response(False, "Form values are missing.", 400)

        category = form_params.get("category")
        valid_categories = {name for name, _ in CATEGORY_OPTIONS}
        if not isinstance(category, str) or category not in valid_categories:
            return looker_response(
                False,
                "Choose a valid category.",
                400,
                validation_errors={"category": "Choose a valid category."},
            )

        notes = form_params.get("notes", "")
        if not isinstance(notes, str):
            return looker_response(
                False,
                "Notes must be text.",
                400,
                validation_errors={"notes": "Notes must be text."},
            )

        settings = payload.get("data", {})
        destination: dict[str, str] = {}
        for key in ("project_id", "dataset_id", "table_id"):
            value = settings.get(key)
            if not isinstance(value, str) or not value.strip():
                return looker_response(
                    False,
                    f"Missing required action setting: {key}.",
                    400,
                )
            destination[key] = value.strip()

        row = build_row(
            field_value=data["value"],
            category=category,
            notes=notes,
            looker_instance=request.headers.get("X-Looker-Instance"),
        )
        table_id = (
            f"{destination['project_id']}."
            f"{destination['dataset_id']}."
            f"{destination['table_id']}"
        )

        try:
            client = client_factory(destination["project_id"])
            errors = client.insert_rows_json(table_id, [row])
        except (GoogleAPICallError, GoogleAuthError):
            app.logger.exception("BigQuery insert failed.")
            return looker_response(
                False,
                "BigQuery write failed. Check the service credentials, permissions, "
                "and destination table schema.",
                502,
            )

        if errors:
            app.logger.error("BigQuery rejected the row: %s", errors)
            return looker_response(
                False,
                "BigQuery rejected the row. Check the destination table schema "
                "and service permissions.",
                502,
            )

        return looker_response(True, "Cell saved to BigQuery.", 200)

    return app


def get_form_fields(form_params: dict[str, Any]) -> list[dict[str, Any]]:
    category_default = form_params.get("category", CATEGORY_OPTIONS[0][0])
    valid_categories = {name for name, _ in CATEGORY_OPTIONS}
    if not isinstance(category_default, str) or category_default not in valid_categories:
        category_default = CATEGORY_OPTIONS[0][0]
    notes_default = form_params.get("notes", "")
    if not isinstance(notes_default, str):
        notes_default = ""

    return [
        {
            "name": "category",
            "label": "Category",
            "type": "select",
            "required": True,
            "default": category_default,
            "options": [
                {"name": name, "label": label}
                for name, label in CATEGORY_OPTIONS
            ],
        },
        {
            "name": "notes",
            "label": "Notes",
            "type": "textarea",
            "required": False,
            "default": notes_default,
        },
    ]


def build_row(
    field_value: str,
    category: str,
    notes: str,
    looker_instance: str | None,
) -> dict[str, str | None]:
    return {
        "inserted_at": datetime.now(timezone.utc).isoformat(),
        "field_value": field_value,
        "category": category,
        "notes": notes,
        "looker_instance": looker_instance,
    }


def looker_response(
    success: bool,
    message: str,
    status_code: int,
    validation_errors: dict[str, str] | None = None,
):
    return (
        jsonify(
            {
                "looker": {
                    "success": success,
                    "message": message,
                    "refresh_query": False,
                    "validation_errors": validation_errors or {},
                    "state": None,
                }
            }
        ),
        status_code,
    )


logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
