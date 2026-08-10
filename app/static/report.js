const runId = window.location.pathname.split("/").filter(Boolean)[1];
const reportContent = document.querySelector("#report-content");
const reportMeta = document.querySelector("#report-meta");
const pageError = document.querySelector("#page-error");
const runLink = document.querySelector("#run-link");
const reportTitle = document.querySelector("#report-title");
const exportButton = document.querySelector("#export-button");

runLink.href = `/runs/${encodeURIComponent(runId)}`;
exportButton.href = `/api/runs/${encodeURIComponent(runId)}/report/export`;
loadReport();

async function loadReport() {
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/report`);
    const report = await readJson(response);
    if (!response.ok) throw new Error(errorMessage(report, "读取研究结果失败"));
    const title = report.analysis_type === "fundamental" ? "个股基本面分析报告" : "技术面研究报告";
    reportTitle.textContent = title;
    document.title = `${title} · 金融投研 Agent`;
    const versions = report.analysis_type === "technical"
      ? ` · 数据版本 ${report.data_version || "—"} · 指标 ${report.indicator_version || "—"} · Kronos ${report.kronos_model_version || "—"}`
      : ` · Evidence ${report.evidence_count ?? "—"} · Assumption ${report.assumption_count ?? "—"} · Writer ${report.writer_status || (report.ready_for_writer ? "completed" : "not_started")} · Report ${report.report_status || "—"} v${report.result_version ?? "—"}`;
    reportMeta.textContent = `${report.security_name || ""} ${report.resolved_symbol || report.normalized_symbol || report.input_symbol} · ${analysisLabel(report.analysis_type)} · 数据截止 ${report.as_of}${versions}`.trim();
    reportContent.innerHTML = report.html;
    if (typeof window.renderFinancialReportCharts === "function") {
      window.renderFinancialReportCharts(reportContent);
    }
    if (typeof window.renderFinancialReportChartTooltips === "function") {
      window.renderFinancialReportChartTooltips(reportContent);
    }
    exportButton.hidden = false;
  } catch (error) {
    exportButton.hidden = true;
    pageError.textContent = error.message;
    pageError.hidden = false;
    reportMeta.textContent = `任务 ID：${runId}`;
  }
}

function analysisLabel(value) {
  return value === "technical" ? "技术面分析" : "基本面分析";
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
