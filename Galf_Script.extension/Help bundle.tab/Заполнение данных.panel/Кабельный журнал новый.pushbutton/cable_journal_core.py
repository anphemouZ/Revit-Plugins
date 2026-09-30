# -*- coding: utf-8 -*-
"""Deterministic, Revit-independent cable route ordering.

Input contract::

    nodes = {
        "P": {"kind": "panel", "label": u"ЩР"},
        "R": {"kind": "route", "label": u"Труба", "length_m": 2.5,
              "method": u"В трубе"},
        "B": {"kind": "box", "label": u"РК", "break_cable": False},
        "D": {"kind": "device", "label": u"Розетка"},
    }
    edges = [("P", "R"), ("R", "B"), ("B", "D")]
    systems = [{"id": "s1", "number": u"Гр.1", "panel": "P",
                "destinations": ["D"], "conductor": u"ВВГнг"}]

``build_journal`` returns ``runs``, ``segments`` and ``issues``. A run is a
continuous cable between termination points. A segment is the ordered portion
between named waypoints (panel, box, device) in that run. No rows are emitted
for a system whose physical route or branching is ambiguous.
"""
from __future__ import unicode_literals

import math
import re
from collections import deque


try:
    _text_type = unicode
except NameError:  # Python 3
    _text_type = str


_LENGTH_KINDS = frozenset(("route", "fitting"))
_WAYPOINT_KINDS = frozenset(("panel", "box", "device"))
_VALID_KINDS = _LENGTH_KINDS | _WAYPOINT_KINDS


def _text(value):
    if value is None:
        return _text_type("")
    if isinstance(value, _text_type):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("utf-8", "replace")
    return _text_type(value)


def natural_key(value):
    """Comparison key that sorts ``Гр.2`` before ``Гр.10`` on Python 2/3."""
    result = []
    for part in re.split(r"(\d+)", _text(value).lower()):
        if part.isdigit():
            result.append((1, int(part)))
        else:
            result.append((0, part))
    return tuple(result)


def _node_key(nodes, node_id):
    node = nodes[node_id]
    return (natural_key(node.get("label", node_id)), natural_key(node_id))


def _issue(code, system_id=None, destination_id=None, node_ids=None):
    issue = {"code": code, "system_id": system_id}
    if destination_id is not None:
        issue["destination_id"] = destination_id
    if node_ids is not None:
        issue["node_ids"] = list(node_ids)
    return issue


def _edge_pair(edge):
    if isinstance(edge, dict):
        return (edge.get("from_id", edge.get("from")),
                edge.get("to_id", edge.get("to")))
    try:
        if len(edge) == 2:
            return edge[0], edge[1]
    except (TypeError, KeyError):
        pass
    return None, None


def _make_graph(nodes, edges, issues):
    graph = dict((node_id, set()) for node_id in nodes)
    invalid = False
    for node_id, node in nodes.items():
        kind = node.get("kind")
        if kind not in _VALID_KINDS:
            issues.append(_issue("invalid_node_kind", node_ids=[node_id]))
            invalid = True
        if kind in _LENGTH_KINDS:
            try:
                length = float(node["length_m"])
            except (KeyError, TypeError, ValueError, OverflowError):
                length = -1.0
            if length < 0 or math.isnan(length) or math.isinf(length):
                issues.append(_issue("invalid_length", node_ids=[node_id]))
                invalid = True
    for edge in edges:
        start, end = _edge_pair(edge)
        if start not in nodes or end not in nodes or start == end:
            issues.append(_issue("invalid_edge", node_ids=[start, end]))
            invalid = True
            continue
        graph[start].add(end)
        graph[end].add(start)
    if invalid:
        return None
    return dict((node_id, sorted(peers, key=lambda item: _node_key(nodes, item)))
                for node_id, peers in graph.items())


