from __future__ import annotations

import base64
import html
import re
from html.parser import HTMLParser
from urllib.parse import quote


EXPORT_STYLE = """
:root { --background:#f7f7f8; --surface:#fff; --text:#111318; --muted:#646872; --border:#d9dce2; --primary:#002fa7; --font:Helvetica, sans-serif; }
* { box-sizing:border-box; }
html { color-scheme:light; }
body { margin:0; background:var(--background); color:var(--text); font-family:var(--font); font-size:16px; line-height:1.5; }
.site-header { align-items:center; background:var(--surface); border-bottom:1px solid var(--border); display:flex; justify-content:space-between; min-height:64px; padding:0 32px; }
.brand { color:var(--text); font-size:18px; font-weight:700; }
.page-shell { margin:0 auto; max-width:960px; padding:48px 32px 72px; }
.page-heading { border-left:6px solid var(--primary); margin-bottom:32px; padding-left:18px; }
.page-heading h1 { font-size:36px; line-height:1.08; margin:0 0 10px; }
.page-heading p { color:var(--muted); margin:0; }
.report-body { background:var(--surface); border:1px solid var(--border); padding:clamp(24px, 6vw, 64px); }
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
@media (max-width:700px) { .site-header { padding:16px 20px; } .page-shell { padding:32px 16px 56px; } .report-body { padding:20px; } }
""".strip()


class _ChartSourceRewriter(HTMLParser):
    def __init__(self, source_url: str, data_uri: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source_url = source_url
        self.data_uri = data_uri
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img" or not any(key == "src" and value == self.source_url for key, value in attrs):
            self.parts.append(self.get_starttag_text() or "")
            return
        original = self.get_starttag_text() or ""
        self.parts.append(re.sub(r'(\bsrc\s*=\s*["\'])' + re.escape(self.source_url) + r'(["\'])', r'\g<1>' + self.data_uri + r'\g<2>', original, count=1))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.parts and not self.parts[-1].rstrip().endswith("/>"):
            self.parts[-1] = self.parts[-1].rstrip()[:-1] + "/>"

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self.parts.append(f"<?{data}>")

    def render(self) -> str:
        return "".join(self.parts)


def inline_chart(html_text: str, source_url: str, chart_bytes: bytes) -> str:
    data_uri = "data:image/png;base64," + base64.b64encode(chart_bytes).decode("ascii")
    parser = _ChartSourceRewriter(source_url, data_uri)
    parser.feed(html_text)
    parser.close()
    return parser.render()


def build_export_document(run, report_html: str, chart_bytes: bytes | None = None) -> str:
    body = report_html
    chart_url = f"/api/runs/{run.run_id}/artifacts/technical_chart.png"
    if chart_bytes is not None:
        body = inline_chart(body, chart_url, chart_bytes)
    title = "基本面分析报告" if run.analysis_type == "fundamental" else "技术面研究报告"
    security = html.escape(run.security_name or run.resolved_symbol or run.input_symbol, quote=True)
    symbol = html.escape(run.resolved_symbol or run.normalized_symbol or run.input_symbol, quote=True)
    as_of = html.escape(run.as_of, quote=True)
    return f'''<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)} · {security}</title><style>{EXPORT_STYLE}</style></head>
<body><header class="site-header"><span class="brand">金融投研 Agent</span><span>{security} · {symbol}</span></header>
<main class="page-shell"><section class="page-heading"><h1>{html.escape(title)}</h1><p>{security} · {symbol} · 数据截止 {as_of}</p></section><article class="report-body">{body}</article></main>
</body></html>'''


def export_filename(run) -> str:
    label = "fundamental" if run.analysis_type == "fundamental" else "technical"
    stem = f"{run.security_name or run.resolved_symbol or run.input_symbol}_{run.resolved_symbol or run.input_symbol}_{label}_{run.as_of}"
    stem = re.sub(r'[\\/:\x00-\x1f\x7f]+', "_", stem)
    stem = re.sub(r"\s+", "_", stem).strip("._")[:180].strip("._")
    return (stem or f"report_{run.run_id}") + ".html"


def content_disposition(filename: str) -> str:
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'
