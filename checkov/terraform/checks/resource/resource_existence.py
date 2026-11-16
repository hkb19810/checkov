"""Opt-in Terraform resource existence facts; never decides policy compliance.

True means at least one configured instance, False means none, and None means
unknown/invalid multiplicity. Importing this module does not change any registry,
base check, graph, or other rule. Callers decide how absence affects their policy.
"""
from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping
from typing import Any

from checkov.common.graph.graph_builder.graph_components.attribute_names import CustomAttributes as A
from checkov.common.util.consts import TRUE_AFTER_UNKNOWN


def _constant(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if len(text) > 65536:
        return None
    if text.startswith('${') and text.endswith('}'):
        text = text[2:-1].strip()
    if re.fullmatch(r'\d+(?:\.0+)?', text):
        return int(float(text)) if '.' in text else int(text)
    # Collection constructors left in source by some Checkov render paths.
    match = re.fullmatch(r'(toset|tomap|jsondecode)\s*\((.*)\)', text, re.S)
    if match:
        try:
            argument = ast.literal_eval(match[2])
            if match[1] == 'jsondecode' and isinstance(argument, str):
                return json.loads(argument)
            if match[1] == 'tomap' and isinstance(argument, dict):
                return argument
            if match[1] == 'toset' and isinstance(argument, (list, tuple)) and all(isinstance(x, str) for x in argument):
                return set(argument)
        except (ValueError, TypeError, SyntaxError, RecursionError):
            pass
    return value


def _unresolved_key(value: Any) -> bool:
    return not isinstance(value, str) or value == TRUE_AFTER_UNKNOWN or '${' in value or '%{' in value


def multiplicity(conf: Mapping[str, Any], *, wrapped: bool = True) -> bool | None:
    """Inspect one rendered block. Wrapped is True for scan_resource_conf data.

    Graph vertices use unwrapped attributes. Unknown map VALUES do not affect
    for_each instance existence; unknown map keys do. Lists are not accepted as
    for_each sets without a set constructor, avoiding invalid [] -> absent.
    """
    if not isinstance(conf, Mapping):
        return None
    if 'count' in conf and 'for_each' in conf:
        return None
    attr = 'count' if 'count' in conf else 'for_each' if 'for_each' in conf else None
    if attr is None:
        return True
    value = conf[attr]
    if wrapped and isinstance(value, list):
        if len(value) != 1:
            return None
        value = value[0]
    value = _constant(value)
    if attr == 'count':
        if type(value) is int and value >= 0:
            return value > 0
        if type(value) is float and value >= 0 and value.is_integer():
            return value > 0
        return None
    if isinstance(value, Mapping):
        if any(_unresolved_key(key) for key in value):
            return None
        return bool(value)
    if isinstance(value, (set, frozenset)):
        return None if any(_unresolved_key(key) for key in value) else bool(value)
    return None


def _scope(value: Any) -> tuple | None:
    if value is None or value == {} or value == 'null':
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        value = dict(value)
    if not value:
        return None
    return (str(value['path']).replace('\\', '/'), value['name'],
            value.get('foreach_idx'), _scope(value.get('nested_tf_module')))


def graph_nodes(graph: Any) -> dict[Any, dict[str, Any]]:
    if graph is None:
        return {}
    if hasattr(graph, 'weighted_edge_list'):
        return dict(graph.nodes())
    return dict(graph.nodes(data=True))


def _unbound_selector(node: Mapping[str, Any], nodes: Mapping[Any, Mapping[str, Any]]) -> bool:
    """Do not interpret a missing input rendered as an empty collection as zero.

    Follow the selector's retained provenance, including local/module inputs.
    Only callers considering an empty result use this guard: unknown map VALUES
    must not invalidate a nonempty map with known instance keys.
    """
    pending = [(node, key) for key in ('count', 'for_each') if key in node]
    visited: set[tuple[int, str]] = set()
    while pending:
        owner, attribute = pending.pop()
        identity = (id(owner), attribute)
        if identity in visited:
            continue
        visited.add(identity)
        for crumb in owner.get(A.RENDERING_BREADCRUMBS, {}).get(attribute, []):
            kind = crumb.get('type')
            if kind not in ('variable', 'locals'):
                continue
            matches = [item for item in nodes.values() if item.get(A.BLOCK_TYPE) == kind
                       and item.get(A.BLOCK_NAME) == crumb.get('name')
                       and str(item.get(A.FILE_PATH, '')).replace('\\', '/') == str(crumb.get('path', '')).replace('\\', '/')]
            for item in matches:
                key = 'default' if kind == 'variable' else crumb['name']
                if key not in item:
                    return True
                pending.append((item, key))
    return False


def node_existence(node: Mapping[str, Any], nodes: Mapping[Any, Mapping[str, Any]], *,
                   value_resolver: Any = None) -> bool | None:
    """Inspect a graph vertex and its enclosing module instances.

    An optional resolver returns True/False/None for a vertex's OWN multiplicity.
    Rules with additional expression/source metadata can supply that resolver.
    Ancestors are matched by module identity and source path, never display name
    alone. No graph nodes or attributes are modified.
    """
    visited: set[int] = set()
    pending = node
    uncertain = False
    for _ in range(128):
        if id(pending) in visited:
            return None
        visited.add(id(pending))
        status = value_resolver(pending) if value_resolver else multiplicity(pending, wrapped=False)
        if status is False:
            if _unbound_selector(pending, nodes):
                status = None
            else:
                return False
        if status is not True:
            uncertain = True
        try:
            scope = _scope(pending.get(A.SOURCE_MODULE_OBJECT))
        except (KeyError, TypeError, ValueError, RecursionError):
            return None
        if scope is None:
            return None if uncertain else True
        matches = []
        for candidate in nodes.values():
            if candidate.get(A.BLOCK_TYPE) != 'module':
                continue
            name = candidate.get(A.BLOCK_NAME, '')
            index = None
            suffix = re.fullmatch(r'(.+)\[(.+)\]', name)
            if suffix:
                try:
                    name, index = suffix[1], json.loads(suffix[2])
                except (TypeError, ValueError):
                    continue
            try:
                identity = (str(candidate.get(A.FILE_PATH, '')).replace('\\', '/'), name, index,
                            _scope(candidate.get(A.SOURCE_MODULE_OBJECT)))
            except (KeyError, TypeError, ValueError, RecursionError):
                continue
            if identity == scope:
                matches.append(candidate)
        if len(matches) != 1:
            return None
        pending = matches[0]
    return None


def resource_existence(conf: Mapping[str, Any], graph: Any = None,
                       entity_type: str | None = None, entity_path: str | None = None) -> bool | None:
    """Entry-point helper for an opted-in resource/data check.

    Resolves existence only. It does not change UNKNOWN to PASS/FAIL, remove
    vertices, suppress other checks, or assert anything about deployed state.
    """
    own = multiplicity(conf)
    nodes = graph_nodes(graph)
    if not nodes:
        return own
    address = conf.get(A.TF_RESOURCE_ADDRESS)
    matches = [node for node in nodes.values() if address is not None
               and node.get(A.TF_RESOURCE_ADDRESS) == address
               and (entity_type is None or node.get(A.RESOURCE_TYPE) == entity_type)]
    if len(matches) > 1 and entity_path:
        path = entity_path.rsplit(':', 2)[0].replace('\\', '/').lstrip('/')
        matches = [node for node in matches if ('/' + str(node.get(A.FILE_PATH, '')).replace('\\', '/')).endswith('/' + path)]
    if not matches:
        return own
    if len(matches) != 1:
        return None
    node = matches[0]
    def own_status(item: Mapping[str, Any]) -> bool | None:
        if item is node:
            # The scan configuration preserves the collection wrapper and is
            # authoritative for this invocation, including empty sets.
            return own
        return multiplicity(item, wrapped=False)
    return node_existence(node, nodes, value_resolver=own_status)
