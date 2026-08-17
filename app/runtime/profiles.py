from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from app.runtime.exceptions import ProfileNotFoundError, ProfileValidationError
from app.runtime.schemas import AgentProfile


_PROMPT_APPENDICES = {
    "writer_planning": """

## 本轮 Deep 任务分配规则

Writer Planning 上下文中的 `deep_research_summary` 是 Deep 的压缩专题摘要，不是原始检索材料。请根据摘要中的 `task_id`、主题和结论，在每个 `report_composition` 中填写 `deep_task_ids`。同一张跨领域任务卡可以同时分配给多个 writer_group；不要为了避免重合而漏分配。Section Writer 会读取完整 Deep，但只写与本组职责相关的部分。
""",
    "writer_section": """

## Deep 专题使用规则

`research_briefs.deep` 包含完整 Deep 研究。严格按照 `writer_assignment.deep_task_ids` 选择专题；跨领域任务卡可以被多个 Writer 使用，但本组只写与自身职责相关的公司、行业或财务含义。不要把其他 Writer 的分析机械重复一遍，也不要因任务卡跨领域而回避有价值的材料。
""",
}


class ProfileLoader:
    def __init__(self, profile_dir: str | Path) -> None:
        self.profile_dir = Path(profile_dir)
        self._profiles: dict[str, AgentProfile] = {}
        self._load_all()

    def load(self, profile_id: str) -> AgentProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise ProfileNotFoundError(f"AgentProfile 不存在：{profile_id}") from exc

    def list_profiles(self) -> list[AgentProfile]:
        return [self._profiles[key] for key in sorted(self._profiles)]

    def _load_all(self) -> None:
        if not self.profile_dir.is_dir():
            raise ProfileValidationError(f"Profile 目录不存在：{self.profile_dir}")
        paths = sorted(self.profile_dir.glob("*.json"))
        for path in paths:
            try:
                profile = AgentProfile.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
                raise ProfileValidationError(f"Profile 加载失败：{path.name}") from exc
            if profile.profile_id in self._profiles:
                raise ProfileValidationError(
                    f"Profile ID 重复：{profile.profile_id}"
                )
            appendix = _PROMPT_APPENDICES.get(profile.profile_id, "")
            if appendix:
                profile = profile.model_copy(
                    update={"system_prompt": profile.system_prompt + appendix}
                )
            self._profiles[profile.profile_id] = profile
