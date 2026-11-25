import hmac
import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from flask import Flask, jsonify, render_template, request
from google.api_core.exceptions import GoogleAPICallError
from google.auth.exceptions import GoogleAuthError
from google.cloud import bigquery


ACTION_NAME = "bigquery_cell_capture"
QUERY_ACTION_NAME = "bigquery_query_sql_capture"
CATEGORY_OPTIONS = (
    ("review", "Review"),
    ("follow_up", "Follow up"),
    ("ignore", "Ignore"),
)
_SENSITIVE_LOG_KEY_PATTERN = re.compile(
    r"authorization|cookie|token|secret|password|credential|state|csrf|"
    r"private[_-]?key|api[_-]?key|client[_-]?assertion|jwt|signature",
    re.IGNORECASE,
)
_REDACTED_LOG_VALUE = "[REDACTED]"
_MAX_LOG_PAYLOAD_BYTES = 180 * 1024
_PUBLIC_READ_ENDPOINTS = frozenset({"health", "docs", "openapi_spec", "static"})
_ALLOWED_HTTP_METHODS = frozenset({"GET", "HEAD", "POST"})
_PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9.:-]{1,128}$")
_BQ_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,1024}$")
_DEFAULT_RATE_LIMIT_PER_MINUTE = 120
_RATE_LIMIT_WINDOW_SECONDS = 60.0

BigQueryClientFactory = Callable[[str], Any]


