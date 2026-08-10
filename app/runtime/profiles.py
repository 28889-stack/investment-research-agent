from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from app.runtime.exceptions import ProfileNotFoundError, ProfileValidationError
from app.runtime.schemas import AgentProfile


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
            self._profiles[profile.profile_id] = profile
