const runId = window.location.pathname.split("/").filter(Boolean)[1];
const terminalStatuses = new Set(["COMPLETED", "HUMAN_REVIEW_REQUIRED", "FAILED", "CANCELLED"]);

const analysisLabels = {
  technical: "技术面分析",
  fundamental: "基本面研究",
};

const statusLabels = {
  CREATED: "等待执行",
  RUNNING: "研究中",
  COMPLETED: "已完成",
  HUMAN_REVIEW_REQUIRED: "待人工复核",
  FAILED: "失败",
  CANCELLED: "已取消",
};

const nodeDescriptions = {
  resolve_security: "正在解析证券名称、代码和市场标识，为后续研究建立统一标的。",
  technical_research: "正在计算趋势、量价、动量、波动率和本次实际识别的技术形态。",
  kronos: "Kronos 正在基于标准化行情评估短期方向与概率分布。",
  technical_assembly: "正在对照指标与模型结果，整理一致信号、分歧和观察条件。",
  write_report: "正在组装技术面正文与本次识别形态对应的原生图表。",
  lead_planning: "Lead 正在识别公司业务、行业类型和关键研究问题。",
  business_research: "Business Agent 正在从公司经营端研究业务、项目、竞争优势与成长兑现。",
  industry_research: "Industry Agent 正在研究供需、成本、竞争、政策与重要宏观定价变量。",
  lead_review: "Lead 正在复核首轮研究，修正主线并明确需要深化的专题。",
  deep_research: "Deep Research 正在围绕补充任务开展专题深化与核验。",
  assemble_retrieval_package: "正在整理全流程 Evidence 索引和研究简报，避免向后续节点注入来源全文。",
  financial_research: "正在解释财务数据、经营变化和现金流质量。",
  valuation_research: "正在依据权威财务与估值结果形成估值分析。",
  lead_final_review: "Lead 正在检查研究覆盖度并收束最终报告边界。",
  lead_synthesis: "Lead 正在生成报告主线、章节论点和资料使用说明。",
  writer_planning: "正在规划章节分工、写作重点以及具有比较价值的图表。",
  build_fundamental_visuals: "正在从权威数据中生成通过口径校验的图表规格。",
  fundamental_writer: "多个 Section Writer 正在依据统一计划撰写各自章节。",
  final_synthesis: "正在组装章节、统一衔接和补充主线中尚未覆盖的内容。",
  write_fundamental_report: "正在生成可离线阅读和导出的原生 HTML 研报。",
};

const technicalOrder = [
  "resolve_security", "technical_research", "kronos", "technical_assembly", "write_report",
];
const fundamentalOrder = [
  "resolve_security", "lead_planning", "business_research", "industry_research", "lead_review",
  "deep_research", "assemble_retrieval_package", "financial_research", "valuation_research",
  "lead_final_review", "lead_synthesis", "writer_planning", "build_fundamental_visuals",
  "fundamental_writer", "final_synthesis", "write_fundamental_report",
];

