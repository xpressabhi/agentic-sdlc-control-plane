/* Conductor dashboard — dependency-free client for the control-plane API. */
"use strict";

const state = {
  apiKey: localStorage.getItem("asdlc.apiKey") || "dev-key",
  view: "runs",
  runs: [],
  scenarios: [],
  approvals: [],
  metrics: null,
  selected: null,
  snapshot: null,
  drawerStage: null,
  drawerTab: "overview",
  timer: null,
  launching: false,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

/* --------------------------------------------------------------- helpers */

function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "html") node.innerHTML = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function statusClass(status) {
  const map = {
    SUCCEEDED: "succeeded",
    FAILED: "failed",
    ROLLED_BACK: "failed",
    SAFE_STOPPED: "waiting",
    AWAITING_APPROVAL: "waiting",
    RUNNING: "running",
    READY: "running",
    CANCELLED: "pending",
    PENDING: "pending",
    SKIPPED: "pending",
  };
  return map[status] || "pending";
}

function statusPill(status, extra = "") {
  return h(
    "span",
    { class: `status ${status} ${extra}` },
    h("i"),
    String(status || "UNKNOWN").replaceAll("_", " ")
  );
}

function fmtDuration(start, end) {
  if (!start) return "—";
  const from = new Date(start).getTime();
  const to = end ? new Date(end).getTime() : Date.now();
  const seconds = Math.max(0, (to - from) / 1000);
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

function fmtTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function fmtAgo(value) {
  if (!value) return "—";
  const seconds = (Date.now() - new Date(value).getTime()) / 1000;
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function fmtPct(value, digits = 1) {
  if (value === null || value === undefined) return "—";
  return `${(Number(value) * 100).toFixed(digits)}%`;
}

function fmtSeconds(value) {
  if (value === null || value === undefined) return "—";
  const seconds = Number(value);
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(2)}s`;
  return `${(seconds / 60).toFixed(1)}m`;
}

let toastTimer = null;
function toast(message, bad = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("bad", bad);
  node.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.add("hidden"), 4200);
}

function setConnection(ok) {
  const node = $("#connection");
  node.classList.toggle("bad", !ok);
  $("span", node).textContent = ok ? "connected" : "unreachable";
}

async function api(path, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    "X-API-Key": state.apiKey,
    ...(options.headers || {}),
  };
  let response;
  try {
    response = await fetch(path, { ...options, headers });
  } catch (error) {
    setConnection(false);
    throw new Error("Network error: control plane is unreachable");
  }
  setConnection(true);
  if (!response.ok) {
    let body = {};
    try {
      body = await response.json();
    } catch {
      body = { error: response.statusText };
    }
    const error = new Error(body.error || `HTTP ${response.status}`);
    error.status = response.status;
    error.code = body.code;
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}

/* ------------------------------------------------------------------ boot */

document.addEventListener("DOMContentLoaded", init);

async function init() {
  $("#api-key").value = state.apiKey;
  bindEvents();
  try {
    await Promise.all([loadScenarios(), refreshAll(true)]);
    routeFromHash();
    if (!state.selected && state.runs.length && state.view === "runs") {
      await selectRun(state.runs[0].id, { replace: true, silent: true });
    }
  } catch (error) {
    toast(error.message, true);
    setConnection(false);
  }
  $("#boot").classList.add("done");
  setTimeout(routeFromHash, 350);
  state.timer = setInterval(poll, 2600);
}

function bindEvents() {
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => setView(tab.dataset.view)));
  $("#refresh").addEventListener("click", () => refreshAll(false));
  $("#auto-approve").checked = localStorage.getItem("asdlc.autoApprove") === "1";
  $("#auto-approve").addEventListener("change", (event) => {
    localStorage.setItem("asdlc.autoApprove", event.target.checked ? "1" : "0");
  });
  $("#api-key").addEventListener("change", (event) => {
    state.apiKey = event.target.value.trim() || "dev-key";
    localStorage.setItem("asdlc.apiKey", state.apiKey);
    refreshAll(false);
  });
  $("#api-key").addEventListener("keydown", (event) => {
    if (event.key === "Enter") event.target.blur();
  });
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#drawer-backdrop").addEventListener("click", closeDrawer);
  $("#modal-close").addEventListener("click", closeModal);
  $("#modal").addEventListener("click", (event) => {
    if (event.target.id === "modal") closeModal();
  });
  $("#rail-toggle").addEventListener("click", () => $(".rail").classList.toggle("open"));
  window.addEventListener("hashchange", routeFromHash);
  window.addEventListener("resize", () => requestAnimationFrame(drawEdges));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeModal();
      closeDrawer();
    }
  });
}

async function loadScenarios() {
  const response = await api("/api/v1/scenarios");
  state.scenarios = response.items;
  const list = $("#scenario-list");
  list.replaceChildren(
    ...state.scenarios.map((scenario, index) =>
      h(
        "button",
        {
          class: "scenario",
          type: "button",
          onclick: () => launchScenario(scenario.key),
          title: scenario.intent,
        },
        h("span", { class: "num", text: String(index + 1).padStart(2, "0") }),
        h(
          "span",
          {},
          h("div", { class: "s-name", text: scenario.name.split("—")[0].trim() }),
          h("div", {
            class: "s-meta",
            text: `${scenario.stages.length} stages · ${scenario.environment}`,
          })
        ),
        h("span", { class: "arrow", text: "→" })
      )
    )
  );
}

async function refreshAll(silent = false) {
  try {
    const [runsResponse, approvalsResponse] = await Promise.all([
      api("/api/v1/runs?limit=50"),
      api("/api/v1/approvals"),
    ]);
    state.runs = runsResponse.items || [];
    state.approvals = approvalsResponse.items || [];
    renderRunList();
    renderApprovalBadge();
    if (state.selected) {
      state.snapshot = await api(`/api/v1/runs/${state.selected}`);
      renderRunsView();
    }
    if (state.view === "approvals") renderApprovals();
    if (state.view === "metrics") {
      state.metrics = await api("/api/v1/metrics/summary");
      renderMetrics();
    }
  } catch (error) {
    if (!silent) toast(error.message, true);
  }
}

function poll() {
  if (document.hidden) return;
  refreshAll(true);
}

function routeFromHash() {
  const hash = location.hash.replace(/^#\/?/, "");
  if (hash.startsWith("runs/")) {
    const id = hash.split("/")[1];
    if (id) selectRun(id, { replace: true, silent: true });
    else setView("runs");
    return;
  }
  if (hash === "approvals" || hash === "metrics") {
    setView(hash);
    return;
  }
  setView("runs");
}

/* -------------------------------------------------------------- run list */

function renderRunList() {
  $("#run-count").textContent = String(state.runs.length);
  const list = $("#run-list");
  if (!state.runs.length) {
    list.replaceChildren(h("div", { class: "empty-state small" }, h("p", { text: "No runs yet." })));
    return;
  }
  list.replaceChildren(
    ...state.runs.map((run) =>
      h(
        "button",
        {
          class: `run-item ${run.id === state.selected ? "active" : ""}`,
          type: "button",
          onclick: () => selectRun(run.id),
        },
        h("span", { class: `dot ${statusClass(run.status)}` }),
        h(
          "span",
          { style: "min-width:0" },
          h("div", { class: "r-name", text: run.name }),
          h(
            "div",
            { class: "r-meta" },
            h("span", { text: run.id.slice(4, 12) }),
            h("span", { text: run.status.replaceAll("_", " ").toLowerCase() }),
            h("span", { text: fmtAgo(run.created_at) })
          )
        )
      )
    )
  );
}

function renderApprovalBadge() {
  const pending = state.approvals.filter((approval) => approval.status === "PENDING").length;
  const badge = $("#approval-count");
  badge.textContent = String(pending);
  badge.classList.toggle("hidden", pending === 0);
}

/* ------------------------------------------------------------ run detail */

async function selectRun(runId, options = {}) {
  if (!runId) return;
  state.selected = runId;
  setView("runs", { replace: options.replace !== false });
  try {
    const snapshot = options.snapshot || (await api(`/api/v1/runs/${runId}`));
    state.snapshot = snapshot;
    renderRunList();
    renderRunsView();
  } catch (error) {
    toast(error.message, true);
  }
  if (!options.silent) $(".rail").classList.remove("open");
}

function renderRunsView() {
  renderRunHeader();
  renderActions();
  renderMetricsStrip();
  renderDag();
  renderArtifacts();
  renderAudit();
  if (state.drawerStage && $("#drawer").classList.contains("open")) {
    renderDrawer();
  }
}

function renderRunHeader() {
  const container = $("#run-header");
  const snapshot = state.snapshot;
  if (!snapshot) {
    container.className = "run-header empty";
    container.replaceChildren(
      h("div", {}, h("div", { class: "eyebrow", text: "Run detail" }), h("h1", { text: "Select or launch a run" }))
    );
    return;
  }
  const run = snapshot.run;
  container.className = "run-header";
  const stageCount = snapshot.stages.length;
  const done = snapshot.stages.filter((stage) => stage.status === "SUCCEEDED").length;
  container.replaceChildren(
    h(
      "div",
      {},
      h("div", { class: "eyebrow", text: `Run / ${run.scenario}` }),
      h(
        "div",
        { class: "run-title-row" },
        h("h1", { text: run.name }),
        statusPill(run.status),
        h("span", { class: "chip", text: run.id })
      ),
      h(
        "div",
        { class: "run-meta" },
        h("span", {}, "workflow ", h("b", { text: run.workflow_id })),
        h("span", {}, "mode ", h("b", { text: run.mode })),
        h("span", {}, "env ", h("b", { text: run.context?.environment || "—" })),
        h("span", {}, "started ", h("b", { text: fmtTime(run.started_at) })),
        h("span", {}, "elapsed ", h("b", { text: fmtDuration(run.started_at, run.ended_at) })),
        h("span", {}, "replans ", h("b", { text: String(run.replan_count) })),
        h("span", {}, "stages ", h("b", { text: `${done}/${stageCount}` }))
      ),
      run.error ? h("div", { class: "banner bad", text: run.error, style: "margin-top:14px" }) : null,
      run.safe_stop_reason
        ? h("div", { class: "banner warn", text: `Safe stopped: ${run.safe_stop_reason}`, style: "margin-top:14px" })
        : null
    )
  );
}

function pendingApproval() {
  return (state.snapshot?.approvals || []).find((approval) => approval.status === "PENDING") || null;
}

function renderActions() {
  const container = $("#run-actions");
  const snapshot = state.snapshot;
  if (!snapshot) {
    container.classList.add("hidden");
    container.replaceChildren();
    return;
  }
  container.classList.remove("hidden");
  const actions = snapshot.actions || [];
  const buttons = [];
  if (actions.includes("approve")) {
    buttons.push(
      h("button", {
        class: "btn primary",
        type: "button",
        text: "Approve gate",
        onclick: () => {
          const approval = pendingApproval();
          if (approval) openApprovalModal(approval, "approve");
        },
      })
    );
  }
  if (actions.includes("reject")) {
    buttons.push(
      h("button", {
        class: "btn danger",
        type: "button",
        text: "Reject",
        onclick: () => {
          const approval = pendingApproval();
          if (approval) openApprovalModal(approval, "reject");
        },
      })
    );
  }
  if (actions.includes("safe_stop") || snapshot.run.status === "RUNNING") {
    buttons.push(
      h("button", {
        class: "btn",
        type: "button",
        text: "Safe stop",
        onclick: openSafeStopModal,
      })
    );
  }
  if (actions.includes("resume")) {
    buttons.push(
      h("button", {
        class: "btn",
        type: "button",
        text: "Resume run",
        onclick: resumeRun,
      })
    );
  }
  if (actions.includes("replan")) {
    buttons.push(
      h("button", {
        class: "btn ghost",
        type: "button",
        text: "Re-plan downstream",
        onclick: openReplanModal,
      })
    );
  }
  container.replaceChildren(...buttons);
}

function renderMetricsStrip() {
  const container = $("#metrics-strip");
  const snapshot = state.snapshot;
  if (!snapshot) {
    container.replaceChildren();
    return;
  }
  const stages = snapshot.stages;
  const succeeded = stages.filter((stage) => stage.status === "SUCCEEDED").length;
  const retries = stages.reduce((total, stage) => total + (stage.retry_count || 0), 0);
  const pending = snapshot.approvals.filter((approval) => approval.status === "PENDING").length;
  const verification = snapshot.audit_verification;
  const cells = [
    ["Pipeline", `${succeeded}/${stages.length}`, "stages complete"],
    ["Retries", String(retries), `${snapshot.run.replan_count} re-plan(s)`],
    ["Human gates", pending ? `${pending} waiting` : "clear", `${snapshot.approvals.length} approval(s)`],
    ["Audit chain", verification.valid ? "verified" : "invalid", `${snapshot.audit.length} run events`],
    ["Elapsed", fmtDuration(snapshot.run.started_at, snapshot.run.ended_at), snapshot.run.mode],
  ];
  container.replaceChildren(
    ...cells.map(([key, value, sub]) =>
      h(
        "div",
        { class: "metric-cell" },
        h("div", { class: "k", text: key }),
        h("div", { class: "v", text: value }),
        h("div", { class: "sub", text: sub })
      )
    )
  );
}

/* -------------------------------------------------------------------- dag */

function renderDag() {
  const levels = $("#dag-levels");
  const empty = $("#dag-empty");
  const snapshot = state.snapshot;
  if (!snapshot) {
    levels.replaceChildren();
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  const stageMap = new Map(snapshot.stages.map((stage) => [stage.stage_id, stage]));
  levels.replaceChildren(
    ...snapshot.levels.map((ids, index) =>
      h(
        "div",
        { class: "dag-level" },
        h("div", { class: "level-label", text: `LEVEL ${String(index + 1).padStart(2, "0")}` }),
        ...ids
          .map((id) => stageMap.get(id))
          .filter(Boolean)
          .map((stage) => stageCard(stage))
      )
    )
  );
  requestAnimationFrame(drawEdges);
  setTimeout(drawEdges, 120);
}

function stageCard(stage) {
  return h(
    "button",
    {
      class: `stage-card ${stage.status}`,
      type: "button",
      dataset: { stageId: stage.stage_id },
      title: stage.description || stage.name,
      onclick: () => openDrawer(stage.stage_id),
    },
    h(
      "div",
      { class: "sc-top" },
      h("span", { class: "sc-status", text: stage.status.replaceAll("_", " ") }),
      stage.stale ? h("span", { class: "stale", text: "STALE" }) : null
    ),
    h("div", { class: "sc-name", text: stage.name }),
    h(
      "div",
      { class: "sc-foot" },
      h("span", { text: stage.agent }),
      h("span", { class: "sep", text: "·" }),
      h("span", { text: `#${stage.attempt}` }),
      h("span", { class: "sep", text: "·" }),
      h("span", { text: `${(stage.artifacts || []).length} art` })
    )
  );
}

