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
    assert "创建研究任务" in response.text
    assert 'id="research-form"' in response.text
    assert "第五阶段：正式基本面报告与状态闭环" in response.text
    assert "基本面分析（Runtime 验证）" not in response.text


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
    assert 'id="execution-list"' in run_page.text
    assert 'id="fundamental-stages"' in run_page.text
    assert "Lead 规划" in run_page.text
    assert "正式报告 Writer" in run_page.text
    assert "生成正式报告" in run_page.text
    assert report_page.status_code == 200
    assert 'id="report-content"' in report_page.text


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


def test_export_fundamental_report_has_no_chart_data_uri(client, settings, session_factory):
    run_id = _run_fundamental(settings, session_factory)

    response = client.get(f"/api/runs/{run_id}/report/export")

    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "data:image/png;base64," not in response.text
    assert "/static/" not in response.text
    assert "result_manifest.json" not in response.text


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
    assert "<script>" not in response.text
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
