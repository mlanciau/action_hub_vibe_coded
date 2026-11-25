(function () {
  "use strict";

  const SAMPLE_SQL = [
    "SELECT",
    "  transactions.block_hash AS transactions_block_hash,",
    "  transactions.block_number AS transactions_block_number",
    "FROM `bigquery-public-data.crypto_bitcoin.transactions` AS transactions",
    "WHERE transactions.block_timestamp_month > '2018-01-01'",
    "LIMIT 5",
  ].join("\n");

  const tokenInput = document.getElementById("tokenInput");
  const toggleTokenBtn = document.getElementById("toggleTokenBtn");
  const checkHealthBtn = document.getElementById("checkHealthBtn");
  const listActionsBtn = document.getElementById("listActionsBtn");
  const healthBadge = document.getElementById("healthBadge");
  const authOutputWrap = document.getElementById("authOutputWrap");
  const authStatusBadge = document.getElementById("authStatusBadge");
  const authLatency = document.getElementById("authLatency");
  const authOutput = document.getElementById("authOutput");

  const actionSelect = document.getElementById("actionSelect");
  const modeBadge = document.getElementById("modeBadge");
  const lookerInstanceInput = document.getElementById("lookerInstanceInput");
  const projectIdInput = document.getElementById("projectIdInput");
  const datasetIdInput = document.getElementById("datasetIdInput");
  const tableIdInput = document.getElementById("tableIdInput");
  const userIdInput = document.getElementById("userIdInput");
  const queryFieldsSection = document.getElementById("queryFieldsSection");
  const cellFieldsSection = document.getElementById("cellFieldsSection");
  const sqlInput = document.getElementById("sqlInput");
  const sampleQueryBtn = document.getElementById("sampleQueryBtn");
  const cellValueInput = document.getElementById("cellValueInput");
  const categorySelect = document.getElementById("categorySelect");
  const notesInput = document.getElementById("notesInput");
  const fetchFormBtn = document.getElementById("fetchFormBtn");
  const executeActionBtn = document.getElementById("executeActionBtn");
  const requestEndpointLabel = document.getElementById("requestEndpointLabel");
  const requestPreview = document.getElementById("requestPreview");
  const responseStatusBadge = document.getElementById("responseStatusBadge");
  const responseLatency = document.getElementById("responseLatency");
  const responseOutput = document.getElementById("responseOutput");
  const specVersionBadge = document.getElementById("specVersionBadge");
  const endpointsContainer = document.getElementById("endpointsContainer");

  function setBadge(el, text, kind) {
    el.textContent = text;
    el.className = "badge badge-" + kind;
  }

  function buildAuthHeaders(extraHeaders) {
    const headers = Object.assign({}, extraHeaders || {});
    const token = tokenInput.value;
    if (token) {
      headers.Authorization = 'Token token="' + token + '"';
    }
    const lookerInstance = lookerInstanceInput.value.trim();
    if (lookerInstance) {
      headers["X-Looker-Instance"] = lookerInstance;
    }
    return headers;
  }

  function buildExecutePayload() {
    const actionName = actionSelect.value;
    const data = {
      project_id: projectIdInput.value.trim() || projectIdInput.placeholder,
      dataset_id: datasetIdInput.value.trim() || datasetIdInput.placeholder,
      table_id: tableIdInput.value.trim() || tableIdInput.placeholder,
    };
    const userId = userIdInput.value.trim();
    if (userId) {
      data.looker_user_id = userId;
    }
    const formParams = {
      category: categorySelect.value,
      notes: notesInput.value,
    };

    if (actionName === "bigquery_cell_capture") {
      data.value = cellValueInput.value || cellValueInput.placeholder;
      return {
        type: "cell",
        data: data,
        form_params: formParams,
      };
    }

    return {
      type: "query",
      data: data,
      attachment: {
        mimetype: "application/json",
        extension: "json",
        data: {
          sql: sqlInput.value || SAMPLE_SQL,
        },
      },
      form_params: formParams,
    };
  }

  function updateModeAndPreview() {
    const actionName = actionSelect.value;
    const isCell = actionName === "bigquery_cell_capture";
    queryFieldsSection.classList.toggle("hidden", isCell);
    cellFieldsSection.classList.toggle("hidden", !isCell);
    setBadge(modeBadge, isCell ? "Cell Action" : "Query SQL Action", "info");
    requestEndpointLabel.textContent = "POST /actions/" + actionName + "/execute";
    requestPreview.textContent = JSON.stringify(buildExecutePayload(), null, 2);
  }

  async function callJsonEndpoint(url, options) {
    const started = performance.now();
    const response = await fetch(url, options);
    const elapsedMs = Math.round(performance.now() - started);
    const rawText = await response.text();
    let parsedText = rawText;
    try {
      parsedText = JSON.stringify(JSON.parse(rawText), null, 2);
    } catch (_err) {
      // Leave plain text as-is when response is not JSON.
    }
    return { status: response.status, ok: response.ok, elapsedMs: elapsedMs, body: parsedText };
  }

  async function checkHealth() {
    try {
      const res = await callJsonEndpoint("/health", { method: "GET" });
      setBadge(healthBadge, res.ok ? "Health: OK (200)" : "Health: " + res.status, res.ok ? "ok" : "err");
      authOutputWrap.classList.remove("hidden");
      setBadge(authStatusBadge, "GET /health -> " + res.status, res.ok ? "ok" : "err");
      authLatency.textContent = res.elapsedMs + " ms";
      authOutput.textContent = res.body;
    } catch (err) {
      setBadge(healthBadge, "Health: error", "err");
      authOutputWrap.classList.remove("hidden");
      authOutput.textContent = String(err);
    }
  }

  async function listActions() {
    try {
      const res = await callJsonEndpoint("/", {
        method: "POST",
        headers: buildAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({}),
      });
      authOutputWrap.classList.remove("hidden");
      setBadge(authStatusBadge, "POST / -> " + res.status, res.ok ? "ok" : "err");
      authLatency.textContent = res.elapsedMs + " ms";
      authOutput.textContent = res.body;
    } catch (err) {
      authOutputWrap.classList.remove("hidden");
      setBadge(authStatusBadge, "Network error", "err");
      authOutput.textContent = String(err);
    }
  }

  async function fetchForm() {
    const actionName = actionSelect.value;
    const payload = {
      form_params: {
        category: categorySelect.value,
        notes: notesInput.value,
      },
    };
    requestEndpointLabel.textContent = "POST /actions/" + actionName + "/form";
    requestPreview.textContent = JSON.stringify(payload, null, 2);
    try {
      const res = await callJsonEndpoint("/actions/" + encodeURIComponent(actionName) + "/form", {
        method: "POST",
        headers: buildAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });
      setBadge(responseStatusBadge, "HTTP " + res.status, res.ok ? "ok" : "err");
      responseLatency.textContent = res.elapsedMs + " ms";
      responseOutput.textContent = res.body;
    } catch (err) {
      setBadge(responseStatusBadge, "Network error", "err");
      responseOutput.textContent = String(err);
    }
  }

  async function executeAction() {
    const actionName = actionSelect.value;
    const payload = buildExecutePayload();
    requestEndpointLabel.textContent = "POST /actions/" + actionName + "/execute";
    requestPreview.textContent = JSON.stringify(payload, null, 2);
    try {
      const res = await callJsonEndpoint("/actions/" + encodeURIComponent(actionName) + "/execute", {
        method: "POST",
        headers: buildAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });
      setBadge(responseStatusBadge, "HTTP " + res.status, res.ok ? "ok" : "err");
      responseLatency.textContent = res.elapsedMs + " ms";
      responseOutput.textContent = res.body;
    } catch (err) {
      setBadge(responseStatusBadge, "Network error", "err");
      responseOutput.textContent = String(err);
    }
  }

  async function loadOpenApiSpec() {
    try {
      const res = await fetch("/openapi.json", { method: "GET" });
      if (!res.ok) {
        return;
      }
      const spec = await res.json();
      setBadge(specVersionBadge, "OpenAPI " + (spec.openapi || "3.0"), "info");
      const items = [];
      const paths = spec.paths || {};
      Object.keys(paths).forEach(function (path) {
        const methods = paths[path] || {};
        Object.keys(methods).forEach(function (method) {
          const op = methods[method] || {};
          const item = document.createElement("div");
          item.className = "endpoint-item";

          const head = document.createElement("div");
          head.className = "endpoint-head";

          const methodTag = document.createElement("span");
          methodTag.className = "method-tag";
          methodTag.textContent = method.toUpperCase();

          const pathEl = document.createElement("span");
          pathEl.className = "endpoint-path";
          pathEl.textContent = path;

          const authBadge = document.createElement("span");
          const requiresAuth = Array.isArray(op.security) && op.security.length > 0;
          setBadge(authBadge, requiresAuth ? "Token required" : "Public", requiresAuth ? "info" : "ok");

          head.appendChild(methodTag);
          head.appendChild(pathEl);
          head.appendChild(authBadge);
          item.appendChild(head);

          if (op.summary || op.description) {
            const desc = document.createElement("p");
            desc.className = "endpoint-desc";
            desc.textContent = op.summary || op.description;
            item.appendChild(desc);
          }

          items.push(item);
        });
      });
      endpointsContainer.replaceChildren.apply(endpointsContainer, items);
    } catch (_err) {
      // Ignore OpenAPI load failures gracefully.
    }
  }

  toggleTokenBtn.addEventListener("click", function () {
    const showing = tokenInput.type === "text";
    tokenInput.type = showing ? "password" : "text";
    toggleTokenBtn.textContent = showing ? "Show" : "Hide";
  });

  sampleQueryBtn.addEventListener("click", function () {
    sqlInput.value = SAMPLE_SQL;
    updateModeAndPreview();
  });

  [
    actionSelect,
    projectIdInput,
    datasetIdInput,
    tableIdInput,
    userIdInput,
    sqlInput,
    cellValueInput,
    categorySelect,
    notesInput,
  ].forEach(function (el) {
    el.addEventListener("input", updateModeAndPreview);
    el.addEventListener("change", updateModeAndPreview);
  });

  checkHealthBtn.addEventListener("click", checkHealth);
  listActionsBtn.addEventListener("click", listActions);
  fetchFormBtn.addEventListener("click", fetchForm);
  executeActionBtn.addEventListener("click", executeAction);

  sqlInput.value = SAMPLE_SQL;
  updateModeAndPreview();
  checkHealth();
  loadOpenApiSpec();
})();