function drawEdges() {
  const svg = $("#dag-edges");
  const dag = $("#dag");
  const snapshot = state.snapshot;
  if (!svg || !dag || !snapshot) return;
  const cards = new Map(
    $$(".stage-card", dag).map((card) => [card.dataset.stageId, card])
  );
  if (!cards.size) {
    svg.replaceChildren();
    return;
  }
  const origin = dag.getBoundingClientRect();
  svg.replaceChildren();
  svg.setAttribute("width", String(Math.max(dag.scrollWidth, dag.clientWidth)));
  svg.setAttribute("height", String(Math.max(dag.scrollHeight, dag.clientHeight)));
  for (const stage of snapshot.stages) {
    const target = cards.get(stage.stage_id);
    if (!target) continue;
    for (const dependency of stage.depends_on || []) {
      const source = cards.get(dependency);
      if (!source) continue;
      const a = source.getBoundingClientRect();
      const b = target.getBoundingClientRect();
      const x1 = a.right - origin.left;
      const y1 = a.top - origin.top + a.height / 2;
      const x2 = b.left - origin.left;
      const y2 = b.top - origin.top + b.height / 2;
      const bend = Math.max(24, (x2 - x1) * 0.5);
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", `M${x1},${y1} C${x1 + bend},${y1} ${x2 - bend},${y2} ${x2},${y2}`);
      if (stage.status === "RUNNING" || stage.status === "AWAITING_APPROVAL") {
        path.classList.add("active");
      } else if (stage.status === "SUCCEEDED") {
        path.classList.add("done");
      }
      svg.append(path);
    }
  }
}