class _SlidingWindowRateLimiter:
    """Thread-safe per-IP sliding-window rate limiter.

    TODO(security): For multi-instance horizontal scaling on Cloud Run, also
    enforce distributed rate limiting via Cloud Armor or a shared store.
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._buckets: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self._max_requests <= 0:
            return True
        now = time.monotonic()
        cutoff = now - self._window_seconds
        with self._lock:
            timestamps = self._buckets.get(key)
            if timestamps is None:
                timestamps = deque()
                self._buckets[key] = timestamps
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= self._max_requests:
                return False
            timestamps.append(now)
            return True


def _redact_log_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                _REDACTED_LOG_VALUE
                if _SENSITIVE_LOG_KEY_PATTERN.search(str(key))
                else _redact_log_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_log_value(item) for item in value]
    if isinstance(value, str):
        try:
            nested_value = json.loads(value)
        except json.JSONDecodeError:
            return value
        if isinstance(nested_value, (dict, list)):
            return json.dumps(
                _redact_log_value(nested_value),
                ensure_ascii=False,
                separators=(",", ":"),
            )
    return value


def _log_looker_request(
    app: Flask,
    action_name: str,
    endpoint: str,
    payload: Any,
) -> None:
    safe_payload = _redact_log_value(payload)
    serialized_payload = json.dumps(
        safe_payload, ensure_ascii=False, separators=(",", ":")
    )
    payload_bytes = serialized_payload.encode("utf-8")
    if len(payload_bytes) > _MAX_LOG_PAYLOAD_BYTES:
        preview = payload_bytes[:_MAX_LOG_PAYLOAD_BYTES].decode(
            "utf-8", errors="ignore"
        )
        safe_payload = {
            "truncated": True,
            "original_bytes": len(payload_bytes),
            "preview": preview,
        }

    log_entry = {
        "event": "looker_action_request",
        "action_name": action_name,
        "endpoint": endpoint,
        "method": request.method,
        "content_type": request.content_type,
        "content_length": request.content_length,
        "looker_instance": request.headers.get("X-Looker-Instance"),
        "payload": safe_payload,
    }
    app.logger.info(
        "LOOKER_ACTION_REQUEST %s",
        json.dumps(log_entry, ensure_ascii=False, separators=(",", ":")),
    )


def create_app(
    bigquery_client_factory: BigQueryClientFactory | None = None,
    rate_limit_per_minute: int | None = None,
) -> Flask:
    app = Flask(__name__)
    client_factory = bigquery_client_factory or (
        lambda project_id: bigquery.Client(project=project_id)
    )
    configured_limit = rate_limit_per_minute
    if configured_limit is None:
        try:
            configured_limit = int(
                os.environ.get(
                    "RATE_LIMIT_PER_MINUTE", str(_DEFAULT_RATE_LIMIT_PER_MINUTE)
                )
            )
        except ValueError:
            configured_limit = _DEFAULT_RATE_LIMIT_PER_MINUTE
    rate_limiter = _SlidingWindowRateLimiter(
        max_requests=configured_limit,
        window_seconds=_RATE_LIMIT_WINDOW_SECONDS,
    )

    @app.after_request
    def apply_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        return response

    @app.before_request
    def enforce_method_rate_and_auth():
        if request.method not in _ALLOWED_HTTP_METHODS:
            return jsonify({"error": "Method not allowed."}), 405

        client_ip = (
            request.access_route[-1]
            if request.access_route
            else (request.remote_addr or "unknown")
        )
        if not rate_limiter.allow(client_ip):
            return jsonify({"error": "Rate limit exceeded."}), 429

        if (
            request.endpoint in _PUBLIC_READ_ENDPOINTS
            and request.method in {"GET", "HEAD"}
        ):
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

    @app.get("/docs", strict_slashes=False)
    def docs():
        return render_template("docs.html")

    @app.get("/openapi.json")
    def openapi_spec():
        return jsonify(build_openapi_spec())

    @app.post("/")
    def list_actions():
        base_url = os.environ.get("ACTION_HUB_BASE_URL", "").rstrip("/")
        if not base_url:
            app.logger.error("ACTION_HUB_BASE_URL is not configured.")
            return jsonify({"error": "Action Hub URL is not configured."}), 503

        params = [
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
            {
                "name": "looker_user_id",
                "label": "Looker user ID",
                "description": "Mapped to Looker's built-in id user attribute.",
                "required": True,
                "sensitive": False,
                "user_attribute_name": "id",
            },
        ]
        shared_action_settings = {
            "form_url": f"{base_url}/actions/{ACTION_NAME}/form",
            "params": params,
            "supported_download_settings": ["push"],
            "uses_oauth": False,
        }
        return jsonify(
            {
                "label": os.environ.get("ACTION_HUB_LABEL", "BigQuery Cell Actions"),
                "integrations": [
                    {
                        **shared_action_settings,
                        "name": ACTION_NAME,
                        "label": "Save cell to BigQuery",
                        "description": (
                            "Save the selected Looker cell value and form details "
                            "to a BigQuery table."
                        ),
                        "supported_action_types": ["cell"],
                        "required_fields": [{"tag": "bigquery_write"}],
                        "url": f"{base_url}/actions/{ACTION_NAME}/execute",
                    },
                    {
                        **shared_action_settings,
                        "name": QUERY_ACTION_NAME,
                        "label": "Save Explore SQL to BigQuery",
                        "description": (
                            "Save the SQL generated for a Looker Explore query "
                            "to BigQuery."
                        ),
                        "supported_action_types": ["query"],
                        "supported_formats": ["json_detail"],
                        "required_fields": [],
                        "url": f"{base_url}/actions/{QUERY_ACTION_NAME}/execute",
                        "form_url": f"{base_url}/actions/{QUERY_ACTION_NAME}/form",
                    },
                ],
            }
        )

    @app.post("/actions/<action_name>/form")
    def action_form(action_name: str):
        if action_name not in {ACTION_NAME, QUERY_ACTION_NAME}:
            return jsonify({"error": "Unknown action."}), 404
        payload = request.get_json(silent=True)
        _log_looker_request(app, action_name, "form", payload)
        if not isinstance(payload, dict):
            payload = {}
        form_params = payload.get("form_params", {})
        if not isinstance(form_params, dict):
            form_params = {}
        return jsonify({"fields": get_form_fields(form_params)})

    @app.post("/actions/<action_name>/execute")
    def execute_action(action_name: str):
        if action_name not in {ACTION_NAME, QUERY_ACTION_NAME}:
            return jsonify({"error": "Unknown action."}), 404

        payload = request.get_json(silent=True)
        _log_looker_request(app, action_name, "execute", payload)
        if not isinstance(payload, dict):
            return looker_response(False, "Request body must be a JSON object.", 400)

        field_value = None
        generated_sql = None
        if action_name == ACTION_NAME:
            if payload.get("type") != "cell":
                return looker_response(
                    False,
                    "This action only supports cell payloads.",
                    400,
                )
            data = payload.get("data")
            if not isinstance(data, dict):
                return looker_response(False, "The cell data is missing.", 400)
            if "value" not in data or not isinstance(data["value"], str):
                return looker_response(False, "The cell value is missing or invalid.", 400)
            field_value = data["value"]
        else:
            if payload.get("type") != "query":
                return looker_response(
                    False,
                    "This action only supports query payloads.",
                    400,
                )
            generated_sql = extract_generated_sql(payload)
            if generated_sql is None:
                return looker_response(
                    False,
                    "The query attachment does not contain generated SQL. "
                    "Ensure Looker sends json_detail format.",
                    400,
                )

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

        settings = payload.get("data")
        if not isinstance(settings, dict):
            return looker_response(False, "Action settings are missing.", 400)
        destination: dict[str, str] = {}
        for key in ("project_id", "dataset_id", "table_id"):
            value = settings.get(key)
            if not isinstance(value, str) or not value.strip():
                return looker_response(
                    False,
                    f"Missing required action setting: {key}.",
                    400,
                )
            trimmed = value.strip()
            pattern = (
                _PROJECT_ID_PATTERN if key == "project_id" else _BQ_IDENTIFIER_PATTERN
            )
            if not pattern.fullmatch(trimmed):
                return looker_response(
                    False,
                    f"Invalid BigQuery identifier for {key}.",
                    400,
                )
            destination[key] = trimmed

        raw_user_id = settings.get("looker_user_id")
        if raw_user_id is None:
            looker_user_id = None
        elif isinstance(raw_user_id, str):
            looker_user_id = raw_user_id.strip() or None
        elif isinstance(raw_user_id, int) and not isinstance(raw_user_id, bool):
            looker_user_id = str(raw_user_id)
        else:
            return looker_response(False, "Looker user ID must be text.", 400)

        row = build_row(
            field_value=field_value,
            category=category,
            notes=notes,
            looker_instance=request.headers.get("X-Looker-Instance"),
            generated_sql=generated_sql,
            looker_user_id=looker_user_id,
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

        message = (
            "Cell saved to BigQuery."
            if action_name == ACTION_NAME
            else "Explore SQL saved to BigQuery."
        )
        return looker_response(True, message, 200)

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


def extract_generated_sql(payload: dict[str, Any]) -> str | None:
    attachment = payload.get("attachment")
    if not isinstance(attachment, dict):
        return None

    content = attachment.get("data")
    if isinstance(content, str):
        try:
            detail = json.loads(content)
        except json.JSONDecodeError:
            return None
    elif isinstance(content, dict):
        detail = content
    else:
        return None

    if not isinstance(detail, dict):
        return None
    sql = detail.get("sql")
    return sql if isinstance(sql, str) and sql.strip() else None


def build_row(
    field_value: str | None,
    category: str,
    notes: str,
    looker_instance: str | None,
    generated_sql: str | None = None,
    looker_user_id: str | None = None,
) -> dict[str, str | None]:
    return {
        "inserted_at": datetime.now(timezone.utc).isoformat(),
        "field_value": field_value,
        "category": category,
        "notes": notes,
        "looker_instance": looker_instance,
        "generated_sql": generated_sql,
        "looker_user_id": looker_user_id,
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


def build_openapi_spec() -> dict[str, Any]:
    """Return an OpenAPI 3.0.3 specification for the Action Hub endpoints."""
    token_security = [{"lookerTokenAuth": []}]
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Looker BigQuery Cell Action Hub",
            "version": "1.0.0",
            "description": (
                "Looker Action Hub API for saving cell values and Explore SQL "
                "queries to BigQuery."
            ),
        },
        "components": {
            "securitySchemes": {
                "lookerTokenAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "Authorization",
                    "description": 'Looker token header: Token token="<ACTION_HUB_TOKEN>"',
                }
            }
        },
        "paths": {
            "/health": {
                "get": {
                    "summary": "Unauthenticated health check returning service status.",
                    "responses": {"200": {"description": "Service is healthy."}},
                }
            },
            "/openapi.json": {
                "get": {
                    "summary": "OpenAPI 3.0.3 specification for this Action Hub.",
                    "responses": {"200": {"description": "OpenAPI JSON document."}},
                }
            },
            "/docs": {
                "get": {
                    "summary": "Interactive API documentation and query tester UI.",
                    "responses": {"200": {"description": "HTML documentation UI."}},
                }
            },
            "/": {
                "post": {
                    "summary": "Discover available Looker integrations and parameters.",
                    "security": token_security,
                    "responses": {
                        "200": {"description": "Looker Action Hub integration list."},
                        "401": {"description": "Unauthorized."},
                    },
                }
            },
            "/actions/{action_name}/form": {
                "post": {
                    "summary": (
                        "Return dynamic Looker form fields for bigquery_cell_capture "
                        "or bigquery_query_sql_capture."
                    ),
                    "security": token_security,
                    "parameters": [
                        {
                            "name": "action_name",
                            "in": "path",
                            "required": True,
                            "schema": {
                                "type": "string",
                                "enum": [ACTION_NAME, QUERY_ACTION_NAME],
                            },
                        }
                    ],
                    "responses": {
                        "200": {"description": "Looker form field definitions."},
                        "401": {"description": "Unauthorized."},
                        "404": {"description": "Unknown action."},
                    },
                }
            },
            "/actions/{action_name}/execute": {
                "post": {
                    "summary": (
                        "Execute bigquery_cell_capture or bigquery_query_sql_capture "
                        "and insert a row into BigQuery."
                    ),
                    "security": token_security,
                    "parameters": [
                        {
                            "name": "action_name",
                            "in": "path",
                            "required": True,
                            "schema": {
                                "type": "string",
                                "enum": [ACTION_NAME, QUERY_ACTION_NAME],
                            },
                        }
                    ],
                    "responses": {
                        "200": {"description": "Row inserted into BigQuery."},
                        "400": {"description": "Invalid Looker payload or form data."},
                        "401": {"description": "Unauthorized."},
                        "404": {"description": "Unknown action."},
                        "502": {"description": "BigQuery write failed."},
                    },
                }
            },
        },
    }


logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
