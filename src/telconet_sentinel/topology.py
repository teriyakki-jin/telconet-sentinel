from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from heapq import heappop, heappush

from .models import Link, Node


class Topology:
    def __init__(self, nodes: Iterable[Node], links: Iterable[Link]) -> None:
        node_items = tuple(nodes)
        link_items = tuple(links)

        self._nodes_by_name: dict[str, Node] = {}
        for node in node_items:
            if node.name in self._nodes_by_name:
                raise ValueError(f"duplicate node: {node.name}")
            self._nodes_by_name[node.name] = node

        self._links_by_id: dict[str, Link] = {}
        for link in link_items:
            if link.id in self._links_by_id:
                raise ValueError(f"duplicate link: {link.id}")
            if link.cost <= 0:
                raise ValueError(f"link cost must be positive: {link.id}")
            if link.endpoint_a == link.endpoint_b:
                raise ValueError(f"self link is not allowed: {link.id}")
            unknown = {
                endpoint
                for endpoint in (link.endpoint_a, link.endpoint_b)
                if endpoint not in self._nodes_by_name
            }
            if unknown:
                raise ValueError(f"unknown endpoint in {link.id}: {', '.join(sorted(unknown))}")
            self._links_by_id[link.id] = link

    @property
    def nodes(self) -> tuple[Node, ...]:
        return tuple(self._nodes_by_name[name] for name in sorted(self._nodes_by_name))

    @property
    def links(self) -> tuple[Link, ...]:
        return tuple(self._links_by_id[link_id] for link_id in sorted(self._links_by_id))

    def node(self, name: str) -> Node:
        try:
            return self._nodes_by_name[name]
        except KeyError as exc:
            raise ValueError(f"unknown node: {name}") from exc

    def link(self, link_id: str) -> Link:
        try:
            return self._links_by_id[link_id]
        except KeyError as exc:
            raise ValueError(f"unknown link: {link_id}") from exc

    def neighbors(self, node_name: str, excluded_link: str | None = None) -> tuple[str, ...]:
        self.node(node_name)
        if excluded_link is not None:
            self.link(excluded_link)

        neighbors: set[str] = set()
        for link in self._links_by_id.values():
            if link.id == excluded_link:
                continue
            if link.endpoint_a == node_name:
                neighbors.add(link.endpoint_b)
            elif link.endpoint_b == node_name:
                neighbors.add(link.endpoint_a)
        return tuple(sorted(neighbors))

    def has_path(self, source: str, targets: set[str], excluded_link: str | None = None) -> bool:
        self.node(source)
        known_targets = targets.intersection(self._nodes_by_name)
        if not known_targets:
            return False
        if source in known_targets:
            return True

        visited = {source}
        pending = deque([source])
        while pending:
            current = pending.popleft()
            for neighbor in self.neighbors(current, excluded_link):
                if neighbor in visited:
                    continue
                if neighbor in known_targets:
                    return True
                visited.add(neighbor)
                pending.append(neighbor)
        return False

    def shortest_distance(
        self,
        source: str,
        targets: set[str],
        excluded_link: str | None = None,
    ) -> int | None:
        self.node(source)
        if excluded_link is not None:
            self.link(excluded_link)
        known_targets = targets.intersection(self._nodes_by_name)
        if not known_targets:
            return None

        distances = {source: 0}
        pending: list[tuple[int, str]] = [(0, source)]
        while pending:
            distance, current = heappop(pending)
            if distance != distances[current]:
                continue
            if current in known_targets:
                return distance
            for link in self._links_by_id.values():
                if link.id == excluded_link:
                    continue
                if link.endpoint_a == current:
                    neighbor = link.endpoint_b
                elif link.endpoint_b == current:
                    neighbor = link.endpoint_a
                else:
                    continue
                candidate = distance + link.cost
                if candidate < distances.get(neighbor, candidate + 1):
                    distances[neighbor] = candidate
                    heappush(pending, (candidate, neighbor))
        return None