/* --------------------------------------------------------------- artifacts */

function renderArtifacts() {
  const container = $("#artifact-panel");
  const snapshot = state.snapshot;
  const artifacts = snapshot ? [...snapshot.artifacts].sort((a, b) => a.created_at.localeCompare(b.created_at)) : [];
  $("#artifact-total").textContent = String(artifacts.length);
  if (!artifacts.length) {
    container.className = "artifact-panel empty-state small";
    container.replaceChildren(h("p", { text: "Artifacts appear as stages complete." }));
    return;
  }
  container.className = "artifact-panel";
  container.replaceChildren(
    ...artifacts.map((artifact) =>
      h(
        "button",
        {
          class: "artifact-row",
          type: "button",
          onclick: () => openArtifact(artifact.id),
        },
        h("span", { class: "artifact-kind", text: artifact.kind.slice(0, 4) }),
        h(
          "span",
          { style: "min-width:0" },
          h("div", { class: "artifact-name", text: artifact.name }),
          h("div", {
            class: "artifact-meta",
            text: `${artifact.stage_id} · ${artifact.content_hash.slice(0, 10)}`,
          })
        ),
        h("span", { class: "artifact-meta", text: `v${artifact.version}` })
      )
    )
  );
}

function renderAudit() {
  const container = $("#audit-panel");
  const snapshot = state.snapshot;
  if (!snapshot) {
    container.replaceChildren(h("p", { text: "Audit events appear here." }));
    return;
  }
  const verification = snapshot.audit_verification;
  const chip = $("#audit-state");
  chip.textContent = verification.valid ? "chain valid" : "chain invalid";
  chip.className = `chip ${verification.valid ? "ok" : "bad"}`;
  const events = [...snapshot.audit].reverse().slice(0, 80);
  container.className = "audit-panel";
  container.replaceChildren(
    ...events.map((event) =>
      h(
        "button",
        {
          class: `audit-row ${event.event_type.startsWith("policy") ? "policy" : ""} ${
            /failed|rejected|safe_stopped/.test(event.event_type) ? "fail" : ""
          }`,
          type: "button",
          style: "background:none;border:0;text-align:left;width:100%;cursor:pointer",
          onclick: () => openJsonModal(`Event #${event.seq} · ${event.event_type}`, event),
        },
        h("span", { class: "audit-seq", text: `#${event.seq}` }),
        h("span", { class: "audit-type", text: event.event_type }),
        h("span", { class: "audit-time", text: fmtTime(event.created_at) })
      )
    )
  );
}