def _bridges(graph, nodes):
    """Find every bridge in an undirected graph without recursion."""
    discovery = {}
    low = {}
    parent = {}
    bridges = set()
    time = 0
    for root in sorted(graph, key=lambda item: _node_key(nodes, item)):
        if root in discovery:
            continue
        discovery[root] = low[root] = time
        time += 1
        stack = [(root, iter(graph[root]))]
        while stack:
            current, peers = stack[-1]
            try:
                peer = next(peers)
            except StopIteration:
                stack.pop()
                previous = parent.get(current)
                if previous is not None:
                    low[previous] = min(low[previous], low[current])
                    if low[current] > discovery[previous]:
                        bridges.add(frozenset((previous, current)))
                continue
            if peer == parent.get(current):
                continue
            if peer not in discovery:
                parent[peer] = current
                discovery[peer] = low[peer] = time
                time += 1
                stack.append((peer, iter(graph[peer])))
            else:
                low[current] = min(low[current], discovery[peer])
    return bridges


def _path(graph, start, finish):
    """Return one shortest path; uniqueness is checked with graph bridges."""
    previous = {start: None}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        if current == finish:
            result = []
            while current is not None:
                result.append(current)
                current = previous[current]
            result.reverse()
            return result
        for peer in graph[current]:
            if peer not in previous:
                previous[peer] = current
                queue.append(peer)
    return None


def _unique_path(graph, bridges, start, finish):
    path = _path(graph, start, finish)
    if path is None:
        return None, "missing_path"
    for index in range(len(path) - 1):
        if frozenset((path[index], path[index + 1])) not in bridges:
            return None, "ambiguous_path"
    return path, None


def _shared_branch_issues(nodes, system_id, paths):
    issues = []
    destinations = sorted(paths, key=lambda item: _node_key(nodes, item))
    for index, first in enumerate(destinations):
        first_path = paths[first]
        for second in destinations[index + 1:]:
            second_path = paths[second]
            common = 0
            for first_node, second_node in zip(first_path, second_path):
                if first_node != second_node:
                    break
                common += 1
            if common <= 1:
                continue
            # The common trunk is safe only when its branching node explicitly
            # terminates that cable. This also permits de-duplicating P -> B.
            branch = first_path[common - 1]
            branch_node = nodes[branch]
            if branch_node.get("kind") != "box" or branch_node.get("break_cable") is not True:
                issues.append(_issue("shared_branch_without_break", system_id,
                                     node_ids=[first, second, branch]))
    return issues


def _split_runs(nodes, path):
    run_paths = []
    start = 0
    for index in range(1, len(path)):
        node_id = path[index]
        if nodes[node_id].get("kind") == "box" and nodes[node_id].get("break_cable") is True:
            run_paths.append(path[start:index + 1])
            start = index
    if start < len(path) - 1:
        run_paths.append(path[start:])
    return run_paths


def _length(nodes, node_ids):
    return round(sum(float(nodes[node_id]["length_m"])
                     for node_id in node_ids
                     if nodes[node_id].get("kind") in _LENGTH_KINDS), 6)


def _segments_for_run(nodes, system, run, route_index_start):
    path = run["node_ids"]
    result = []
    anchor = 0
    for index in range(1, len(path)):
        node_id = path[index]
        if nodes[node_id].get("kind") not in _WAYPOINT_KINDS and index != len(path) - 1:
            continue
        route_nodes = [candidate for candidate in path[anchor + 1:index]
                       if nodes[candidate].get("kind") in _LENGTH_KINDS]
        result.append({
            "run_id": run["id"],
            "system_id": system["id"],
            "system_number": system.get("number", ""),
            "conductor": system.get("conductor", ""),
            "sequence": len(result) + 1,
            "route_index": route_index_start + len(result),
            "from_id": path[anchor],
            "to_id": node_id,
            "from_label": nodes[path[anchor]].get("label", _text(path[anchor])),
            "to_label": nodes[node_id].get("label", _text(node_id)),
            "route_node_ids": route_nodes,
            "node_ids": path[anchor:index + 1],
            "length_m": _length(nodes, route_nodes),
        })
        anchor = index
    return result


