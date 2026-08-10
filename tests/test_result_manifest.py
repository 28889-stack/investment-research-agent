from __future__ import annotations

import json

import pytest

from app.fundamental.result_manifest import DEPENDENCIES, ManifestInputChangedError, ResultManifestStore, sha256_file


def _write(path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def test_fixed_dependencies_match_phase_five_contract() -> None:
    assert DEPENDENCIES["business_research"] == ["lead_plan", "company_profile"]
    assert DEPENDENCIES["industry_research"] == ["lead_plan"]
    assert DEPENDENCIES["deep_research"] == ["lead_review"]
    assert "deep_research" in DEPENDENCIES["financial_research"]
    assert DEPENDENCIES["valuation_result"] == ["financial_data", "financial_metrics", "assumptions"]
    assert DEPENDENCIES["retrieval_package"] == ["evidence"]
    assert DEPENDENCIES["writer_plan"] == ["lead_synthesis", "lead_final_review", "financial_metrics", "valuation_result"]
    assert DEPENDENCIES["fundamental_report"] == ["fundamental_writer", "report_visuals", "financial_metrics", "valuation_result", "evidence", "assumptions"]


def test_manifest_first_record_is_version_one_and_atomic(tmp_path) -> None:
    _write(tmp_path / "lead_plan.json", "lead")
    store = ResultManifestStore(tmp_path, "run-1", "fundamental_v1")

    entry = store.record("lead_plan")

    assert entry.version == 1
    assert entry.status == "current"
    assert entry.sha256 == sha256_file(tmp_path / "lead_plan.json")
    assert not (tmp_path / ".result_manifest.json.tmp").exists()
    saved = json.loads((tmp_path / "result_manifest.json").read_text(encoding="utf-8"))
    assert saved["workflow_version"] == "fundamental_v1"


def test_manifest_successful_rebuild_increments_version(tmp_path) -> None:
    _write(tmp_path / "lead_plan.json", "lead-v1")
    store = ResultManifestStore(tmp_path, "run-1", "fundamental_v1")
    store.record("lead_plan")
    _write(tmp_path / "lead_plan.json", "lead-v2")

    rebuilt = store.record("lead_plan")

    assert rebuilt.version == 2
    assert rebuilt.sha256 == sha256_file(tmp_path / "lead_plan.json")


def test_unchanged_inputs_remain_current(tmp_path) -> None:
    for name in ("lead_plan", "company_profile", "evidence", "business_research"):
        _write(tmp_path / f"{name}.json", name)
    store = ResultManifestStore(tmp_path, "run-1", "fundamental_v1")
    for name in ("lead_plan", "company_profile", "evidence", "business_research"):
        store.record(name)

    assert store.audit() == []
    assert store.load().results["business_research"].status == "current"


def test_financial_metrics_change_marks_only_downstream_stale(tmp_path) -> None:
    names = {
        "lead_plan", "company_profile", "evidence", "business_research", "industry_research",
        "lead_review", "deep_research", "financial_data", "financial_metrics", "financial_research", "assumptions",
        "valuation_result", "valuation_research", "lead_final_review", "lead_synthesis", "writer_plan", "fundamental_writer", "report_visuals", "fundamental_report", "retrieval_package",
    }
    for name in names:
        _write(tmp_path / (f"{name}.html" if name == "fundamental_report" else f"{name}.json"), name)
    store = ResultManifestStore(tmp_path, "run-1", "fundamental_v1")
    for name in sorted(names):
        store.record(name)
    _write(tmp_path / "financial_metrics.json", "changed")

    stale = store.audit()

    assert stale == ["financial_metrics", "financial_research", "valuation_result", "valuation_research", "lead_final_review", "lead_synthesis", "writer_plan", "fundamental_writer", "report_visuals", "fundamental_report"]
    assert store.load().results["business_research"].status == "current"


def test_evidence_change_propagates_to_reference_consumers(tmp_path) -> None:
    names = ["lead_plan", "company_profile", "evidence", "business_research", "industry_research", "lead_review", "deep_research", "retrieval_package", "financial_data", "financial_metrics", "financial_research", "assumptions", "valuation_result", "valuation_research", "lead_final_review", "lead_synthesis", "writer_plan", "fundamental_writer", "report_visuals", "fundamental_report"]
    for name in names:
        _write(tmp_path / (f"{name}.html" if name == "fundamental_report" else f"{name}.json"), name)
    store = ResultManifestStore(tmp_path, "run-1", "fundamental_v1")
    for name in names:
        store.record(name)
    _write(tmp_path / "evidence.json", "changed")

    stale = store.audit()

    # Evidence remains decoupled from research rounds. It first rebuilds the
    # retrieval package, then the Lead/Writer material-selection chain.
    assert stale == ["retrieval_package", "lead_final_review", "lead_synthesis", "writer_plan", "fundamental_writer", "fundamental_report"]
    assert "fundamental_writer" in stale
    assert "fundamental_report" in stale
    assert "lead_plan" not in stale


def test_persisted_stale_status_cannot_self_clear_when_bytes_are_restored(tmp_path) -> None:
    for name in ("lead_plan", "company_profile", "evidence", "business_research"):
        _write(tmp_path / f"{name}.json", name)
    store = ResultManifestStore(tmp_path, "run-1", "fundamental_v1")
    for name in ("lead_plan", "company_profile", "evidence", "business_research"):
        store.record(name)
    original = (tmp_path / "lead_plan.json").read_text(encoding="utf-8")
    _write(tmp_path / "lead_plan.json", "tampered")
    assert "business_research" in store.audit()
    _write(tmp_path / "lead_plan.json", original)

    stale = store.audit()

    assert "lead_plan" in stale
    assert "business_research" in stale


def test_record_uses_input_sha_compare_and_swap(tmp_path) -> None:
    for name in ("lead_plan", "company_profile", "evidence", "business_research"):
        _write(tmp_path / f"{name}.json", name)
    store = ResultManifestStore(tmp_path, "run-1", "fundamental_v1")
    for name in ("lead_plan", "company_profile", "evidence", "business_research"):
        store.record(name)
    expected = store.input_hashes("business_research")
    # V4: business_research no longer depends on evidence, so tampering
    # evidence.json would NOT be detected here. Tamper lead_plan.json instead
    # — business_research still depends on lead_plan, so the SHA compare-and-swap
    # still raises ManifestInputChangedError.
    _write(tmp_path / "lead_plan.json", "changed-during-generation")

    with pytest.raises(ManifestInputChangedError):
        store.record("business_research", expected_inputs=expected)


def test_refresh_rejects_changed_result_bytes(tmp_path) -> None:
    store = ResultManifestStore(tmp_path, "run-1", "fundamental_v1")
    _write(tmp_path / "lead_plan.json", "lead")
    _write(tmp_path / "company_profile.json", "profile")
    _write(tmp_path / "evidence.json", "evidence-v1")
    _write(tmp_path / "business_research.json", "business-v1")
    for name in ("lead_plan", "company_profile", "evidence", "business_research"):
        store.record(name)
    original_sha = store.load().results["business_research"].sha256

    _write(tmp_path / "evidence.json", "evidence-v2")
    _write(tmp_path / "business_research.json", "tampered")

    with pytest.raises(ManifestInputChangedError):
        store.refresh("business_research")

    entry = store.load().results["business_research"]
    assert entry.status == "stale"
    assert entry.sha256 == original_sha

    assert store.load().results["business_research"].status == "stale"