/* ----------------------------------------------------------------- drawer */

function openDrawer(stageId) {
  state.drawerStage = stageId;
  state.drawerTab = "overview";
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#drawer-backdrop").classList.add("open");
  renderDrawer();
}

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#drawer-backdrop").classList.remove("open");
  state.drawerStage = null;
}

function currentStage() {
  return (state.snapshot?.stages || []).find((stage) => stage.stage_id === state.drawerStage) || null;
}

function renderDrawer() {
  const stage = currentStage();
  if (!stage) return;
  $("#drawer-eyebrow").textContent = `Stage / ${stage.stage_id}`;
  $("#drawer-title").textContent = stage.name;
  const meta = $("#drawer-meta");
  meta.replaceChildren(
    ...[
      statusPill(stage.status),
      h("span", { class: "chip", text: stage.agent }),
      h("span", { class: "chip", text: `attempt ${stage.attempt}` }),
      h("span", { class: "chip", text: stage.exit_gate }),
      stage.requires_approval ? h("span", { class: "chip strong", text: "approval required" }) : null,
    ].filter(Boolean)
  );
  const tabs = [
    ["overview", "Overview"],
    ["artifacts", `Artifacts ${stage.artifacts.length}`],
    ["decisions", "Decisions"],
    ["policy", "Policy"],
    ["audit", "Audit"],
  ];
  $("#drawer-tabs").replaceChildren(
    ...tabs.map(([key, label]) =>
      h("button", {
        class: `drawer-tab ${state.drawerTab === key ? "active" : ""}`,
        type: "button",
        text: label,
        onclick: () => {
          state.drawerTab = key;
          renderDrawer();
        },
      })
    )
  );
  const body = $("#drawer-body");
  body.replaceChildren();
  if (state.drawerTab === "overview") renderStageOverview(stage, body);
  if (state.drawerTab === "artifacts") renderStageArtifacts(stage, body);
  if (state.drawerTab === "decisions") renderStageDecisions(stage, body);
  if (state.drawerTab === "policy") renderStagePolicy(stage, body);
  if (state.drawerTab === "audit") renderStageAudit(stage, body);
}

