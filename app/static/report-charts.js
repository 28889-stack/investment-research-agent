window.renderFinancialReportCharts = function renderFinancialReportCharts(container) {
  const root = container.querySelector("[data-report-visuals]");
  if (!root) return;
  let visuals;
  try { visuals = JSON.parse(root.dataset.reportVisuals); } catch { return; }
  const draw = (canvas, chart) => {
    const rect = canvas.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    const width = Math.max(320, rect.width);
    const height = 260;
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    const context = canvas.getContext("2d");
    context.scale(ratio, ratio);
    context.clearRect(0, 0, width, height);
    const values = chart.series.flatMap((series) => series.values).filter((value) => typeof value === "number" && Number.isFinite(value));
    if (!values.length) return;
    const minimum = Math.min(...values);
    const maximum = Math.max(...values);
    const span = maximum - minimum || 1;
    const padding = { left: 42, right: 16, top: 16, bottom: 34 };
    context.strokeStyle = "#dbe3ed";
    for (let index = 0; index < 4; index += 1) {
      const y = padding.top + (height - padding.top - padding.bottom) * index / 3;
      context.beginPath(); context.moveTo(padding.left, y); context.lineTo(width - padding.right, y); context.stroke();
    }
    chart.series.forEach((series) => {
      const points = series.values.map((value, index) => [
        padding.left + (width - padding.left - padding.right) * (chart.labels.length < 2 ? 0 : index / (chart.labels.length - 1)),
        padding.top + (height - padding.top - padding.bottom) * (1 - ((typeof value === "number" ? value : minimum) - minimum) / span),
      ]);
      context.strokeStyle = series.color; context.lineWidth = 2; context.beginPath();
      points.forEach(([x, y], index) => (index ? context.lineTo(x, y) : context.moveTo(x, y))); context.stroke();
      context.fillStyle = series.color; points.forEach(([x, y]) => { context.beginPath(); context.arc(x, y, 3, 0, Math.PI * 2); context.fill(); });
    });
    context.fillStyle = "#637287"; context.font = "11px sans-serif";
    chart.labels.forEach((label, index) => {
      const x = padding.left + (width - padding.left - padding.right) * (chart.labels.length < 2 ? 0 : index / (chart.labels.length - 1));
      context.fillText(label, x - 12, height - 12);
    });
    canvas.title = chart.series.map((series) => `${series.name}: ${series.values.join(" / ")}`).join("\n");
  };
  root.querySelectorAll("canvas[data-chart]").forEach((canvas) => {
    const chart = visuals.charts.find((item) => item.id === canvas.dataset.chart);
    if (!chart) return;
    draw(canvas, chart);
    window.addEventListener("resize", () => draw(canvas, chart), { passive: true });
  });
};

window.renderFinancialReportChartTooltips = function renderFinancialReportChartTooltips(container) {
  const root = container.querySelector("[data-report-visuals]");
  if (!root) return;
  let visuals;
  try { visuals = JSON.parse(root.dataset.reportVisuals); } catch { return; }
  root.querySelectorAll("canvas[data-chart]").forEach((canvas) => {
    const chart = visuals.charts.find((item) => item.id === canvas.dataset.chart);
    if (!chart) return;
    const host = canvas.parentElement;
    host.style.position = "relative";
    const tooltip = document.createElement("div");
    tooltip.hidden = true;
    tooltip.style.cssText = "position:absolute;z-index:2;max-width:260px;padding:5px 8px;background:#0d223d;color:#fff;font-size:12px;pointer-events:none";
    host.append(tooltip);
    canvas.addEventListener("mousemove", (event) => {
      const rect = canvas.getBoundingClientRect();
      const ratio = (event.clientX - rect.left) / Math.max(rect.width, 1);
      const index = Math.max(0, Math.min(chart.labels.length - 1, Math.round(ratio * (chart.labels.length - 1))));
      tooltip.textContent = `${chart.labels[index]} · ${chart.series.map((series) => `${series.name}: ${series.values[index] ?? "—"}`).join(" | ")}`;
      tooltip.style.left = `${event.clientX - rect.left + 12}px`;
      tooltip.style.top = `${event.clientY - rect.top + 12}px`;
      tooltip.hidden = false;
    });
    canvas.addEventListener("mouseleave", () => { tooltip.hidden = true; });
  });
};
