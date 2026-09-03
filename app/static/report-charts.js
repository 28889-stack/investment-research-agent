(function installReportChartRuntime() {
  const readVisuals = (container) => {
    const root = container.querySelector("[data-report-visuals]");
    if (!root) return null;
    try {
      return { root, charts: JSON.parse(root.dataset.reportVisuals).charts || [] };
    } catch {
      return null;
    }
  };

  const finite = (value) => typeof value === "number" && Number.isFinite(value);

  const formatAxisTick = (value, unit = "") => {
    if (!finite(value)) return "—";
    if (/(%|率|比例|margin|roe|roa)/i.test(unit) && Math.abs(value) <= 2) {
      return `${(value * 100).toFixed(Math.abs(value * 100) >= 10 ? 0 : 1)}%`;
    }
    const absolute = Math.abs(value);
    if (absolute >= 1e8) return `${(value / 1e8).toFixed(absolute >= 1e9 ? 1 : 2)}亿`;
    if (absolute >= 1e4) return `${(value / 1e4).toFixed(absolute >= 1e5 ? 1 : 2)}万`;
    if (absolute >= 1000) return value.toLocaleString("zh-CN", { maximumFractionDigits: 0 });
    if (absolute >= 10) return value.toFixed(absolute % 1 ? 1 : 0);
    return value.toFixed(absolute && absolute < 1 ? 2 : 1).replace(/\.0$/, "");
  };
  window.formatAxisTick = formatAxisTick;

  const draw = (canvas, chart) => {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const width = Math.max(320, rect.width);
    const height = Math.max(300, rect.height || 340);
    const padding = { left: 64, right: 28, top: 30, bottom: 48 };
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    const context = canvas.getContext("2d");
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, width, height);
    const values = chart.series.flatMap((series) => series.values).filter(finite);
    if (!values.length) return;

    const minimum = Math.min(0, ...values);
    const maximum = Math.max(...values);
    const span = maximum - minimum || 1;
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    const x = (index) => padding.left + plotWidth * (index + 0.5) / Math.max(chart.labels.length, 1);
    const y = (value) => padding.top + plotHeight * (1 - (value - minimum) / span);

    context.strokeStyle = "#D9DDE3";
    context.lineWidth = 1;
    for (let index = 0; index < 5; index += 1) {
      const gridY = padding.top + plotHeight * index / 4;
      context.beginPath();
      context.moveTo(padding.left, gridY);
      context.lineTo(width - padding.right, gridY);
      context.stroke();
      context.fillStyle = "#7A8492";
      context.font = '11px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif';
      context.textAlign = "right";
      context.textBaseline = "middle";
      context.fillText(
        formatAxisTick(maximum - span * index / 4, chart.unit || ""),
        padding.left - 10,
        gridY,
      );
    }

    context.strokeStyle = "rgba(74,85,101,.32)";
    context.beginPath();
    context.moveTo(padding.left, padding.top);
    context.lineTo(padding.left, height - padding.bottom);
    context.lineTo(width - padding.right, height - padding.bottom);
    context.stroke();

    const barLike = new Set(["bar", "stacked_bar", "waterfall", "timeline"]);
    const barSeries = chart.series.filter((series) => series.style === "bar" || barLike.has(chart.chart_type));
    const barWidth = Math.max(3, plotWidth / Math.max(chart.labels.length, 1) / Math.max(barSeries.length + 1, 2));
    chart.series.forEach((series) => {
      const isBar = series.style === "bar" || barLike.has(chart.chart_type);
      if (isBar) {
        const barIndex = Math.max(0, barSeries.indexOf(series));
        context.fillStyle = series.color;
        series.values.forEach((value, index) => {
          if (!finite(value)) return;
          const barX = x(index) + (barIndex - (barSeries.length - 1) / 2) * barWidth;
          context.fillRect(
            barX - barWidth * 0.42,
            Math.min(y(value), y(0)),
            barWidth * 0.84,
            Math.max(1, Math.abs(y(value) - y(0))),
          );
        });
        return;
      }
      const points = series.values.map((value, index) => finite(value) ? [x(index), y(value)] : null);
      context.strokeStyle = series.color;
      context.lineWidth = 2;
      context.beginPath();
      let started = false;
      points.forEach((point) => {
        if (!point) return;
        if (started) context.lineTo(point[0], point[1]);
        else context.moveTo(point[0], point[1]);
        started = true;
      });
      context.stroke();
      points.forEach((point) => {
        if (!point) return;
        context.fillStyle = series.color;
        context.beginPath();
        context.arc(point[0], point[1], 2.6, 0, Math.PI * 2);
        context.fill();
      });
    });

    context.fillStyle = "#7A8492";
    context.font = '11px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif';
    context.textAlign = "center";
    context.textBaseline = "top";
    chart.labels.forEach((label, index) => {
      const text = String(label);
      context.fillText(text.length > 10 ? `${text.slice(0, 9)}…` : text, x(index), height - padding.bottom + 13);
    });
    (chart.annotations || []).forEach((annotation) => {
      if (annotation.index < 0 || annotation.index >= chart.labels.length) return;
      const annotationX = x(annotation.index);
      context.strokeStyle = "#163A5F";
      context.setLineDash([3, 3]);
      context.beginPath();
      context.moveTo(annotationX, padding.top);
      context.lineTo(annotationX, height - padding.bottom);
      context.stroke();
      context.setLineDash([]);
      context.fillStyle = "#163A5F";
      context.fillText(String(annotation.label).slice(0, 18), Math.min(annotationX + 4, width - 130), padding.top + 12);
    });
    canvas.title = chart.series.map((series) => `${series.name}: ${series.values.join(" / ")}`).join("\n");
  };

  window.renderFinancialReportCharts = function renderFinancialReportCharts(container) {
    const payload = readVisuals(container);
    if (!payload) return;
    payload.root.querySelectorAll("canvas[data-chart]").forEach((canvas) => {
      const chart = payload.charts.find((item) => (item.chart_id || item.id) === canvas.dataset.chart && item.status !== "skipped");
      if (!chart) return;
      requestAnimationFrame(() => draw(canvas, chart));
      let resizeFrame = 0;
      window.addEventListener("resize", () => {
        cancelAnimationFrame(resizeFrame);
        resizeFrame = requestAnimationFrame(() => draw(canvas, chart));
      }, { passive: true });
    });
  };

  window.renderFinancialReportChartTooltips = function renderFinancialReportChartTooltips(container) {
    const payload = readVisuals(container);
    if (!payload) return;
    payload.root.querySelectorAll("canvas[data-chart]").forEach((canvas) => {
      const chart = payload.charts.find((item) => (item.chart_id || item.id) === canvas.dataset.chart && item.status !== "skipped");
      if (!chart) return;
      if (canvas.dataset.tooltipReady === "1") return;
      canvas.dataset.tooltipReady = "1";
      const host = canvas.parentElement;
      host.style.position = "relative";
      const tooltip = document.createElement("div");
      tooltip.hidden = true;
      tooltip.className = "chart-tooltip";
      host.append(tooltip);
      canvas.addEventListener("mousemove", (event) => {
        const rect = canvas.getBoundingClientRect();
        const index = Math.max(0, Math.min(chart.labels.length - 1, Math.floor((event.clientX - rect.left) / Math.max(rect.width, 1) * chart.labels.length)));
        tooltip.textContent = `${chart.labels[index]} · ${chart.series.map((series) => `${series.name}: ${series.values[index] ?? "—"}`).join(" | ")}`;
        tooltip.style.left = `${event.clientX - rect.left + 12}px`;
        tooltip.style.top = `${event.clientY - rect.top + 12}px`;
        tooltip.hidden = false;
      });
      canvas.addEventListener("mouseleave", () => { tooltip.hidden = true; });
    });
  };
}());
