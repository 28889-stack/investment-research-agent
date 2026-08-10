const runId = window.location.pathname.split("/").filter(Boolean)[1];
const terminalStatuses = new Set(["COMPLETED", "HUMAN_REVIEW_REQUIRED", "FAILED", "CANCELLED"]);
const analysisLabels = {
  technical: "技术面分析",
  fundamental: "基本面分析",
};

const elements = {
  runId: document.querySelector("#run-id"),
  symbol: document.querySelector("#symbol"),
  analysisType: document.querySelector("#analysis-type"),
  currentStage: document.querySelector("#current-stage"),
  asOf: document.querySelector("#as-of"),
  currentNode: document.querySelector("#current-node"),
  runtimeMode: document.querySelector("#runtime-mode"),
  checkpointStatus: document.querySelector("#checkpoint-status"),
  statusBadge: document.querySelector("#status-badge"),
  progressValue: document.querySelector("#progress-value"),
  progressBar: document.querySelector("#progress-bar"),
  progressTrack: document.querySelector(".progress-track"),
  eventList: document.querySelector("#event-list"),
  cancelButton: document.querySelector("#cancel-button"),
  reportButton: document.querySelector("#report-button"),
  pageError: document.querySelector("#page-error"),
  runError: document.querySelector("#run-error"),
  executionList: document.querySelector("#execution-list"),
  technicalStages: document.querySelector("#technical-stages"),
  fundamentalStages: document.querySelector("#fundamental-stages"),
};

let pollingTimer = null;
let requestInFlight = false;
elements.runId.textContent = `任务 ID：${runId}`;
elements.cancelButton.addEventListener("click", cancelRun);
loadRun();
pollingTimer = window.setInterval(loadRun, 2000);

async function loadRun() {
  if (requestInFlight) return;
  requestInFlight = true;
  try {
    const [response, executionsResponse] = await Promise.all([
      fetch(`/api/runs/${encodeURIComponent(runId)}`),
      fetch(`/api/runs/${encodeURIComponent(runId)}/executions`),
    ]);
    const [run, executions] = await Promise.all([
      readJson(response),
      readJson(executionsResponse),
    ]);
    if (!response.ok) throw new Error(errorMessage(run, "读取任务失败"));
    if (!executionsResponse.ok) {
      throw new Error(errorMessage(executions, "读取 Agent 执行摘要失败"));
    }
    hideNotice(elements.pageError);
    renderRun(run);
    renderExecutions(executions);
    if (terminalStatuses.has(run.status)) stopPolling();
  } catch (error) {
    showNotice(elements.pageError, error.message);
    stopPolling();
  } finally {
    requestInFlight = false;
  }
}

