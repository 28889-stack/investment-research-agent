from __future__ import annotations

import json

import pytest

from app.fundamental.report import generate_fundamental_report
from app.fundamental.schemas import FundamentalWriterOutput
from app.fundamental.workflow import FundamentalWorkflow
from app.run_service import RunService
from app.runtime.pi_client import MockPiClient


HEADINGS = [
    "# 个股基本面分析报告",
    "## 一、报告摘要",
    "## 二、公司与证券信息",
    "## 三、商业模式与业务结构",
    "## 四、行业与产业链分析",
    "## 五、财务表现",
    "### 收入和利润增长",
    "### 盈利能力",
    "### 资产负债",
    "### 现金流",
    "### 运营效率",
    "## 六、核心盈利驱动",
    "## 七、关键预测假设",
    "## 八、估值分析",
    "### PE / PB / PS",
    "### 简化 DCF",
    "### 敏感性分析",
    "## 九、研究证据",
    "## 十、研究冲突与不确定性",
    "## 十一、主要风险",
    "## 十二、数据和方法限制",
    "## 十三、版本信息",
    "## 免责声明",
]


def _prepared_directory(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    run = service.create_run(symbol="贵州茅台", analysis_type="fundamental", as_of="2026-08-05")
    workflow = FundamentalWorkflow(
        settings,
        session_factory,
        pi_client=MockPiClient(),
        interrupt_after=["lead_final_review"],
    )
    try:
        workflow.run(run.run_id)
    finally:
        workflow.shutdown()
    directory = settings.artifacts_dir / run.run_id
    writer = FundamentalWriterOutput.model_validate(
        {
            "symbol": "600519.SH",
            "as_of": "2026-08-05",
            "status": "completed",
            "executive_summary": "品牌、行业周期、现金流与估值假设需要联合理解。",
            "business": {"summary": "品牌与渠道构成业务基础。", "evidence_ids": ["ev_001"]},
            "industry": {"summary": "行业存在周期性。", "evidence_ids": []},
            "financial": {"summary": "财务表现需要结合现金流。", "evidence_ids": ["ev_001"], "assumption_ids": ["asm_001"]},
            "valuation": {"summary": "估值对输入假设敏感。", "evidence_ids": ["ev_001"], "assumption_ids": ["asm_001"]},
            "key_findings": ["业务基础与行业风险并存"],
            "conflicts": ["稳定性与周期性需要联合观察"],
            "risks": ["需求与假设变化风险"],
            "missing_information": [],
            "conclusion": "应结合假设和限制审慎理解。",
            "disclaimer": "本输出不构成投资建议、交易指令或收益承诺。",
        }
    )
    (directory / "fundamental_writer.json").write_text(
        json.dumps(writer.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )
    return run, directory


def test_report_contains_fixed_structure(settings, session_factory) -> None:
    run, directory = _prepared_directory(settings, session_factory)
    text = generate_fundamental_report(directory, run_id=run.run_id, workflow_version="fundamental_v1", writer_profile_version="v1").read_text(encoding="utf-8")
    positions = [text.index(heading) for heading in HEADINGS]
    assert positions == sorted(positions)
    assert "Financial Data Result：v1" in text
    assert "财务指标脚本：financial_metric_v1" in text
    assert "估值脚本：valuation_v1" in text
    assert "Writer Profile：v1" in text


def test_report_exact_numbers_come_from_authoritative_artifacts(settings, session_factory) -> None:
    run, directory = _prepared_directory(settings, session_factory)
    metrics = json.loads((directory / "financial_metrics.json").read_text(encoding="utf-8"))
    valuation = json.loads((directory / "valuation_result.json").read_text(encoding="utf-8"))
    assumptions = json.loads((directory / "assumptions.json").read_text(encoding="utf-8"))
    period = metrics["periods"][-1]
    text = generate_fundamental_report(directory, run_id=run.run_id, workflow_version="fundamental_v1", writer_profile_version="v1").read_text(encoding="utf-8")

    assert str(metrics["cash_flow"][period]["free_cash_flow"]) in text
    assert str(valuation["relative"]["pe"]["value"]) in text
    assert str(valuation["dcf"]["per_share_value"]) in text
    assert str(assumptions["items"][0]["value"]) in text


def test_report_only_expands_referenced_evidence_and_bounds_excerpt(settings, session_factory) -> None:
    run, directory = _prepared_directory(settings, session_factory)
    evidence = json.loads((directory / "evidence.json").read_text(encoding="utf-8"))
    evidence["items"][0]["content"] = "甲" * 2_000
    (directory / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
    text = generate_fundamental_report(directory, run_id=run.run_id, workflow_version="fundamental_v1", writer_profile_version="v1").read_text(encoding="utf-8")

    assert "ev_001" in text
    assert "ev_002" not in text
    assert "甲" * 500 not in text


def test_report_rejects_unknown_evidence_reference(settings, session_factory) -> None:
    run, directory = _prepared_directory(settings, session_factory)
    writer = json.loads((directory / "fundamental_writer.json").read_text(encoding="utf-8"))
    writer["business"]["evidence_ids"] = ["ev_999"]
    (directory / "fundamental_writer.json").write_text(json.dumps(writer, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="Evidence"):
        generate_fundamental_report(directory, run_id=run.run_id, workflow_version="fundamental_v1", writer_profile_version="v1")


def test_report_is_atomic_hides_absolute_paths_and_includes_disclaimer(settings, session_factory) -> None:
    run, directory = _prepared_directory(settings, session_factory)
    path = generate_fundamental_report(directory, run_id=run.run_id, workflow_version="fundamental_v1", writer_profile_version="v1")
    text = path.read_text(encoding="utf-8")

    assert not (directory / ".fundamental_report.md.tmp").exists()
    assert str(directory) not in text
    assert "本报告基于公开资料、历史财务数据、研究假设及简化估值模型生成。" in text
    assert "本报告不构成投资建议、交易指令或收益承诺。" in text
    assert "预测和估值结果依赖上述假设" in text


def test_report_does_not_hard_block_writer_language(settings, session_factory) -> None:
    run, directory = _prepared_directory(settings, session_factory)
    writer = json.loads((directory / "fundamental_writer.json").read_text(encoding="utf-8"))
    writer["executive_summary"] = "本研究给出买入评级"
    (directory / "fundamental_writer.json").write_text(json.dumps(writer, ensure_ascii=False), encoding="utf-8")

    report = generate_fundamental_report(
        directory, run_id=run.run_id, workflow_version="fundamental_v1", writer_profile_version="v1"
    ).read_text(encoding="utf-8")
    assert "买入评级" in report


def test_report_emits_self_contained_financial_html_with_canvas_visuals_and_optimization_section(
    settings, session_factory
) -> None:
    run, directory = _prepared_directory(settings, session_factory)
    writer = json.loads((directory / "fundamental_writer.json").read_text(encoding="utf-8"))
    writer["missing_information"] = ["补充可比公司口径与分部经营数据"]
    (directory / "fundamental_writer.json").write_text(
        json.dumps(writer, ensure_ascii=False), encoding="utf-8"
    )

    generate_fundamental_report(
        directory, run_id=run.run_id, workflow_version="fundamental_v1", writer_profile_version="v1"
    )

    html_path = directory / "fundamental_report.html"
    visuals_path = directory / "report_visuals.json"
    html = html_path.read_text(encoding="utf-8")
    assert visuals_path.is_file()
    assert html.startswith("<!doctype html>")
    assert "<canvas" in html
    assert "data-report-visuals=" in html
    assert "mousemove" in html
    assert "优化建议" in html
    assert "缺失信息" not in html
    assert "https://cdn" not in html
