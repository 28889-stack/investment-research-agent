from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from app.fundamental.evidence import ResearchSourceError
from app.fundamental.schemas import ResearchSearchResults, ResearchSource
from app.retrieval.registry import PROVIDER_SOURCE_KINDS, get_search_provider

logger = logging.getLogger(__name__)


# Canonical source_kind aliases. Agent-provided ``sources`` are only ranking
# preferences; they must never control which internally registered providers
# run. This prevents a guessed website/provider name from collapsing a whole
# research pass into an empty search.
_SOURCE_KIND_ALIASES: dict[str, str] = {
    "announcement": "announcement",
    "disclosure": "announcement",
    "公告": "announcement",
    "披露": "announcement",
    "research_report": "research_report",
    "research": "research_report",
    "report": "research_report",
    "研报": "research_report",
    "研究报告": "research_report",
    "news": "news",
    "新闻": "news",
    "web": "web",
    "网页": "web",
    "网络": "web",
}

# Every source_kind the closed catalog can emit.
_ALL_SOURCE_KINDS: set[str] = set(PROVIDER_SOURCE_KINDS.values())


def _source_preference_ranks(sources: list[str] | None) -> dict[str, int]:
    """Resolve optional source preferences to ordered canonical source kinds.

    Registered Provider names remain accepted for backwards compatibility, but
    are converted to their source kind and never exposed as a routing control.
    Unknown terms are ignored after a warning: they may be website brands or
    model guesses, neither of which should block configured retrieval.
    """
    ranks: dict[str, int] = {}
    unknown: list[str] = []
    for raw_token in sources or []:
        token = (raw_token or "").strip().lower()
        if not token:
            continue
        token = _SOURCE_KIND_ALIASES.get(token, token)
        kind = PROVIDER_SOURCE_KINDS.get(token, token)
        if kind not in _ALL_SOURCE_KINDS:
            unknown.append(token)
            continue
        if kind not in ranks:
            ranks[kind] = len(ranks)
    if unknown:
        logger.warning(
            "忽略未知来源偏好 %s，继续聚合全部已配置 Provider",
            sorted(set(unknown)),
        )
    return ranks


def _result_priority(item: ResearchSource, preferences: dict[str, int]) -> tuple[int, int]:
    kind = item.source_kind
    if kind in preferences:
        return (0, preferences[kind])
    return (1, _SOURCE_KIND_PRIORITY.get(kind, _DEFAULT_PRIORITY))


# Source-kind priority for reranking (lower = more authoritative). Weights are
# hard-coded, NOT config-driven, so a compromised/edited config cannot reorder
# sources to demote authoritative disclosures or promote untrusted web pages.
_SOURCE_KIND_PRIORITY = {
    "announcement": 0,
    "research_report": 1,
    "news": 2,
    "financial": 3,
    "technical": 4,
    "web": 5,
}
_DEFAULT_PRIORITY = 9

# Cap parallel fan-out workers so a long provider list never spawns an
# unbounded thread pool. Providers are network-bound, so a modest cap is fine.
_MAX_WORKERS = 8


