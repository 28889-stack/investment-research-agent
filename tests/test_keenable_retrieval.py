from __future__ import annotations

import os

from app.retrieval.keenable import KeenableSearchProvider


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {
            "results": [
                {
                    "title": "铜精矿供需报告",
                    "url": "https://example.com/copper",
                    "description": "铜供需摘要",
                    "snippet": "全球铜精矿供应与冶炼需求变化。",
                    "published_at": "2026-02-01T00:00:00Z",
                },
                {
                    "title": "第二份行业资料",
                    "url": "https://example.com/copper-2",
                    "description": "补充资料",
                }
            ]
        }


class _Client:
    def __init__(self, captured: dict[str, object], **_kwargs: object) -> None:
        self._captured = captured

    def post(self, url: str, **kwargs: object) -> _Response:
        self._captured["url"] = url
        self._captured.update(kwargs)
        return _Response()


def test_keenable_search_uses_its_native_endpoint_header_and_result_shape() -> None:
    captured: dict[str, object] = {}
    os.environ["TEST_KEENABLE_KEY"] = "test-only"

    provider = KeenableSearchProvider(
        client_factory=lambda **kwargs: _Client(captured, **kwargs),
        api_key_env_name="TEST_KEENABLE_KEY",
    )

    result = provider.search(query="全球铜供需", symbol="", max_results=1, timeout=10)

    assert captured["url"] == "https://api.keenable.ai/v1/search"
    assert captured["headers"] == {"X-API-Key": "test-only"}
    assert captured["json"] == {"query": "全球铜供需"}
    assert result.items[0].title == "铜精矿供需报告"
    assert result.items[0].summary == "全球铜精矿供应与冶炼需求变化。"
    assert result.items[0].date == "2026-02-01T00:00:00Z"
    assert len(result.items) == 1
