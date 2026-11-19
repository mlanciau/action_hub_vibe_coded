# Looker BigQuery Cell Action Hub

A small Python Looker Action Hub that adds a cell-level action for saving a
selected Looker field value, along with a custom form response, to BigQuery.

## What it does

- Appears on fields tagged `bigquery_write`.
- Shows a form with a category dropdown and optional notes.
- Inserts one row per action into a BigQuery table.
- Also exposes a query-level action that stores Looker's generated SQL from
  the `json_detail` attachment, one row per Explore query.
- Uses Application Default Credentials (ADC); it does not collect Google
  service-account credentials in Looker.

## Configure BigQuery

Create a table with this schema (or adapt `build_row` in `app.py` to match an
existing table):

```sql
CREATE TABLE `your-project.your_dataset.looker_cell_actions` (
  inserted_at TIMESTAMP NOT NULL,
  field_value STRING,
  generated_sql STRING,
  category STRING,
  notes STRING,
  looker_instance STRING
);
```

Grant the identity used by the action service permission to insert rows into
the dataset, such as BigQuery Data Editor. For local development, authenticate
with `gcloud auth application-default login`. In a cloud deployment, prefer
the platform's attached service account or workload identity over a key file.

## Run locally

Requires Python 3.10 or later.

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export ACTION_HUB_BASE_URL="https://your-public-host.example"
export ACTION_HUB_TOKEN="replace-with-a-long-random-token"
python app.py
```

`ACTION_HUB_BASE_URL` must be the externally reachable HTTPS URL of the hub,
without a trailing slash. Looker requires a trusted HTTPS connection.

For production, run with a WSGI server such as:

```sh
gunicorn --bind "0.0.0.0:${PORT:-8080}" app:app
```

## Cloud Logging request diagnostics

The service logs JSON-encoded diagnostics for Looker form and execute requests
at `INFO` level. The payload includes non-secret request fields, including SQL
and submitted cell/form values. Authorization and cookie headers are not
logged; payload fields that look like credentials, tokens, OAuth state, or
signatures are redacted. JSON-encoded strings inside the payload are also
checked for sensitive fields. Payload previews are capped at 180 KiB.

Cloud Run forwards these application logs to Cloud Logging. For example:

```sh
gcloud run services logs read SERVICE \
  --project=PROJECT_ID \
  --region=REGION \
  --freshness=1h
```

Because request logs can contain SQL and business data, restrict Cloud Logging
access and retention to the appropriate operators.

When adding the hub in Looker under **Admin > Actions**, use
`ACTION_HUB_BASE_URL` as the hub URL and enter the exact value of
`ACTION_HUB_TOKEN` as its authorization token. The service expects Looker's
standard `Authorization: Token token="..."` header. Keep the token secret.

After adding the hub, configure the required **BigQuery project ID**,
**BigQuery dataset ID**, and **BigQuery table ID** parameters for each action
in Looker. The cell action continues to write the selected value;
**Save Explore SQL to BigQuery** is a query-level action that writes the SQL
with `field_value` left `NULL`.

## Tag fields in LookML

Add the required tag to each field where the cell action should appear:

```lookml
dimension: customer_id {
  type: string
  sql: ${TABLE}.customer_id ;;
  tags: ["bigquery_write"]
}
```

## Customize the form and table row

Edit `CATEGORY_OPTIONS` and `get_form_fields` in `app.py` to change the
category choices or form fields. Update `build_row` and the table schema above
to change what is inserted. Form values arrive as strings in Looker's
`form_params`. The query-level action requires Looker's `json_detail` format,
which contains the generated SQL.

## Verify

Run the unit tests with:

```sh
python -m unittest discover -s tests
```

The service also exposes an unauthenticated `GET /health` endpoint for
deployment health checks. Action API endpoints require the configured token.
