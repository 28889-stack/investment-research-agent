from __future__ import annotations

from app.retrieval.firecrawl import FirecrawlSearchProvider


class _Response:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {
            "success": True,
            "data": {
                "web": [
                    {
                        "title": "全球铜矿供需展望",
                        "url": "https://example.com/copper-outlook",
                        "description": "铜矿供应、冶炼产能与需求变化。",
                    }
                ]
            },
        }


class _Client:
    def __init__(self, captured: dict[str, object], **_kwargs: object) -> None:
        self._captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def post(self, url: str, **kwargs: object) -> _Response:
        self._captured["url"] = url
        self._captured.update(kwargs)
        return _Response()


def test_firecrawl_search_uses_v2_metadata_only_contract(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-only")
    provider = FirecrawlSearchProvider(
        client_factory=lambda **kwargs: _Client(captured, **kwargs)
    )

    result = provider.search(
        query="全球铜矿供需", symbol="601899.SH", max_results=3, timeout=30
    )

    assert captured["url"] == "https://api.firecrawl.dev/v2/search"
    assert captured["headers"] == {"Authorization": "Bearer test-only"}
    assert captured["json"] == {
        "query": "全球铜矿供需",
        "limit": 3,
        "sources": [{"type": "web"}],
    }
    assert len(result.items) == 1
    assert result.items[0].title == "全球铜矿供需展望"
    assert result.items[0].summary == "铜矿供应、冶炼产能与需求变化。"
    assert result.items[0].content == ""
    assert result.items[0].source_kind == "web"
