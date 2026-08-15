import re
from pathlib import Path

from app import main as main_module
from app.run_service import RunService
from app.runtime.pi_client import MockPiClient
from app.technical.workflow import TechnicalWorkflow
from app.fundamental.workflow import FundamentalWorkflow


def _run_technical(settings, session_factory) -> str:
    service = RunService(session_factory, settings.artifacts_dir)
    run = service.create_run(symbol="贵州茅台", analysis_type="technical", as_of="2026-08-05")
    assert service.claim_next_created_run() == run.run_id
    workflow = TechnicalWorkflow(settings, session_factory, pi_client=MockPiClient())
    try:
        workflow.run(run.run_id)
    finally:
        workflow.shutdown()
    return run.run_id


def _run_fundamental(settings, session_factory) -> str:
    service = RunService(
        session_factory,
        settings.artifacts_dir,
        settings.pi_runtime_mode,
        settings.technical_workflow_version,
        settings.fundamental_workflow_version,
    )
    run = service.create_run(symbol="贵州茅台", analysis_type="fundamental", as_of="2026-08-05")
    workflow = FundamentalWorkflow(settings, session_factory, pi_client=MockPiClient())
    try:
        workflow.run(run.run_id)
    finally:
        workflow.shutdown()
    return run.run_id


def test_home_page_serves_research_form(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "弦月研究" in response.text
    assert "选择研究标的" in response.text
    assert 'id="research-form"' in response.text
    assert 'id="technical-mode"' in response.text
    assert 'id="fundamental-mode"' in response.text
    assert 'id="workflow-preview"' in response.text
    assert 'id="recent-research"' in response.text
    assert 'class="signal-disc"' in response.text
    assert 'class="ambient-field"' in response.text
    assert 'id="cursor-spotlight"' in response.text
    assert "基本面分析（Runtime 验证）" not in response.text
    assert "Kimi" not in response.text


def test_home_page_contains_the_invite_access_gate(client):
    response = client.get("/")
    index_script = (main_module.STATIC_DIR / "index.js").read_text(encoding="utf-8")

    assert 'id="access-gate"' in response.text
    assert "/api/auth/invite" in index_script


def test_run_and_report_pages_are_available(client):
    created = client.post(
        "/api/runs",
        json={"symbol": "600519", "analysis_type": "technical"},
    ).json()

    run_page = client.get(f"/runs/{created['run_id']}")
    report_page = client.get(f"/runs/{created['run_id']}/report")

    assert run_page.status_code == 200
    assert 'id="progress-bar"' in run_page.text
    assert 'id="current-node"' in run_page.text
    assert 'id="current-work"' in run_page.text
    assert 'id="execution-list"' in run_page.text
    assert 'id="fundamental-stages"' in run_page.text
    assert 'class="workflow-branch"' in run_page.text
    assert 'class="ambient-field"' in run_page.text
    assert 'id="cursor-spotlight"' in run_page.text
    assert 'data-node="business_research"' in run_page.text
    assert 'data-node="industry_research"' in run_page.text
    assert "Lead 规划" in run_page.text
    assert "正式报告 Writer" in run_page.text
    assert "生成正式报告" in run_page.text
    assert report_page.status_code == 200
    assert 'id="report-content"' in report_page.text


def test_application_pages_define_eclipse_glass_motion_and_responsive_tracks():
    stylesheet = (main_module.STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    index_script = (main_module.STATIC_DIR / "index.js").read_text(encoding="utf-8")
    run_script = (main_module.STATIC_DIR / "run.js").read_text(encoding="utf-8")

    assert ".signal-disc" in stylesheet
    assert ".workflow-branch" in stylesheet
    assert ".ambient-field" in stylesheet
    assert ".glass-surface" in stylesheet
    assert ".cursor-spotlight" in stylesheet
    assert ".border-beam" in stylesheet
    assert "--app-bg: #07080b" in stylesheet.lower()
    assert "@supports not (backdrop-filter: blur(1px))" in stylesheet
    assert "@media (prefers-reduced-motion: reduce)" in stylesheet
    assert "setupSpotlight" in index_script
    assert "setupSpotlight" in run_script
    assert "animateProgressValue" in run_script


def test_research_track_has_a_deliberate_inset_from_the_sidebar_edge():
    stylesheet = (main_module.STATIC_DIR / "styles.css").read_text(encoding="utf-8")

    assert "padding: 34px 32px 42px 20px;" in stylesheet


def test_application_uses_deep_space_glass_tokens_and_reduced_motion_support():
    stylesheet = (main_module.STATIC_DIR / "styles.css").read_text(encoding="utf-8")

    assert "--app-void: #050711;" in stylesheet
    assert "--app-glass: rgba(14, 20, 37, .72);" in stylesheet
    assert "--app-ice: #91b8ff;" in stylesheet
    assert ".glass-surface::before" in stylesheet
    assert "@keyframes glass-beam-sweep" in stylesheet
    assert "@media (prefers-reduced-motion: reduce)" in stylesheet


def test_home_page_preview_uses_the_selected_mode_element_without_runtime_error():
    index_script = (main_module.STATIC_DIR / "index.js").read_text(encoding="utf-8")
    match = re.search(
        r"function renderWorkflowPreview\(\) \{(?P<body>.*?)\n\}",
        index_script,
        flags=re.DOTALL,
    )

    assert match is not None
    preview_body = match.group("body")
    assert "const selectedMode = document.querySelector" in preview_body
    assert "selectedMode?.value || \"technical\"" in preview_body
    assert "selectedMode?.closest(\".research-mode\")" in preview_body


def test_report_html_is_sanitized(client, app, tmp_path: Path):
    created = client.post(
        "/api/runs",
        json={"symbol": "600519", "analysis_type": "technical"},
    ).json()
    report_path = tmp_path / "unsafe.md"
    report_path.write_text(
        "# 安全报告\n\n<script>alert('unsafe')</script>\n\n[链接](javascript:alert(1))",
        encoding="utf-8",
    )
    app.state.run_service.transition_run(
        created["run_id"],
        status="COMPLETED",
        stage="任务完成",
        progress=100,
        event_type="RUN_COMPLETED",
        message="测试报告已生成",
        report_path=str(report_path),
    )

    response = client.get(f"/api/runs/{created['run_id']}/report")

    assert response.status_code == 200
    html = response.json()["html"]
    assert "<h1>安全报告</h1>" in html
    assert "<script>" not in html
    assert "javascript:" not in html


def test_report_page_has_export_button(client):
    created = client.post(
        "/api/runs",
        json={"symbol": "600519", "analysis_type": "technical"},
    ).json()

    report_page = client.get(f"/runs/{created['run_id']}/report")

    assert report_page.status_code == 200
    assert 'id="export-button"' in report_page.text


def test_export_technical_report_is_self_contained(client, settings, session_factory):
    run_id = _run_technical(settings, session_factory)

    response = client.get(f"/api/runs/{run_id}/report/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "attachment" in response.headers["content-disposition"]
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"
    body = response.text
    assert body.startswith("<!doctype html>")
    assert 'charset="utf-8"' in body
    assert "/static/styles.css" not in body
    assert f"/api/runs/{run_id}/artifacts/technical_chart.png" not in body
    assert "data:image/png;base64," in body
    assert "<canvas" in body
    assert "getContext('2d')" in body
    artifact = (settings.artifacts_dir / run_id / "technical_report.html").read_text(
        encoding="utf-8"
    )
    assert body == artifact
    assert "requestAnimationFrame" in body
    assert "formatAxisTick" in body


def test_export_fundamental_report_has_no_chart_data_uri(client, settings, session_factory):
    run_id = _run_fundamental(settings, session_factory)

    response = client.get(f"/api/runs/{run_id}/report/export")

    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "data:image/png;base64," not in response.text
    assert "/static/" not in response.text
    assert "result_manifest.json" not in response.text
    assert "requestAnimationFrame" in response.text
    assert "formatAxisTick" in response.text
    assert response.text == (
        settings.artifacts_dir / run_id / "fundamental_report.html"
    ).read_text(encoding="utf-8")


def test_export_report_is_sanitized(client, app, tmp_path: Path):
    created = client.post(
        "/api/runs",
        json={"symbol": "600519", "analysis_type": "technical"},
    ).json()
    report_path = tmp_path / "unsafe.md"
    report_path.write_text(
        "# 安全报告\n\n<script>alert('unsafe')</script>\n\n[链接](javascript:alert(1))",
        encoding="utf-8",
    )
    app.state.run_service.transition_run(
        created["run_id"],
        status="COMPLETED",
        stage="任务完成",
        progress=100,
        event_type="RUN_COMPLETED",
        message="测试报告已生成",
        report_path=str(report_path),
    )

    response = client.get(f"/api/runs/{created['run_id']}/report/export")

    assert response.status_code == 200
    assert response.text.count("<script>") == 1
    assert "<script>alert('unsafe')</script>" not in response.text
    assert "javascript:" not in response.text


def test_export_report_not_ready_returns_json_conflict(client):
    created = client.post(
        "/api/runs",
        json={"symbol": "600519", "analysis_type": "technical"},
    ).json()

    response = client.get(f"/api/runs/{created['run_id']}/report/export")

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/json")
    assert "content-disposition" not in response.headers


def test_export_report_unknown_run_returns_404(client):
    response = client.get("/api/runs/does-not-exist/report/export")

    assert response.status_code == 404
    assert "content-disposition" not in response.headers


def test_web_entrypoint_uses_configured_host_and_port(settings, monkeypatch):
    configured = settings.model_copy(
        update={
            "app_host": "0.0.0.0",
            "app_port": 9100,
            "log_level": "DEBUG",
            "allow_public_bind": True,
        }
    )
    call = {}

    def fake_run(application, **kwargs):
        call["application"] = application
        call.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)

    main_module.run_server(configured)

    assert call["application"].state.settings == configured
    assert {key: call[key] for key in ("host", "port", "log_level")} == {
        "host": "0.0.0.0",
        "port": 9100,
        "log_level": "debug",
    }


def test_web_entrypoint_rejects_unauthorized_public_bind(settings):
    import pytest
    from app.readiness import ConfigurationError

    with pytest.raises(ConfigurationError, match="ALLOW_PUBLIC_BIND"):
        main_module.run_server(settings.model_copy(update={"app_host": "0.0.0.0"}))