function renderStageOverview(stage, body) {
  if (stage.error) body.append(h("div", { class: "banner bad", text: stage.error }));
  if (stage.status === "AWAITING_APPROVAL") {
    const approval = state.snapshot.approvals.find(
      (item) => item.stage_id === stage.stage_id && item.status === "PENDING"
    );
    if (approval) {
      body.append(
        h("div", { class: "banner warn", text: "This stage is paused for human approval." }),
        h(
          "div",
          { class: "approval-actions" },
          h("button", {
            class: "btn primary small",
            type: "button",
            text: "Approve",
            onclick: () => openApprovalModal(approval, "approve"),
          }),
          h("button", {
            class: "btn danger small",
            type: "button",
            text: "Reject",
            onclick: () => openApprovalModal(approval, "reject"),
          })
        )
      );
    }
  }
  const kv = h("dl", { class: "kv" });
  const add = (key, value) => {
    if (value === null || value === undefined || value === "") return;
    kv.append(h("dt", { text: key }), h("dd", { text: String(value) }));
  };
  add("Agent", stage.agent);
  add("Dependencies", (stage.depends_on || []).join(", ") || "none");
  add("Entry gate", stage.entry_gate || "always");
  add("Exit gate", stage.exit_gate);
  add("Failure policy", stage.failure_policy);
  add("Retry budget", stage.max_retries);
  add("Approval", stage.requires_approval ? "required" : "not required");
  add("Total attempts", stage.total_attempts ?? stage.attempt);
  add("Retries", stage.retry_count ?? 0);
  add("Stale", stage.stale ? "yes (re-planned)" : "no");
  add("Started", stage.started_at ? `${fmtTime(stage.started_at)} (${fmtDuration(stage.started_at, stage.ended_at)})` : "—");
  add("Output", stage.output_summary || "—");
  body.append(h("h3", { text: "Stage contract" }), kv);
  if (stage.description) {
    body.append(h("h3", { text: "Purpose" }), h("p", { class: "muted", text: stage.description }));
  }
}

function renderStageArtifacts(stage, body) {
  if (!stage.artifacts.length) {
    body.append(h("p", { class: "muted", text: "No artifacts produced yet." }));
    return;
  }
  body.append(
    h(
      "div",
      { class: "drawer-list" },
      ...stage.artifacts.map((artifact) =>
        h(
          "button",
          {
            class: "drawer-item",
            type: "button",
            onclick: () => openArtifact(artifact.id),
          },
          h("div", { class: "t", text: artifact.name }),
          h("div", {
            class: "m",
            text: `${artifact.kind} · v${artifact.version} · ${artifact.content_hash.slice(0, 12)}`,
          })
        )
      )
    )
  );
}

function renderStageDecisions(stage, body) {
  const decisions = state.snapshot.decisions.filter(
    (decision) => decision.stage_id === stage.stage_id && decision.kind !== "policy"
  );
  if (!decisions.length) {
    body.append(h("p", { class: "muted", text: "No agent decisions recorded for this stage." }));
    return;
  }
  body.append(
    h(
      "div",
      { class: "drawer-list" },
      ...decisions.map((decision) =>
        h(
          "div",
          { class: "decision" },
          h("div", { class: "topic", text: `${decision.kind} · ${decision.topic}` }),
          h("div", { class: "what", text: decision.decision }),
          h("div", { class: "why", text: decision.rationale }),
          h("div", { class: "m", text: `${decision.actor} · ${fmtTime(decision.created_at)}` })
        )
      )
    )
  );
}

function renderStagePolicy(stage, body) {
  const decisions = state.snapshot.decisions.filter(
    (decision) => decision.stage_id === stage.stage_id && decision.kind === "policy"
  );
  if (!decisions.length) {
    body.append(h("p", { class: "muted", text: "No policy findings for this stage." }));
    return;
  }
  body.append(
    h(
      "div",
      { class: "drawer-list" },
      ...decisions.map((decision) => {
        const denied = decision.decision === "deny";
        return h(
          "div",
          { class: "decision" },
          h("div", { class: "topic", text: `${decision.topic} · ${denied ? "DENIED" : "ALLOWED"}` }),
          h("div", { class: "what", text: decision.rationale }),
          h("div", { class: "m", text: `policy engine · ${fmtTime(decision.created_at)}` })
        );
      })
    )
  );
}

