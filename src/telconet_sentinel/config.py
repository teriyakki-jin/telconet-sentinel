from pathlib import Path
from typing import Any

import yaml

from .models import Link, Node, NodeRole
from .topology import Topology


def load_topology(path: Path) -> Topology:
    document: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("intent root must be a mapping")

    raw_nodes = document.get("nodes")
    raw_links = document.get("links")
    if not isinstance(raw_nodes, dict) or not isinstance(raw_links, list):
        raise ValueError("intent must define nodes and links")

    nodes: list[Node] = []
    for name, attributes in raw_nodes.items():
        if not isinstance(name, str) or not isinstance(attributes, dict):
            raise ValueError("invalid node declaration")
        prefixes = attributes.get("prefixes", [])
        if not isinstance(prefixes, list) or not all(isinstance(item, str) for item in prefixes):
            raise ValueError(f"invalid prefixes for node: {name}")
        try:
            role = NodeRole(attributes["role"])
        except (KeyError, ValueError) as exc:
            raise ValueError(f"invalid role for node: {name}") from exc
        nodes.append(Node(name=name, role=role, prefixes=tuple(prefixes)))

    links: list[Link] = []
    for attributes in raw_links:
        if not isinstance(attributes, dict):
            raise ValueError("invalid link declaration")
        endpoints = attributes.get("endpoints")
        if (
            not isinstance(endpoints, list)
            or len(endpoints) != 2
            or not all(isinstance(item, str) for item in endpoints)
        ):
            raise ValueError("link endpoints must contain two node names")
        link_id = attributes.get("id")
        if not isinstance(link_id, str):
            raise ValueError("link id must be a string")
        cost = attributes.get("cost", 1)
        if not isinstance(cost, int) or isinstance(cost, bool):
            raise ValueError(f"link cost must be an integer: {link_id}")
        links.append(Link(link_id, endpoints[0], endpoints[1], cost))

    return Topology(nodes, links)