const elements = {
  runId: document.querySelector("#run-id"),
  runSymbolTitle: document.querySelector("#run-symbol-title"),
  symbol: document.querySelector("#symbol"),
  analysisType: document.querySelector("#analysis-type"),
  currentStage: document.querySelector("#current-stage"),
  asOf: document.querySelector("#as-of"),
  currentNode: document.querySelector("#current-node"),
  currentNodeLabel: document.querySelector("#current-node-label"),
  currentWorkDescription: document.querySelector("#current-work-description"),
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
let displayedProgress = 0;
elements.runId.textContent = `任务 ID：${runId}`;
elements.cancelButton.addEventListener("click", cancelRun);
setupSpotlight();
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
    const [run, executions] = await Promise.all([readJson(response), readJson(executionsResponse)]);
    if (!response.ok) throw new Error(errorMessage(run, "读取任务失败"));
    if (!executionsResponse.ok) throw new Error(errorMessage(executions, "读取 Agent 执行摘要失败"));
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
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
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
  const symbol = run.normalized_symbol || run.input_symbol;
  const analysisType = analysisLabels[run.analysis_type] || run.analysis_type;
  const progress = Math.max(0, Math.min(100, Number(run.progress) || 0));

  elements.runSymbolTitle.textContent = symbol;
  elements.symbol.textContent = symbol;
  elements.analysisType.textContent = analysisType;
  elements.currentStage.textContent = run.current_stage;
  elements.asOf.textContent = run.as_of;
  elements.currentNode.textContent = run.current_node || "等待进入流程";
  elements.runtimeMode.textContent = run.runtime_mode || "未配置";
  elements.checkpointStatus.textContent = run.checkpoint_enabled ? "已启用" : "未启用";
  elements.statusBadge.textContent = statusLabels[run.status] || run.status;
  elements.statusBadge.className = `status-badge ${statusClass(run.status)}`.trim();
  animateProgressValue(progress);
  document.title = `${symbol} · 研究过程 · 弦月研究`;

  renderCurrentWork(run);
  renderEvents(run.events || []);
  renderTechnicalStages(run);
  renderFundamentalStages(run);
  renderRunMessage(run);

  const terminal = terminalStatuses.has(run.status);
  elements.cancelButton.disabled = terminal || run.cancel_requested;
  elements.cancelButton.textContent = run.cancel_requested ? "已请求取消" : "取消任务";
  elements.reportButton.hidden = !run.report_ready;
  elements.reportButton.href = `/runs/${encodeURIComponent(runId)}/report`;
}

function renderCurrentWork(run) {
  if (run.status === "COMPLETED") {
    elements.currentNodeLabel.textContent = "研究完成";
    elements.currentWorkDescription.textContent = "全部研究节点已经完成，可以打开正式报告查看结果。";
    return;
  }
  if (run.status === "HUMAN_REVIEW_REQUIRED") {
    elements.currentNodeLabel.textContent = "等待复核";
    elements.currentWorkDescription.textContent = "自动流程已保留现有研究产物，等待人工补充或确认后继续。";
    return;
  }
  if (["FAILED", "CANCELLED"].includes(run.status)) {
    elements.currentNodeLabel.textContent = statusLabels[run.status];
    elements.currentWorkDescription.textContent = run.error_message || "当前任务已经停止。";
    return;
  }
  const node = run.current_node || "";
  const activeItem = document.querySelector(`.workflow-node[data-node="${cssEscape(node)}"] strong`);
  elements.currentNodeLabel.textContent = activeItem?.textContent || "等待调度";
  elements.currentWorkDescription.textContent = nodeDescriptions[node] || "Worker 正在领取任务，进入流程后将在这里显示当前节点。";
}

function renderRunMessage(run) {
  if (run.status === "HUMAN_REVIEW_REQUIRED") {
    const missing = Array.isArray(run.missing_information) && run.missing_information.length
      ? `：${run.missing_information.join("；")}`
      : run.error_message ? `：${run.error_message}` : "";
    showNotice(elements.runError, `需要人工复核${missing}`);
  } else if (run.status === "CANCELLED") {
    showNotice(elements.runError, "任务已取消，不会再启动新的研究节点。");
  } else if (run.report_status === "stale") {
    showNotice(elements.runError, "报告已陈旧，Worker 将从最早受影响节点重建。");
  } else if (run.error_message) {
    showNotice(elements.runError, `${errorCategory(run.error_message)}：${run.error_message}`);
  } else {
    hideNotice(elements.runError);
  }
}

function errorCategory(message) {
  if (/CONFIG|PI_MODEL|API_KEY|Configuration/i.test(message)) return "配置缺失";
  if (/MARKET|FUNDAMENTAL_DATA|AKSHARE|DATA_/i.test(message)) return "外部数据源失败";
  if (/SEARCH|TAVILY|SOURCE/i.test(message)) return "检索失败";
  if (/MODEL|KRONOS|AGENT|BRIDGE|PI_/i.test(message)) return "模型失败";
  return "任务失败";
}

function renderFundamentalStages(run) {
  elements.fundamentalStages.hidden = run.analysis_type !== "fundamental";
  if (run.analysis_type === "fundamental") renderStageList(elements.fundamentalStages, fundamentalOrder, run);
}

function renderTechnicalStages(run) {
  elements.technicalStages.hidden = run.analysis_type !== "technical";
  if (run.analysis_type === "technical") renderStageList(elements.technicalStages, technicalOrder, run);
}

