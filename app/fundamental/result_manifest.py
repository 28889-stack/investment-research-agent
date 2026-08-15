from __future__ import annotations

import hashlib
import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


DEPENDENCIES = {
    # V4 evidence decoupling: "evidence" removed from the research-round
    # dependencies. evidence is still an EDITABLE_INPUT and still flows into
    # fundamental_writer / fundamental_report for staleness, but a SHA change to
    # evidence.json no longer cascades through business_research/industry_research
    # and on to every middle node — it only rebuilds from the Writer onward.
    # `context_refs` still expose `artifact:evidence` to these agents at runtime
    # (they can read it); only the staleness edge is cut.
    "business_research": ["lead_plan", "company_profile"],
    "industry_research": ["lead_plan"],
    "lead_review": ["business_research", "industry_research"],
    "deep_research": ["lead_review"],
    "retrieval_package": ["evidence"],
    "financial_research": ["lead_review", "deep_research", "financial_data", "financial_metrics"],
    "valuation_result": ["financial_data", "financial_metrics", "assumptions"],
    "valuation_research": ["financial_research", "valuation_result", "assumptions"],
    "lead_final_review": ["business_research", "industry_research", "deep_research", "financial_data", "financial_metrics", "financial_research", "valuation_research", "retrieval_package"],
    "lead_synthesis": ["lead_final_review", "business_research", "industry_research", "deep_research", "financial_research", "valuation_research", "retrieval_package", "assumptions"],
    "writer_plan": ["lead_synthesis", "lead_final_review", "financial_metrics", "valuation_result"],
    "report_visuals": ["writer_plan", "financial_data", "financial_metrics", "valuation_result", "evidence", "assumptions"],
    "fundamental_writer": ["lead_synthesis", "writer_plan", "report_visuals", "business_research", "industry_research", "deep_research", "financial_research", "valuation_research", "lead_final_review", "retrieval_package", "evidence", "assumptions"],
    "fundamental_report": ["fundamental_writer", "report_visuals", "financial_metrics", "valuation_result", "evidence", "assumptions"],
}

RESULT_ORDER = [
    "lead_plan", "company_profile", "evidence", "business_research",
    "industry_research", "lead_review", "deep_research", "retrieval_package", "financial_data", "financial_metrics",
    "financial_research", "assumptions", "valuation_result", "valuation_research",
    "lead_final_review", "lead_synthesis", "writer_plan", "report_visuals", "fundamental_writer", "fundamental_report",
]
# Evidence and Assumption are the only intentionally editable inputs. Other
# zero-dependency results are deterministic/generated and must be rebuilt when
# their bytes change rather than silently promoted to a new current version.
EDITABLE_INPUTS = {"evidence", "assumptions"}
FILENAMES = {name: f"{name}.json" for name in RESULT_ORDER}
FILENAMES["fundamental_report"] = "fundamental_report.html"


class ManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    status: Literal["current", "stale", "failed"]
    sha256: str
    inputs: dict[str, str]


class ResultManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    workflow_version: str
    results: dict[str, ManifestEntry]
    updated_at: str


