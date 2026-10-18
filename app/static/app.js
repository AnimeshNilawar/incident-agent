/* Micuppa Incident Dashboard — plain JavaScript, no build step.
 *
 * Two hash-routed views: the incident list and one incident's detail
 * (with its investigation report). All server data is treated as
 * untrusted and passed through h() before it touches innerHTML, so an
 * alert title or an LLM-generated explanation can never inject markup.
 *
 * Authentication: the API endpoints answer 401 with a Basic challenge
 * and the browser handles the credential prompt natively for
 * same-origin fetches — no credentials are stored or handled here.
 */
(() => {
  "use strict";

  const API_BASE = "/api/v1/dashboard";
  const UUID_PATTERN =
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

  const state = {
    incidents: [],
    severity: "",
    status: "",
    loading: false,
  };

  // --- Small helpers -----------------------------------------------------

  /** Escape an untrusted value for interpolation into HTML. */
  function h(value) {
    if (value === null || value === undefined) return "";
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatTimestamp(iso) {
    if (!iso) return "\u2014";
    const parsed = new Date(iso);
    if (Number.isNaN(parsed.getTime())) return String(iso);
    return parsed.toLocaleString();
  }

  /** CSS-class-safe form of a server-provided status word. */
  function slug(value) {
    return String(value || "")
      .toLowerCase()
      .replace(/[^a-z0-9-]/g, "");
  }

  const SOURCE_LABELS = {
    grafana_alert: "Grafana alert data",
    backend_logs: "Backend logs",
    frontend_errors: "PostHog frontend errors",
    source_code: "Source code",
  };

  function sourceLabel(source) {
    return SOURCE_LABELS[source] || source || "Unknown source";
  }

  function severityBadge(severity) {
    const value = slug(severity) || "unknown";
    const known = ["critical", "high", "medium", "low"];
    const cls = known.includes(value)
      ? "badge-sev-" + value
      : "badge-status";
    return `<span class="badge ${cls}">${h(severity || "unknown")}</span>`;
  }

  function statusBadge(status) {
    if (!status) return `<span class="badge-none">\u2014</span>`;
    return `<span class="badge badge-status badge-status-${slug(status)}">${h(status)}</span>`;
  }

  function investigationCell(info) {
    if (!info || !info.job_status) {
      return `<span class="badge-none">Not investigated</span>`;
    }
    return statusBadge(info.job_status);
  }

  function reportCell(info) {
    if (!info || !info.report_available) {
      return `<span class="badge-none">No report</span>`;
    }
    const status = String(info.report_status || "available");
    const extra =
      status === "completed"
        ? "badge-status-completed"
        : status === "inconclusive"
          ? "badge-inconclusive"
          : "badge-status";
    return `<span class="badge badge-status ${extra}">${h(status)}</span>`;
  }

  // --- API access --------------------------------------------------------

  class ApiError extends Error {
    constructor(status) {
      super("Request failed with status " + status);
      this.status = status;
    }
  }

  async function fetchJson(url) {
    let response;
    try {
      response = await fetch(url, {
        headers: { Accept: "application/json" },
      });
    } catch (_networkError) {
      const error = new Error("network");
      error.network = true;
      throw error;
    }
    if (!response.ok) throw new ApiError(response.status);
    return response.json();
  }

  function messageFor(error) {
    if (error && error.network) {
      return "Could not reach the dashboard API. Check your connection and try again.";
    }
    const status = error && error.status;
    if (status === 401) {
      return (
        "Sign-in required: the browser should prompt for the incident " +
        "dashboard credentials. If no prompt appeared, reload the page. " +
        "(HTTP 401)"
      );
    }
    if (status === 503) {
      return (
        "Dashboard access is not configured on the server: set " +
        "INCIDENT_API_USERNAME and INCIDENT_API_PASSWORD. (HTTP 503)"
      );
    }
    if (status === 404) {
      return "The requested record no longer exists. (HTTP 404)";
    }
    if (status) {
      return `The server returned an error. (HTTP ${status})`;
    }
    return "Unexpected dashboard error.";
  }

  function setError(message) {
    const banner = document.getElementById("error-banner");
    if (message) {
      banner.textContent = message;
      banner.hidden = false;
    } else {
      banner.textContent = "";
      banner.hidden = true;
    }
  }

  // --- List view ---------------------------------------------------------

  function filteredIncidents() {
    return state.incidents.filter((incident) => {
      if (state.severity && incident.severity !== state.severity) {
        return false;
      }
      if (state.status && incident.status !== state.status) {
        return false;
      }
      return true;
    });
  }

  function rowHtml(incident) {
    return `
      <tr>
        <td data-label="Incident" class="incident-title">
          <a href="#/incidents/${h(incident.id)}">${h(incident.title)}</a>
        </td>
        <td data-label="Service">${h(incident.service_name)}</td>
        <td data-label="Severity">${severityBadge(incident.severity)}</td>
        <td data-label="Status">${statusBadge(incident.status)}</td>
        <td data-label="First seen" class="cell-muted">${h(formatTimestamp(incident.first_seen_at))}</td>
        <td data-label="Last seen" class="cell-muted">${h(formatTimestamp(incident.last_seen_at))}</td>
        <td data-label="Investigation">${investigationCell(incident.investigation)}</td>
        <td data-label="Report">${reportCell(incident.investigation)}</td>
      </tr>`;
  }

  function renderList() {
    const loadingNote = document.getElementById("list-loading");
    const emptyNote = document.getElementById("list-empty");
    const noMatchNote = document.getElementById("list-no-match");
    const tableWrap = document.querySelector(".table-wrap");
    const tbody = document.getElementById("incident-rows");

    loadingNote.hidden = !state.loading;

    const rows = filteredIncidents();
    emptyNote.hidden = state.incidents.length !== 0;
    noMatchNote.hidden = !(
      state.incidents.length > 0 && rows.length === 0
    );
    tableWrap.hidden = rows.length === 0 && !state.loading;
    tbody.innerHTML = rows.map(rowHtml).join("");
  }

  async function loadIncidents() {
    state.loading = true;
    setError(null);
    renderList();

    const refreshButton = document.getElementById("refresh-button");
    refreshButton.disabled = true;

    try {
      state.incidents = await fetchJson(
        `${API_BASE}/incidents?limit=100`
      );
    } catch (error) {
      state.incidents = [];
      setError(messageFor(error));
    } finally {
      state.loading = false;
      refreshButton.disabled = false;
      renderList();
    }
  }

  // --- Detail view -------------------------------------------------------

  function citationsHtml(ids) {
    if (!Array.isArray(ids) || ids.length === 0) return "";
    const chips = ids
      .map(
        (id) =>
          `<li><button type="button" class="citation-chip" data-evidence-id="${h(id)}">${h(id)}</button></li>`
      )
      .join("");
    return `<ul class="citation-list">${chips}</ul>`;
  }

  function evidenceItemHtml(item) {
    const metadata = item.metadata || {};
    const metaKeys = Object.keys(metadata);
    const metaHtml =
      metaKeys.length > 0
        ? `<dl class="evidence-meta">${metaKeys
            .map(
              (key) =>
                `<dt>${h(key)}</dt><dd>${h(metadata[key])}</dd>`
            )
            .join("")}</dl>`
        : "";
    const contentHtml = item.content
      ? `<details class="evidence-details"><summary>Show content</summary><pre class="evidence-content">${h(item.content)}</pre></details>`
      : "";
    const observed = item.observed_at
      ? `observed ${h(formatTimestamp(item.observed_at))}`
      : "";

    return `
      <div class="evidence-item" id="evidence-${h(item.id)}">
        <div class="evidence-head">
          <span class="evidence-source">${h(sourceLabel(item.source))}</span>
          <span class="evidence-id">${h(item.id)}</span>
          <span class="evidence-observed">${observed}</span>
        </div>
        <div class="evidence-body">
          <p class="evidence-description">${h(item.description)}</p>
          ${contentHtml}
          ${metaHtml}
        </div>
      </div>`;
  }

  function missingEvidenceHtml(item) {
    return `
      <div class="evidence-item evidence-item-missing" id="evidence-${h(item.id)}">
        <div class="evidence-head">
          <span class="evidence-gap-label">Not collected</span>
          <span class="evidence-source">${h(sourceLabel(item.source))}</span>
          <span class="evidence-id">${h(item.id)}</span>
        </div>
        <div class="evidence-body">
          <p class="evidence-description">${h(item.description)}</p>
        </div>
      </div>`;
  }

  function evidenceSectionHtml(report) {
    const evidence = Array.isArray(report.evidence) ? report.evidence : [];
    const available = evidence.filter((item) => item.status === "available");
    const missing = evidence.filter((item) => item.status === "missing");

    let html = `
      <section class="card">
        <h3>Supporting evidence</h3>
        <p class="notice notice-ai">
          Observed evidence: everything below was collected by the system
          around the incident. Missing sources are listed explicitly &mdash;
          their content is unknown, not omitted. Conclusions drawn from this
          evidence appear in the investigation section above.
        </p>
        <p class="evidence-count">${available.length} collected &middot; ${missing.length} missing</p>`;

    if (available.length > 0) {
      html += available.map(evidenceItemHtml).join("");
    } else {
      html += `<p class="state-note">No evidence items were collected for this incident.</p>`;
    }

    if (missing.length > 0) {
      html += `<h4>Missing evidence (explicit gaps)</h4>`;
      html += missing.map(missingEvidenceHtml).join("");
    }

    html += `</section>`;
    return html;
  }

  function reportSectionHtml(report) {
    const rootCauseHtml =
      report.status === "completed" && report.probable_root_cause
        ? `
          <h4>Probable root cause (hypothesis)</h4>
          <p class="root-cause">${h(report.probable_root_cause)}</p>
          ${citationsHtml(report.root_cause_evidence_ids)}
          <h4>Explanation</h4>
          <p class="explanation">${h(report.explanation || "\u2014")}</p>`
        : `
          <p class="notice notice-none">
            The investigation was inconclusive: the available evidence did
            not support stating a probable root cause. See the limitations
            below.
          </p>`;

    const confidence =
      report.confidence === null || report.confidence === undefined
        ? "not reported"
        : `${Math.round(report.confidence * 100)}% (AI-assessed)`;

    const limitations =
      Array.isArray(report.limitations) && report.limitations.length > 0
        ? `<ul class="limitation-list">${report.limitations
            .map((line) => `<li>${h(line)}</li>`)
            .join("")}</ul>`
        : `<p class="notice notice-none">No limitations were recorded.</p>`;

    const fixes =
      Array.isArray(report.suggested_fixes) &&
      report.suggested_fixes.length > 0
        ? `<p class="notice notice-advisory">
             Advisory only: these remediation steps are suggestions for a
             human to review. Nothing has been applied automatically.
           </p>
           <ol class="fix-list">${report.suggested_fixes
             .map(
               (fix) => `
             <li class="fix">
               <p class="fix-title">${h(fix.title)}</p>
               <p class="fix-description">${h(fix.description)}</p>
               ${citationsHtml(fix.evidence_ids)}
             </li>`
             )
             .join("")}</ol>`
        : `<p class="notice notice-none">No remediation suggestions were produced.</p>`;

    const modelLine = [
      report.model_name ? `model: ${report.model_name}` : null,
      `report updated ${formatTimestamp(report.updated_at)}`,
    ]
      .filter(Boolean)
      .join(" \u00b7 ");

    return `
      <section class="card">
        <h3>Investigation report ${statusBadge(report.status)}</h3>
        <p class="notice notice-ai">
          AI-generated investigation: the root cause, explanation and
          remediation below are model hypotheses cited against observed
          evidence, not confirmed facts.
        </p>
        <p class="evidence-count">${h(modelLine)} &middot; confidence: ${h(confidence)}</p>
        ${rootCauseHtml}
        <h4>Limitations</h4>
        ${limitations}
        <h4>Suggested remediation</h4>
        ${fixes}
      </section>`;
  }

  function investigationSectionHtml(detail) {
    const job = detail.job;
    const report = detail.report;

    if (!job) {
      return `
        <section class="card">
          <h3>Investigation</h3>
          <p class="notice notice-none">
            No investigation has been created for this incident yet.
          </p>
        </section>`;
    }

    let jobLine = `job ${statusBadge(job.status)} \u00b7 attempt ${job.attempts}/${job.max_attempts}`;
    jobLine += ` \u00b7 created ${formatTimestamp(job.created_at)}`;
    if (job.started_at) jobLine += ` \u00b7 started ${formatTimestamp(job.started_at)}`;
    if (job.completed_at) {
      jobLine += ` \u00b7 finished ${formatTimestamp(job.completed_at)}`;
    }

    let html = `
      <section class="card">
        <h3>Investigation</h3>
        <p class="evidence-count">${jobLine}</p>`;

    if (job.status === "failed" && job.last_error) {
      html += `
        <h4>Last error</h4>
        <pre class="evidence-content">${h(job.last_error)}</pre>`;
    }

    if (!report) {
      html += `
        <p class="notice notice-none">
          No report has been produced for this investigation yet. It will
          appear here once the investigation completes.
        </p>`;
    }

    html += `</section>`;

    if (report) html += reportSectionHtml(report);
    return html;
  }

  function metaSectionHtml(detail) {
    const rows = [
      ["Service", h(detail.service_name)],
      ["Severity", severityBadge(detail.severity)],
      ["Status", statusBadge(detail.status)],
      ["Source", h(detail.source)],
      ["Fingerprint", `<code>${h(detail.fingerprint)}</code>`],
      ["First seen", h(formatTimestamp(detail.first_seen_at))],
      ["Last seen", h(formatTimestamp(detail.last_seen_at))],
      ["Incident ID", `<code>${h(detail.id)}</code>`],
    ];

    let html = `
      <section class="card detail-header">
        <h2>${h(detail.title)}</h2>
        <div class="detail-badges">
          ${severityBadge(detail.severity)}
          ${statusBadge(detail.status)}
          ${detail.investigation ? investigationCell(detail.investigation) : ""}
          ${detail.investigation ? reportCell(detail.investigation) : ""}
        </div>
        <dl class="meta-grid">
          ${rows.map(([key, value]) => `<dt>${key}</dt><dd>${value}</dd>`).join("")}
        </dl>
      </section>`;

    if (detail.summary) {
      html += `
        <section class="card">
          <h3>Alert summary (as received)</h3>
          <p class="summary-text">${h(detail.summary)}</p>
        </section>`;
    }

    return html;
  }

  function renderDetail(detail) {
    const container = document.getElementById("detail-content");
    let html = metaSectionHtml(detail);
    html += investigationSectionHtml(detail);

    if (detail.report) {
      html += evidenceSectionHtml(detail.report);
    } else {
      html += `
        <section class="card">
          <h3>Supporting evidence</h3>
          <p class="notice notice-none">
            Evidence is part of the investigation report. It will appear
            here once an investigation has run for this incident.
          </p>
        </section>`;
    }

    container.innerHTML = html;
  }

  async function showDetail(incidentId) {
    const viewList = document.getElementById("view-list");
    const viewDetail = document.getElementById("view-detail");
    const container = document.getElementById("detail-content");

    viewList.hidden = true;
    viewDetail.hidden = false;
    setError(null);
    container.innerHTML = `<p class="state-note">Loading incident&hellip;</p>`;

    try {
      const detail = await fetchJson(
        `${API_BASE}/incidents/${encodeURIComponent(incidentId)}`
      );
      renderDetail(detail);
    } catch (error) {
      container.innerHTML = "";
      setError(messageFor(error));
    }
  }

  function showList() {
    document.getElementById("view-list").hidden = false;
    document.getElementById("view-detail").hidden = true;
    document.getElementById("detail-content").innerHTML = "";
  }

  // --- Router ------------------------------------------------------------

  function route() {
    const hash = window.location.hash || "#/";
    const match = hash.match(/^#\/incidents\/([^/]+)$/);

    if (match) {
      const id = decodeURIComponent(match[1]);
      if (!UUID_PATTERN.test(id)) {
        showList();
        setError("That incident link is not valid.");
        renderList();
        return;
      }
      showDetail(id);
      return;
    }

    showList();
    loadIncidents();
  }

  // --- Wiring ------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", () => {
    document
      .getElementById("refresh-button")
      .addEventListener("click", loadIncidents);

    document
      .getElementById("filter-severity")
      .addEventListener("change", (event) => {
        state.severity = event.target.value;
        renderList();
      });

    document
      .getElementById("filter-status")
      .addEventListener("change", (event) => {
        state.status = event.target.value;
        renderList();
      });

    // Citation chips scroll to the cited evidence and open it if it is
    // collapsed. getElementById (not a selector) keeps odd evidence IDs
    // from being interpreted as selector syntax.
    document
      .getElementById("detail-content")
      .addEventListener("click", (event) => {
        const chip = event.target.closest(".citation-chip");
        if (!chip) return;
        const target = document.getElementById(
          "evidence-" + chip.dataset.evidenceId
        );
        if (!target) return;
        const details = target.querySelector("details");
        if (details) details.open = true;
        target.scrollIntoView();
      });

    window.addEventListener("hashchange", route);
    route();
  });
})();