def build_journal(nodes, edges, systems):
    """Order physical routes from each panel through boxes to each device.

    Systems with a missing/ambiguous path, unknown box termination, or an
    unmodelled shared branch yield issues and no clean rows. Shared upstream
    runs are emitted once when the branch is a box with ``break_cable=True``.
    """
    result = {"runs": [], "segments": [], "issues": []}
    graph = _make_graph(nodes, edges, result["issues"])
    if graph is None:
        return result
    bridges = _bridges(graph, nodes)
    ordered_systems = sorted(systems, key=lambda system: (
        natural_key(system.get("number", "")), natural_key(system.get("id", ""))))
    seen_system_ids = set()
    for system in ordered_systems:
        system_id = system.get("id")
        if system_id in seen_system_ids:
            result["issues"].append(_issue("duplicate_system_id", system_id))
            continue
        seen_system_ids.add(system_id)
        panel = system.get("panel")
        if panel not in nodes or nodes[panel].get("kind") != "panel":
            result["issues"].append(_issue("invalid_panel", system_id, node_ids=[panel]))
            continue
        destinations = sorted(set(system.get("destinations", [])),
                              key=lambda item: _node_key(nodes, item) if item in nodes
                              else (natural_key(item), natural_key(item)))
        if not destinations:
            result["issues"].append(_issue("no_destinations", system_id))
            continue
        paths = {}
        system_issues = []
        for destination in destinations:
            if destination not in nodes or nodes[destination].get("kind") not in ("device", "panel", "box"):
                system_issues.append(_issue("invalid_destination", system_id, destination))
                continue
            if (nodes[destination].get("kind") == "box" and
                    nodes[destination].get("break_cable") is not True):
                system_issues.append(_issue("unknown_box_break", system_id,
                                            destination, [destination]))
                continue
            path, problem = _unique_path(graph, bridges, panel, destination)
            if problem:
                system_issues.append(_issue(problem, system_id, destination))
                continue
            for node_id in path:
                if (nodes[node_id].get("kind") == "box" and
                        nodes[node_id].get("break_cable") is None):
                    system_issues.append(_issue("unknown_box_break", system_id,
                                                destination, [node_id]))
            paths[destination] = path
        if not system_issues:
            system_issues.extend(_shared_branch_issues(nodes, system_id, paths))
        if system_issues:
            result["issues"].extend(system_issues)
            continue
        runs_by_path = {}
        system_runs = []
        for destination in destinations:
            for run_path in _split_runs(nodes, paths[destination]):
                key = tuple(run_path)
                if key in runs_by_path:
                    runs_by_path[key]["destination_ids"].append(destination)
                    continue
                run = {
                    "id": _text(system_id) + ":R" + _text(len(system_runs) + 1),
                    "system_id": system_id,
                    "system_number": system.get("number", ""),
                    "conductor": system.get("conductor", ""),
                    "sequence": len(system_runs) + 1,
                    "route_index": len(system_runs) + 1,
                    "from_id": run_path[0],
                    "to_id": run_path[-1],
                    "from_label": nodes[run_path[0]].get("label", _text(run_path[0])),
                    "to_label": nodes[run_path[-1]].get("label", _text(run_path[-1])),
                    "node_ids": list(run_path),
                    "route_node_ids": [item for item in run_path
                                       if nodes[item].get("kind") in _LENGTH_KINDS],
                    "destination_ids": [destination],
                    "length_m": _length(nodes, run_path),
                }
                runs_by_path[key] = run
                system_runs.append(run)
        result["runs"].extend(system_runs)
        route_index = 1
        for run in system_runs:
            rows = _segments_for_run(nodes, system, run, route_index)
            result["segments"].extend(rows)
            route_index += len(rows)
    return result