function renderStageAudit(stage, body) {
  const events = state.snapshot.audit
    .filter((event) => event.stage_id === stage.stage_id)
    .reverse();
  if (!events.length) {
    body.append(h("p", { class: "muted", text: "No audit events for this stage yet." }));
    return;
  }
  body.append(
    h(
      "div",
      { class: "drawer-list" },
      ...events.map((event) =>
        h(
          "button",
          {
            class: "drawer-item",
            type: "button",
            onclick: () => openJsonModal(`Event #${event.seq} · ${event.event_type}`, event),
          },
          h("div", { class: "t", text: event.event_type }),
          h("div", { class: "m", text: `#${event.seq} · ${event.actor} · ${fmtTime(event.created_at)}` })
        )
      )
    )
  );
}

/* -------------------------------------------------------------- approvals */

function setView(view, options = {}) {
  state.view = view;
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === view));
  $("#view-runs").classList.toggle("hidden", view !== "runs");
  $("#view-approvals").classList.toggle("hidden", view !== "approvals");
  $("#view-metrics").classList.toggle("hidden", view !== "metrics");
  if (view === "approvals") renderApprovals();
  if (view === "metrics") loadMetrics();
  if (options.replace !== false) {
    const hash =
      view === "runs"
        ? state.selected
          ? `#/runs/${state.selected}`
          : "#/runs"
        : `#/${view}`;
    history.replaceState(null, "", hash);
  }
}

function renderApprovals() {
  const container = $("#approvals-list");
  const pending = state.approvals.filter((approval) => approval.status === "PENDING");
  const decided = state.approvals
    .filter((approval) => approval.status !== "PENDING")
    .slice(0, 12);
  $("#inbox-count").textContent = `${pending.length} pending`;
  if (!state.approvals.length) {
    container.replaceChildren(
      h("div", { class: "empty-card", text: "No approval requests. High-impact stages will appear here." })
    );
    return;
  }
  const cards = [];
  for (const approval of pending) cards.push(approvalCard(approval));
  for (const approval of decided) cards.push(approvalCard(approval));
  container.replaceChildren(...cards);
}

function approvalCard(approval) {
  const pending = approval.status === "PENDING";
  const context = approval.context || {};
  const lines = [];
  if (context.stage) lines.push(`Stage: ${context.stage}`);
  if (context.description) lines.push(`Purpose: ${context.description}`);
  if (context.reason) lines.push(`Reason: ${context.reason}`);
  if (context.targets) lines.push(`Targets: ${context.targets.join(", ")}`);
  if (context.changed_stage) lines.push(`Changed upstream stage: ${context.changed_stage}`);
  if (context.source) lines.push(`Source: ${context.source}`);
  if (context.upstream?.length) lines.push(`Upstream artifacts: ${context.upstream.join(", ")}`);
  if (context.requirement) {
    const requirement = String(context.requirement).replace(/\s+/g, " ").trim();
    lines.push(`Requirement: ${requirement.slice(0, 520)}${requirement.length > 520 ? "…" : ""}`);
  }
  return h(
    "article",
    { class: `approval-card ${approval.status.toLowerCase()}` },
    h(
      "div",
      { class: "approval-top" },
      h(
        "div",
        {},
        h("div", {
          class: "approval-title",
          text:
            approval.kind === "replan"
              ? "Governed re-plan"
              : `Stage gate · ${approval.stage_id}`,
        }),
        h(
          "div",
          { class: "approval-sub" },
          h("span", { text: approval.run_name || approval.run_id }),
          h("span", { text: approval.scenario || "—" }),
          h("span", { text: `requested ${fmtAgo(approval.requested_at)}` }),
          h("span", { text: approval.status })
        )
      ),
      h("span", { class: "chip", text: approval.kind })
    ),
    lines.length
      ? h("div", { class: "approval-context", text: lines.join("\n") })
      : null,
    pending
      ? h(
          "div",
          { class: "approval-actions" },
          h("button", {
            class: "btn primary small",
            type: "button",
            text: "Approve",
            onclick: () => openApprovalModal(approval, "approve"),
          }),
          h("button", {
            class: "btn danger small",
            type: "button",
            text: "Reject",
            onclick: () => openApprovalModal(approval, "reject"),
          }),
          h("button", {
            class: "btn ghost small",
            type: "button",
            text: "Open run",
            onclick: () => selectRun(approval.run_id),
          })
        )
      : h(
          "div",
          { class: "approval-sub", style: "margin-top:10px" },
          h("span", { text: `decided by ${approval.decided_by || "—"}` }),
          h("span", { text: approval.rationale || "" }),
          h("button", {
            class: "btn ghost small",
            type: "button",
            text: "Open run",
            onclick: () => selectRun(approval.run_id),
          })
        )
  );
}

