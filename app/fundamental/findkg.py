from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Literal
import unicodedata

from pydantic import BaseModel, ConfigDict


FinDKGFocus = Literal[
    "industry",
    "commodity",
    "macro",
    "technology",
    "demand",
    "supply",
    "competition",
    "general",
]


class FinDKGRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    relation: str
    target: str
    time_context: Literal["historical"] = "historical"
    strength: Literal["high", "medium", "low"]


class FinDKGQueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_entities: list[str]
    relationships: list[FinDKGRelationship]
    related_entities: list[str]
    research_hints: list[str]


@dataclass(frozen=True)
class _Edge:
    source_id: str
    relation_id: str
    target_id: str
    occurrences: int


@dataclass(frozen=True)
class _Graph:
    entities: dict[str, str]
    relations: dict[str, str]
    entity_index: dict[str, tuple[str, ...]]
    adjacency: dict[str, tuple[_Edge, ...]]


class LocalFinDKG:
    """Small, read-only adapter for the published FinDKG text dataset."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._graph: _Graph | None = None
        self._lock = Lock()

    def query(
        self,
        *,
        entities: list[str],
        focus: FinDKGFocus,
        max_hops: int,
        limit: int,
    ) -> FinDKGQueryResult:
        graph = self._load()
        if graph is None:
            return _empty_result(entities)

        seed_ids: list[str] = []
        for requested in entities:
            seed_ids.extend(graph.entity_index.get(_normalize(requested), ()))
        seed_ids = list(dict.fromkeys(seed_ids))
        if not seed_ids:
            return _empty_result(entities)

        ranked_by_seed: list[list[_Edge]] = []
        for seed_id in seed_ids:
            candidates = _collect_edges(graph, seed_id, max_hops)
            ranked_by_seed.append(sorted(
                candidates,
                key=lambda edge: (
                    candidates[edge],
                    -edge.occurrences,
                    graph.entities.get(edge.source_id, edge.source_id).casefold(),
                    graph.entities.get(edge.target_id, edge.target_id).casefold(),
                    graph.relations.get(edge.relation_id, edge.relation_id).casefold(),
                ),
            ))
        # Round-robin across requested entities so a highly connected generic
        # entity cannot consume the entire result limit.
        ranked: list[_Edge] = []
        selected: set[_Edge] = set()
        widest = max((len(items) for items in ranked_by_seed), default=0)
        for position in range(widest):
            for items in ranked_by_seed:
                if position >= len(items) or items[position] in selected:
                    continue
                selected.add(items[position])
                ranked.append(items[position])
                if len(ranked) >= limit:
                    break
            if len(ranked) >= limit:
                break
        relationships = [
            FinDKGRelationship(
                source=graph.entities.get(edge.source_id, edge.source_id),
                relation=graph.relations.get(edge.relation_id, edge.relation_id),
                target=graph.entities.get(edge.target_id, edge.target_id),
                strength=_strength(edge.occurrences),
            )
            for edge in ranked
        ]
        seed_set = set(seed_ids)
        related_entities: list[str] = []
        for edge in ranked:
            for entity_id in (edge.source_id, edge.target_id):
                name = graph.entities.get(entity_id, entity_id)
                if entity_id not in seed_set and name not in related_entities:
                    related_entities.append(name)

        return FinDKGQueryResult(
            query_entities=entities,
            relationships=relationships,
            related_entities=related_entities[:limit],
            research_hints=_build_hints(relationships, focus),
        )

    def _load(self) -> _Graph | None:
        if self._graph is not None:
            return self._graph
        with self._lock:
            if self._graph is not None:
                return self._graph
            graph = _load_graph(self._data_dir)
            if graph is not None:
                self._graph = graph
            return graph


def _load_graph(data_dir: Path) -> _Graph | None:
    entity_path = data_dir / "entity2id.txt"
    relation_path = data_dir / "relation2id.txt"
    triple_paths = [data_dir / name for name in ("train.txt", "valid.txt", "test.txt")]
    if not entity_path.is_file() or not relation_path.is_file():
        return None

    entities = _load_names(entity_path)
    relations = _load_names(relation_path)
    if not entities or not relations:
        return None

    counts: Counter[tuple[str, str, str]] = Counter()
    for path in triple_paths:
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            columns = raw_line.split("\t")
            if len(columns) < 3:
                continue
            source_id, relation_id, target_id = columns[:3]
            if source_id in entities and relation_id in relations and target_id in entities:
                counts[(source_id, relation_id, target_id)] += 1
    if not counts:
        return None

    adjacency: dict[str, list[_Edge]] = defaultdict(list)
    for (source_id, relation_id, target_id), occurrences in counts.items():
        edge = _Edge(source_id, relation_id, target_id, occurrences)
        adjacency[source_id].append(edge)
        if target_id != source_id:
            adjacency[target_id].append(edge)

    index: dict[str, list[str]] = defaultdict(list)
    for entity_id, name in entities.items():
        normalized = _normalize(name)
        if normalized:
            index[normalized].append(entity_id)
    return _Graph(
        entities=entities,
        relations=relations,
        entity_index={key: tuple(value) for key, value in index.items()},
        adjacency={key: tuple(value) for key, value in adjacency.items()},
    )


def _collect_edges(graph: _Graph, seed_id: str, max_hops: int) -> dict[_Edge, int]:
    queue = deque([(seed_id, 0)])
    visited_depth = {seed_id: 0}
    candidates: dict[_Edge, int] = {}
    while queue:
        entity_id, depth = queue.popleft()
        if depth >= max_hops:
            continue
        for edge in graph.adjacency.get(entity_id, ()):
            candidates[edge] = min(candidates.get(edge, max_hops + 1), depth + 1)
            neighbour = edge.target_id if edge.source_id == entity_id else edge.source_id
            next_depth = depth + 1
            if next_depth < visited_depth.get(neighbour, max_hops + 1):
                visited_depth[neighbour] = next_depth
                queue.append((neighbour, next_depth))
    return candidates


def _load_names(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        columns = raw_line.split("\t")
        if len(columns) < 2:
            continue
        name, identifier = columns[:2]
        if name and identifier:
            result[identifier] = name
    return result


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _strength(occurrences: int) -> Literal["high", "medium", "low"]:
    if occurrences >= 3:
        return "high"
    if occurrences >= 2:
        return "medium"
    return "low"


def _build_hints(
    relationships: list[FinDKGRelationship], focus: FinDKGFocus
) -> list[str]:
    focus_names = {
        "industry": "行业结构与传导",
        "commodity": "商品价格与供需",
        "macro": "宏观变量传导",
        "technology": "技术演进与商业化",
        "demand": "需求变化",
        "supply": "供给变化",
        "competition": "竞争格局",
        "general": "基本面影响",
    }
    hints: list[str] = []
    for item in relationships[:5]:
        hint = (
            f"核验“{item.source}”与“{item.target}”之间的“{item.relation}”关系，"
            f"并判断其是否影响当前公司的{focus_names[focus]}。"
        )
        if hint not in hints:
            hints.append(hint)
    return hints


def _empty_result(entities: list[str]) -> FinDKGQueryResult:
    return FinDKGQueryResult(
        query_entities=entities,
        relationships=[],
        related_entities=[],
        research_hints=[],
    )
