from __future__ import annotations

import html
import re
from urllib.parse import quote

from app.technical.report import TECHNICAL_CANVAS_RUNTIME, TECHNICAL_REPORT_STYLE


EXPORT_STYLE = """
:root { --background:#f5f7f9; --surface:#fff; --text:#202124; --muted:#666b73; --border:#d9dde3; --primary:#163a5f; --font:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }
* { box-sizing:border-box; }
html { color-scheme:light; }
body { margin:0; background:var(--background); color:var(--text); font-family:var(--font); font-size:15.5px; line-height:1.8; font-variant-numeric:tabular-nums; }
.site-header { align-items:center; background:var(--surface); border-bottom:1px solid var(--border); display:flex; justify-content:space-between; min-height:64px; padding:0 32px; }
.brand { color:var(--text); font-size:18px; font-weight:700; }
.page-shell { margin:0 auto; max-width:1272px; padding:48px 56px 80px; }
.page-heading { border-left:6px solid var(--primary); margin-bottom:32px; padding-left:18px; }
.page-heading h1 { font-size:32px; line-height:1.25; margin:0 0 10px; }
.page-heading p { color:var(--muted); margin:0; }
.report-body { background:var(--surface); border:0; padding:0; }
.report-body img { border:1px solid var(--border); display:block; height:auto; margin:24px auto; max-width:100%; }
.report-body h1 { border-bottom:2px solid var(--primary); font-size:34px; margin:0 0 32px; padding-bottom:16px; }
.report-body h2 { border-top:1px solid var(--border); font-size:22px; margin:36px 0 16px; padding-top:24px; }
.report-body li, .report-body p { line-height:1.75; }
.report-body code { background:var(--background); border:1px solid var(--border); padding:2px 4px; }
table { border-collapse:collapse; min-width:720px; width:100%; }
th, td { border-bottom:1px solid var(--border); padding:13px 10px; text-align:left; vertical-align:top; }
th { color:var(--muted); font-size:13px; }
blockquote { border-left:4px solid var(--border); color:var(--muted); margin:20px 0; padding-left:16px; }
pre { background:#f0f1f3; overflow-x:auto; padding:16px; }
@media (max-width:700px) { .site-header { padding:16px 20px; } .page-shell { padding:28px 18px 48px; } .report-body { padding:0; } }
@media print { @page { size:A4; margin:14mm; } body { background:#fff; } .site-header,.page-heading { display:none; } .page-shell { max-width:none; padding:0; } table,.report-body img { break-inside:avoid; page-break-inside:avoid; } h1,h2,h3 { break-after:avoid; page-break-after:avoid; } }
""".strip()


def build_export_document(run, report_html: str, chart_bytes: bytes | None = None) -> str:
    # Legacy callers may still pass PNG bytes.  Native technical exports
    # intentionally ignore them and render the embedded ChartSpec instead.
    del chart_bytes
    body = report_html
    title = "基本面分析报告" if run.analysis_type == "fundamental" else "技术面研究报告"
    security = html.escape(run.security_name or run.resolved_symbol or run.input_symbol, quote=True)
    symbol = html.escape(run.resolved_symbol or run.normalized_symbol or run.input_symbol, quote=True)
    as_of = html.escape(run.as_of, quote=True)
    technical_style = TECHNICAL_REPORT_STYLE if run.analysis_type == "technical" else ""
    runtime = f"<script>{TECHNICAL_CANVAS_RUNTIME}</script>" if run.analysis_type == "technical" else ""
    return f'''<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)} · {security}</title><style>{EXPORT_STYLE}\n{technical_style}</style></head>
<body><header class="site-header"><span class="brand">金融投研 Agent</span><span>{security} · {symbol}</span></header>
<main class="page-shell"><section class="page-heading"><h1>{html.escape(title)}</h1><p>{security} · {symbol} · 数据截止 {as_of}</p></section><article class="report-body">{body}</article></main>
{runtime}</body></html>'''


def export_filename(run) -> str:
    label = "fundamental" if run.analysis_type == "fundamental" else "technical"
    stem = f"{run.security_name or run.resolved_symbol or run.input_symbol}_{run.resolved_symbol or run.input_symbol}_{label}_{run.as_of}"
    stem = re.sub(r'[\\/:\x00-\x1f\x7f]+', "_", stem)
    stem = re.sub(r"\s+", "_", stem).strip("._")[:180].strip("._")
    return (stem or f"report_{run.run_id}") + ".html"


def content_disposition(filename: str) -> str:
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'