function openApprovalModal(approval, mode) {
  const approve = mode === "approve";
  const body = h("div", {});
  body.append(
    h("p", {
      text: approve
        ? "Record a human approval. The decision, identity and rationale are written to the audit chain."
        : "Rejecting stops the run and preserves all artifacts for review.",
    }),
    h("label", { text: "Rationale (recorded in audit)" }),
    h("textarea", { id: "approval-rationale", text: approve ? "Evidence reviewed; approved." : "Evidence insufficient; rejected." }),
    h(
      "div",
      { class: "modal-actions" },
      h("button", { class: "btn ghost", type: "button", text: "Cancel", onclick: closeModal }),
      h("button", {
        class: `btn ${approve ? "primary" : "danger"}`,
        type: "button",
        text: approve ? "Approve and resume" : "Reject run",
        onclick: async () => {
          const rationale = $("#approval-rationale").value.trim();
          if (!rationale) {
            toast("A rationale is required", true);
            return;
          }
          try {
            const result = await api(
              `/api/v1/runs/${approval.run_id}/${approve ? "approve" : "reject"}`,
              {
                method: "POST",
                body: JSON.stringify({ stage_id: approval.stage_id, rationale }),
              }
            );
            closeModal();
            toast(approve ? "Approved and resumed" : "Rejected; run stopped");
            state.selected = approval.run_id;
            state.snapshot = result;
            await selectRun(approval.run_id, { snapshot: result, silent: true });
            refreshAll(true);
          } catch (error) {
            toast(error.message, true);
          }
        },
      })
    )
  );
  openModal({
    eyebrow: approve ? "Human approval" : "Human rejection",
    title: approval.kind === "replan" ? "Governed re-plan" : `Gate: ${approval.stage_id}`,
    body,
  });
}

/* ----------------------------------------------------------------- metrics */

async function loadMetrics() {
  try {
    state.metrics = await api("/api/v1/metrics/summary");
    renderMetrics();
  } catch (error) {
    toast(error.message, true);
  }
}

function renderMetrics() {
  const metrics = state.metrics;
  if (!metrics) return;
  const reliability = metrics.reliability;
  const runs = metrics.runs;
  const stages = metrics.stages;
  const cards = [
    ["Success rate", fmtPct(runs.success_rate), `${runs.succeeded}/${runs.terminal} terminal runs`, runs.success_rate],
    ["Retry frequency", fmtPct(stages.retry_frequency), `${stages.retries} retries / ${stages.total_attempts} attempts`, stages.retry_frequency],
    ["Rollback frequency", fmtPct(reliability.rollback_frequency), `${reliability.rollback_runs} rolled-back runs`, reliability.rollback_frequency],
    ["MTTR", fmtSeconds(reliability.mttr_seconds), "mean time to recovery", null],
    ["E2E latency p95", fmtSeconds(reliability.e2e_latency_p95_seconds), "run end-to-end", null],
    ["Re-plans", String(reliability.replans), "dynamic scope changes", null],
    ["Policy denials", String(reliability.policy_violations), "persisted decisions", null],
    ["Audit events", String(reliability.audit_events), `${reliability.pending_approvals} pending approvals`, null],
  ];
  $("#metrics-cards").replaceChildren(
    ...cards.map(([key, value, detail, ratio]) =>
      h(
        "div",
        { class: "metric-card" },
        h("div", { class: "k", text: key }),
        h("div", { class: "v", text: value }),
        h("div", { class: "d", text: detail }),
        ratio === null
          ? null
          : h("div", { class: "bar" }, h("span", { style: `width:${Math.min(100, Number(ratio) * 100)}%` }))
      )
    )
  );
  const counts = runs.by_status || {};
  const total = Object.values(counts).reduce((sum, value) => sum + value, 0) || 1;
  $("#status-bars").replaceChildren(
    ...Object.entries(counts)
      .sort((a, b) => b[1] - a[1])
      .map(([status, count]) =>
        h(
          "div",
          { class: "status-bar-row" },
          h("span", { text: status.replaceAll("_", " ").toLowerCase() }),
          h("div", { class: "status-bar-track" }, h("span", { style: `width:${(count / total) * 100}%` })),
          h("span", { text: String(count), style: "text-align:right" })
        )
      )
  );
  $("#recent-runs").replaceChildren(
    ...state.runs.slice(0, 10).map((run) =>
      h(
        "div",
        { class: "recent-row", onclick: () => selectRun(run.id) },
        h("span", { class: "id", text: run.id.slice(4, 12) }),
        h("span", { class: "nm", text: run.name }),
        h("span", {}, statusPill(run.status)),
        h("span", { class: "tm", text: fmtAgo(run.created_at) })
      )
    )
  );
}

/* ------------------------------------------------------------------ modals */

function openModal({ eyebrow, title, body }) {
  $("#modal-eyebrow").textContent = eyebrow || "";
  $("#modal-title").textContent = title || "";
  $("#modal-body").replaceChildren(body);
  $("#modal").classList.remove("hidden");
}

function closeModal() {
  $("#modal").classList.add("hidden");
  $("#modal-body").replaceChildren();
}

