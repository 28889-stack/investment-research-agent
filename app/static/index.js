const form = document.querySelector("#research-form");
const submitButton = document.querySelector("#submit-button");
const refreshButton = document.querySelector("#refresh-button");
const formError = document.querySelector("#form-error");
const historyError = document.querySelector("#history-error");
const runList = document.querySelector("#run-list");

const analysisLabels = {
  technical: "技术面分析",
  fundamental: "基本面分析",
};

document.querySelector("#as-of").value = formatLocalDate(new Date());

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  hideNotice(formError);
  setButtonLoading(submitButton, true, "正在创建…");

  const payload = {
    symbol: document.querySelector("#symbol").value,
    analysis_type: document.querySelector("#analysis-type").value,
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
    if (!response.ok) {
      throw new Error(errorMessage(body, "创建任务失败"));
    }
    window.location.assign(`/runs/${encodeURIComponent(body.run_id)}`);
  } catch (error) {
    showNotice(formError, error.message);
    setButtonLoading(submitButton, false, "开始分析");
  }
});

refreshButton.addEventListener("click", loadRuns);
loadRuns();

async function loadRuns() {
  hideNotice(historyError);
  setButtonLoading(refreshButton, true, "刷新中…");
  try {
    const response = await fetch("/api/runs");
    const runs = await readJson(response);
    if (!response.ok) {
      throw new Error(errorMessage(runs, "读取历史任务失败"));
    }
    renderRuns(runs);
  } catch (error) {
    showNotice(historyError, error.message);
    renderEmpty("暂时无法读取任务");
  } finally {
    setButtonLoading(refreshButton, false, "刷新");
  }
}

function renderRuns(runs) {
  runList.replaceChildren();
  if (runs.length === 0) {
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
  badge.textContent = status;
  item.append(badge);
  return item;
}

function statusClass(status) {
  if (status === "COMPLETED") return "is-success";
  if (["FAILED", "CANCELLED"].includes(status)) return "is-error";
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
  button.textContent = text;
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
  if (Array.isArray(body.detail)) {
    return body.detail.map((item) => item.msg).join("；");
  }
  return fallback;
}