class ManifestInputChangedError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ResultManifestStore:
    def __init__(self, directory: Path, run_id: str, workflow_version: str) -> None:
        self.directory = Path(directory)
        self.path = self.directory / "result_manifest.json"
        self.lock_path = self.directory / ".result_manifest.lock"
        self.run_id = run_id
        self.workflow_version = workflow_version

    def load(self) -> ResultManifest:
        with self._lock(exclusive=False):
            return self._load_unlocked()

    def _load_unlocked(self) -> ResultManifest:
        if not self.path.is_file():
            return ResultManifest(run_id=self.run_id, workflow_version=self.workflow_version, results={}, updated_at=_now())
        manifest = ResultManifest.model_validate_json(self.path.read_text(encoding="utf-8"))
        if manifest.run_id != self.run_id or manifest.workflow_version != self.workflow_version:
            raise ValueError("Result Manifest 身份或工作流版本不一致")
        return manifest

    def input_hashes(self, name: str) -> dict[str, str]:
        with self._lock(exclusive=False):
            return self._input_hashes_unlocked(name)

    def record(
        self, name: str, *, expected_inputs: dict[str, str] | None = None
    ) -> ManifestEntry:
        with self._lock(exclusive=True):
            path = self._artifact_path(name)
            digest = sha256_file(path)
            manifest = self._load_unlocked()
            previous = manifest.results.get(name)
            inputs = self._input_hashes_unlocked(name)
            if expected_inputs is not None and inputs != expected_inputs:
                if previous is not None:
                    previous.status = "stale"
                    manifest.updated_at = _now()
                    self._save_unlocked(manifest)
                raise ManifestInputChangedError(f"{name} 生成期间输入发生变化")
            entry = ManifestEntry(
                version=(previous.version + 1 if previous else 1),
                status="current",
                sha256=digest,
                inputs=inputs,
            )
            manifest.results[name] = entry
            manifest.updated_at = _now()
            self._save_unlocked(manifest)
            return entry

    def refresh(self, name: str) -> ManifestEntry:
        """Refresh SHA/input baseline without claiming a new artifact rebuild."""
        with self._lock(exclusive=True):
            manifest = self._load_unlocked()
            previous = manifest.results.get(name)
            if previous is None:
                raise ValueError(f"Result 尚未生成: {name}")
            current_sha = sha256_file(self._artifact_path(name))
            if current_sha != previous.sha256:
                previous.status = "stale"
                manifest.updated_at = _now()
                self._save_unlocked(manifest)
                raise ManifestInputChangedError(f"{name} 输出在刷新输入基线前发生变化")
            entry = ManifestEntry(
                version=previous.version,
                status="current",
                sha256=current_sha,
                inputs=self._input_hashes_unlocked(name),
            )
            manifest.results[name] = entry
            manifest.updated_at = _now()
            self._save_unlocked(manifest)
            return entry

    def audit(self, *, persist: bool = True) -> list[str]:
        with self._lock(exclusive=persist):
            return self._audit_unlocked(persist=persist)

    def _audit_unlocked(self, *, persist: bool) -> list[str]:
        manifest = self._load_unlocked()
        direct_stale: set[str] = set()
        changed_sources: set[str] = set()
        for name, entry in manifest.results.items():
            if entry.status != "current":
                direct_stale.add(name)
            path = self._artifact_path(name)
            if not path.is_file():
                entry.status = "failed"
                direct_stale.add(name)
                continue
            current_sha = sha256_file(path)
            if current_sha != entry.sha256:
                if name in EDITABLE_INPUTS and name not in direct_stale:
                    entry.sha256 = current_sha
                    entry.version += 1
                    entry.status = "current"
                    changed_sources.add(name)
                else:
                    entry.status = "stale"
                    direct_stale.add(name)
        for name, entry in manifest.results.items():
            if name in direct_stale:
                continue
            for dependency, recorded_sha in entry.inputs.items():
                path = self._artifact_path(dependency)
                if not path.is_file() or sha256_file(path) != recorded_sha:
                    entry.status = "stale"
                    direct_stale.add(name)
                    break
        stale = set(direct_stale)
        queue = list(direct_stale | changed_sources)
        while queue:
            upstream = queue.pop(0)
            for name, dependencies in DEPENDENCIES.items():
                if upstream in dependencies and name in manifest.results and name not in stale:
                    manifest.results[name].status = "stale"
                    stale.add(name)
                    queue.append(name)
        manifest.updated_at = _now()
        if persist:
            self._save_unlocked(manifest)
        return [name for name in RESULT_ORDER if name in stale]

    def mark_failed(self, name: str) -> None:
        with self._lock(exclusive=True):
            manifest = self._load_unlocked()
            entry = manifest.results.get(name)
            if entry:
                entry.status = "failed"
                manifest.updated_at = _now()
                self._save_unlocked(manifest)

    def _artifact_path(self, name: str) -> Path:
        try:
            return self.directory / FILENAMES[name]
        except KeyError as exc:
            raise ValueError(f"未知 Result: {name}") from exc

    def _input_hashes_unlocked(self, name: str) -> dict[str, str]:
        return {
            dependency: sha256_file(self._artifact_path(dependency))
            for dependency in DEPENDENCIES.get(name, [])
        }

    @contextmanager
    def _lock(self, *, exclusive: bool):
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _save_unlocked(self, manifest: ResultManifest) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
