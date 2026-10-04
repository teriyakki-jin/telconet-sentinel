from pathlib import Path
from typing import Any

import yaml
from yaml.loader import SafeLoader
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver


class UniqueKeySafeLoader(SafeLoader):
    pass


def _construct_unique_mapping(
    loader: SafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ValueError("unhashable YAML mapping key") from exc
        if duplicate:
            raise ValueError(f"duplicate mapping key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeySafeLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_yaml(path: Path) -> Any:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeySafeLoader)