function renderStageList(list, order, run) {
  const currentIndex = order.indexOf(run.current_node);
  list.querySelectorAll(".workflow-node[data-node]").forEach((item) => {
    const index = order.indexOf(item.dataset.node);
    let state = "PENDING";
    if (["FAILED", "HUMAN_REVIEW_REQUIRED"].includes(run.status) && index === currentIndex) state = run.status;
    else if (run.status === "CANCELLED" && index === currentIndex) state = "CANCELLED";
    else if (run.status === "COMPLETED" || (currentIndex >= 0 && index < currentIndex)) state = "COMPLETED";
    else if (index === currentIndex) state = "RUNNING";
    item.dataset.state = state;
    item.classList.toggle("node-flow", state === "RUNNING");
    item.setAttribute("aria-label", `${item.querySelector("strong")?.textContent || item.dataset.node}：${state}`);
  });

  list.querySelectorAll(".workflow-branch").forEach((branch) => {
    const childStates = [...branch.querySelectorAll(".workflow-node")].map((item) => item.dataset.state);
    branch.dataset.state = childStates.includes("RUNNING") ? "RUNNING"
      : childStates.every((state) => state === "COMPLETED") ? "COMPLETED"
        : childStates.some((state) => ["FAILED", "HUMAN_REVIEW_REQUIRED", "CANCELLED"].includes(state)) ? "FAILED"
          : "PENDING";
  });
}

function renderExecutions(executions) {
  elements.executionList.replaceChildren();
  if (!Array.isArray(executions) || executions.length === 0) {
    const row = document.createElement("tr");
    const item = document.createElement("td");
    item.colSpan = 5;
    item.className = "empty-state";
    item.textContent = "尚无 Agent 执行记录";
    row.append(item);
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
      const item = document.createElement("td");
      item.textContent = value;
      if (index === 1) item.className = "monospace execution-profile";
      row.append(item);
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
  if (status === "HUMAN_REVIEW_REQUIRED") return "is-review";
  return "";
}

function formatDateTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "medium" }).format(new Date(value));
}

function cssEscape(value) {
  if (window.CSS?.escape) return window.CSS.escape(value);
  return String(value).replace(/[^a-zA-Z0-9_-]/g, "");
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

function animateProgressValue(target) {
  const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)");
  if (reduceMotion?.matches || displayedProgress === target) {
    displayedProgress = target;
    applyProgressValue(target);
    return;
  }
  const initial = displayedProgress;
  const duration = Math.min(520, Math.max(180, Math.abs(target - initial) * 8));
  const startedAt = performance.now();
  const tick = (now) => {
    const elapsed = Math.min(1, (now - startedAt) / duration);
    const eased = 1 - (1 - elapsed) * (1 - elapsed);
    const value = initial + (target - initial) * eased;
    applyProgressValue(value);
    if (elapsed < 1) window.requestAnimationFrame(tick);
    else displayedProgress = target;
  };
  window.requestAnimationFrame(tick);
}

function applyProgressValue(value) {
  const rounded = Math.round(value);
  elements.progressValue.textContent = `${rounded}%`;
  elements.progressBar.style.width = `${value}%`;
  elements.progressTrack.setAttribute("aria-valuenow", String(rounded));
}

function setupSpotlight() {
  const spotlight = document.querySelector("#cursor-spotlight");
  const pointerQuery = window.matchMedia?.("(pointer: fine)");
  const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)");
  if (!spotlight || !pointerQuery || !pointerQuery.matches || reduceMotion?.matches) return;

  let frame = 0;
  let position = null;
  const draw = () => {
    frame = 0;
    if (!position) return;
    spotlight.style.setProperty("--spotlight-x", `${position.x}px`);
    spotlight.style.setProperty("--spotlight-y", `${position.y}px`);
    spotlight.dataset.visible = "true";
  };
  document.addEventListener("pointermove", (event) => {
    position = { x: event.clientX, y: event.clientY };
    if (!frame) frame = window.requestAnimationFrame(draw);
  }, { passive: true });
  document.addEventListener("pointerleave", () => { spotlight.dataset.visible = "false"; });
}
