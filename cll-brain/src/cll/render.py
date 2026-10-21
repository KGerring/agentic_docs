"""Graph → markdown drafts with YAML frontmatter.

File layout: ``<drafts_dir>/<type>/<slug>.md`` (the node id is the path without
``.md``). Each file has three regions:

1. frontmatter — machine-owned keys, plus the keys a person may edit
   (``status``, ``tags``, ``summary``, ``aliases``, ``relations`` entries with
   ``provenance: curated``, and any key the machine does not use);
2. machine body — regenerated; the relations block sits between
   ``<!-- cll:relations -->`` markers so downstream tools can strip it;
3. notes — everything after the ``<!-- cll:notes … -->`` marker is preserved.

A file is rewritten only when its content hash changes, and a draft that the
downstream ingest moved or deleted is not re-emitted until the node changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from cll import SCHEMA, __version__
from cll.config import Config
from cll.model import CURATED, INVERSE, NODE_TYPES, Node
from cll.state import Store

NOTES_MARK = "<!-- cll:notes: everything below this line is yours; it is preserved across runs -->"
_NOTES_RE = re.compile(r"<!-- cll:notes\b[^>]*-->")
_REL_OPEN, _REL_CLOSE = "<!-- cll:relations -->", "<!-- /cll:relations -->"
# libyaml when present: parsing thousands of drafts in pure Python dominates a run otherwise.
_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_Dumper = getattr(yaml, "CSafeDumper", yaml.SafeDumper)
_FM_RE = re.compile(r"\A---\n(.*?)\n---[ \t]*\n?(.*)\Z", re.S)

# Frontmatter keys the machine owns on every node (type-specific ones are tracked per node).
BASE_KEYS = (
    "id",
    "type",
    "title",
    "summary",
    "status",
    "tags",
    "aliases",
    "date",
    "created",
    "updated",
    "rev",
    "schema",
    "generator",
    "source",
    "relations",
)

_EDGE_ORDER = (
    "defined_in",
    "part_of",
    "defines",
    "sources",
    "invokes",
    "calls",
    "uses",
    "runs",
    "touches",
    "on_day",
    "follows",
    "from_source",
    "pairs_with",
)


def parse_document(text: str) -> tuple[dict[str, Any], str] | None:
    """Split a markdown file into ``(frontmatter, body)``; ``None`` if it has no frontmatter."""
    m = _FM_RE.match(text.replace("\r\n", "\n"))
    if m is None:
        return None
    try:
        fm = yaml.load(m[1], Loader=_Loader)
    except yaml.YAMLError:
        return None
    return (fm, m[2]) if isinstance(fm, dict) else None


def split_notes(body: str) -> tuple[str, str]:
    m = _NOTES_RE.search(body)
    return (body, "") if m is None else (body[: m.start()], body[m.end() :].strip("\n"))


def strip_relations(body: str) -> str:
    return re.sub(re.escape(_REL_OPEN) + r".*?" + re.escape(_REL_CLOSE), "", body, flags=re.S)


def _label(text: str) -> str:
    return re.sub(r"[\[\]|]", " ", text).strip()


class Linker:
    def __init__(self, style: str, titles: dict[str, str]) -> None:
        self.style, self.titles = style, titles

    def __call__(self, target: str, depth: int = 1) -> str:
        title = self.titles.get(target)
        if title is None:
            return f"`{target}`"  # a curated edge to a node that does not exist (yet)
        if self.style == "markdown":
            return f"[{_label(title)}]({'../' * depth}{target}.md)"
        return f"[[{target}|{_label(title)}]]"


def _edge_key(edge_type: str) -> tuple[int, str]:
    return (_EDGE_ORDER.index(edge_type) if edge_type in _EDGE_ORDER else len(_EDGE_ORDER), edge_type)


def backlinks(nodes: dict[str, Node]) -> dict[str, list[tuple[Node, Any]]]:
    out: dict[str, list[tuple[Node, Any]]] = defaultdict(list)
    for node in nodes.values():
        for e in node.edges:
            target = nodes.get(e.target)
            if target is None:
                continue
            inverse = INVERSE.get(e.type)
            if inverse and any(x.type == inverse and x.target == node.id for x in target.edges):
                continue
            out[e.target].append((node, e))
    return out


def relations_block(node: Node, inbound: list[tuple[Node, Any]], link: Linker, limit: int) -> str:
    lines: list[str] = []
    if node.edges:
        lines += ["## Relations", ""]
        groups: dict[str, list] = defaultdict(list)
        for e in node.edges:
            groups[e.type].append(e)
        for etype in sorted(groups, key=_edge_key):
            lines += [f"### {etype}", ""]
            for e in sorted(groups[etype], key=lambda e: (-e.weight, e.target)):
                why = f" — {e.why}" if e.why else ""
                lines.append(f"- {link(e.target)}{why} · _{e.provenance}_")
            lines.append("")
    if inbound:
        lines += ["## Referenced by", ""]
        groups = defaultdict(list)
        for src, e in inbound:
            groups[e.type].append((src, e))
        for etype in sorted(groups, key=_edge_key):
            items = sorted(groups[etype], key=lambda se: (se[0].date or "", se[0].title), reverse=True)
            lines += [f"### {etype} ← {len(items)}", ""]
            for src, e in items[:limit]:
                why = f" — {e.why}" if e.why else ""
                lines.append(f"- {link(src.id)}{why} · _{e.provenance}_")
            if len(items) > limit:
                lines.append(f"- …and {len(items) - limit} more")
            lines.append("")
    if not lines:
        return ""
    return _REL_OPEN + "\n" + "\n".join(lines).rstrip() + "\n" + _REL_CLOSE


def _clean(value: Any) -> Any:
    """Drop empty values so frontmatter stays readable."""
    if isinstance(value, dict):
        return {k: c for k, v in value.items() if (c := _clean(v)) not in (None, "", [], {})}
    return value


def frontmatter(node: Node) -> dict[str, Any]:
    fm: dict[str, Any] = {
        "id": node.id,
        "type": node.type,
        "title": node.title,
        "summary": node.summary,
        "status": node.status,
        "tags": list(node.tags),
        "aliases": list(node.aliases),
    }
    if node.date:
        fm["date"] = node.date
    fm.update(
        created=None, updated=None, rev=None, schema=SCHEMA, generator=f"cll {__version__}", source=_clean(node.source)
    )
    for k, v in _clean(node.meta).items():
        fm.setdefault(k, v)
    for k, v in node.extra.items():
        fm.setdefault(k, v)  # human keys never shadow machine keys
    fm["relations"] = [e.as_dict() for e in sorted(node.edges, key=lambda e: (_edge_key(e.type), -e.weight, e.target))]
    return fm


def body(node: Node, inbound: list[tuple[Node, Any]], link: Linker, cfg: Config) -> str:
    parts: list[str] = []
    if cfg.render.h1:
        parts.append(f"# {node.title}")
    if node.summary:
        parts.append(f"> {node.summary}")
    if node.body:
        parts.append(node.body)
    if rel := relations_block(node, inbound, link, cfg.render.backlink_limit):
        parts.append(rel)
    parts.append(NOTES_MARK + ("\n\n" + node.notes if node.notes else ""))
    return "\n\n".join(parts) + "\n"


def dump(fm: dict[str, Any], text: str) -> str:
    head = yaml.dump(fm, Dumper=_Dumper, sort_keys=False, allow_unicode=True, default_flow_style=False, width=10_000)
    return f"---\n{head}---\n\n{text}"


def node_path(drafts: Path, node_id: str) -> Path:
    return drafts / f"{node_id}.md"


def file_sig(path: Path) -> str:
    st = path.stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


def _atomic_write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cll-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return file_sig(path)


def documents(nodes: dict[str, Node], cfg: Config) -> dict[str, tuple[dict[str, Any], str]]:
    """``id → (frontmatter without created/updated/rev, body)`` for every node."""
    link = Linker(cfg.render.link_style, {n.id: n.title for n in nodes.values()})
    inbound = backlinks(nodes)
    return {n.id: (frontmatter(n), body(n, inbound.get(n.id, []), link, cfg)) for n in nodes.values()}


def write(
    nodes: dict[str, Node], ignored: set[str], cfg: Config, store: Store, now: int, dry_run: bool = False
) -> Counter[str]:
    stats: Counter[str] = Counter()
    drafts = cfg.paths.drafts_dir
    stamp = datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")
    rows = store.node_rows()
    docs = documents(nodes, cfg)

    for nid, (fm, text) in docs.items():
        node = nodes[nid]
        digest = hashlib.sha256((json.dumps(fm, sort_keys=True, default=str) + "\0" + text).encode()).hexdigest()
        row = rows.get(nid)
        path = node_path(drafts, nid)
        if row is not None and row["hash"] == digest and not row["stale"]:
            if path.exists():
                stats["unchanged"] += 1
                continue
            if cfg.render.on_consumed == "skip":
                stats["consumed"] += 1
                continue
            rev, created, updated = row["rev"], row["created"], row["updated"]
            stats["rewritten"] += 1
        else:
            rev = row["rev"] + 1 if row is not None else 1
            created = row["created"] if row is not None else stamp
            updated = stamp
            stats["updated" if row is not None else "created"] += 1
        fm.update(created=created, updated=updated, rev=rev)
        if dry_run:
            continue
        sig = _atomic_write(path, dump(fm, text))
        machine = {**node.machine, "meta_keys": [k for k in fm if k not in BASE_KEYS and k not in node.extra]}
        store.put_node(nid, digest, rev, created, updated, machine, file_sig=sig)

    # Nodes the graph no longer produces: flag the draft once, never delete it.
    for nid, row in rows.items():
        if nid in docs or nid in ignored or row["stale"]:
            continue
        stats["stale"] += 1
        if dry_run:
            continue
        path = node_path(drafts, nid)
        sig = row["file_sig"]
        if path.exists() and (doc := parse_document(path.read_text(encoding="utf-8"))) is not None:
            fm, text = doc
            fm["status"] = "stale"
            sig = _atomic_write(path, dump(fm, text.lstrip("\n")))
        store.put_node(
            nid,
            row["hash"],
            row["rev"],
            row["created"],
            row["updated"],
            json.loads(row["machine"]),
            stale=1,
            file_sig=sig,
        )

    if not dry_run:
        if cfg.render.write_index:
            _write_index(nodes, cfg, store, stamp)
        _write_graph(nodes, cfg, stamp)
        store.db.commit()
    return stats


def _write_index(nodes: dict[str, Node], cfg: Config, store: Store, stamp: str) -> None:
    link = Linker(cfg.render.link_style, {n.id: n.title for n in nodes.values()})
    by_type: dict[str, list[Node]] = defaultdict(list)
    for n in nodes.values():
        by_type[n.type].append(n)
    lines = ["# Index", "", "Generated map of content. Regenerated on every run; edit the nodes, not this file.", ""]
    lines += ["| Type | Nodes |", "| --- | ---: |"]
    lines += [f"| {t} | {len(by_type[t])} |" for t in NODE_TYPES if by_type.get(t)]
    for t, heading, key, limit in (
        ("day", "Latest days", lambda n: n.date or "", 14),
        ("tool", "Most used tools", lambda n: n.meta.get("interactive_uses", 0), 20),
        ("script", "Recently modified scripts", lambda n: n.date or "", 20),
        ("repo", "Repositories", lambda n: n.date or "", 20),
        ("log", "Log families", lambda n: n.title, 20),
    ):
        items = sorted(by_type.get(t, []), key=key, reverse=True)[:limit]
        if items:
            lines += ["", f"## {heading}", ""] + [f"- {link(n.id, depth=0)} — {n.summary}" for n in items]
    text = "\n".join(lines) + "\n"
    digest = hashlib.sha256(text.encode()).hexdigest()
    path = cfg.paths.drafts_dir / "_index.md"
    if store.get_meta("index_hash") == digest and path.exists():
        return
    fm = {
        "id": "_index",
        "type": "index",
        "title": "Index",
        "updated": stamp,
        "schema": SCHEMA,
        "generator": f"cll {__version__}",
    }
    _atomic_write(path, dump(fm, text))
    store.set_meta("index_hash", digest)


def graph_data(nodes: dict[str, Node], stamp: str) -> dict[str, Any]:
    """NetworkX node-link format: ``nx.node_link_graph(data, edges="links")``."""
    return {
        "directed": True,
        "multigraph": True,
        "graph": {"schema": SCHEMA, "generated": stamp},
        "nodes": [
            {
                "id": n.id,
                "type": n.type,
                "title": n.title,
                "summary": n.summary,
                "tags": n.tags,
                "status": n.status,
                "date": n.date,
            }
            for n in nodes.values()
        ],
        "links": [
            {"source": n.id, "target": e.target, **{k: v for k, v in e.as_dict().items() if k != "target"}}
            for n in nodes.values()
            for e in n.edges
            if e.target in nodes
        ],
    }


def _write_graph(nodes: dict[str, Node], cfg: Config, stamp: str) -> None:
    data = graph_data(nodes, stamp)
    path = cfg.paths.graph_file
    comparable = json.dumps({k: v for k, v in data.items() if k != "graph"}, sort_keys=True)
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if json.dumps({k: v for k, v in old.items() if k != "graph"}, sort_keys=True) == comparable:
                return
        except (OSError, ValueError):
            pass
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=1) + "\n")


def curated(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Relations a person wrote: ``provenance: curated`` or no provenance at all."""
    return [e for e in edges if isinstance(e, dict) and e.get("provenance", CURATED) == CURATED]