class AggregatingSearchProvider:
    """Fan-out to multiple registered providers, dedup, rerank, isolate failures.

    `provider_names` is the fan-out list. Each name is resolved through the closed
    `get_search_provider` registry — configuration still cannot import arbitrary
    code. Per-provider failures are logged and skipped; only total failure raises.

    V4 additions (backward-compatible):
    - ``sources``: optional source-kind preferences
      (``announcement``/``research_report``/``news``/``web``). Every configured
      Provider still participates; preferences only move matching results ahead
      of the default authority ordering. Legacy Provider names are translated to
      their source kind, while unknown strings are ignored with a warning.
    - ``max_per_kind``: optional soft type-quota. When set, each ``source_kind`` is
      capped at this many items before the final ``max_results`` truncation, then
      backfilled in rerank order so the result never shrinks below what is
      available. Defaults to None (disabled) so same-kind-only result sets are
      returned unchanged.
    - Fan-out runs on a ``ThreadPoolExecutor``; results are reassembled in
      ``provider_names`` order before dedup so first-seen semantics are preserved.
    """

    def __init__(self, provider_names: list[str]) -> None:
        self._provider_names = list(provider_names)

    def search(
        self,
        *,
        query: str,
        symbol: str,
        max_results: int,
        timeout: float,
        sources: list[str] | None = None,
        max_per_kind: int | None = None,
    ) -> ResearchSearchResults:
        if not self._provider_names:
            raise ResearchSourceError("RESEARCH_SOURCE_FAILED: 聚合检索未配置任何来源")
        preferences = _source_preference_ranks(sources)
        selected_names = list(self._provider_names)

        failures: list[Exception] = []
        # Each task returns (items, exc). We run them concurrently but consume the
        # results in `selected_names` order — ThreadPoolExecutor.map yields in
        # input order — so dedup's first-seen rule still follows provider order.
        def _call(name: str):
            try:
                provider = get_search_provider(name)
                result = provider.search(
                    query=query, symbol=symbol, max_results=max_results, timeout=timeout
                )
                return list(result.items), None
            except ResearchSourceError as exc:
                return None, exc
            except Exception as exc:  # defensive: isolate any per-source fault
                return None, exc

        per_provider: list[tuple[list[ResearchSource] | None, Exception | None]] = []
        worker_count = max(1, min(len(selected_names), _MAX_WORKERS))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            per_provider = list(executor.map(_call, selected_names))

        collected: list[ResearchSource] = []
        seen_keys: set[str] = set()
        for items, exc in per_provider:
            if exc is not None:
                failures.append(exc)
                continue
            for item in items or []:
                key = _dedup_key(item.url)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                collected.append(item)
        if not collected:
            if failures:
                raise ResearchSourceError(
                    "RESEARCH_SOURCE_FAILED: 全部检索来源失败"
                ) from failures[0]
            raise ResearchSourceError("RESEARCH_SOURCE_FAILED: 聚合检索无结果")
        # Rerank via two stable sorts: first by date descending (newer first,
        # empty dates last — reverse=True puts the empty string at the tail),
        # then by optional Agent source preferences, with the fixed authority
        # order as the default. Both sorts are stable, so dates remain descending
        # within each source kind.
        collected.sort(key=lambda item: item.date, reverse=True)
        collected.sort(key=lambda item: _result_priority(item, preferences))
        # Soft type-quota: cap each source_kind at `max_per_kind`, then backfill
        # from the remaining items in rerank order so the final set never shrinks
        # below what is available. Opt-in (None) keeps the original behavior.
        if max_per_kind is not None and max_per_kind > 0:
            collected = _apply_type_quota(collected, max_per_kind)
        truncated = collected[:max_results]
        # Stable re-numbering after merge/rerank so downstream read_research_source
        # can look up by result_id regardless of which source produced it.
        for index, item in enumerate(truncated, 1):
            item.result_id = f"src_{index:03d}"
        return ResearchSearchResults(items=truncated)


def _apply_type_quota(
    items: list[ResearchSource], max_per_kind: int
) -> list[ResearchSource]:
    """Cap each source_kind at ``max_per_kind``; backfill leftovers in rerank order.

    Soft quota: ensures heterogeneity when multiple kinds are present, but never
    reduces a same-kind-only set below the available count. The input is assumed
    already reranked (kind priority asc, date desc within kind); we preserve that
    order in both the primary selection and the backfill.
    """
    primary: list[ResearchSource] = []
    leftovers: list[ResearchSource] = []
    counts: dict[str, int] = {}
    for item in items:
        kind = item.source_kind
        if counts.get(kind, 0) < max_per_kind:
            primary.append(item)
            counts[kind] = counts.get(kind, 0) + 1
        else:
            leftovers.append(item)
    # Backfill in rerank order until the primary list is as large as the original.
    primary.extend(leftovers)
    return primary


def _dedup_key(url: str) -> str:
    """Normalized URL for cross-source dedup: drop query/fragment, lowercase host."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    path = parts.path.rstrip("/")
    return f"{host}|{path}"
