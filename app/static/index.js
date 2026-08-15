const form = document.querySelector("#research-form");
const submitButton = document.querySelector("#submit-button");
const refreshButton = document.querySelector("#refresh-button");
const formError = document.querySelector("#form-error");
const historyError = document.querySelector("#history-error");
const runList = document.querySelector("#run-list");
const symbolInput = document.querySelector("#symbol");
const modeInputs = [...document.querySelectorAll('input[name="analysis_type"]')];

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

const workflowPreviews = {
  technical: {
    title: "技术面研究路径",
    description: "解析证券与行情数据，完成指标研究、Kronos 分析、信号组装和报告生成。",
    steps: ["证券解析", "指标研究", "Kronos", "信号组装", "报告"],
  },
  fundamental: {
    title: "基本面研究路径",
    description: "由 Lead 规划问题，公司与行业双线研究，再完成深度检索、财务估值和正式写作。",
    steps: ["Lead 规划", "双线研究", "深度检索", "财务估值", "写作与组装"],
  },
};

document.querySelector("#as-of").value = formatLocalDate(new Date());
modeInputs.forEach((input) => input.addEventListener("change", renderWorkflowPreview));
form.addEventListener("submit", createRun);
refreshButton.addEventListener("click", loadRuns);

setupSpotlight();
renderWorkflowPreview();
loadRuns();
symbolInput.focus({ preventScroll: true });

async function createRun(event) {
  event.preventDefault();
  hideNotice(formError);
  setButtonLoading(submitButton, true, "正在创建…");

  const selectedMode = document.querySelector('input[name="analysis_type"]:checked');
  const payload = {
    symbol: symbolInput.value,
    analysis_type: selectedMode?.value || "technical",
    policy_id: document.querySelector("#policy-id").value,
    as_of: document.querySelector("#as-of").value || null,
  };

  try {
    const response = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await readJson(response);
    if (!response.ok) throw new Error(errorMessage(body, "创建任务失败"));
    window.location.assign(`/runs/${encodeURIComponent(body.run_id)}`);
  } catch (error) {
    showNotice(formError, error.message);
    setButtonLoading(submitButton, false, "开始研究");
  }
}

function renderWorkflowPreview() {
  const selectedMode = document.querySelector('input[name="analysis_type"]:checked');
  const selected = selectedMode?.value || "technical";
  const preview = workflowPreviews[selected];
  document.querySelector("#workflow-preview").dataset.mode = selected;
  document.querySelector("#workflow-title").textContent = preview.title;
  document.querySelector("#workflow-description").textContent = preview.description;
  modeInputs.forEach((input) => input.closest(".research-mode")?.classList.remove("border-beam"));
  selectedMode?.closest(".research-mode")?.classList.add("border-beam");
  const steps = document.querySelector("#workflow-steps");
  steps.replaceChildren(...preview.steps.map((label, index) => {
    const item = document.createElement("li");
    const number = document.createElement("span");
    const text = document.createElement("strong");
    number.textContent = String(index + 1).padStart(2, "0");
    text.textContent = label;
    item.append(number, text);
    return item;
  }));
}

async function loadRuns() {
  hideNotice(historyError);
  setButtonLoading(refreshButton, true, "刷新中…");
  try {
    const response = await fetch("/api/runs");
    const runs = await readJson(response);
    if (!response.ok) throw new Error(errorMessage(runs, "读取历史任务失败"));
    renderRuns(runs);
  } catch (error) {
    showNotice(historyError, error.message);
    renderEmpty("暂时无法读取研究记录");
  } finally {
    setButtonLoading(refreshButton, false, "刷新");
  }
}

function renderRuns(runs) {
  runList.replaceChildren();
  if (!Array.isArray(runs) || runs.length === 0) {
    renderEmpty("暂无研究任务");
    return;
  }

  runs.forEach((run) => {
    const row = document.createElement("tr");
    const symbolCell = document.createElement("td");
    const link = document.createElement("a");
    link.href = `/runs/${encodeURIComponent(run.run_id)}`;
    link.textContent = run.input_symbol;
    symbolCell.append(link);
    row.append(
      symbolCell,
      cell(analysisLabels[run.analysis_type] || run.analysis_type),
      statusCell(run.status),
      cell(run.current_stage),
      cell(formatDateTime(run.created_at)),
    );
    runList.append(row);
  });
}

function renderEmpty(message) {
  runList.replaceChildren();
  const row = document.createElement("tr");
  const item = cell(message);
  item.colSpan = 5;
  item.className = "empty-state";
  row.append(item);
  runList.append(row);
}

function statusCell(status) {
  const item = document.createElement("td");
  const badge = document.createElement("span");
  badge.className = `status-badge ${statusClass(status)}`.trim();
  badge.textContent = statusLabels[status] || status;
  item.append(badge);
  return item;
}

function statusClass(status) {
  if (status === "COMPLETED") return "is-success";
  if (["FAILED", "CANCELLED"].includes(status)) return "is-error";
  if (status === "HUMAN_REVIEW_REQUIRED") return "is-review";
  return "";
}

function cell(value) {
  const item = document.createElement("td");
  item.textContent = value ?? "—";
  return item;
}

function formatDateTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function formatLocalDate(value) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function setButtonLoading(button, loading, text) {
  button.disabled = loading;
  const label = button.querySelector("span") || button;
  label.textContent = text;
}

function showNotice(element, message) {
  element.textContent = message;
  element.hidden = false;
}

function hideNotice(element) {
  element.hidden = true;
  element.textContent = "";
}

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

function errorMessage(body, fallback) {
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail)) return body.detail.map((item) => item.msg).join("；");
  return fallback;
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