async function cancelRun() {
  elements.cancelButton.disabled = true;
  elements.cancelButton.textContent = "正在取消…";
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/cancel`, {
      method: "POST",
    });
    const body = await readJson(response);
    if (!response.ok) throw new Error(errorMessage(body, "取消任务失败"));
    await loadRun();
  } catch (error) {
    showNotice(elements.pageError, error.message);
    elements.cancelButton.disabled = false;
    elements.cancelButton.textContent = "取消任务";
  }
}

function renderRun(run) {
  elements.symbol.textContent = run.normalized_symbol || run.input_symbol;
  elements.analysisType.textContent = analysisLabels[run.analysis_type] || run.analysis_type;
  elements.currentStage.textContent = run.current_stage;
  elements.asOf.textContent = run.as_of;
  elements.currentNode.textContent = run.current_node || "等待进入流程";
  elements.runtimeMode.textContent = run.runtime_mode || "未配置";
  elements.checkpointStatus.textContent = run.checkpoint_enabled ? "已启用" : "未启用";
  elements.statusBadge.textContent = run.status;
  elements.statusBadge.className = `status-badge ${statusClass(run.status)}`.trim();
  elements.progressValue.textContent = `${run.progress}%`;
  elements.progressBar.style.width = `${run.progress}%`;
  elements.progressTrack.setAttribute("aria-valuenow", String(run.progress));
  renderEvents(run.events);
  renderTechnicalStages(run);
  renderFundamentalStages(run);

  if (run.status === "HUMAN_REVIEW_REQUIRED") {
    const missing = Array.isArray(run.missing_information) && run.missing_information.length
      ? `：${run.missing_information.join("；")}`
      : run.error_message
        ? `：${run.error_message}`
        : "";
    showNotice(elements.runError, `需要人工复核${missing}`);
  } else if (run.status === "CANCELLED") {
    showNotice(elements.runError, "任务已取消，不会再启动新的研究节点。");
  } else if (run.report_status === "stale") {
    showNotice(elements.runError, "报告已陈旧，Worker 将从最早受影响节点重建。");
  } else if (run.error_message) {
    const message = String(run.error_message);
    let category = "任务失败";
    if (/CONFIG|PI_MODEL|API_KEY|Configuration/i.test(message)) category = "配置缺失";
    else if (/MARKET|FUNDAMENTAL_DATA|AKSHARE|DATA_/i.test(message)) category = "外部数据源失败";
    else if (/SEARCH|TAVILY|SOURCE/i.test(message)) category = "检索失败";
    else if (/MODEL|KRONOS|AGENT|BRIDGE|PI_/i.test(message)) category = "模型失败";
    showNotice(elements.runError, `${category}：${message}`);
  } else {
    hideNotice(elements.runError);
  }

  const terminal = terminalStatuses.has(run.status);
  elements.cancelButton.disabled = terminal || run.cancel_requested;
  elements.cancelButton.textContent = run.cancel_requested ? "已请求取消" : "取消任务";
  elements.reportButton.hidden = !run.report_ready;
  elements.reportButton.href = `/runs/${encodeURIComponent(runId)}/report`;
}

function renderFundamentalStages(run) {
  elements.fundamentalStages.hidden = run.analysis_type !== "fundamental";
  if (run.analysis_type !== "fundamental") return;
  const order = ["resolve_security", "lead_planning", "business_research", "industry_research", "lead_review", "deep_research", "assemble_retrieval_package", "financial_research", "valuation_research", "lead_final_review", "lead_synthesis", "writer_planning", "fundamental_writer", "write_fundamental_report"];
  renderStageList(elements.fundamentalStages, order, run);
}

function renderStageList(list, order, run) {
  const currentIndex = order.indexOf(run.current_node);
  list.querySelectorAll("li").forEach((item, index) => {
    let state = "PENDING";
    if (["FAILED", "HUMAN_REVIEW_REQUIRED"].includes(run.status) && index === currentIndex) state = run.status;
    else if (run.status === "CANCELLED" && index === currentIndex) state = "CANCELLED";
    else if (run.status === "COMPLETED" || (currentIndex >= 0 && index < currentIndex)) state = "COMPLETED";
    else if (index === currentIndex) state = "RUNNING";
    item.dataset.state = state;
    item.setAttribute("aria-label", `${item.textContent}：${state}`);
  });
}

function renderTechnicalStages(run) {
  elements.technicalStages.hidden = run.analysis_type !== "technical";
  if (run.analysis_type !== "technical") return;
  const order = ["resolve_security", "technical_research", "kronos", "technical_assembly", "write_report"];
  const currentIndex = order.indexOf(run.current_node);
  elements.technicalStages.querySelectorAll("li").forEach((item, index) => {
    let state = "PENDING";
    if (run.status === "FAILED" && index === currentIndex) state = "FAILED";
    else if (run.status === "CANCELLED" && index === currentIndex) state = "CANCELLED";
    else if (run.status === "COMPLETED" || (currentIndex >= 0 && index < currentIndex)) state = "COMPLETED";
    else if (index === currentIndex) state = "RUNNING";
    item.dataset.state = state;
    item.setAttribute("aria-label", `${item.textContent}：${state}`);
  });
}

function renderExecutions(executions) {
  elements.executionList.replaceChildren();
  if (!Array.isArray(executions) || executions.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.className = "empty-state";
    cell.textContent = "尚无 Agent 执行记录";
    row.append(cell);
    elements.executionList.append(row);
    return;
  }

  executions.forEach((execution) => {
    const row = document.createElement("tr");
    const values = [
      execution.node_name,
      `${execution.profile_id} / ${execution.profile_version}`,
      execution.status,
      String(execution.tool_call_count),
      execution.validated_summary || execution.error_type || "—",
    ];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      if (index === 1) cell.className = "monospace execution-profile";
      row.append(cell);
    });
    elements.executionList.append(row);
  });
}

function renderEvents(events) {
  elements.eventList.replaceChildren();
  if (events.length === 0) {
    const empty = document.createElement("li");
    empty.className = "empty-state";
    empty.textContent = "暂无执行记录";
    elements.eventList.append(empty);
    return;
  }

  events.forEach((event) => {
    const item = document.createElement("li");
    const stage = document.createElement("span");
    stage.className = "event-stage";
    stage.textContent = event.stage;
    const time = document.createElement("time");
    time.className = "event-time";
    time.dateTime = event.created_at;
    time.textContent = formatDateTime(event.created_at);
    const message = document.createElement("span");
    message.className = "event-message";
    message.textContent = event.message;
    item.append(stage, time, message);
    elements.eventList.append(item);
  });
}

function statusClass(status) {
  if (status === "COMPLETED") return "is-success";
  if (["FAILED", "CANCELLED"].includes(status)) return "is-error";
  return "";
}

function formatDateTime(value) {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(new Date(value));
}

function stopPolling() {
  if (pollingTimer !== null) {
    window.clearInterval(pollingTimer);
    pollingTimer = null;
  }
}

function showNotice(element, message) {
  element.textContent = message;
  element.hidden = false;
}

function hideNotice(element) {
  element.textContent = "";
  element.hidden = true;
}

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

function errorMessage(body, fallback) {
  return typeof body.detail === "string" ? body.detail : fallback;
}