function openArtifact(artifactId) {
  const artifact = (state.snapshot?.artifacts || []).find((item) => item.id === artifactId);
  openModal({
    eyebrow: "Artifact",
    title: artifact ? artifact.name : "Artifact",
    body: h("p", { class: "muted", text: "Loading artifact…" }),
  });
  api(`/api/v1/artifacts/${artifactId}`)
    .then((full) => {
      openModal({
        eyebrow: `${full.kind} · v${full.version} · ${full.content_hash.slice(0, 12)}`,
        title: full.name,
        body: h("pre", { class: "code-block", text: full.content }),
      });
    })
    .catch((error) => toast(error.message, true));
}

function openJsonModal(title, value) {
  openModal({
    eyebrow: "Audit event",
    title,
    body: h("pre", { class: "code-block", text: JSON.stringify(value, null, 2) }),
  });
}

function openSafeStopModal() {
  const run = state.snapshot?.run;
  if (!run) return;
  openModal({
    eyebrow: "Operator control",
    title: "Safe stop this run",
    body: h(
      "div",
      {},
      h("p", {
        text: "The scheduler stops at the next boundary, cancels pending stages, preserves all artifacts, and records the reason in the audit chain. The run can be resumed.",
      }),
      h("label", { text: "Reason" }),
      h("textarea", { id: "stop-reason", text: "Operator requested safe stop" }),
      h(
        "div",
        { class: "modal-actions" },
        h("button", { class: "btn ghost", type: "button", text: "Cancel", onclick: closeModal }),
        h("button", {
          class: "btn danger",
          type: "button",
          text: "Safe stop",
          onclick: async () => {
            try {
              const result = await api(`/api/v1/runs/${run.id}/safe-stop`, {
                method: "POST",
                body: JSON.stringify({ reason: $("#stop-reason").value.trim() || "operator requested" }),
              });
              closeModal();
              state.snapshot = result;
              await selectRun(run.id, { snapshot: result, silent: true });
              toast("Run safe-stopped");
            } catch (error) {
              toast(error.message, true);
            }
          },
        })
      )
    ),
  });
}

async function resumeRun() {
  const run = state.snapshot?.run;
  if (!run) return;
  try {
    const result = await api(`/api/v1/runs/${run.id}/resume`, { method: "POST" });
    state.snapshot = result;
    await selectRun(run.id, { snapshot: result, silent: true });
    toast("Run resumed");
  } catch (error) {
    toast(error.message, true);
  }
}

function openReplanModal() {
  const run = state.snapshot?.run;
  const snapshot = state.snapshot;
  if (!run || !snapshot) return;
  const succeeded = snapshot.stages.filter((stage) => stage.status === "SUCCEEDED");
  const select = h(
    "select",
    { id: "replan-stage" },
    ...succeeded.map((stage) => h("option", { value: stage.stage_id, text: stage.name }))
  );
  openModal({
    eyebrow: "Dynamic re-plan",
    title: "Re-plan downstream stages",
    body: h(
      "div",
      {},
      h("p", {
        text: "Select the stage whose output changed. The reverse-dependency impact closure is reset, prior artifacts remain as lineage, and each re-executed stage produces a new version.",
      }),
      h("label", { text: "Changed upstream stage" }),
      select,
      h("label", { text: "Reason (recorded in audit)" }),
      h("textarea", { id: "replan-reason", text: "Upstream decision changed; downstream outputs must be regenerated." }),
      h(
        "label",
        { class: "check-row" },
        h("input", { id: "replan-approval", type: "checkbox", checked: "checked" }),
        "Require human approval before applying"
      ),
      h(
        "div",
        { class: "modal-actions" },
        h("button", { class: "btn ghost", type: "button", text: "Cancel", onclick: closeModal }),
        h("button", {
          class: "btn primary",
          type: "button",
          text: "Request re-plan",
          onclick: async () => {
            const reason = $("#replan-reason").value.trim();
            if (!reason) {
              toast("A reason is required", true);
              return;
            }
            try {
              const result = await api(`/api/v1/runs/${run.id}/replan`, {
                method: "POST",
                body: JSON.stringify({
                  changed_stage: $("#replan-stage").value,
                  reason,
                  require_approval: $("#replan-approval").checked,
                }),
              });
              closeModal();
              state.snapshot = result;
              await selectRun(run.id, { snapshot: result, silent: true });
              toast(
                result.run.status === "AWAITING_APPROVAL"
                  ? "Re-plan requested; approval pending"
                  : "Re-plan applied"
              );
            } catch (error) {
              toast(error.message, true);
            }
          },
        })
      )
    ),
  });
}

/* ------------------------------------------------------------------ launch */

async function launchScenario(key) {
  if (state.launching) return;
  state.launching = true;
  const auto = $("#auto-approve").checked;
  toast(`Launching ${key}${auto ? " with demo auto-approval" : ""}…`);
  try {
    const snapshot = await api("/api/v1/runs", {
      method: "POST",
      body: JSON.stringify({ scenario: key, auto_approve: auto }),
    });
    state.selected = snapshot.run.id;
    state.snapshot = snapshot;
    await selectRun(snapshot.run.id, { snapshot, silent: true });
    refreshAll(true);
    toast(
      snapshot.run.status === "AWAITING_APPROVAL"
        ? "Run paused at a human approval gate"
        : `Run ${snapshot.run.status.toLowerCase()}`
    );
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.launching = false;
  }
}
